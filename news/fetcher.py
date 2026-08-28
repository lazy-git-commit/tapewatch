# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
from news.shadow_classifier import shadow_score
from trading.executor import resolve_t212_ticker

_LONDON = pytz.timezone("Europe/London")

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.massive.com/benzinga/v2/news"
_TIMEOUT = 10
_claude = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

# ── Scored-article dedup (session-scoped, reset daily) ───────────────────────
# Articles that Claude has ALREADY scored this session. This is what lets the
# freshness window be wider than the poll cadence without re-scoring the same
# article every cycle: is_article_seen() only covers articles that reached the
# price-check stage (news_signals rows), so neutrals/gated positives were only
# kept out of Claude by the 1-minute freshness cutoff. That razor-thin cutoff
# silently dropped every article the Benzinga feed indexed >60s after its
# publish timestamp, and every article that landed while a cycle overran its
# 60s interval (buy fills block the cycle up to 30s) — real catalysts lost
# with no trace. Only articles that were successfully scored are added, so a
# failed Claude batch is naturally refetched and retried next cycle.
_scored_articles: dict = {"date": None, "ids": set()}


def _already_scored(article_id: str) -> bool:
    today = datetime.now(timezone.utc).date()
    if _scored_articles["date"] != today:
        _scored_articles["date"] = today
        _scored_articles["ids"] = set()
    return article_id in _scored_articles["ids"]


def _mark_scored(article_ids) -> None:
    today = datetime.now(timezone.utc).date()
    if _scored_articles["date"] != today:
        _scored_articles["date"] = today
        _scored_articles["ids"] = set()
    _scored_articles["ids"].update(article_ids)


# ── Same-day same-ticker history (session-scoped, reset daily) ──────────────
# A stock can get multiple articles about the SAME underlying event hours
# apart with opposite framing. 2026-07-09: LEVI got "Stock Tumbles 4% Despite
# Q2 Earnings Beat" at 09:39 ET (scored negative, correctly never traded),
# then "Posts Beat-And-Raise Quarter, Analysts See More Upside In 2H" at
# 11:30 ET (scored positive, 85% confidence) — same earnings, opposite spin.
# Claude scored the second article with zero memory of the first, and the
# system bought right at the top of the recovery bounce the first article's
# "tumble" had already produced. Prior same-day verdicts for a ticker are now
# surfaced as context on its NEXT article so a reversal/respin gets read with
# the fuller picture instead of scored blind.
_ticker_history: dict = {"date": None, "tickers": {}}


def _record_ticker_history(ticker: str, headline: str, sentiment: str, scored_at: datetime) -> None:
    today = datetime.now(timezone.utc).date()
    if _ticker_history["date"] != today:
        _ticker_history["date"] = today
        _ticker_history["tickers"] = {}
    _ticker_history["tickers"].setdefault(ticker, []).append({
        "headline": headline, "sentiment": sentiment, "scored_at": scored_at,
    })


def _prior_ticker_context(ticker: str) -> str | None:
    """One-line summary of today's already-scored articles for `ticker`, or None."""
    today = datetime.now(timezone.utc).date()
    if _ticker_history["date"] != today:
        return None
    entries = _ticker_history["tickers"].get(ticker)
    if not entries:
        return None
    # Cap at the 3 most recent so one heavily-covered ticker can't balloon
    # the prompt; the most recent prior verdict matters most.
    parts = [
        f'{e["scored_at"].strftime("%H:%M UTC")} {e["sentiment"]} ("{e["headline"][:80]}")'
        for e in entries[-3:]
    ]
    return "; ".join(parts)

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

# Digest/preview/listicle headlines are NOT single-stock catalysts — they are
# compilations written about the market, with tickers tagged incidentally.
# 2026-07-10: Benzinga's "Market-Moving News for July 10th" (3 tickers tagged,
# sliding under the >3-ticker roundup filter) was classified by Claude as
# "earnings_beat, 80% confidence" for THREE unrelated companies at once
# (WD-40, Circle, Delta) — a fabricated catalyst that put CRCL on the
# premarket watchlist and bought the exact top of a 13% parabolic spike
# (−3.97%, the second-worst trade on record). A deterministic title check is
# cheaper and more reliable than hoping the classifier reads through the
# template: no digest reaches Claude at all.
# Pattern hygiene (v20.1 review finding): every phrase here must be one that
# CANNOT plausibly appear in a genuine single-stock catalyst headline, because
# a false positive is a silently-missed trade with no eval-loop trace. Two
# were removed for exactly that reason: "market update" ("Acme Provides
# Market Update On Phase 3 Results" is a real PR template) and "day ahead"
# ("Acme Soars, Investor Day Ahead"). "Week ahead" stays — companies don't
# phrase PRs that way; only digests do.
_DIGEST_RE = re.compile(
    r"\b("
    r"market[- ]moving news"
    r"|stocks? (to watch|making moves)"
    r"|(pre[- ]?market|after[- ]?hours|midday|morning|premarket) (movers|gainers|losers)"
    r"|top (stock )?(gainers|losers|movers|stories|picks)"
    r"|biggest (movers|gainers|losers)"
    r"|(before|after) the bell"
    r"|(opening|closing) bell"
    r"|market (wrap|recap|snapshot|rundown|preview)"
    r"|daily (recap|digest|rundown|briefing)"
    r"|week ahead"
    r"|earnings (preview|calendar|scheduled|on deck|this week|to watch)"
    r"|what to watch"
    r"|things to know"
    r"|trending tickers"
    r"|\d+ (stocks?|things|names) (to|you|that|worth)"
    r")\b",
    re.IGNORECASE,
)

