"""
news/fetcher.py
───────────────
Fetches real-time financial news from Benzinga (via massive.com) and
scores it with Claude to identify bullish US equity momentum signals.

Prompt-engineering design (v14):
  - The classification rubric lives in the SYSTEM prompt with cache_control:
    the news cycle runs every 60s and Anthropic prompt caching has a 5-min
    TTL, so the ~1.5k-token rubric is a cache hit on every cycle after the
    first (~90% input-cost reduction, lower latency).
  - temperature=0 — this is a classifier; sampling noise on borderline calls
    is pure harm.
  - Structured output via FORCED TOOL USE (tool_choice) — the model must call
    classify_articles with a schema-validated payload. This replaces the old
    "respond with JSON only" + truncation-recovery string hacks.
  - Every article gets a catalyst_type tag and an already_moved flag. CODE
    decides which catalyst classes are tradeable (cfg.tradeable_catalysts) —
    the model classifies, the system trades. Keeping that boundary makes the
    model's job smaller (more accurate) and the trading policy auditable.

Eval loop:
  EVERY scored article — positive, neutral, negative — is persisted to the
  sentiment_scores table. A nightly job (analysis/forward_returns.py) fills
  in 5/15/60-min forward returns so prompt changes can be measured against
  actual market outcomes instead of guessed at.

API call optimisations:
  1. Articles are filtered (tickers present, not blocklisted, not already seen)
     BEFORE any Claude call is made.
  2. All eligible headlines are scored in a single batched Claude call per cycle.
"""

import html
import logging
import re
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
import anthropic
import pytz
from config.settings import cfg
from storage.database import is_article_seen, save_sentiment_scores
from trading.executor import resolve_t212_ticker

_LONDON = pytz.timezone("Europe/London")

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.massive.com/benzinga/v2/news"
_TIMEOUT = 10
_claude = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

# Analyst rating events are never tradeable (catalyst_type=analyst_action is not
# in TRADEABLE_CATALYSTS). Catching them by headline regex before the Claude call
# avoids spending tokens on articles code gates will always reject. The pattern
# is conservative — only unambiguous analyst-action language.
_ANALYST_ACTION_RE = re.compile(
    r"\b("
    r"price target|pt (raise|cut|increase|decrease|hike|lowered?)"
    r"|reiterates? (its |their |a )?(buy|sell|hold|overweight|underweight|neutral|outperform|underperform|equal.?weight)"
    r"|maintains? (its |their |a )?(buy|sell|hold|overweight|underweight|neutral|outperform|underperform|equal.?weight|rating)"
    r"|upgrades? (\w+ )?to (buy|overweight|outperform|strong buy)"
    r"|downgrades? (\w+ )?to (sell|underweight|underperform|neutral|hold)"
    r"|initiates? (coverage|with|at)"
    r"|starts? (coverage|with|at)"
    r"|resumes? (coverage|with|at)"
    r"|assumed? (coverage|with|at)"
    r")\b",
    re.IGNORECASE,
)

# ── Claude availability guard ───────────────────────────────────────────────────
# The classifier is the one external dependency with NO fallback: if Claude can't
# score articles, there are no signals at all (positive/neutral/negative are all
# Claude's call). Two failure modes have to be handled distinctly, because they
# need opposite responses, and 2026-06-23 saw a real Claude outage mid-session:
#
#   • OUTAGE / OVERLOAD (HTTP 529 overloaded_error, 500 api_error, or a network
#     error) — transient. Self-heals in minutes. We back off briefly so we don't
#     hammer a struggling API every 60s, then resume automatically.
#   • OUT-OF-CREDITS / BILLING (HTTP 403 with error type "billing_error", or the
#     401 auth case) — does NOT self-heal until the human tops up / fixes the key.
#     A 60s retry loop here just burns log noise and (for some error classes) can
#     accrue charges. We back off much longer and log it as a distinct CRITICAL
#     so it's obvious in the journal what actually broke.
#
# In BOTH cases _batch_score_sentiment returns {} (fail-closed: unscored articles
# are never traded), and the cooldown simply suppresses the *call* until it's
# worth trying again. While Claude is down the system makes no trades — correctly,
# since it can't assess the news — but the rest of the pipeline stays alive.
_CLAUDE_OUTAGE_COOLDOWN_SECONDS = 120        # transient outage / overload / 529
_CLAUDE_BILLING_COOLDOWN_SECONDS = 1800      # out-of-credits / auth — needs a human
# {"until": monotonic deadline, "reason": str} — None means Claude is believed up.
_claude_cooldown: dict | None = None


