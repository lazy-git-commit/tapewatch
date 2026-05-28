"""
market/price_check.py
──────────────────────
Fetches live price data and checks whether a signal is confirmed by actual
price movement and elevated volume.

Price sources:
  - Current price  — Finnhub REST quote (real-time, <1s latency)
  - Momentum baseline — yfinance 1-min bars (15-min delayed, intentional: aligns
                        with MOMENTUM_WINDOW_MINUTES=15 so the last bar ≈ now-15min)
  - Volume stats   — yfinance 20-day daily history

A signal is confirmed when ALL of the following hold:
  1. Recent momentum  — price is up >= cfg.min_price_move_pct over the last
                        cfg.momentum_window_minutes minutes
  2. Volume spike     — today's cumulative volume > 1.5× the 20-day average
  3. No dead-cat bounce — stock is not down more than cfg.max_day_drop_pct
                          from today's open (guards against buying a brief
                          bounce inside a larger intraday sell-off)
"""

import logging
import requests
from datetime import datetime, timezone, timedelta
import yfinance as yf
from dataclasses import dataclass
import pytz
from config.settings import cfg
from market.finnhub_bars import get_finnhub_quote

logger = logging.getLogger(__name__)

# Suppress yfinance's own error logs — we handle missing data ourselves
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


_ET = pytz.timezone("America/New_York")
_MARKET_OPEN = (9, 30)   # 09:30 ET
_MARKET_CLOSE = (16, 0)  # 16:00 ET


def next_market_open() -> datetime:
    """
    Return the next NYSE market open as a UTC-aware datetime.
    If called during market hours, returns today's open (already passed).
    Skips weekends.
    """
    now_et = datetime.now(_ET)
    candidate = now_et.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)

    # If today's open is still in the future, that's our answer
    if candidate > now_et and now_et.weekday() < 5:
        return candidate.astimezone(timezone.utc)

    # Otherwise advance to the next weekday
    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)


def _to_yf_ticker(t212_ticker: str) -> str:
    """Strip Trading 212 suffix to get a yfinance-compatible ticker."""
    return t212_ticker.split("_")[0]


def is_too_late_to_buy() -> bool:
    """
    Return True if we are within TIME_STOP_MINUTES of market close.
    Returns False outside of market hours — is_market_open() handles that.
    """
    now_et = datetime.now(_ET)
    close_et = now_et.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
    minutes_to_close = (close_et - now_et).total_seconds() / 60
    return 0 < minutes_to_close <= cfg.time_stop_minutes


def is_market_open() -> bool:
    """
    Check whether the US market is open using the Finnhub market-status API.
    This is authoritative — it handles holidays, early closes, and weekends.
    Falls back to False on any error so we don't trade on uncertainty.
    """
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/market-status",
            params={"exchange": "US", "token": cfg.finnhub_api_key},
            timeout=5,
        )
        resp.raise_for_status()
        return bool(resp.json().get("isOpen", False))
    except Exception as exc:
        logger.warning("Finnhub market-status check failed: %s", exc)
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
    reason_code: str          # short keyword: approved | low_momentum | low_volume | dead_cat | no_price_data


def confirm_price_signal(t212_ticker: str) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing active upward momentum that
    corroborates a bullish news signal.

    Current price — Finnhub REST quote (real-time).
    Momentum baseline — yfinance intraday bar (15-min delayed, which aligns
      with the 15-min window: the most recent yfinance bar is approximately
      the price from cfg.momentum_window_minutes ago).
    Volume — yfinance intraday cumulative (delayed but fine for ratio checks).

    Returns None if data cannot be fetched.
    """
    yf_ticker = _to_yf_ticker(t212_ticker)

    try:
        # ── Current price via Finnhub REST (real-time) ────────────────────────
        quote = get_finnhub_quote(yf_ticker)
        if quote is None:
            logger.warning("No Finnhub quote for %s — skipping", yf_ticker)
            return None

        current_price = float(quote["c"])
        open_price = float(quote["o"]) if quote.get("o") else current_price
        day_move_pct = ((current_price - open_price) / open_price) * 100 if open_price else 0.0

        # ── Momentum baseline via yfinance intraday bars ──────────────────────
        # yfinance data is ~15 min delayed. With momentum_window_minutes=15, the
        # most recent available bar is our target baseline — this is intentional.
        stock = yf.Ticker(yf_ticker)
        intraday = stock.history(period="1d", interval="1m")
        if intraday.empty:
            logger.warning("No intraday data for %s — market may be closed or ticker delisted", yf_ticker)
            return None

        # The last yfinance bar is ~15 min ago; use it as the momentum baseline
        past_price = float(intraday["Close"].iloc[-1])
        recent_move_pct = ((current_price - past_price) / past_price) * 100 if past_price else 0.0

        # ── Volume: 20-day daily average via yfinance ─────────────────────────
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
            reason_code = "dead_cat"
            reason = (
                f"Dead-cat bounce guard: stock is down {day_move_pct:.2f}% on the day "
                f"(max allowed drop: -{cfg.max_day_drop_pct}%) — skipping"
            )
        elif not momentum_ok:
            is_confirmed = False
            reason_code = "low_momentum"
            reason = (
                f"Insufficient recent momentum: {recent_move_pct:+.2f}% "
                f"over last {cfg.momentum_window_minutes} min "
                f"(threshold: +{cfg.min_price_move_pct}%)"
            )
        elif volume_ok:
            is_confirmed = True
            reason_code = "approved"
            reason = (
                f"+{recent_move_pct:.2f}% in last {cfg.momentum_window_minutes} min "
                f"with {volume_ratio:.1f}× average volume "
                f"(day: {day_move_pct:+.2f}%)"
            )
        else:
            is_confirmed = False
            reason_code = "low_volume"
            reason = (
                f"+{recent_move_pct:.2f}% in last {cfg.momentum_window_minutes} min "
                f"but low volume ({volume_ratio:.1f}× avg, threshold 1.5×) — rejected"
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
            reason_code=reason_code,
        )

    except Exception as exc:
        logger.error("Price check failed for %s: %s", yf_ticker, exc)
        return None


def get_current_price(t212_ticker: str) -> float | None:
    """Fast lookup of the latest price for an open position monitor."""
    yf_ticker = _to_yf_ticker(t212_ticker)
    quote = get_finnhub_quote(yf_ticker)
    if quote is not None:
        return float(quote["c"])
    # Fallback to yfinance if Finnhub unavailable
    try:
        data = yf.Ticker(yf_ticker).history(period="1d", interval="1m")
        if data.empty:
            return None
        return float(data["Close"].iloc[-1])
    except Exception as exc:
        logger.error("get_current_price failed for %s: %s", yf_ticker, exc)
        return None
