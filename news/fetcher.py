"""
news/fetcher.py
───────────────
Fetches broad financial news from:
  1. NewsAPI  — top business headlines (no ticker filter)
  2. RSS feeds — Reuters Business, BBC Business, Yahoo Finance

Extracts a ticker from each article by matching company name keywords,
then drops any ticker on the blocklist.

Returns a list of NewsItem dataclasses.
"""

import logging
import socket
import feedparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from newsapi import NewsApiClient
from config.settings import cfg

_TIMEOUT = 10  # seconds for all outbound network calls

logger = logging.getLogger(__name__)

# RSS feeds to monitor — add or remove as you like
RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://finance.yahoo.com/news/rssindex",
]

# Company name keywords → ticker.  Add rows to expand coverage.
COMPANY_TICKER_MAP: dict[str, str] = {
    "Apple":     "AAPL_US_EQ",
    "AAPL":      "AAPL_US_EQ",
    "Tesla":     "TSLA_US_EQ",
    "TSLA":      "TSLA_US_EQ",
    "Amazon":    "AMZN_US_EQ",
    "AMZN":      "AMZN_US_EQ",
    "Microsoft": "MSFT_US_EQ",
    "MSFT":      "MSFT_US_EQ",
    "Google":    "GOOGL_US_EQ",
    "Alphabet":  "GOOGL_US_EQ",
    "GOOGL":     "GOOGL_US_EQ",
    "Meta":      "META_US_EQ",
    "Facebook":  "META_US_EQ",
    "META":      "META_US_EQ",
    "Nvidia":    "NVDA_US_EQ",
    "NVDA":      "NVDA_US_EQ",
    "Netflix":   "NFLX_US_EQ",
    "NFLX":      "NFLX_US_EQ",
    "AMD":       "AMD_US_EQ",
    "Intel":     "INTC_US_EQ",
    "INTC":      "INTC_US_EQ",
    "Salesforce": "CRM_US_EQ",
    "CRM":       "CRM_US_EQ",
    "Uber":      "UBER_US_EQ",
    "UBER":      "UBER_US_EQ",
    "Spotify":   "SPOT_US_EQ",
    "SPOT":      "SPOT_US_EQ",
    "PayPal":    "PYPL_US_EQ",
    "PYPL":      "PYPL_US_EQ",
    "Shopify":   "SHOP_US_EQ",
    "SHOP":      "SHOP_US_EQ",
    "Disney":    "DIS_US_EQ",
    "DIS":       "DIS_US_EQ",
    "Boeing":    "BA_US_EQ",
    "JPMorgan":  "JPM_US_EQ",
    "JPM":       "JPM_US_EQ",
    "Goldman":   "GS_US_EQ",
    "Pfizer":    "PFE_US_EQ",
    "PFE":       "PFE_US_EQ",
    "Johnson":   "JNJ_US_EQ",
    "JNJ":       "JNJ_US_EQ",
    "Exxon":     "XOM_US_EQ",
    "XOM":       "XOM_US_EQ",
}


@dataclass
class NewsItem:
    ticker: str
    headline: str
    body: str           # description / summary
    source: str
    published_at: datetime


def _extract_ticker(text: str) -> str | None:
    """
    Scan text for known company keywords and return the matching ticker.
    Returns the first match, or None if no known company is mentioned.
    """
    lower = text.lower()
    for keyword, ticker in COMPANY_TICKER_MAP.items():
        if keyword.lower() in lower:
            return ticker
    return None


def _is_blocked(ticker: str) -> bool:
    return ticker in cfg.blocklist


def fetch_newsapi(lookback_hours: int = 1) -> list[NewsItem]:
    """Fetch recent top business headlines from NewsAPI."""
    if not cfg.newsapi_key:
        return []

    client = NewsApiClient(api_key=cfg.newsapi_key)
    from_dt = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    try:
        response = client.get_top_headlines(
            category="business",
            language="en",
            page_size=100,
            timeout=_TIMEOUT,
        )
        items = []
        for article in response.get("articles", []):
            headline = article.get("title", "")
            body = article.get("description", "") or ""
            ticker = _extract_ticker(headline + " " + body)
            if ticker is None or _is_blocked(ticker):
                continue
            try:
                published_at = datetime.fromisoformat(
                    article["publishedAt"].replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                published_at = datetime.now(timezone.utc)
            if published_at < datetime.now(timezone.utc) - timedelta(hours=lookback_hours):
                continue
            items.append(
                NewsItem(
                    ticker=ticker,
                    headline=headline,
                    body=body,
                    source=article.get("source", {}).get("name", "newsapi"),
                    published_at=published_at,
                )
            )
        logger.info("NewsAPI: %d total articles, %d matched known tickers", len(response.get("articles", [])), len(items))
        return items
    except Exception as exc:
        logger.warning("NewsAPI fetch failed: %s", exc)
        return []


def fetch_rss(lookback_hours: int = 1) -> list[NewsItem]:
    """Scan RSS feeds for articles mentioning any known company."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items = []

    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_TIMEOUT)
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                ticker = _extract_ticker(title + " " + summary)
                if ticker is None or _is_blocked(ticker):
                    continue

                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                if published and published < cutoff:
                    continue

                items.append(
                    NewsItem(
                        ticker=ticker,
                        headline=title,
                        body=summary,
                        source=feed.feed.get("title", feed_url),
                        published_at=published or datetime.now(timezone.utc),
                    )
                )
        except Exception as exc:
            logger.warning("RSS fetch failed (%s): %s", feed_url, exc)

    socket.setdefaulttimeout(old_timeout)
    logger.info("RSS: %d articles matched known tickers", len(items))
    return items


def fetch_all_news(lookback_hours: int = 1) -> list[NewsItem]:
    """
    Fetch broad market news from all sources, extract tickers by company
    name matching, drop blocklisted tickers, and deduplicate by headline.
    """
    seen: set[str] = set()
    results: list[NewsItem] = []

    for item in fetch_newsapi(lookback_hours) + fetch_rss(lookback_hours):
        key = item.headline.strip().lower()
        if key not in seen:
            seen.add(key)
            results.append(item)

    logger.info(
        "Fetched %d unique news items across %d tickers (blocklist: %s)",
        len(results),
        len({i.ticker for i in results}),
        cfg.blocklist or "none",
    )
    return results