def _claude_available() -> bool:
    """
    False while a cooldown from a prior Claude failure is still active.

    Skips the Claude call entirely during the cooldown window so an outage or a
    spent credit balance doesn't trigger a fresh API attempt (and a fresh stack
    trace) on every 60s cycle. Logs once when the cooldown lifts.
    """
    global _claude_cooldown
    if _claude_cooldown is None:
        return True
    if time.monotonic() >= _claude_cooldown["until"]:
        logger.info(
            "Claude cooldown elapsed (was: %s) — resuming sentiment scoring",
            _claude_cooldown["reason"],
        )
        _claude_cooldown = None
        return True
    return False


def _enter_claude_cooldown(seconds: float, reason: str) -> None:
    """Suppress Claude calls for `seconds`; record the reason for logging."""
    global _claude_cooldown
    _claude_cooldown = {"until": time.monotonic() + seconds, "reason": reason}


def _record_claude_event(event_type: str, detail: str) -> None:
    """Best-effort system_event for a Claude failure (alerting/observability).

    Import-local and swallowing — an event-log failure must never affect the
    news path. See storage.database.record_system_event.
    """
    try:
        from storage.database import record_system_event
        record_system_event(event_type, detail)
    except Exception as exc:
        logger.debug("Could not record Claude system_event: %s", exc)


def _api_error_type(exc: Exception) -> str | None:
    """
    Extract the Anthropic API error type (e.g. "billing_error") from an SDK error.

    The SDK exception's `.type` attribute is populated from the TOP level of the
    response body, which for a real API error is the literal wrapper string
    "error" — NOT the specific type. The actual error type lives nested at
    `body["error"]["type"]` (verified against anthropic 0.103.1: a 403 with
    `{"type":"error","error":{"type":"billing_error",...}}` has `exc.type ==
    "error"`). So read the nested field, defensively, and fall back to `.type`.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("type"):
            return str(inner["type"])
        # Some shapes put the type at the top level of body directly.
        if body.get("type") and body.get("type") != "error":
            return str(body["type"])
    t = getattr(exc, "type", None)
    return str(t) if t and t != "error" else None

# Catalyst taxonomy. The model must pick exactly one per article. Which of
# these actually trade is a CODE decision (cfg.tradeable_catalysts) — see
# module docstring.
CATALYST_TYPES = [
    "earnings_beat",      # revenue/EPS above estimates
    "guidance_raise",     # raised full-year outlook
    "fda_approval",       # approval / positive trial / regulatory green light
    "ma_target",          # company is being ACQUIRED (binding offer)
    "ma_acquirer",        # company is the BUYER (usually drops — never traded)
    "contract_win",       # major contract with concrete dollar value
    "product_launch",     # surprise product/tech announcement, material impact
    "short_squeeze",      # squeeze setup with confirmed unusual buying
    "partnership",        # partnership without revenue figures (weak)
    "offering_dilution",  # share offering / ATM / warrants — NEGATIVE signal
    "halt_or_resume",     # circuit-breaker halt/resume article — never traded
    "recap_explainer",    # "Why is X up?" — written AFTER the move
    "analyst_action",     # upgrades, PT raises, reiterations
    "other",
]

# ── System prompt (cached) ────────────────────────────────────────────────────
# Static across calls → eligible for prompt caching. Keep ALL per-cycle
# content (the articles) in the user message so the cache prefix never varies.
_SYSTEM_PROMPT = """You are an expert US equity day trader evaluating breaking news for 5–15 minute momentum trades. For each article you receive, decide whether the news will cause IMMEDIATE intraday buying in the tagged stock.

Work through this decision tree for every article, in order:

STEP 1 — Is this article actually NEW information?
A real catalyst is being reported for the first time, seconds-to-minutes old.
If the headline explains or summarises a move that already happened — "What's
Going On With X Stock?", "Why Is X Up Today?", "X Stock Rally Explained",
"Shares Halted On Circuit Breaker", "Stock Halted And Resumed", "Halt Lifted"
— the move is OVER. Halt articles in particular publish AFTER a 30–120% spike.
→ sentiment=neutral, already_moved=true, catalyst_type=recap_explainer or
halt_or_resume. No exceptions.

