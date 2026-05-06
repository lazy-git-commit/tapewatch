"""
market/price_check.py
──────────────────────
Fetches live price data via yfinance and checks whether a signal is confirmed
by actual price movement and elevated volume.

A signal is confirmed when:
  1. The current price is >= cfg.min_price_move_pct above the day's open
  2. Current volume is > 1.5× the 20-day average volume (volume confirmation)
"""

import logging
import yfinance as yf
from dataclasses import dataclass
from datetime import datetime, timezone
from config.settings import cfg

logger = logging.getLogger(__name__)


def is_market_open() -> bool:
    """Return True if the US stock market is currently open (Mon–Fri, 14:30–21:00 UTC)."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now.replace(hour=14, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=21, minute=0, second=0, microsecond=0)
    return market_open <= now < market_close

# yfinance logs its own fetch errors at ERROR level — suppress them since
# we handle missing data ourselves with return None / our own warnings
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Trading 212 instrument codes use suffixes — map to yfinance tickers
# yfinance uses standard Yahoo Finance tickers (e.g. AAPL, TSLA)
def _to_yf_ticker(t212_ticker: str) -> str:
    """Strip Trading 212 suffix to get a yfinance-compatible ticker."""
    # e.g. "AAPL_US_EQ" → "AAPL"
    return t212_ticker.split("_")[0]


@dataclass
class PriceConfirmation:
    ticker: str
    yf_ticker: str
    current_price: float
    open_price: float
    price_move_pct: float
    current_volume: int
    avg_volume: int
    volume_ratio: float
    is_confirmed: bool
    reason: str         # human-readable explanation


def confirm_price_signal(t212_ticker: str) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing the kind of price movement that
    corroborates a bullish news signal.

    Returns None if data cannot be fetched.
    """
    yf_ticker = _to_yf_ticker(t212_ticker)

    try:
        stock = yf.Ticker(yf_ticker)

        # 1-day intraday data at 5-minute resolution
        intraday = stock.history(period="1d", interval="5m")
        if intraday.empty:
            logger.warning("No intraday data for %s", yf_ticker)
            return None

        open_price = float(intraday["Open"].iloc[0])
        current_price = float(intraday["Close"].iloc[-1])
        price_move_pct = ((current_price - open_price) / open_price) * 100

        # Volume: compare today's cumulative volume to 20-day daily average
        daily = stock.history(period="21d", interval="1d")
        if len(daily) >= 2:
            avg_volume = int(daily["Volume"].iloc[:-1].mean())  # exclude today
        else:
            avg_volume = 0

        current_volume = int(intraday["Volume"].sum())
        volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0

        # Confirmation criteria
        price_ok = price_move_pct >= cfg.min_price_move_pct
        volume_ok = volume_ratio >= 1.5

        if price_ok and volume_ok:
            is_confirmed = True
            reason = (
                f"Price +{price_move_pct:.2f}% from open "
                f"(threshold: +{cfg.min_price_move_pct}%) "
                f"with {volume_ratio:.1f}× average volume"
            )
        elif price_ok:
            # Accept on price alone but warn — volume is a weaker signal
            is_confirmed = True
            reason = (
                f"Price +{price_move_pct:.2f}% from open but low volume "
                f"({volume_ratio:.1f}× avg) — weak confirmation"
            )
        else:
            is_confirmed = False
            reason = (
                f"Price only +{price_move_pct:.2f}% from open "
                f"(threshold: +{cfg.min_price_move_pct}%) — not confirmed"
            )

        result = PriceConfirmation(
            ticker=t212_ticker,
            yf_ticker=yf_ticker,
            current_price=current_price,
            open_price=open_price,
            price_move_pct=price_move_pct,
            current_volume=current_volume,
            avg_volume=avg_volume,
            volume_ratio=volume_ratio,
            is_confirmed=is_confirmed,
            reason=reason,
        )

        logger.info(
            "Price check [%s]: %.2f%% move, %.1f× volume — confirmed=%s",
            yf_ticker, price_move_pct, volume_ratio, is_confirmed,
        )
        return result

    except Exception as exc:
        logger.error("Price check failed for %s: %s", yf_ticker, exc)
        return None


def get_current_price(t212_ticker: str) -> float | None:
    """Fast lookup of the latest price for an open position monitor."""
    yf_ticker = _to_yf_ticker(t212_ticker)
    try:
        ticker = yf.Ticker(yf_ticker)
        data = ticker.history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as exc:
        logger.error("get_current_price failed for %s: %s", yf_ticker, exc)
        return None