# Explainer/recap headlines describe a price move that has ALREADY HAPPENED.
# They are commentary about the tape, not the catalyst that moved it — and the
# primary newswire item ("Acme Q2 EPS Beats...") arrives separately, so nothing
# tradeable is lost by dropping them.
#
# 2026-08-04, trade #25 (the only trade of the day, −2.82%): Benzinga's
# "Bloom Energy Stock Charges Higher Tuesday: What's Driving the Post-Earnings
# Rally?" was scored guidance_raise / positive / conf 0.75 with
# already_moved=FALSE — while the headline says in its own words that the rally
# was underway and the stock was already +3.99% on the day when we bought it.
# already_moved is the single field that would have blocked the entry, and the
# classifier got it wrong on an article whose whole subject is a completed move.
#
# Same reasoning as _DIGEST_RE: a deterministic title check is cheaper and more
# reliable than hoping the classifier reads through a template it has already
# been shown to misread. Claude does usually get these right (it spent
# catalyst_type=recap_explainer on 140 articles that same day) — this covers the
# case where the template names a real earnings/guidance event and pulls the
# classification toward the catalyst.
#
# Pattern hygiene (inherited from _DIGEST_RE, v20.1): every phrase must be one
# that CANNOT plausibly appear in a genuine single-stock catalyst headline.
# These all pass that bar because companies never issue a PR commenting on
# their own share price, and the newswire templates state the event, never
# explain the move. Validated against 2,415 real scored headlines (2026-07-25
# → 2026-08-04): 49 matches (2.0%), zero false positives, and exactly ONE of
# the 49 had scored positive — the Bloom Energy article above.
_EXPLAINER_RE = re.compile(
    r"("
    r"what'?s? (driving|behind|fueling|powering|going on with|happening (with|to))"
    r"|here'?s why"
    r"|\b(stock|shares)\b[^.:;]{0,30}\b(is|are) (trading|moving|heading) (higher|lower|up|down)"
    r"|\b(stock|shares)\b[^.:;]{0,30}\b(charges?|charged|climbs?|climbed|slides?|slid) (higher|lower)"
    r"|\b(stock|shares)\b[^.:;]{0,30}\b(is|are) (soaring|surging|plunging|sinking|tanking|rallying|tumbling)"
    r"|post[- ]earnings (rally|selloff|sell[- ]off|surge|slide|drop|pop)"
    r")",
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

# Empty-classification handling (see the loop in _batch_score_sentiment).
#
# v21.13 — RETRIES ALONE DO NOT WORK ON THIS FAILURE. History:
#   v21.7  1 retry  → 2026-08-04: 25 consecutive cycles, both calls empty
#   v21.12 3 attempts → 2026-08-06: 58 consecutive cycles, ALL THREE empty, every
#          time, for 98 minutes (07:00-08:38 ET). 174 wasted API calls, and it
#          overlapped the 08:00-09:30 ET premarket watchlist build by 38 min.
#
# The failure clearly persists on a timescale of tens of minutes, so hammering it
# three times per 60s cycle is the wrong shape entirely. Two changes:
#
#   1. Back down to ONE retry. The retry only ever earns its keep on a genuinely
#      isolated empty response; beyond that it is pure waste.
#   2. After _EMPTY_BATCH_COOLDOWN_TRIGGER consecutive all-empty CYCLES, stand
#      the classifier down via the SAME cooldown used for 529/billing failures.
#      One cycle's articles are still lost, but the next ~2 minutes cost zero API
#      calls instead of ~6, and the articles stay eligible (`_mark_scored` only
#      fires on success) so they are re-offered when scoring resumes.
#
# Fail-closed is preserved throughout: no scores → no signals → no trades.
_EMPTY_BATCH_ATTEMPTS = 2
_EMPTY_BATCH_BACKOFF_SECONDS = 2.0
_EMPTY_BATCH_COOLDOWN_TRIGGER = 2
_EMPTY_BATCH_COOLDOWN_SECONDS = 120

# Classification output budget (v21.15). Measured from production
# `classifier_calls` rows: a real classification costs 68-72 output tokens per
# article, so the old 60/article allowance was BELOW cost and truncated large
# batches mid tool-call — which is what every "empty batch" outage actually was
# (see the long note in _batch_score_sentiment). These are deliberately ~2x the
# measured need: max_tokens is a ceiling, only generated tokens are billed, and
# forced tool use with a strict schema means the model cannot ramble to fill it.
# Under-budgeting has cost entire sessions; over-budgeting costs nothing.
_TOKENS_PER_ARTICLE = 150
_MIN_OUTPUT_TOKENS = 1024

# Hard cap on articles per Claude call (v21.15.1). Raising the per-article
# allowance above rescaled the truncation cliff; it did not remove it, because
# nothing bounded `len(articles)` — `to_score` in fetch_all_news is the entire
# unscored backlog, and a truncated cycle GROWS it (_mark_scored fires only on
# success). Chunking makes max_tokens a bounded constant (25*150+256 = 4006,
# vs a measured need of ~25*72 = 1800), so no achievable backlog can truncate,
# and a single bad chunk costs one chunk's articles instead of the whole cycle.
#
# backtest/backtest.py has chunked at 20 "to stay within Claude token limits"
# since long before any of this; the live path simply never adopted it. Every
# property of the outage — deterministic recurrence, self-reinforcement,
# clustering at session boundaries — was a consequence of unbounded n, not of
# the constant 60.
_MAX_ARTICLES_PER_BATCH = 25

# Consecutive news cycles whose batch came back empty on every attempt. Reset by
# ANY successful scoring pass (see _batch_score_sentiment) — a single good cycle
# means the classifier is answering again.
_consecutive_empty_batches = 0

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


# ── Prompt-cache verification (v21.16) ────────────────────────────────────────
# cache_control is a REQUEST HINT, not a guarantee: below the model's minimum
# cacheable prefix (4096 tokens on Haiku 4.5) the API accepts the field, ignores
# it, returns 200 OK and reports zero cached tokens. There is no error to catch
# and no field that says "your prompt was too short" — which is why this went
# unnoticed for 1,140 calls while every one of them paid full input price.
#
# The prompt is now sized past the threshold, but "sized past" rests on a
# character-count estimate of a tokenizer we do not run locally, and any future
# edit to the rubric or the tool schema can quietly drop it back under. So the
# claim is VERIFIED FROM LIVE USAGE instead of trusted: after this many
# consecutive successful calls with zero cached tokens, say so loudly. One
# alert, then silence until caching is seen working again.
_CACHE_MISS_ALERT_AFTER = 25
_consecutive_uncached_calls = 0
_cache_alert_raised = False


def _note_cache_usage(cached_tokens: int) -> None:
    """Track whether prompt caching is actually engaging. Never raises."""
    global _consecutive_uncached_calls, _cache_alert_raised
    try:
        if cached_tokens > 0:
            if _cache_alert_raised:
                logger.info(
                    "Prompt caching is working again (%d cached tokens) — "
                    "clearing the cache-miss alert", cached_tokens,
                )
            _consecutive_uncached_calls = 0
            _cache_alert_raised = False
            return
        _consecutive_uncached_calls += 1
        if _consecutive_uncached_calls < _CACHE_MISS_ALERT_AFTER or _cache_alert_raised:
            return
        _cache_alert_raised = True
        logger.error(
            "Prompt caching is NOT engaging: %d consecutive Claude calls "
            "reported zero cached tokens. cache_control is being silently "
            "ignored, which happens when the cached prefix (tool schema + "
            "system prompt) falls below the model's %d-token minimum. Every "
            "call is paying full input price for the rubric. Check whether "
            "_SYSTEM_PROMPT or _CLASSIFY_TOOL was recently shortened.",
            _consecutive_uncached_calls, 4096,
        )
        _record_claude_event(
            "claude_cache_ineffective",
            f"{_consecutive_uncached_calls} consecutive calls with zero cached "
            f"tokens — cached prefix likely below the 4096-token minimum",
        )
    except Exception as exc:   # observability must never break classification
        logger.debug("Could not track cache usage: %s", exc)


def _record_claude_call(articles: list, latency_ms: int, msg, ok: bool,
                        scored_count: int = 0,
                        error_type: str | None = None,
                        live: bool = True) -> None:
    """
    Record one Claude classification call to `classifier_calls` (v21.14).

    This is Claude's half of the shadow comparison — without it we would have
    Qwen's latency and liveness with nothing to compare them against. Failed
    calls are recorded too: the `ok=false` rows ARE the outage record that the
    2026-08-04 and 08-06 blind spots lacked.

    `live=False` (backtest replay) skips the write entirely — see the `live`
    parameter of _batch_score_sentiment for why replay traffic must stay out of
    this table.

    Best-effort and import-local, exactly like _record_claude_event.
    """
    if not live:
        return
    try:
        from storage.database import record_classifier_call
        usage = getattr(msg, "usage", None)
        cached = None
        if usage is not None:
            # v21.16: count cache CREATION as well as reads. Anthropic reports
            # input_tokens as the non-cached remainder only, so the true input
            # size is input + creation + read; counting reads alone made the
            # first call of every 5-min TTL window — the write — indistinguishable
            # from no caching at all, both here and in the cost figures that
            # analysis/classifier_compare.py derives from these columns.
            read = getattr(usage, "cache_read_input_tokens", None) or 0
            created = getattr(usage, "cache_creation_input_tokens", None) or 0
            cached = (read + created) or None
            _note_cache_usage(read + created)
        record_classifier_call(
            "claude", "claude-haiku-4-5-20251001", len(articles), latency_ms,
            ok=ok, scored_count=scored_count, error_type=error_type,
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
            tokens_cached=cached,
        )
    except Exception as exc:
        logger.debug("Could not record classifier_call for claude: %s", exc)


def _record_claude_failure(articles: list, error_type: str,
                           detail: str | None = None,
                           latency_ms: int | None = None,
                           live: bool = True) -> None:
    """
    Record a Claude batch that produced NO usable classification (v21.14.1).

    Without this, Claude writes a `classifier_calls` row only when the HTTP
    call succeeded and returned a parseable body — so every 403/401/429/5xx and
    every cooldown-suppressed cycle left NO row at all, while the shadow
    provider records one for each of its own failures. `latency_and_liveness()`
    computes `success_rate` and `worst_failure_streak` per provider over the
    rows that exist, so the effect was not a small bias: a total Claude outage
    rendered as success_rate=100%, worst_failure_streak=0 — the outage rows
    were simply absent. The module docstring tells the reader to weigh
    worst_failure_streak above everything else, so the fallback decision would
    have been made on a dataset with Claude's outages deleted.

    `latency_ms` stays None for a suppressed cycle (no call was made, so there
    is no latency to report and the percentiles must not see a zero).
    """
    if not live:
        return
    try:
        from storage.database import record_classifier_call
        record_classifier_call(
            "claude", "claude-haiku-4-5-20251001", len(articles), latency_ms,
            ok=False, scored_count=0, error_type=error_type,
            error_detail=(str(detail)[:500] if detail else None),
        )
    except Exception as exc:
        logger.debug("Could not record classifier_call failure for claude: %s", exc)


def _record_claude_event(event_type: str, detail: str,
                         live: bool = True) -> None:
    """Best-effort system_event for a Claude failure (alerting/observability).

    Import-local and swallowing — an event-log failure must never affect the
    news path. See storage.database.record_system_event.

    `live=False` (backtest replay) writes nothing. record_system_event stamps
    event_day from TODAY and de-dupes atomically on (event_type, event_day), so
    a replayed event would consume the day's only alert slot and silently
    suppress the genuine production one — the same "a replayed row permanently
    blocks the real one" hazard already guarded for qwen_scores.
    """
    if not live:
        return
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
    "fda_approval",       # US FDA approval / positive trial / regulatory green light — FDA ONLY, never other regulators (Health Canada, EMA, MHRA -> other)
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

DIGESTS AND PREVIEWS ARE NEVER CATALYSTS: a compilation headline ("Market-
Moving News for July 10th", "Stocks To Watch", "Premarket Movers", "Earnings
Scheduled For Today") is written ABOUT the market, with tickers tagged
incidentally — it contains no new single-stock information regardless of how
positive its contents sound. → sentiment=neutral, catalyst_type=
recap_explainer, catalyst_magnitude=1, for EVERY ticker on the article. A
real catalyst headline names the specific company and the specific event.

SAME-TICKER CONTEXT: Some articles include a line "PRIOR ARTICLE(S) TODAY ON
THIS TICKER" — earlier headlines about the SAME stock, already scored earlier
this session. Use it. If a prior article was NEGATIVE (e.g. "X Tumbles
Despite Earnings Beat") and this new article reframes the same underlying
event positively (e.g. "Analysts See More Upside"), treat the new article
with EXTRA skepticism: this is very likely commentary/analysis on an event
the market has already priced in and reacted to, not fresh information. Lower
confidence and set already_moved=true — UNLESS the new article describes a
genuinely NEW, separate fact (a new number, a new deal, a new filing) rather
than just a different spin on the same news.

STEP 2 — Is the tagged ticker the actual SUBJECT of the news?
In "Company B acquires Company A", the TARGET (A) spikes; the ACQUIRER (B)
drops or stays flat. If the tagged ticker is the acquirer → sentiment=neutral,
catalyst_type=ma_acquirer. If the article is primarily about a different
company than the tagged ticker → neutral.

STEP 3 — Is the catalyst binding and material?
- Binding: definitive merger agreement, firm buyout offer, signed contract
  with a dollar value, actual FDA approval. → can be positive.
- fda_approval means the US FDA specifically. A Health Canada, EMA, MHRA, or
  other non-US-regulator approval is NOT catalyst_type=fda_approval — this
  system trades US equities off a US-regulator edge that does not extend to
  foreign approvals. → catalyst_type=other.
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

PRELIMINARY RESULTS / PRE-ANNOUNCEMENTS: a headline shaped like "Company
Reports Preliminary Q_ Revenue To Be [Above/Near/Below] $XB–$YB Range; ...
Margins Expected In Range Of A%–B%" (or "Updates Q_ Outlook", "Pre-Announces
Q_ Results") is the company guiding the market AHEAD of its scheduled
earnings date — this is a guidance event, not a completed report.
catalyst_type=guidance_raise, not earnings_beat. Reserve earnings_beat for a
completed report compared against a stated consensus estimate (headline
contains "Beats"/"Misses"/"vs. Est"). This distinction is about ROUTING only
— it does not relax STEP 3's evidence bar: without an explicit comparison
point in the text ("vs. prior guidance of X", "raised from Y", "Beats Z
Est"), you cannot tell whether a bare figure is a raise or a cut, so
sentiment stays neutral/low-confidence exactly as it would for any other
unanchored number. Getting the type right just means a LATER article with
the missing comparison (or a same-day follow-up load — see SAME-TICKER
CONTEXT) is no longer misrouted into a bucket this system never trades.

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

Headline: "Acme Receives Health Canada Approval For Lead Drug ACM-101"
→ {"sentiment": "neutral", "confidence": 0.3, "catalyst_type": "other", "already_moved": false, "catalyst_magnitude": 1}

Headline: "Acme Therapeutics Shares Halted On Circuit Breaker To The Upside"
→ {"sentiment": "neutral", "confidence": 0.2, "catalyst_type": "halt_or_resume", "already_moved": true, "catalyst_magnitude": 1}

Headline: "What's Going On With Acme Therapeutics Stock On Tuesday?"
→ {"sentiment": "neutral", "confidence": 0.2, "catalyst_type": "recap_explainer", "already_moved": true, "catalyst_magnitude": 1}

WORKED EXAMPLES FROM REAL ERRORS

Each of the following was misclassified in production and cost money. The
common thread: a TEMPLATED headline whose wording announces that the move has
already happened, or that no single-stock news exists at all. Read the words
that are actually there rather than the sentiment they imply.

Headline: "Acme Energy Stock Charges Higher Tuesday: What's Driving The Post-Earnings Rally?"
→ {"sentiment": "neutral", "confidence": 0.2, "catalyst_type": "recap_explainer", "already_moved": true, "catalyst_magnitude": 1}
WHY: "Charges Higher" and "Rally" are past-tense descriptions of a move in
progress; the article asks what is driving it, so it is not itself the driver.
The earnings report that caused this is a separate, earlier wire item. This was
scored guidance_raise / positive / 0.75 with already_moved=false, and the stock
was already up 4% on the day at entry. already_moved is the single field that
decides these cases — set it true whenever the headline itself reports the move.

Headline: "Market-Moving News For July 10: Acme Corp, Beta Industries, Gamma Holdings"
→ {"sentiment": "neutral", "confidence": 0.1, "catalyst_type": "recap_explainer", "already_moved": true, "catalyst_magnitude": 1}
WHY: a digest tags several unrelated tickers on one article. There is no fact
here about any single company. Scored as earnings_beat / 0.8 for all three
tickers, this bought the top of a 13% spike. Apply this to EVERY ticker on such
an article, however positive the contents sound.

Headline: "Acme Corp Reports Preliminary Q3 Revenue Of $412M, Above Prior Guidance Of $380M–$395M"
→ {"sentiment": "positive", "confidence": 0.85, "catalyst_type": "guidance_raise", "already_moved": false, "catalyst_magnitude": 3}
WHY: a pre-announcement ahead of the scheduled report, and the comparison point
is explicit ("Above Prior Guidance Of"), so the direction is knowable. This is
guidance_raise, not earnings_beat — no completed report is being compared to a
consensus estimate.

Headline: "Acme Corp Reports Preliminary Q3 Revenue Of $412M"
→ {"sentiment": "neutral", "confidence": 0.4, "catalyst_type": "guidance_raise", "already_moved": false, "catalyst_magnitude": 1}
WHY: same routing, but the number is unanchored — with no prior guidance or
estimate stated, $412M could be a raise or a cut. Correct type, neutral
sentiment. Never infer the direction of a bare figure.

Headline: "Beta Industries To Acquire Acme Corp For $28 Per Share In Cash" [tagged ticker: BETA, the acquirer]
→ {"sentiment": "neutral", "confidence": 0.3, "catalyst_type": "ma_acquirer", "already_moved": false, "catalyst_magnitude": 1}
WHY: the target re-prices to the offer instantly; the acquirer typically falls
on deal risk and dilution. Always check which side the TAGGED ticker is on
before calling M&A positive.

Headline: "Acme Corp Announces $75M Registered Direct Offering Priced At-The-Market"
→ {"sentiment": "negative", "confidence": 0.8, "catalyst_type": "offering_dilution", "already_moved": false, "catalyst_magnitude": 4}
WHY: an offering is new, binding and material — but the direction is DOWN.
Small caps sell equity into strength, and this reliably reverses the run-up
that preceded it. Materiality is not the same as bullishness.

GETTING guidance_raise RIGHT

This class carries more weight than the others: it is the one whose measured
forward returns still climb an hour after publication, which is the shape a
minutes-latency entry can actually capture. Both error directions are costly —
a missed raise is a missed trade, and a false raise is a real loss — so apply
the definition literally.

A guidance_raise requires a company statement about its OWN FUTURE results
that is higher than a previously stated figure. All three parts are required:
the company itself (not an analyst), a forward period (not a completed one),
and an upward comparison against something stated.

Counts as guidance_raise:
- "Raises FY25 Revenue Outlook To $1.2B–$1.25B From $1.1B–$1.15B"
- "Now Sees Q4 EPS Above Prior View"
- "Lifts Full-Year Margin Target After Strong Demand"
- A preliminary/pre-announced figure explicitly above prior guidance.

Does NOT count as guidance_raise:
- "Analyst Raises Price Target To $95" → analyst_action. A price target is an
  outsider's opinion, not company guidance. This is routine flow.
- "Reports Q3 EPS Of $1.42, Beating The $1.30 Estimate" → earnings_beat. A
  completed period compared to consensus is a different class.
- "Reaffirms FY25 Guidance" / "Maintains Outlook" → other, neutral. Confirming
  an existing number is not raising it, however reassuring the tone.
- "Guides Q4 Below Consensus" → sentiment=negative. Read the direction; do not
  let the word "guidance" imply a raise.
- "Management Optimistic About Second-Half Demand" → other, low confidence.
  Sentiment in an interview is not a numeric revision.

When a guidance raise is genuine, size catalyst_magnitude on how far the new
range sits above the old one, not on the absolute dollar figure: a 2% lift to
a mega-cap's outlook is magnitude 1–2, while a small cap raising a full-year
revenue target by 20%+ is magnitude 4.

Headline: "Acme Announces $40M Registered Direct Offering Priced At-The-Market"
→ {"sentiment": "negative", "confidence": 0.85, "catalyst_type": "offering_dilution", "already_moved": false, "catalyst_magnitude": 3}

Headline: "MegaBank Maintains Overweight On Apple, Raises Price Target To $310"
→ {"sentiment": "neutral", "confidence": 0.3, "catalyst_type": "analyst_action", "already_moved": false, "catalyst_magnitude": 1}

Headline: "Acme Signs Non-Binding LOI To Merge With Beta Corp"
→ {"sentiment": "neutral", "confidence": 0.4, "catalyst_type": "other", "already_moved": false, "catalyst_magnitude": 2}

Headline: "SmallCorp Wins $45M DoD Contract (Annual Revenue ~$120M)"
→ {"sentiment": "positive", "confidence": 0.85, "catalyst_type": "contract_win", "already_moved": false, "catalyst_magnitude": 4}

Headline: "Acme Corp Reports Preliminary Q4 Revenue To Be Near Low End Of
$900M-$950M Range; Non-GAAP Gross Margin Expected In Range Of 30%-33%"
→ {"sentiment": "neutral", "confidence": 0.35, "catalyst_type": "guidance_raise", "already_moved": false, "catalyst_magnitude": 2}
(A pre-announcement, so catalyst_type=guidance_raise even though nothing here
is confirmed positive — the margin range has no prior-guidance baseline to
compare against, so sentiment stays neutral/low-confidence rather than
guessing. Compare to the next example, which gives the missing baseline.)

Headline: "Acme Corp Raises Q4 Non-GAAP Gross Margin Guidance To 30%-33%,
Up From Prior 18%-20%"
→ {"sentiment": "positive", "confidence": 0.85, "catalyst_type": "guidance_raise", "already_moved": false, "catalyst_magnitude": 4}

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


# ── Benzinga availability tracking ───────────────────────────────────────────
# Benzinga is the LAST external dependency with no outage marker: Twelvedata
# exhaustion and Claude outages both emit system_events, but a dead news feed
# (expired key, API down) produces zero signals — indistinguishable in every
# dashboard from a quiet news day until the zero-trade tripwire fires days
# later. After this many CONSECUTIVE failed fetches (~10 min at the 1-min
# cadence) we emit one system_event (DB-deduped to one row/day) and shout.
_BENZINGA_OUTAGE_THRESHOLD = 10
_benzinga_consecutive_failures = 0


def _note_benzinga_failure() -> None:
    global _benzinga_consecutive_failures
    _benzinga_consecutive_failures += 1
    if _benzinga_consecutive_failures == _BENZINGA_OUTAGE_THRESHOLD:
        logger.error(
            "Benzinga feed has failed %d consecutive fetches — NO news signals "
            "are flowing (RTH and pre-market both blind). Check the "
            "MASSIVE_BENZINGA_API_KEY / massive.com status.",
            _BENZINGA_OUTAGE_THRESHOLD,
        )
        try:
            from storage.database import record_system_event
            record_system_event(
                "benzinga_outage",
                f"{_BENZINGA_OUTAGE_THRESHOLD} consecutive failed news fetches",
            )
        except Exception as exc:
            logger.debug("Could not record benzinga_outage system_event: %s", exc)


def _note_benzinga_ok() -> None:
    global _benzinga_consecutive_failures
    _benzinga_consecutive_failures = 0


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
            _note_benzinga_failure()
            return []
        data = resp.json()
        # A 200 OK with a body that isn't the expected envelope shape (schema
        # change on massive.com's side, an error wrapped in a 200, a paginated
        # response under a different key) must NOT be treated as "fetched zero
        # articles this cycle" — that outcome resets _benzinga_consecutive_failures
        # via _note_benzinga_ok() below, which means the outage tripwire this
        # function exists to back could never fire while every cycle silently
        # returns nothing. Only recognized envelope shapes count as success.
        if not isinstance(data, dict) or ("results" not in data and "articles" not in data):
            logger.warning(
                "Benzinga API: unrecognized response shape (keys=%s) — "
                "treating as a fetch failure, not zero articles",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            _note_benzinga_failure()
            return []
        articles = data.get("results", data.get("articles", []))
        logger.debug("Benzinga: fetched %d raw articles (lookback=%d min)", len(articles), lookback_minutes)
        _note_benzinga_ok()
        return articles
    except requests.exceptions.Timeout:
        logger.warning("Benzinga API timeout after %ds — skipping cycle", _TIMEOUT)
        _note_benzinga_failure()
        return []
    except requests.RequestException as exc:
        logger.warning("Benzinga API request failed: %s", exc)
        _note_benzinga_failure()
        return []
    except Exception as exc:
        logger.error("Benzinga API unexpected error: %s", exc, exc_info=True)
        _note_benzinga_failure()
        return []


def _output_budget(n_articles: int) -> int:
    """
    max_tokens for a batch of n_articles.

    Single definition, imported by news/shadow_classifier.py and asserted by the
    tests, so the live call, the shadow call and the test expectation can never
    disagree. v21.14.1 shipped a duplicated formula and the copies drifted;
    sharing only the two constants (v21.15) left the arithmetic duplicated in
    six places, where a changed overhead term would still have diverged
    silently.
    """
    return max(_MIN_OUTPUT_TOKENS, n_articles * _TOKENS_PER_ARTICLE + 256)


def _batch_score_sentiment(articles: list[dict],
                           live: bool = True) -> dict[str, dict]:
    """
    Score sentiment for a list of articles, chunked into bounded Claude calls.

    Splits into batches of at most _MAX_ARTICLES_PER_BATCH and merges the
    results, so the output budget per call is bounded regardless of how large
    the unscored backlog has grown. A chunk that fails contributes nothing and
    leaves its own articles unscored (and therefore still eligible next cycle);
    it does not discard the chunks around it.
    """
    if not articles:
        return {}
    scores: dict[str, dict] = {}
    for i in range(0, len(articles), _MAX_ARTICLES_PER_BATCH):
        chunk = articles[i:i + _MAX_ARTICLES_PER_BATCH]
        scores.update(_score_one_batch(chunk, live=live))
    return scores


def _score_one_batch(articles: list[dict],
                     live: bool = True) -> dict[str, dict]:
    """
    Score sentiment for one bounded batch of articles in a single Claude call.

    articles: list of dicts with keys 'id', 'headline', 'teaser', 'ticker', and
              optionally 'prior_context' (see _prior_ticker_context).
    live:     this batch is production traffic. Governs BOTH shadow dispatch
              and `classifier_calls` recording, because both write production
              observability datasets. OFFLINE callers (backtest replays) MUST
              pass False:
                * `qwen_scores` is UNIQUE per article with ON CONFLICT DO
                  NOTHING, so a replayed row permanently blocks the real one
                  for that article;
                * `classifier_calls` feeds the p50/p95 that decides whether a
                  provider fits inside the 60s news cycle — replay latency and
                  20-article replay batches are not production traffic and
                  must not be mixed in;
                * replay volume would also push `min(calls)` past
                  `_MIN_CALLS_FOR_VERDICT` and invite a verdict on data that
                  never came from the live path.
    Returns: dict mapping article id → {sentiment, confidence, catalyst_type,
             already_moved}. Empty dict on failure (fail-closed: unscored
             articles are never traded).
    """
    global _consecutive_empty_batches

    if not articles:
        return {}

    # Per-cycle content goes in the user message; the static rubric stays in
    # the cached system block.
    lines = []
    for a in articles:
        line = f'ID {a["id"]}: {a["headline"]}\n   Teaser: {a["teaser"]}'
        prior = a.get("prior_context")
        if prior:
            line += f'\n   PRIOR ARTICLE(S) TODAY ON THIS TICKER: {prior}'
        lines.append(line)
    user_content = "Articles to classify:\n\n" + "\n\n".join(lines)

    # Shadow-mode second opinion (v21.14). Fire-and-forget on a background
    # thread; Claude's answer remains the only one that reaches a trading
    # decision and nothing is read back from here. No-op unless QWEN_* are set.
    #
    # Deliberately dispatched BEFORE the Claude cooldown check below. A cooldown
    # means Claude is FAILING, which is precisely the scenario a fallback exists
    # for — gating the shadow behind it would guarantee we never collect a
    # single data point about how Qwen behaves during a Claude outage, the one
    # question this whole exercise is meant to answer.
    #
    # Cost, stated honestly: the empty-batch cooldown is 120s (2 cycles), but
    # the SAME gate also covers the 1800s billing/auth cooldown — 30 cycles,
    # each now firing a paid Qwen batch that previously cost nothing, and
    # because _mark_scored() only fires on Claude success the same (growing)
    # backlog is re-offered every one of them. That is accepted deliberately:
    # Qwen-Flash is ~20x cheaper per token, UNIQUE(article_id) + ON CONFLICT DO
    # NOTHING means re-offered articles never double-count in qwen_scores, and
    # a billing outage is exactly the event the fallback is being evaluated for.
    if live:
        try:
            shadow_score(articles, user_content)
        except Exception as exc:      # must never affect the Claude path
            logger.debug("Shadow classifier dispatch failed: %s", exc)

    # Skip the Claude call while a prior outage/billing failure cooldown is
    # active — returning {} fails closed (no scores → no trades) without
    # re-hitting a known-down API every cycle.
    if not _claude_available():
        # Recorded as a liveness failure even though no HTTP call was made:
        # from the pipeline's point of view this cycle got no answer from
        # Claude, which is precisely what the comparison must capture. Omitting
        # it made a multi-cycle outage invisible in Claude's failure streak.
        _record_claude_failure(articles, "cooldown_suppressed", live=live)
        return {}

    # ── Output budget (v21.15 — this was THE "empty batch" bug) ──────────────
    #
    # The old budget was `max(400, n * 60 + 64)`, from a claimed "~55 tokens per
    # article empirically". Measured against real production rows in
    # `classifier_calls`, the true cost is 68-72 tokens/article — ABOVE the
    # allowance. So on any batch big enough for the fixed overhead to stop
    # covering the gap, the response hit the ceiling and was cut off mid
    # tool-call.
    #
    # A truncated forced-tool-use response still returns 200 OK with a tool_use
    # block, but its `input` never finished serialising, so
    # `block.input.get("classifications", [])` yields []. That is the entire
    # "well-formed but EMPTY classifications list" mystery below: not Claude
    # declining to answer, us cutting it off mid-sentence.
    #
    # Proof, 2026-08-12..14: 26 Claude calls had tokens_out EXACTLY equal to
    # this cap; all 26 recorded scored_count=0; and there were exactly 26
    # `empty_batch` errors in the same window. A 1:1 match.
    #
    # It also explains every property of the outages that never fit the old
    # theory:
    #   * why retries never helped (v21.7/v21.12) — same batch, same size, same
    #     deterministic truncation, so a retry is a re-run of the failure;
    #   * why it SELF-REINFORCED — `_mark_scored()` only fires on success, so a
    #     failed cycle returns a BIGGER batch next minute, which truncates
    #     harder: a death spiral, not a flaky API;
    #   * why it always self-recovered without intervention — articles aged out
    #     via `max_age_minutes`, shrinking the batch until it fit again;
    #   * why it clustered at premarket and session boundaries (2026-08-04
    #     07:00 ET, 2026-08-06 07:00-08:38 ET) — that is when the overnight
    #     backlog makes batches largest.
    #
    # max_tokens is a CEILING, not a charge — only tokens actually generated
    # are billed, and forced tool use with a strict schema means the model
    # cannot ramble to fill it. So the budget is now generous on purpose:
    # under-budgeting costs whole trading sessions, over-budgeting costs zero.
    max_tokens = _output_budget(len(articles))
    try:
        # Retries when Claude returns a well-formed but EMPTY classifications
        # list for a non-empty batch. This is not a parsing failure — the 200 OK
        # forced-tool-use call legitimately came back with zero entries. Observed
        # at the first news_cycle tick after a session boundary (2026-07-27
        # 16:06-16:12 ET regular→afterhours, 2026-07-28 07:00-07:07 ET premarket
        # scan start): 6-8 consecutive cycles, each returning [], while the
        # backlog of unscored articles grew every cycle (since _mark_scored only
        # fires on a successful score) until it self-recovered.
        #
        # v21.12 — the v21.7 SINGLE retry is not enough. 2026-08-04: 25 cycles
        # across two windows (07:00-07:18 and 07:31-07:36 ET) in which BOTH the
        # first call and its retry came back empty, every time. The backlog grew
        # 10 → 36 articles and then shrank again as articles aged out via
        # max_age_minutes — i.e. they were discarded WITHOUT EVER BEING SCORED.
        # Impact was contained only because both windows fell before the 08:00 ET
        # watchlist build; the same 25 minutes landing on 09:30-09:55 would blind
        # the system through its most productive window.
        #
        # Two changes: retry _EMPTY_BATCH_ATTEMPTS times with a short backoff
        # (the failure clearly persists across an immediate re-ask), and record a
        # system_event when they are all exhausted. The 2026-08-04 outage left NO
        # trace on any monitoring surface — no system_event, nothing in Grafana,
        # only ERROR lines in the journal. A silent 25-minute blind spot in the
        # one dependency that has no fallback must be visible.
        #
        # Budget: attempts run ~4-8s each and the backoff totals
        # _EMPTY_BATCH_BACKOFF_SECONDS * 2, which keeps the worst case (~30s)
        # inside the 60s news_cycle cadence.
        for score_attempt in range(_EMPTY_BATCH_ATTEMPTS):
            _claude_started = time.monotonic()
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
                        #
                        # ⚠️ v21.16: this had been a silent NO-OP since it was
                        # added. Claude Haiku 4.5 has a MINIMUM CACHEABLE
                        # PREFIX OF 4096 TOKENS; below it, cache_control is
                        # accepted and ignored — no error, no warning, and
                        # usage simply reports zero cached tokens. Measured
                        # over the first 1,140 Claude calls: tokens_cached
                        # summed to exactly 0, while Qwen (whose endpoint
                        # caches automatically with no minimum) accumulated
                        # 38,272. The cached prefix is tools + system, which
                        # totalled ~3.5k tokens — about 600 short.
                        # _SYSTEM_PROMPT is now sized past the threshold with
                        # real worked examples, so DO NOT trim it below ~4.3k
                        # tokens (~15k characters of prompt + tool schema)
                        # without re-checking tokens_cached afterwards.
                        # _warn_if_cache_never_hits() below verifies this from
                        # live usage rather than trusting the estimate.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_content}],
                tools=[_CLASSIFY_TOOL],
                # FORCE the tool call — the model cannot reply with prose.
                tool_choice={"type": "tool", "name": "classify_articles"},
            )
            _claude_latency_ms = int((time.monotonic() - _claude_started) * 1000)

            # v21.15: a response cut off at the ceiling is a BUDGET failure, not
            # a model failure, and must never again be filed as `empty_batch`.
            # Truncation returns 200 OK with a tool_use block whose input never
            # finished serialising, so it is indistinguishable from a genuine
            # empty answer unless stop_reason is read — which is exactly how it
            # went misdiagnosed across v21.7, v21.12 and v21.13.
            if getattr(msg, "stop_reason", None) == "max_tokens":
                logger.error(
                    "Batch sentiment: Claude response TRUNCATED at max_tokens=%d "
                    "for a %d-article batch — the classification list is "
                    "incomplete and is being discarded. This is our output "
                    "budget, not a Claude fault; raise _TOKENS_PER_ARTICLE.",
                    max_tokens, len(articles),
                )
                _record_claude_call(articles, _claude_latency_ms, msg,
                                    ok=False, error_type="truncated", live=live)
                _record_claude_event(
                    "claude_truncated_batch",
                    f"response hit max_tokens={max_tokens} on a "
                    f"{len(articles)}-article batch — output budget too small",
                    live=live,
                )
                return {}

            classifications = None
            for block in msg.content:
                if getattr(block, "type", None) == "tool_use":
                    classifications = block.input.get("classifications", [])
                    break
            if classifications is None:
                logger.error("Batch sentiment: no tool_use block in Claude response")
                _record_claude_call(articles, _claude_latency_ms, msg,
                                    ok=False, error_type="no_tool_use",
                                    live=live)
                return {}
            if not isinstance(classifications, list):
                logger.error(
                    "Batch sentiment: classifications is %s, not a list — discarding",
                    type(classifications).__name__,
                )
                _record_claude_call(articles, _claude_latency_ms, msg,
                                    ok=False, error_type="bad_shape",
                                    live=live)
                return {}
            _record_claude_call(
                articles, _claude_latency_ms, msg,
                ok=bool(classifications), scored_count=len(classifications),
                error_type=None if classifications else "empty_batch",
                live=live,
            )
            if classifications:
                # The classifier is answering again — clear the streak so an
                # earlier bad patch can't push a later isolated blip straight
                # into a cooldown.
                _consecutive_empty_batches = 0
                break
            if score_attempt == _EMPTY_BATCH_ATTEMPTS - 1:
                # All attempts exhausted. Fail closed (unscored → untraded) but
                # make the blind spot VISIBLE — record_system_event de-dupes to
                # one row per day, so this is one alert per outage, not per cycle.
                _consecutive_empty_batches += 1
                logger.error(
                    "Batch sentiment: Claude returned 0 classifications for a "
                    "%d-article batch on all %d attempts (%d cycle(s) in a row) "
                    "— giving up this cycle. These articles stay unscored and "
                    "will be re-offered until they age out of the freshness "
                    "window UNSCORED.",
                    len(articles), _EMPTY_BATCH_ATTEMPTS,
                    _consecutive_empty_batches,
                )
                _record_claude_event(
                    "claude_empty_batch",
                    f"empty classification lists on all {_EMPTY_BATCH_ATTEMPTS} "
                    f"attempts for a {len(articles)}-article batch — news "
                    f"scoring is blind while this persists",
                    live=live,
                )
                # Repeated across cycles this is not a blip; stop paying for it.
                if _consecutive_empty_batches >= _EMPTY_BATCH_COOLDOWN_TRIGGER:
                    logger.error(
                        "Batch sentiment: %d consecutive all-empty cycles — "
                        "pausing sentiment scoring for %ds. On 2026-08-06 this "
                        "state lasted 98 minutes and retrying through it cost "
                        "174 API calls and scored nothing.",
                        _consecutive_empty_batches, _EMPTY_BATCH_COOLDOWN_SECONDS,
                    )
                    _enter_claude_cooldown(
                        _EMPTY_BATCH_COOLDOWN_SECONDS,
                        f"{_consecutive_empty_batches} consecutive empty batches",
                    )
                break
            logger.error(
                "Batch sentiment: Claude returned 0 classifications for a "
                "%d-article batch — retry %d/%d in %.1fs",
                len(articles), score_attempt + 1, _EMPTY_BATCH_ATTEMPTS - 1,
                _EMPTY_BATCH_BACKOFF_SECONDS,
            )
            time.sleep(_EMPTY_BATCH_BACKOFF_SECONDS)

        results: dict[str, dict] = {}
        for r in classifications:
            # Per-record validation, fail-closed per record: one malformed
            # entry (confidence="high", magnitude "big", a stray string in
            # the list) must skip THAT article, not raise out of the loop
            # and discard the whole batch. Out-of-range values are rejected
            # rather than clamped — a confidence of 7 is more likely a
            # mis-scaled 0-10 answer than a genuine 100%+, and guessing the
            # scale on a trading signal is worse than not trading it.
            try:
                if not isinstance(r, dict) or "id" not in r:
                    continue
                confidence = float(r.get("confidence", 0.5))
                magnitude = int(r.get("catalyst_magnitude", 1))
            except (TypeError, ValueError):
                logger.warning(
                    "Batch sentiment: malformed classification skipped: %s",
                    str(r)[:120],
                )
                continue
            if not (0.0 <= confidence <= 1.0) or not (1 <= magnitude <= 5):
                logger.warning(
                    "Batch sentiment: out-of-range classification skipped "
                    "(confidence=%s magnitude=%s): %s",
                    confidence, magnitude, str(r)[:120],
                )
                continue
            results[str(r["id"])] = {
                "sentiment": str(r.get("sentiment", "neutral")).lower(),
                "confidence": confidence,
                "catalyst_type": str(r.get("catalyst_type", "other")),
                "already_moved": bool(r.get("already_moved", False)),
                "catalyst_magnitude": magnitude,
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
        _record_claude_event("claude_billing_error", f"403 {err_type}: {exc}",
                             live=live)
        _record_claude_failure(articles, f"403_{err_type}", exc, live=live)
        return {}
    except anthropic.AuthenticationError as exc:
        logger.critical(
            "Claude authentication failed (401): %s — sentiment scoring suspended "
            "for %d min; the ANTHROPIC_API_KEY is invalid or revoked.",
            exc, _CLAUDE_BILLING_COOLDOWN_SECONDS // 60,
        )
        _enter_claude_cooldown(_CLAUDE_BILLING_COOLDOWN_SECONDS, "401 auth")
        _record_claude_event("claude_auth_error", f"401: {exc}", live=live)
        _record_claude_failure(articles, "401_auth", exc, live=live)
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
        _record_claude_event("claude_outage", f"429 rate limit: {exc}",
                             live=live)
        _record_claude_failure(articles, "429_rate_limit", exc, live=live)
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
        _record_claude_event("claude_outage", f"status {status}: {exc}",
                             live=live)
        _record_claude_failure(articles, f"api_status_{status}", exc, live=live)
        return {}
    except Exception as exc:
        # Unknown failure (malformed response, schema parse, etc.) — fail closed,
        # but DON'T enter a cooldown: this may be a one-off bad batch, and we
        # don't want a single odd article to silence scoring for minutes.
        logger.error("Batch sentiment classification failed: %s", exc, exc_info=True)
        _record_claude_failure(articles, type(exc).__name__, exc, live=live)
        return {}


def fetch_all_news(
    lookback_minutes: int = 5,
    max_age_minutes: float = 3.0,
    seen_checker: Callable[[str, str], bool] = is_article_seen,
) -> list[NewsItem]:
    """
    Fetch recent news from Benzinga, filter, classify in one batched Claude
    call, persist ALL scores for the eval loop, and return the tradeable
    positive signals.

    Parameters:
      lookback_minutes — Benzinga query window.
      max_age_minutes  — drop articles older than this. Default 3 min: wide
                         enough to survive Benzinga feed-indexing latency and
                         an overrunning news cycle (both silently killed
                         catalysts under the old 1-min cutoff), while the
                         _scored_articles session dedup prevents re-scoring.
                         Anything genuinely 3 min old is also still inside the
                         momentum gates' judgement window — the price
                         confirmation decides whether the move is still live.
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
        if not isinstance(article, dict):
            continue  # a null/scalar slipped into the feed's article array
        article_id = str(article.get("benzinga_id") or article.get("url") or article.get("title") or "")
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        # Already scored by Claude this session (wider freshness window means
        # the same article appears in several consecutive fetches).
        if _already_scored(article_id):
            continue

        # Type guards: one non-string entry in one article's ticker list (ints,
        # nulls) would otherwise AttributeError here and kill the ENTIRE fetch
        # cycle; a bare-string tickers field would iterate as characters —
        # and single letters like "A" are real NYSE tickers.
        tickers_field = article.get("tickers") or []
        if not isinstance(tickers_field, (list, tuple)):
            tickers_field = []
        raw_tickers = [
            t for t in tickers_field
            if isinstance(t, str) and t and not t.startswith("X:")  # X:BTCUSD etc. are crypto pairs
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

        # Roundup filter: >3 tickers = market digest, no per-stock catalyst.
        # Checked BEFORE the per-ticker dedup below — it only needs the raw
        # tag count, and skipping first saves a DB round-trip per ticker on
        # every digest article.
        if len(raw_tickers) > 3:
            logger.debug(
                "Skipping roundup article %s — %d tickers (max 3): %s",
                article_id, len(raw_tickers), (article.get("title") or "")[:60],
            )
            continue

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

        # Analyst action pre-filter: these always produce catalyst_type=analyst_action,
        # which is never in TRADEABLE_CATALYSTS. A regex match on the raw headline is
        # far cheaper than a Claude API call, and the pattern is conservative enough
        # that false positives (a tradeable headline accidentally matching) are
        # essentially impossible for the specific phrases targeted.
        # `or ""` (not a .get default): the feed can send title with an
        # explicit null value, and .get's default only covers a MISSING key —
        # regex/slicing on None would crash the whole cycle.
        headline_raw = article.get("title") or ""
        if _ANALYST_ACTION_RE.search(headline_raw):
            logger.debug(
                "Skipping analyst action article (pre-Claude filter): %s", headline_raw[:80],
            )
            continue

        # Digest/preview/listicle pre-filter: compilations are never a
        # single-stock catalyst, and letting them through produced fabricated
        # classifications (CRCL 2026-07-10 — see _DIGEST_RE).
        if _DIGEST_RE.search(headline_raw):
            logger.info(
                "Skipping digest/preview article (pre-Claude filter): %s",
                headline_raw[:80],
            )
            continue

        # Explainer/recap pre-filter: an article ABOUT a move that already
        # happened is not the catalyst that caused it. Letting one through cost
        # trade #25 (BE, 2026-08-04, −2.82%) — see _EXPLAINER_RE.
        if _EXPLAINER_RE.search(headline_raw):
            logger.info(
                "Skipping explainer/recap article (pre-Claude filter): %s",
                headline_raw[:80],
            )
            continue

        eligible.append((article, eligible_tickers, article_id))

    if not eligible:
        logger.info("Benzinga: %d article(s) fetched → 0 eligible after filtering", len(seen_ids))
        return []

    # ── Step 2: batch score all eligible articles in one Claude call ──────────
    to_score = []
    for article, tickers, article_id in eligible:
        entry = {
            "id": article_id,
            "headline": html.unescape(article.get("title") or ""),
            "teaser": html.unescape(article.get("teaser") or (article.get("body") or "")[:200]),
            # Not used to build the Claude prompt (which is id/headline/teaser
            # only) — carried so shadow mode can attribute its row to a ticker.
            # Claude's own attribution happens later, when score_rows is fanned
            # out one row per (article, ticker); qwen_scores is one row per
            # ARTICLE, so it needs a single representative ticker.
            #
            # This is the first ELIGIBLE ticker, not the article's primary
            # Benzinga tag: `tickers` here is already filtered by
            # resolve_t212_ticker(), the blocklist, and the seen-checker. A
            # blocklisted TSLA article that also tags LCID is therefore
            # attributed to LCID. That is acceptable for a shadow row —
            # comparisons join on article_id, and forward returns are read from
            # sentiment_scores, which keeps the full per-ticker fan-out.
            # `eligible` is only appended to when eligible_tickers is non-empty,
            # so the list is never empty here.
            "ticker": tickers[0],
        }
        # Surface today's already-scored articles for the same ticker(s) so a
        # reversal/respin of the same story gets read with that context
        # instead of scored blind (see _ticker_history docstring).
        contexts = [
            f"{t}: {ctx}"
            for t in tickers
            if (ctx := _prior_ticker_context(t)) is not None
        ]
        if contexts:
            entry["prior_context"] = " | ".join(contexts)
        to_score.append(entry)
    scores = _batch_score_sentiment(to_score)
    # Only successfully-scored ids enter the dedup set — a failed batch (Claude
    # outage/cooldown) leaves its articles eligible for refetch next cycle.
    _mark_scored(scores.keys())

    # ── Step 2b: persist EVERY score for the eval loop ────────────────────────
    # This is the feedback loop: without recording neutrals/negatives there is
    # no way to measure the classifier's precision/recall against actual
    # forward returns, and every prompt change is a guess.
    score_rows = []
    scored_at_now = datetime.now(timezone.utc)
    for article, tickers, article_id in eligible:
        s = scores.get(article_id)
        if s is None:
            continue
        headline = html.unescape(article.get("title") or "")
        for ticker in tickers:
            score_rows.append({
                "article_id": article_id,
                "ticker": ticker,
                "headline": headline,
                "sentiment": s["sentiment"],
                "confidence": s["confidence"],
                "catalyst_type": s["catalyst_type"],
                "already_moved": s["already_moved"],
                "catalyst_magnitude": s["catalyst_magnitude"],
                "published_at": article.get("published", ""),
            })
            # Feeds _prior_ticker_context for this ticker's NEXT article today
            # (including neutrals/negatives — the negative "tumbles despite
            # beat" read is exactly the context a later bullish respin needs).
            _record_ticker_history(ticker, headline, s["sentiment"], scored_at_now)
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

        headline = html.unescape(article.get("title") or "")

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

        teaser = html.unescape(article.get("teaser") or (article.get("body") or "")[:200])
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