STEP 2 — Is the tagged ticker the actual SUBJECT of the news?
In "Company B acquires Company A", the TARGET (A) spikes; the ACQUIRER (B)
drops or stays flat. If the tagged ticker is the acquirer → sentiment=neutral,
catalyst_type=ma_acquirer. If the article is primarily about a different
company than the tagged ticker → neutral.

STEP 3 — Is the catalyst binding and material?
- Binding: definitive merger agreement, firm buyout offer, signed contract
  with a dollar value, actual FDA approval. → can be positive.
- Non-binding: LOI, MOU, "exploring strategic alternatives", "in talks",
  early-stage trial commentary. → neutral. These cancel constantly.
- Dilution: share offerings, ATM programs, warrant exercises announced after
  a run-up → sentiment=NEGATIVE, catalyst_type=offering_dilution. Small caps
  sell offerings into spikes; this reliably reverses the stock.

STEP 4 — Is the company small enough to move?
S&P 500 mega-caps (AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA, BRK, JPM, V,
UNH, XOM and peers) do not move 5% on routine news. Unless the catalyst is
extraordinary (>10% earnings surprise, regulatory shutdown, CEO criminal
charges) → neutral.

STEP 5 — Classify what kind of catalyst this is.
Pick exactly one catalyst_type: earnings_beat, guidance_raise, fda_approval,
ma_target, ma_acquirer, contract_win, product_launch, short_squeeze,
partnership, offering_dilution, halt_or_resume, recap_explainer,
analyst_action, other.

Confidence calibration (0.0–1.0):
- 0.8–1.0: unambiguous, binding, material, first-report catalyst
  (earnings beat with numbers, FDA approval, definitive M&A as target)
- 0.5–0.7: plausible but weak — partnership without figures, early trials,
  analyst upgrade WITH a specific new catalyst
- below 0.5: routine flow — PT raises, reiterations, sector commentary,
  conference attendance, dividends, awards, ESG, stale rewrites

STEP 6 — Score catalyst_magnitude (1–5): how large is this catalyst relative
to the company's current market position?

This measures expected move SIZE, not just direction. Use all available
context: company market cap, dollar figures in the headline, catalyst class.

- 5 (transformative): FDA approval or M&A for a micro/small-cap (< $500M
  market cap), earnings beat with >20% revenue surprise, deal value > 50%
  of market cap. Expected intraday move: 20–100%+.

- 4 (major): FDA approval for a mid-cap ($500M–$2B), definitive M&A with
  20–50% premium, contract win > 10% of annual revenue, earnings beat with
  10–20% surprise. Expected move: 8–25%.

- 3 (material): Named contract with dollar value 3–10% of revenue, earnings
  beat with 5–10% surprise, guidance raise with concrete numbers, Phase 3
  trial result for a small-cap. Expected move: 3–10%.

- 2 (modest): Partnership with revenue figures below 3% of annual revenue,
  analyst upgrade with new product catalyst, earnings in-line with slight
  beat, guidance raised modestly. Expected move: 1–5%.

- 1 (noise): PT raise with no new catalyst, reiteration, conference
  attendance, vague partnership MOU, routine contract renewal, awards/ESG.
  Expected move: < 1%.

When market cap is unknown, use the company name and context clues (share
price, sector) to estimate. Biotech names below $1B move violently on FDA
decisions; use 5. S&P 500 names use 1–2 unless the catalyst is extraordinary.

Examples:

Headline: "Acme Therapeutics Receives FDA Approval For Lead Drug ACM-101"
→ {"sentiment": "positive", "confidence": 0.95, "catalyst_type": "fda_approval", "already_moved": false, "catalyst_magnitude": 5}

Headline: "Acme Therapeutics Shares Halted On Circuit Breaker To The Upside"
→ {"sentiment": "neutral", "confidence": 0.2, "catalyst_type": "halt_or_resume", "already_moved": true, "catalyst_magnitude": 1}

Headline: "What's Going On With Acme Therapeutics Stock On Tuesday?"
→ {"sentiment": "neutral", "confidence": 0.2, "catalyst_type": "recap_explainer", "already_moved": true, "catalyst_magnitude": 1}

Headline: "Acme Announces $40M Registered Direct Offering Priced At-The-Market"
→ {"sentiment": "negative", "confidence": 0.85, "catalyst_type": "offering_dilution", "already_moved": false, "catalyst_magnitude": 3}

