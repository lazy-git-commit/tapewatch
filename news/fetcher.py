"""
news/fetcher.py
───────────────
Fetches real-time financial news from Benzinga (via massive.com) and
scores sentiment using Claude to identify bullish US equity signals.

Benzinga provides breaking news with ticker symbols but no built-in
sentiment — Claude classifies each article as bullish/bearish/neutral.

API call optimisations:
  1. Articles are filtered (tickers present, not blocklisted, not already seen)
     BEFORE any Claude call is made.
  2. All eligible headlines are scored in a single batched Claude call per cycle.
  3. Already-seen (article, ticker) pairs are skipped via is_article_seen so the
     same article is never re-scored across consecutive polling windows.
"""

import html
import json
import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import anthropic
import pytz
from config.settings import cfg
from storage.database import is_article_seen

_LONDON = pytz.timezone("Europe/London")

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.massive.com/benzinga/v2/news"
_TIMEOUT = 10
_claude = anthropic.Anthropic(api_key=cfg.anthropic_api_key)


@dataclass
class NewsItem:
    article_id: str
    ticker: str
    headline: str
    body: str
    source: str
    published_at: datetime
    sentiment: str    # "positive" | "neutral" | "negative"
    confidence: float  # 0.0–1.0


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
        resp.raise_for_status()
        data = resp.json()
        return data.get("results", data.get("articles", []))
    except requests.RequestException as exc:
        logger.warning("Benzinga API request failed: %s", exc)
        return []


def _batch_score_sentiment(articles: list[dict]) -> dict[str, tuple[str, float]]:
    """
    Score sentiment for multiple articles in a single Claude call.

    articles: list of dicts with keys 'id', 'headline', 'teaser'
    Returns: dict mapping article id → (sentiment, confidence)
    """
    if not articles:
        return {}

    items_json = json.dumps(
        [{"id": a["id"], "headline": a["headline"], "teaser": a["teaser"]} for a in articles],
        ensure_ascii=False,
    )
    prompt = (
        "You are an expert day trader specialising in US equity momentum trading.\n"
        "Your job is to identify news that will cause a stock to move UP sharply "
        "within the next 15 minutes of market trading.\n\n"

        "Rate each article as 'positive', 'neutral', or 'negative' based on whether "
        "it is likely to drive immediate intraday buying momentum.\n\n"

        "POSITIVE (high confidence 0.8–1.0) — genuine catalysts that move stocks NOW:\n"
        "- Earnings beats: revenue or EPS above analyst estimates\n"
        "- FDA approvals, drug trial success, regulatory green lights\n"
        "- M&A: acquisition announcements, buyout offers, merger deals\n"
        "- Major contract wins with concrete dollar values\n"
        "- Guidance raises: company raises full-year revenue or earnings outlook\n"
        "- Short squeeze signals: stock halted to the upside, unusual volume surge\n"
        "- Surprise CEO/product announcements with material business impact\n\n"

        "NEUTRAL or low-confidence positive (0.5–0.7) — may have some impact but weak:\n"
        "- Analyst initiations or upgrades WITH a specific catalyst mentioned\n"
        "- Partnership announcements without clear revenue figures\n"
        "- Clinical trial updates that are early-stage or mixed\n\n"

        "NEUTRAL (do not mark positive) — these almost never move a stock in 15 min:\n"
        "- Analyst price target raises or reiterations with no new information\n"
        "- 'Maintains Buy/Overweight' — the analyst already had this rating\n"
        "- General market or sector commentary\n"
        "- Conference attendance announcements\n"
        "- Scheduled dividend declarations\n"
        "- Articles that summarise or repeat news published days ago\n"
        "- Awards, rankings, ESG reports\n\n"

        "NEGATIVE — news that will drive the stock DOWN:\n"
        "- Earnings misses, guidance cuts, revenue warnings\n"
        "- FDA rejections, trial failures\n"
        "- Layoffs, CEO departures, fraud investigations\n"
        "- Analyst downgrades with specific negative catalyst\n\n"

        "Respond with a JSON array only — no markdown, no explanation.\n"
        "Each element must have exactly these keys: id, sentiment, confidence.\n"
        'sentiment must be one of: "positive", "neutral", "negative"\n'
        "confidence must be a float 0.0–1.0\n\n"
        f"Articles:\n{items_json}"
    )
    # Budget ~40 tokens per article for the response array
    max_tokens = max(512, len(articles) * 40 + 64)
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()
        # If the response was truncated mid-JSON, recover completed entries
        # by finding the last complete object and closing the array.
        if not raw.endswith("]"):
            last_close = raw.rfind("}")
            if last_close != -1:
                raw = raw[: last_close + 1] + "]"
                logger.warning(
                    "Claude response truncated — recovered %d/%d articles",
                    raw.count('"id"'), len(articles),
                )
            else:
                raise ValueError("No complete JSON object found in response")
        results = json.loads(raw)
        return {
            str(r["id"]): (r.get("sentiment", "neutral").lower(), float(r.get("confidence", 0.5)))
            for r in results
            if "id" in r
        }
    except Exception as exc:
        logger.warning("Batch sentiment classification failed: %s", exc)
        return {}


def fetch_all_news(lookback_minutes: int = 5) -> list[NewsItem]:
    """
    Fetch recent news from Benzinga, filter to eligible articles, classify
    sentiment in a single batched Claude call, and return positive-sentiment
    articles with US equity tickers.

    Filtering happens BEFORE the Claude call:
      - Must have at least one ticker
      - No ticker in the blocklist
      - (article_id, ticker) pair not already seen in the DB
    """
    articles = _fetch(lookback_minutes)
    if not articles:
        return []

    seen_ids: set[str] = set()

    # ── Step 1: filter articles before scoring ────────────────────────────────
    # Build a list of (article, eligible_tickers) for articles worth scoring.
    eligible: list[tuple[dict, list[str], str]] = []  # (article, tickers, article_id)

    for article in articles:
        article_id = str(article.get("benzinga_id") or article.get("url", article.get("title", "")))
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        raw_tickers = [t for t in (article.get("tickers") or []) if t]
        if not raw_tickers:
            continue

        # Skip stale articles — published more than 1 min before we fetched them.
        # We poll every minute, so anything older than 1 min was already seen
        # (or missed) in a prior cycle. Acting on old news risks buying after
        # the move has already happened and reversed.
        try:
            published_at = datetime.fromisoformat(
                article.get("published", "").replace("Z", "+00:00")
            )
            age_minutes = (datetime.now(timezone.utc) - published_at).total_seconds() / 60
            if age_minutes > 1:
                continue
        except (ValueError, AttributeError):
            pass

        # Build T212 tickers and filter blocklist + already-seen pairs
        eligible_tickers = [
            f"{t}_US_EQ"
            for t in raw_tickers
            if f"{t}_US_EQ" not in cfg.blocklist
            and not is_article_seen(article_id, f"{t}_US_EQ")
        ]
        if not eligible_tickers:
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

    # ── Step 3: build NewsItem list from positive results ─────────────────────
    results: list[NewsItem] = []

    for article, tickers, article_id in eligible:
        sentiment, confidence = scores.get(article_id, ("neutral", 0.0))
        if sentiment != "positive":
            continue

        headline = html.unescape(article.get("title", ""))
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
                sentiment=sentiment,
                confidence=confidence,
            ))

    logger.info(
        "Benzinga: %d article(s) fetched → %d eligible → %d positive ticker signal(s)",
        len(seen_ids), len(eligible), len(results),
    )
    return results
