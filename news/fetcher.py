"""
news/fetcher.py
───────────────
Fetches financial news from:
  1. NewsAPI  — headline + description for each watchlist ticker
  2. RSS feeds — Reuters Business, BBC Business (free, no key required)

Returns a list of NewsItem dataclasses.
"""

import logging
import feedparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from newsapi import NewsApiClient
from config.settings import cfg

logger = logging.getLogger(__name__)

# RSS feeds to monitor — add or remove as you like
RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "http://feeds.bbci.co.uk/news/business/rss.xml",
    "https://finance.yahoo.com/news/rssindex",
]

# Map ticker → company name for keyword matching in RSS
TICKER_COMPANY_MAP = {
    "AAPL_US_EQ": ["Apple", "AAPL"],
    "TSLA_US_EQ": ["Tesla", "TSLA"],
    "AMZN_US_EQ": ["Amazon", "AMZN"],
    "MSFT_US_EQ": ["Microsoft", "MSFT"],
    "GOOGL_US_EQ": ["Google", "Alphabet", "GOOGL"],
    "META_US_EQ": ["Meta", "Facebook", "META"],
    "NVDA_US_EQ": ["Nvidia", "NVDA"],
    # Add tickers from your WATCHLIST here
}


@dataclass
class NewsItem:
    ticker: str
    headline: str
    body: str           # description / summary
    source: str
    published_at: datetime


def _company_names(ticker: str) -> list[str]:
    """Return company name keywords for a given ticker."""
    return TICKER_COMPANY_MAP.get(ticker, [ticker.replace("_US_EQ", "")])


def fetch_newsapi(ticker: str, lookback_hours: int = 1) -> list[NewsItem]:
    """Fetch recent articles from NewsAPI for one ticker."""
    if not cfg.newsapi_key:
        return []

    client = NewsApiClient(api_key=cfg.newsapi_key)
    query = " OR ".join(f'"{name}"' for name in _company_names(ticker))
    from_dt = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    try:
        response = client.get_everything(
            q=query,
            from_param=from_dt,
            language="en",
            sort_by="publishedAt",
            page_size=10,
        )
        items = []
        for article in response.get("articles", []):
            items.append(
                NewsItem(
                    ticker=ticker,
                    headline=article.get("title", ""),
                    body=article.get("description", ""),
                    source=article.get("source", {}).get("name", "newsapi"),
                    published_at=datetime.fromisoformat(
                        article["publishedAt"].replace("Z", "+00:00")
                    ),
                )
            )
        logger.debug("NewsAPI: %d articles for %s", len(items), ticker)
        return items
    except Exception as exc:
        logger.warning("NewsAPI fetch failed for %s: %s", ticker, exc)
        return []


def fetch_rss(ticker: str, lookback_hours: int = 1) -> list[NewsItem]:
    """Scan RSS feeds for articles mentioning the given ticker's company names."""
    keywords = [kw.lower() for kw in _company_names(ticker)]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items = []

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                title = entry.get("title", "")
                summary = entry.get("summary", "")
                combined = (title + " " + summary).lower()

                if not any(kw in combined for kw in keywords):
                    continue

                # Parse publication date
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

    logger.debug("RSS: %d articles for %s", len(items), ticker)
    return items


def fetch_all_news(tickers: list[str], lookback_hours: int = 1) -> list[NewsItem]:
    """
    Fetch news from all sources for every ticker in the watchlist.
    Deduplicates by headline.
    """
    seen: set[str] = set()
    results: list[NewsItem] = []

    for ticker in tickers:
        for item in fetch_newsapi(ticker, lookback_hours) + fetch_rss(ticker, lookback_hours):
            key = item.headline.strip().lower()
            if key not in seen:
                seen.add(key)
                results.append(item)

    logger.info("Fetched %d unique news items for %d tickers", len(results), len(tickers))
    return results
