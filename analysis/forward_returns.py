"""
analysis/forward_returns.py
────────────────────────────
The feedback loop for the Claude news classifier.

Every classification (positive / neutral / negative) is stored in the
sentiment_scores table at scoring time. This job runs nightly, after the
close, and fills in what the market actually did in the 5 / 15 / 60 minutes
after each article published — using free yfinance data, since this is
retrospective analysis (no Twelvedata credits, no real-time requirement).

Why this matters more than any individual filter tweak:
  Without measured forward returns there is no way to know whether a prompt
  change improved or degraded the classifier — "positive" articles that go
  nowhere and "neutral" articles that ripped both stay invisible. With this
  table populated you can answer, with SQL:

    -- Classifier precision: how often do positives actually move?
    SELECT sentiment,
           COUNT(*)                              AS n,
           AVG(fwd_return_15m)                   AS avg_15m,
           AVG((fwd_return_15m > 2)::int) * 100  AS pct_moved_2pct
    FROM sentiment_scores
    WHERE returns_computed_at IS NOT NULL
    GROUP BY sentiment;

    -- Which catalyst classes actually pay?
    SELECT catalyst_type, COUNT(*) AS n, AVG(fwd_return_15m) AS avg_15m
    FROM sentiment_scores
    WHERE sentiment = 'positive' AND returns_computed_at IS NOT NULL
    GROUP BY catalyst_type ORDER BY avg_15m DESC;

  Run those after every prompt change — that's the eval.

Scheduling: main.py runs this daily at 22:30 UTC (after the US close in both
winter and summer). It can also be run manually:
  python -m analysis.forward_returns
"""

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from storage.database import get_scores_missing_returns, update_forward_returns

logger = logging.getLogger(__name__)

# Per-ticker-day intraday cache so N articles on the same stock cost one fetch.
_bars_cache: dict[str, pd.DataFrame | None] = {}


def _get_intraday_bars(symbol: str, day: datetime) -> pd.DataFrame | None:
    """1-min bars for one ticker-day via yfinance (UTC index), cached."""
    key = f"{symbol}_{day.strftime('%Y-%m-%d')}"
    if key in _bars_cache:
        return _bars_cache[key]
    try:
        df = yf.Ticker(symbol).history(
            start=day.strftime("%Y-%m-%d"),
            end=(day + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
        )
        if df.empty:
            _bars_cache[key] = None
            return None
        df.index = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
        _bars_cache[key] = df
        return df
    except Exception as exc:
        logger.debug("yfinance fetch failed for %s %s: %s", symbol, day.date(), exc)
        _bars_cache[key] = None
        return None


def _price_at_or_after(bars: pd.DataFrame, ts: datetime) -> float | None:
    """First bar close at/after ts — the realistic 'price when news landed'."""
    after = bars[bars.index >= ts]
    return float(after["Close"].iloc[0]) if not after.empty else None


def _forward_return(bars: pd.DataFrame, ts: datetime, minutes: int) -> float | None:
    """% return from the first bar at/after ts to the bar ~minutes later."""
    p0 = _price_at_or_after(bars, ts)
    p1 = _price_at_or_after(bars, ts + timedelta(minutes=minutes))
    if p0 is None or p1 is None or p0 <= 0:
        return None
    return (p1 - p0) / p0 * 100


def compute_forward_returns(batch_limit: int = 500) -> int:
    """
    Fill forward returns for all scored articles that don't have them yet.
    Returns the number of rows updated. Articles whose price data isn't
    available (delisted, OTC, too recent for yfinance) are marked computed
    with NULL returns so they aren't retried forever.
    """
    rows = get_scores_missing_returns(limit=batch_limit)
    if not rows:
        logger.info("Forward returns: nothing to compute")
        return 0

    updated = 0
    for row in rows:
        symbol = str(row["ticker"]).split("_")[0]  # AAPL_US_EQ → AAPL
        try:
            published = datetime.fromisoformat(str(row["published_at"]).replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            published = published.astimezone(timezone.utc)
        except (ValueError, TypeError):
            # Unparseable timestamp — mark computed (NULL) and move on.
            update_forward_returns(row["id"], None, None, None)
            updated += 1
            continue

        # Skip articles published less than ~65 min ago — the 60-min window
        # hasn't finished printing yet; leave for tomorrow's run.
        if (datetime.now(timezone.utc) - published).total_seconds() < 65 * 60:
            continue

        bars = _get_intraday_bars(symbol, published.replace(hour=0, minute=0, second=0, microsecond=0))
        if bars is None:
            update_forward_returns(row["id"], None, None, None)
            updated += 1
            continue

        r5 = _forward_return(bars, published, 5)
        r15 = _forward_return(bars, published, 15)
        r60 = _forward_return(bars, published, 60)
        update_forward_returns(row["id"], r5, r15, r60)
        updated += 1

    logger.info("Forward returns: computed %d/%d pending rows", updated, len(rows))
    return updated


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    n = compute_forward_returns()
    print(f"Updated {n} rows.")
