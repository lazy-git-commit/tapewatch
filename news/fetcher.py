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

import json
import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import anthropic
from config.settings import cfg
from storage.database import is_article_seen

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
        "You are a financial news sentiment classifier for US equity traders.\n"
        "Classify the sentiment of each article below.\n"
        "Respond with a JSON array only — no markdown, no explanation.\n"
        "Each element must have exactly these keys: id, sentiment, confidence.\n"
        'sentiment must be one of: "positive", "neutral", "negative"\n'
        "confidence must be a float 0.0–1.0\n\n"
        f"Articles:\n{items_json}"
    )
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        results = json.loads(raw.strip())
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
            "headline": article.get("title", ""),
            "teaser": article.get("teaser") or article.get("body", "")[:200],
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

        headline = article.get("title", "")
        teaser = article.get("teaser") or article.get("body", "")[:200]

        try:
            published_at = datetime.fromisoformat(
                article.get("published", "").replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            published_at = datetime.now(timezone.utc)

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
