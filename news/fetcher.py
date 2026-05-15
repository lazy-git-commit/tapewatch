"""
news/fetcher.py
───────────────
Fetches real-time financial news from finlight.me and filters to
positive/bullish signals for US equities.

Finlight provides built-in sentiment (positive/neutral/negative) and a
confidence score (0–1), so no separate Claude sentiment call is needed.

Rate limit: 10,000 requests/month on the current plan. The monthly
request count is tracked in the DB; fetching is halted gracefully if
the limit is reached to avoid a hard API error.
"""

import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from config.settings import cfg
from storage.database import get_api_request_count, increment_api_request_count

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.finlight.me/v2/articles"
_TIMEOUT = 10
_MONTHLY_LIMIT = 10_000


@dataclass
class NewsItem:
    article_id: str
    ticker: str
    headline: str
    body: str
    source: str
    published_at: datetime
    sentiment: str    # "positive" | "neutral" | "negative"
    confidence: float  # 0.0–1.0 as returned by finlight


def _fetch(lookback_minutes: int) -> list[dict]:
    """POST to finlight.me and return raw article dicts."""
    since = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    payload = {
        "from": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "language": "en",
        "countries": ["US"],
        "pageSize": 100,
    }
    try:
        resp = requests.post(
            _BASE_URL,
            json=payload,
            headers={"X-API-KEY": cfg.finlight_api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.error("Finlight rate limit hit (429) — monthly quota exhausted")
            return []
        resp.raise_for_status()
        increment_api_request_count()
        return resp.json().get("articles", [])
    except requests.RequestException as exc:
        logger.warning("Finlight API request failed: %s", exc)
        return []


def fetch_all_news(lookback_minutes: int = 5) -> list["NewsItem"]:
    """
    Fetch recent US equity news from finlight.me.
    Returns only positive-sentiment articles with tickers, deduplicated by article id.
    Stops fetching if the monthly request limit is reached.
    """
    used = get_api_request_count()
    if used >= _MONTHLY_LIMIT:
        logger.error(
            "Finlight monthly limit reached (%d/%d) — skipping fetch until next month",
            used, _MONTHLY_LIMIT,
        )
        return []

    remaining = _MONTHLY_LIMIT - used
    logger.info("Finlight requests used this month: %d/%d", used, _MONTHLY_LIMIT)
    if remaining <= 50:
        logger.warning("Finlight quota nearly exhausted — %d requests remaining", remaining)

    articles = _fetch(lookback_minutes)
    if not articles:
        return []

    seen_ids: set[str] = set()
    results: list[NewsItem] = []

    for article in articles:
        sentiment = article.get("sentiment", "neutral").lower()
        if sentiment != "positive":
            continue

        companies = article.get("companies", [])
        tickers = [
            c["ticker"]
            for c in companies
            if c.get("ticker") and c.get("country") == "US"
        ]
        if not tickers:
            continue

        article_id = article.get("link", article.get("title", ""))
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        try:
            published_at = datetime.fromisoformat(
                article.get("publishDate", "").replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            published_at = datetime.now(timezone.utc)

        for ticker in tickers:
            t212_ticker = f"{ticker}_US_EQ"
            if t212_ticker in cfg.blocklist:
                continue
            results.append(NewsItem(
                article_id=article_id,
                ticker=t212_ticker,
                headline=article.get("title", ""),
                body=article.get("summary") or "",
                source=article.get("source", "finlight"),
                published_at=published_at,
                sentiment=sentiment,
                confidence=float(article.get("confidence", 0.0)),
            ))

    logger.info(
        "Finlight: %d positive article(s) → %d ticker signal(s)",
        len(seen_ids), len(results),
    )
    return results
