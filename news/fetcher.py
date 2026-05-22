"""
news/fetcher.py
───────────────
Fetches real-time financial news from Benzinga (via massive.com) and
scores sentiment using Claude to identify bullish US equity signals.

Benzinga provides breaking news with ticker symbols but no built-in
sentiment — Claude classifies each article as bullish/bearish/neutral.
"""

import json
import logging
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import anthropic
from config.settings import cfg

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


def _score_sentiment(headline: str, teaser: str) -> tuple[str, float]:
    """
    Ask Claude to classify sentiment of a news article.
    Returns (sentiment, confidence) where sentiment is "positive"|"neutral"|"negative"
    and confidence is 0.0–1.0.
    """
    prompt = (
        "You are a financial news sentiment classifier. "
        "Classify the sentiment of this news article for US equity traders. "
        "Respond with a JSON object only — no markdown, no explanation:\n"
        '{"sentiment": "positive" | "neutral" | "negative", "confidence": 0.0-1.0}\n\n'
        f"Headline: {headline}\n"
        f"Summary: {teaser}"
    )
    try:
        msg = _claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        sentiment = result.get("sentiment", "neutral").lower()
        confidence = float(result.get("confidence", 0.5))
        return sentiment, confidence
    except Exception as exc:
        logger.warning("Sentiment classification failed: %s", exc)
        return "neutral", 0.0


def fetch_all_news(lookback_minutes: int = 5) -> list[NewsItem]:
    """
    Fetch recent news from Benzinga, classify sentiment with Claude,
    and return positive-sentiment articles with US equity tickers.
    Deduplicates by article id.
    """
    articles = _fetch(lookback_minutes)
    if not articles:
        return []

    seen_ids: set[str] = set()
    results: list[NewsItem] = []

    for article in articles:
        article_id = str(article.get("benzinga_id") or article.get("url", article.get("title", "")))
        if article_id in seen_ids:
            continue
        seen_ids.add(article_id)

        tickers = [t for t in (article.get("tickers") or []) if t]
        if not tickers:
            continue

        headline = article.get("title", "")
        teaser = article.get("teaser") or article.get("body", "")[:200]

        sentiment, confidence = _score_sentiment(headline, teaser)
        if sentiment != "positive":
            continue

        try:
            published_at = datetime.fromisoformat(
                article.get("published", "").replace("Z", "+00:00")
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
                headline=headline,
                body=teaser,
                source="benzinga",
                published_at=published_at,
                sentiment=sentiment,
                confidence=confidence,
            ))

    logger.info(
        "Benzinga: %d article(s) fetched → %d positive ticker signal(s)",
        len(seen_ids), len(results),
    )
    return results