Headline: "MegaBank Maintains Overweight On Apple, Raises Price Target To $310"
→ {"sentiment": "neutral", "confidence": 0.3, "catalyst_type": "analyst_action", "already_moved": false, "catalyst_magnitude": 1}

Headline: "Acme Signs Non-Binding LOI To Merge With Beta Corp"
→ {"sentiment": "neutral", "confidence": 0.4, "catalyst_type": "other", "already_moved": false, "catalyst_magnitude": 2}

Headline: "SmallCorp Wins $45M DoD Contract (Annual Revenue ~$120M)"
→ {"sentiment": "positive", "confidence": 0.85, "catalyst_type": "contract_win", "already_moved": false, "catalyst_magnitude": 4}

Classify every article you are given. Use the classify_articles tool."""

# Forced tool — guarantees schema-validated structured output. No JSON string
# parsing, no markdown-fence stripping, no truncation recovery.
_CLASSIFY_TOOL = {
    "name": "classify_articles",
    "description": "Submit a sentiment classification for every article in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The article id exactly as given"},
                        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
                        "confidence": {"type": "number", "description": "0.0 to 1.0"},
                        "catalyst_type": {"type": "string", "enum": CATALYST_TYPES},
                        "already_moved": {
                            "type": "boolean",
                            "description": "true if the price move described already happened before publication",
                        },
                        "catalyst_magnitude": {
                            "type": "integer",
                            "description": "1–5: expected move size relative to market cap. 5=transformative (micro-cap FDA/M&A, >20% surprise), 4=major, 3=material, 2=modest, 1=noise/routine.",
                            "minimum": 1,
                            "maximum": 5,
                        },
                    },
                    "required": ["id", "sentiment", "confidence", "catalyst_type", "already_moved", "catalyst_magnitude"],
                },
            }
        },
        "required": ["classifications"],
    },
}


@dataclass
class NewsItem:
    article_id: str
    ticker: str
    headline: str
    body: str
    source: str
    published_at: datetime
    sentiment: str       # "positive" | "neutral" | "negative"
    confidence: float    # 0.0–1.0
    catalyst_type: str   # one of CATALYST_TYPES
    already_moved: bool  # model's judgement: move happened pre-publication
    catalyst_magnitude: int  # 1–5: expected move size relative to market cap


def _fetch(lookback_minutes: int) -> list[dict]:
    """GET from Benzinga via massive.com and return raw article dicts."""
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    params = {
        "published.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 100,
        "sort": "published.desc",
    }
    try:
        resp = requests.get(
            _BASE_URL,
            headers={"Authorization": f"Bearer {cfg.benzinga_api_key}"},
            params=params,
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            logger.warning(
                "Benzinga API HTTP %d — %s",
                resp.status_code, resp.text[:200],
            )
            return []
        data = resp.json()
        articles = data.get("results", data.get("articles", []))
        logger.debug("Benzinga: fetched %d raw articles (lookback=%d min)", len(articles), lookback_minutes)
        return articles
    except requests.exceptions.Timeout:
        logger.warning("Benzinga API timeout after %ds — skipping cycle", _TIMEOUT)
        return []
    except requests.RequestException as exc:
        logger.warning("Benzinga API request failed: %s", exc)
        return []
    except Exception as exc:
        logger.error("Benzinga API unexpected error: %s", exc, exc_info=True)
        return []


def _batch_score_sentiment(articles: list[dict]) -> dict[str, dict]:
    """
    Score sentiment for multiple articles in a single Claude call.

    articles: list of dicts with keys 'id', 'headline', 'teaser'
    Returns: dict mapping article id → {sentiment, confidence, catalyst_type,
             already_moved}. Empty dict on failure (fail-closed: unscored
             articles are never traded).
    """
    if not articles:
        return {}

    # Skip the call while a prior outage/billing failure cooldown is active —
    # returning {} fails closed (no scores → no trades) without re-hitting a
    # known-down API every cycle.
    if not _claude_available():
        return {}

    # Per-cycle content goes in the user message; the static rubric stays in
    # the cached system block.
    lines = [
        f'ID {a["id"]}: {a["headline"]}\n   Teaser: {a["teaser"]}'
        for a in articles
    ]
    user_content = "Articles to classify:\n\n" + "\n\n".join(lines)

    # Tool-use JSON output runs ~55 tokens per article empirically; 60 gives
    # a comfortable margin. Floor at 400 ensures small batches aren't starved.
    max_tokens = max(400, len(articles) * 60 + 64)
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            temperature=0,  # classifier — sampling noise is pure harm
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    # Cached across cycles (5-min TTL > 1-min cadence) —
                    # ~90% input-cost cut on the rubric.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
            tools=[_CLASSIFY_TOOL],
            # FORCE the tool call — the model cannot reply with prose.
            tool_choice={"type": "tool", "name": "classify_articles"},
        )
        classifications = None
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use":
                classifications = block.input.get("classifications", [])
                break
        if classifications is None:
            logger.error("Batch sentiment: no tool_use block in Claude response")
            return {}

        results: dict[str, dict] = {}
        for r in classifications:
            if "id" not in r:
                continue
            results[str(r["id"])] = {
                "sentiment": str(r.get("sentiment", "neutral")).lower(),
                "confidence": float(r.get("confidence", 0.5)),
                "catalyst_type": str(r.get("catalyst_type", "other")),
                "already_moved": bool(r.get("already_moved", False)),
                "catalyst_magnitude": int(r.get("catalyst_magnitude", 1)),
            }
        if len(results) < len(articles):
            logger.warning(
                "Batch sentiment: %d/%d articles scored (missing ids are skipped)",
                len(results), len(articles),
            )
        return results

    # ── Billing / auth: NOT self-healing — needs a human ─────────────────────
    # 403 covers both permission_error and billing_error; the nested API error
    # type disambiguates (see _api_error_type — the SDK's .type is the useless
    # "error" wrapper). Out of Anthropic credits or an over-quota workspace lands
    # here. 401 is a bad/expired key. Either way, retrying every 60s is pointless
    # and noisy, so back off long and shout once.
    except anthropic.PermissionDeniedError as exc:
        err_type = _api_error_type(exc) or "permission_error"
        if err_type == "billing_error":
            logger.critical(
                "Claude OUT OF CREDITS / billing error (403 %s): %s — sentiment "
                "scoring suspended for %d min; NO TRADES until Anthropic billing is "
                "resolved. The pipeline keeps running but cannot assess news.",
                err_type, exc, _CLAUDE_BILLING_COOLDOWN_SECONDS // 60,
            )
        else:
            logger.critical(
                "Claude permission denied (403 %s): %s — sentiment scoring "
                "suspended for %d min; check the API key's workspace/permissions.",
                err_type, exc, _CLAUDE_BILLING_COOLDOWN_SECONDS // 60,
            )
        _enter_claude_cooldown(_CLAUDE_BILLING_COOLDOWN_SECONDS, f"403 {err_type}")
        _record_claude_event("claude_billing_error", f"403 {err_type}: {exc}")
        return {}
    except anthropic.AuthenticationError as exc:
        logger.critical(
            "Claude authentication failed (401): %s — sentiment scoring suspended "
            "for %d min; the ANTHROPIC_API_KEY is invalid or revoked.",
            exc, _CLAUDE_BILLING_COOLDOWN_SECONDS // 60,
        )
        _enter_claude_cooldown(_CLAUDE_BILLING_COOLDOWN_SECONDS, "401 auth")
        _record_claude_event("claude_auth_error", f"401: {exc}")
        return {}

    # ── Outage / overload / network: transient — short back-off, auto-resume ──
    # 529 overloaded_error (today's outage), 500 api_error, rate limit, or a
    # connection failure. These self-heal in minutes; cool down briefly so we
    # don't pile onto a struggling API, then retry on the next cycle.
    except anthropic.RateLimitError as exc:
        logger.warning(
            "Claude rate limited (429): %s — sentiment scoring paused %ds",
            exc, _CLAUDE_OUTAGE_COOLDOWN_SECONDS,
        )
        _enter_claude_cooldown(_CLAUDE_OUTAGE_COOLDOWN_SECONDS, "429 rate limit")
        # Record it too: a SUSTAINED 429 stops all scoring (→ zero trades) and
        # must be visible to the degradation alert, not just the zero-trade
        # tripwire 3 sessions later.
        _record_claude_event("claude_outage", f"429 rate limit: {exc}")
        return {}
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        status = getattr(exc, "status_code", None)
        logger.error(
            "Claude API unavailable (status=%s): %s — sentiment scoring paused %ds "
            "(transient outage/overload; will auto-resume)",
            status, exc, _CLAUDE_OUTAGE_COOLDOWN_SECONDS,
        )
        _enter_claude_cooldown(
            _CLAUDE_OUTAGE_COOLDOWN_SECONDS, f"API status {status}"
        )
        _record_claude_event("claude_outage", f"status {status}: {exc}")
        return {}
    except Exception as exc:
        # Unknown failure (malformed response, schema parse, etc.) — fail closed,
        # but DON'T enter a cooldown: this may be a one-off bad batch, and we
        # don't want a single odd article to silence scoring for minutes.
        logger.error("Batch sentiment classification failed: %s", exc, exc_info=True)
        return {}


def fetch_all_news(
    lookback_minutes: int = 5,
    max_age_minutes: float = 1.0,
    seen_checker: Callable[[str, str], bool] = is_article_seen,
) -> list[NewsItem]:
    """
    Fetch recent news from Benzinga, filter, classify in one batched Claude
    call, persist ALL scores for the eval loop, and return the tradeable
    positive signals.

    Parameters:
      lookback_minutes — Benzinga query window.
      max_age_minutes  — drop articles older than this. RTH cycles use the
                         default 1 min (poll cadence); the pre-market scanner
                         passes a larger window since it accumulates a
                         watchlist rather than trading immediately.
      seen_checker     — dedup predicate (article_id, ticker) → bool. RTH uses
                         the news_signals table; the pre-market scanner passes
                         its own candidate-table check.

    Gates applied to POSITIVE classifications before they become NewsItems
    (every gate's outcome is visible in sentiment_scores for the eval loop):
      1. confidence — must be ≥ cfg.min_sentiment_confidence (1–10 scale)
      2. catalyst   — catalyst_type must be in cfg.tradeable_catalysts
      3. timing     — already_moved must be False
    """
    articles = _fetch(lookback_minutes)
    if not articles:
        return []

    seen_ids: set[str] = set()

    # ── Step 1: filter articles before scoring ────────────────────────────────
    eligible: list[tuple[dict, list[str], str]] = []  # (article, tickers, article_id)

    for article in articles:
        article_id = str(article.get("benzinga_id") or article.get("url", article.get("title", "")))
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        raw_tickers = [
            t for t in (article.get("tickers") or [])
            if t and not t.startswith("X:")  # X:BTCUSD etc. are crypto pairs, not equities
        ]
        if not raw_tickers:
            continue

        # Freshness: drop articles older than max_age_minutes. For RTH cycles
        # (1 min) anything older was seen — or deliberately retried via
        # main.py's retry queue, which bypasses this function entirely.
        try:
            published_at = datetime.fromisoformat(
                article.get("published", "").replace("Z", "+00:00")
            )
            age_minutes = (datetime.now(timezone.utc) - published_at).total_seconds() / 60
            if age_minutes > max_age_minutes:
                continue
        except (ValueError, AttributeError):
            pass

        # Build T212 tickers and filter blocklist + already-seen pairs.
        # resolve_t212_ticker returns None for non-US/foreign listings — those
        # are dropped here (the `t212 is not None` guard must come first, before
        # any check that would receive None).
        eligible_tickers = [
            t212
            for t in raw_tickers
            for t212 in (resolve_t212_ticker(t),)
            if t212 is not None
            and t212 not in cfg.blocklist
            and not seen_checker(article_id, t212)
        ]
        if not eligible_tickers:
            continue

        # Roundup filter: >3 tickers = market digest, no per-stock catalyst.
        if len(raw_tickers) > 3:
            logger.debug(
                "Skipping roundup article %s — %d tickers (max 3): %s",
                article_id, len(raw_tickers), article.get("title", "")[:60],
            )
            continue

        # Analyst action pre-filter: these always produce catalyst_type=analyst_action,
        # which is never in TRADEABLE_CATALYSTS. A regex match on the raw headline is
        # far cheaper than a Claude API call, and the pattern is conservative enough
        # that false positives (a tradeable headline accidentally matching) are
        # essentially impossible for the specific phrases targeted.
        headline_raw = article.get("title", "")
        if _ANALYST_ACTION_RE.search(headline_raw):
            logger.debug(
                "Skipping analyst action article (pre-Claude filter): %s", headline_raw[:80],
            )
            continue

        eligible.append((article, eligible_tickers, article_id))

    if not eligible:
        logger.info("Benzinga: %d article(s) fetched → 0 eligible after filtering", len(seen_ids))
        return []

    # ── Step 2: batch score all eligible articles in one Claude call ──────────
    to_score = [
        {
            "id": article_id,
            "headline": html.unescape(article.get("title", "")),
            "teaser": html.unescape(article.get("teaser") or article.get("body", "")[:200]),
        }
        for article, _, article_id in eligible
    ]
    scores = _batch_score_sentiment(to_score)

    # ── Step 2b: persist EVERY score for the eval loop ────────────────────────
    # This is the feedback loop: without recording neutrals/negatives there is
    # no way to measure the classifier's precision/recall against actual
    # forward returns, and every prompt change is a guess.
    score_rows = []
    for article, tickers, article_id in eligible:
        s = scores.get(article_id)
        if s is None:
            continue
        for ticker in tickers:
            score_rows.append({
                "article_id": article_id,
                "ticker": ticker,
                "headline": html.unescape(article.get("title", "")),
                "sentiment": s["sentiment"],
                "confidence": s["confidence"],
                "catalyst_type": s["catalyst_type"],
                "already_moved": s["already_moved"],
                "catalyst_magnitude": s["catalyst_magnitude"],
                "published_at": article.get("published", ""),
            })
    if score_rows:
        try:
            save_sentiment_scores(score_rows)
        except Exception as exc:
            # Eval-loop storage must never break the trading path.
            logger.warning("Could not persist sentiment scores: %s", exc)

    # ── Step 3: apply trade gates and build NewsItem list ─────────────────────
    results: list[NewsItem] = []

    for article, tickers, article_id in eligible:
        s = scores.get(article_id)
        if s is None or s["sentiment"] != "positive":
            continue

        headline = html.unescape(article.get("title", ""))

        # Gate 1: confidence threshold (1–10 scale, cfg.min_sentiment_confidence).
        # Previously this setting existed but was never enforced anywhere.
        confidence_scaled = round(s["confidence"] * 10)
        if confidence_scaled < cfg.min_sentiment_confidence:
            logger.info(
                "Gate [confidence]: %s scored positive at %.1f/10 < %d — not trading: %s",
                ",".join(tickers), s["confidence"] * 10, cfg.min_sentiment_confidence,
                headline[:70],
            )
            continue

        # Gate 2: catalyst class. The model classifies; code decides what trades.
        if s["catalyst_type"] not in cfg.tradeable_catalysts:
            logger.info(
                "Gate [catalyst]: %s positive but catalyst=%s not tradeable — skipping: %s",
                ",".join(tickers), s["catalyst_type"], headline[:70],
            )
            continue

        # Gate 3: the model believes the move already happened pre-publication.
        if s["already_moved"]:
            logger.info(
                "Gate [already_moved]: %s positive but move pre-dates article — skipping: %s",
                ",".join(tickers), headline[:70],
            )
            continue

        # Gate 4: catalyst magnitude floor. Filters out noise signals (PT raises,
        # routine reiterations, vague MOUs) that the model scores positive with
        # low magnitude. Bernard & Thomas (1992) PEAD evidence shows drift is
        # proportional to earnings surprise size — the same principle applies here.
        if s["catalyst_magnitude"] < cfg.min_catalyst_magnitude:
            logger.info(
                "Gate [magnitude]: %s positive but magnitude=%d < min=%d — skipping: %s",
                ",".join(tickers), s["catalyst_magnitude"], cfg.min_catalyst_magnitude,
                headline[:70],
            )
            continue

        teaser = html.unescape(article.get("teaser") or article.get("body", "")[:200])
        try:
            published_at = datetime.fromisoformat(
                article.get("published", "").replace("Z", "+00:00")
            ).astimezone(_LONDON)
        except (ValueError, AttributeError):
            published_at = datetime.now(_LONDON)

        for ticker in tickers:
            results.append(NewsItem(
                article_id=article_id,
                ticker=ticker,
                headline=headline,
                body=teaser,
                source="benzinga",
                published_at=published_at,
                sentiment=s["sentiment"],
                confidence=s["confidence"],
                catalyst_type=s["catalyst_type"],
                already_moved=s["already_moved"],
                catalyst_magnitude=s["catalyst_magnitude"],
            ))

    logger.info(
        "Benzinga: %d article(s) fetched → %d eligible → %d tradeable positive signal(s)",
        len(seen_ids), len(eligible), len(results),
    )
    return results
