"""
news/fetcher.py
───────────────
Fetches actionable trading signals from two Benzinga endpoints:

  1. WIIM (Why Is It Moving) — articles that explain a current price move.
     These are the highest-quality signals: Benzinga has already determined
     the stock is moving and why.

  2. General News — broad market news filtered to known tickers.
     Used as a secondary source for early signals before WIIM is published.

Both endpoints return XML. Tickers are extracted directly from the
<stocks> tags — no keyword matching needed.

Signals are filtered against the blocklist and deduplicated by article id.
"""

import logging
import requests
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from config.settings import cfg

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.benzinga.com/api/v2/news"
_TIMEOUT = 10


@dataclass
class NewsItem:
    article_id: str
    ticker: str
    headline: str
    body: str
    source: str
    published_at: datetime
    is_wiim: bool  # True = Why Is It Moving signal


def _parse_articles(xml_text: str, lookback_minutes: int) -> list[dict]:
    """Parse Benzinga XML response into a list of article dicts."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    articles = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.findall("item"):
            created_str = item.findtext("created", "")
            try:
                published_at = parsedate_to_datetime(created_str)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
            except Exception:
                published_at = datetime.now(timezone.utc)

            if published_at < cutoff:
                continue

            tickers = [
                s.findtext("name", "").strip()
                for s in item.findall("./stocks/item")
                if s.findtext("sector", "") == "Equity"
            ]
            channels = [c.findtext("name", "") for c in item.findall("./channels/item")]

            articles.append({
                "id": item.findtext("id", ""),
                "title": item.findtext("title", ""),
                "body": item.findtext("teaser", "") or item.findtext("body", ""),
                "tickers": tickers,
                "channels": channels,
                "published_at": published_at,
                "is_wiim": "WIIM" in channels,
            })
    except ET.ParseError as exc:
        logger.warning("Benzinga XML parse error: %s", exc)
    return articles


def _fetch(params: dict) -> str:
    """Make a single Benzinga API request and return raw XML."""
    params["token"] = cfg.benzinga_api_key
    params["displayOutput"] = "abstract"
    try:
        response = requests.get(_BASE_URL, params=params, timeout=_TIMEOUT)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.warning("Benzinga API request failed: %s", exc)
        return ""


def fetch_wiim(lookback_minutes: int = 5) -> list[NewsItem]:
    """Fetch Why Is It Moving articles — stocks already confirmed moving."""
    xml = _fetch({"channels": "WIIM", "pageSize": 100})
    items = []
    for article in _parse_articles(xml, lookback_minutes):
        for ticker in article["tickers"]:
            t212_ticker = f"{ticker}_US_EQ"
            if t212_ticker in cfg.blocklist:
                continue
            items.append(NewsItem(
                article_id=article["id"],
                ticker=t212_ticker,
                headline=article["title"],
                body=article["body"],
                source="Benzinga WIIM",
                published_at=article["published_at"],
                is_wiim=True,
            ))
    logger.info("Benzinga WIIM: %d signals", len(items))
    return items


def fetch_news(lookback_minutes: int = 5) -> list[NewsItem]:
    """Fetch general market news for early signals."""
    xml = _fetch({"pageSize": 100})
    items = []
    for article in _parse_articles(xml, lookback_minutes):
        for ticker in article["tickers"]:
            t212_ticker = f"{ticker}_US_EQ"
            if t212_ticker in cfg.blocklist:
                continue
            items.append(NewsItem(
                article_id=article["id"],
                ticker=t212_ticker,
                headline=article["title"],
                body=article["body"],
                source="Benzinga News",
                published_at=article["published_at"],
                is_wiim=False,
            ))
    logger.info("Benzinga News: %d articles matched", len(items))
    return items


def fetch_all_news(lookback_minutes: int = 5) -> list[NewsItem]:
    """
    Fetch from both WIIM and general news, deduplicate by headline,
    and prioritise WIIM signals (they appear first in results).
    """
    seen: set[str] = set()
    results: list[NewsItem] = []

    for item in fetch_wiim(lookback_minutes) + fetch_news(lookback_minutes):
        key = item.headline.strip().lower()
        if key not in seen:
            seen.add(key)
            results.append(item)

    wiim_count = sum(1 for i in results if i.is_wiim)
    logger.info(
        "Fetched %d unique news items (%d WIIM, %d general) across %d tickers",
        len(results), wiim_count, len(results) - wiim_count,
        len({i.ticker for i in results}),
    )
    return results
