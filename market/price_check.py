"""
market/price_check.py
──────────────────────
Fetches live price data via yfinance and checks whether a signal is confirmed
by actual price movement and elevated volume.

A signal is confirmed when ALL of the following hold:
  1. Recent momentum  — price is up >= cfg.min_price_move_pct over the last
                        cfg.momentum_window_minutes minutes
  2. Volume spike     — today's cumulative volume > 1.5× the 20-day average
  3. No dead-cat bounce — stock is not down more than cfg.max_day_drop_pct
                          from today's open (guards against buying a brief
                          bounce inside a larger intraday sell-off)
"""

import logging
from datetime import datetime, timezone, timedelta
import yfinance as yf
from dataclasses import dataclass
from config.settings import cfg

logger = logging.getLogger(__name__)

# Suppress yfinance's own error logs — we handle missing data ourselves
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _to_yf_ticker(t212_ticker: str) -> str:
    """Strip Trading 212 suffix to get a yfinance-compatible ticker."""
    return t212_ticker.split("_")[0]


def is_market_open() -> bool:
    """
    Check whether the US market is open by fetching 1 minute of live SPY
    data. If yfinance returns rows with a timestamp from the last 5 minutes,
    the market is open. This avoids relying on .info/.fast_info field names
    which vary across yfinance versions.
    """
    try:
        data = yf.Ticker("SPY").history(period="1d", interval="1m")
        if data.empty:
            return False
        last_ts = data.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        else:
            last_ts = last_ts.astimezone(timezone.utc)
        age = datetime.now(timezone.utc) - last_ts
        return age < timedelta(minutes=5)
    except Exception:
        return False


@dataclass
class PriceConfirmation:
    ticker: str
    yf_ticker: str
    current_price: float
    open_price: float
    day_move_pct: float       # price vs today's open (used for dead-cat guard)
    recent_move_pct: float    # price vs cfg.momentum_window_minutes ago
    current_volume: int
    avg_volume: int
    volume_ratio: float
    is_confirmed: bool
    reason: str


def confirm_price_signal(t212_ticker: str) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing active upward momentum that
    corroborates a bullish news signal.

    Returns None if data cannot be fetched.
    """
    yf_ticker = _to_yf_ticker(t212_ticker)

    try:
        stock = yf.Ticker(yf_ticker)

        intraday = stock.history(period="1d", interval="5m")
        if intraday.empty:
            logger.warning(
                "No intraday data for %s — market may be closed or ticker delisted",
                yf_ticker,
            )
            return None

        current_price = float(intraday["Close"].iloc[-1])
        open_price = float(intraday["Open"].iloc[0])
        day_move_pct = ((current_price - open_price) / open_price) * 100

        # Recent momentum: find the bar closest to momentum_window_minutes ago
        window = timedelta(minutes=cfg.momentum_window_minutes)
        now_ts = intraday.index[-1]
        cutoff_ts = now_ts - window
        past_bars = intraday[intraday.index <= cutoff_ts]
        if past_bars.empty:
            # Market just opened — fewer bars than the window; use open price
            past_price = open_price
        else:
            past_price = float(past_bars["Close"].iloc[-1])
        recent_move_pct = ((current_price - past_price) / past_price) * 100

        # Volume: compare today's cumulative volume to 20-day daily average
        daily = stock.history(period="21d", interval="1d")
        avg_volume = int(daily["Volume"].iloc[:-1].mean()) if len(daily) >= 2 else 0
        current_volume = int(intraday["Volume"].sum())
        volume_ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0

        # ── Evaluate conditions ───────────────────────────────────────────────
        momentum_ok = recent_move_pct >= cfg.min_price_move_pct
        volume_ok = volume_ratio >= 1.5
        dead_cat = day_move_pct < -cfg.max_day_drop_pct

        if dead_cat:
            is_confirmed = False
            reason = (
                f"Dead-cat bounce guard: stock is down {day_move_pct:.2f}% on the day "
                f"(max allowed drop: -{cfg.max_day_drop_pct}%) — skipping"
            )
        elif not momentum_ok:
            is_confirmed = False
            reason = (
                f"Insufficient recent momentum: +{recent_move_pct:.2f}% "
                f"over last {cfg.momentum_window_minutes} min "
                f"(threshold: +{cfg.min_price_move_pct}%)"
            )
        elif momentum_ok and volume_ok:
            is_confirmed = True
            reason = (
                f"+{recent_move_pct:.2f}% in last {cfg.momentum_window_minutes} min "
                f"with {volume_ratio:.1f}× average volume "
                f"(day: {day_move_pct:+.2f}%)"
            )
        else:
            # Momentum present but volume weak — still confirm, flag as weak
            is_confirmed = True
            reason = (
                f"+{recent_move_pct:.2f}% in last {cfg.momentum_window_minutes} min "
                f"but low volume ({volume_ratio:.1f}× avg) — weak confirmation "
                f"(day: {day_move_pct:+.2f}%)"
            )

        logger.info(
            "Price check [%s]: recent=%+.2f%% day=%+.2f%% volume=%.1f× — %s",
            yf_ticker, recent_move_pct, day_move_pct, volume_ratio,
            "approved" if is_confirmed else "rejected",
        )
        return PriceConfirmation(
            ticker=t212_ticker,
            yf_ticker=yf_ticker,
            current_price=current_price,
            open_price=open_price,
            day_move_pct=day_move_pct,
            recent_move_pct=recent_move_pct,
            current_volume=current_volume,
            avg_volume=avg_volume,
            volume_ratio=volume_ratio,
            is_confirmed=is_confirmed,
            reason=reason,
        )

    except Exception as exc:
        logger.error("Price check failed for %s: %s", yf_ticker, exc)
        return None


def get_current_price(t212_ticker: str) -> float | None:
    """Fast lookup of the latest price for an open position monitor."""
    yf_ticker = _to_yf_ticker(t212_ticker)
    try:
        data = yf.Ticker(yf_ticker).history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as exc:
        logger.error("get_current_price failed for %s: %s", yf_ticker, exc)
        return None
