"""
market/price_check.py
──────────────────────
Fetches live price data and checks whether a signal is confirmed by actual
price movement and elevated volume.

Price sources:
  - Current price       — Finnhub REST quote (real-time, <1s latency, with retries)
  - Momentum baseline   — Twelvedata /time_series 1-min bars (bar[5] = ~5 min ago)
  - Volume stats        — Twelvedata /time_series 1-day bars (20-day ADV)

A signal is confirmed when ALL of the following hold:
  1. Opening block      — signal arrives at least cfg.open_block_minutes (5 min)
                          after the session open. The opening auction produces
                          violent noise (entire GOAI spike was in the 09:30 bar;
                          system bought at 09:32 into full collapse).
  2. Recent momentum    — price is up >= cfg.min_price_move_pct over the last
                          5 minutes (Twelvedata baseline)
  3. Volume spike       — today's volume > 1.5× the 20-day average after
                          cfg.open_block_minutes; in the open window, require
                          at least 0.5× average (not just non-zero).
  4. Liquidity          — daily dollar volume >= cfg.min_daily_dollar_volume
                          ($1M default). GOAI had ~$390k ADV — thin order book
                          meant our market sell moved price 11.7% below trigger.
  5. No dead-cat bounce — stock is not down more than cfg.max_day_drop_pct
                          from today's open.
  6. Min stock price     — current price >= cfg.min_stock_price ($2.00 default).
                          Sub-$2 stocks have catastrophic spread/slippage.
  7. Max momentum cap    — recent_move_pct <= cfg.max_price_move_pct (15% default).
                          A +15%+ reading means we are buying a post-halt top.
                          Circuit-breaker halt articles publish AFTER the spike —
                          every Jun 8–11 loss was a halt-article trade.
  8. Max volume ceiling  — volume_ratio <= cfg.max_volume_ratio (20× default).
                          Extreme volume (>20×) on micro-caps = circuit-breaker
                          halt pattern, not a genuine catalyst.
"""

import logging
import requests
import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import pytz
from config.settings import cfg
from market.finnhub_bars import get_finnhub_quote
from market.twelvedata_bars import get_momentum_baseline, get_volume_stats

_NYSE = mcal.get_calendar("NYSE")

logger = logging.getLogger(__name__)

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

    if candidate > now_et and now_et.weekday() < 5:
        return candidate.astimezone(timezone.utc)

    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)


def _to_yf_ticker(t212_ticker: str) -> str:
    """Strip Trading 212 suffix to get a Finnhub/Twelvedata-compatible ticker."""
    return t212_ticker.split("_")[0]


def is_too_late_to_buy() -> bool:
    """
    Return True if we are within TIME_STOP_MINUTES of today's market close.
    Uses the calendar's actual close time so early-close days are handled correctly.
    Returns False outside of market hours — is_market_open() handles that.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        close_utc = sched.iloc[0]["market_close"]
        minutes_to_close = (close_utc - now_utc).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.time_stop_minutes
    except Exception:
        now_et = datetime.now(_ET)
        close_et = now_et.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
        minutes_to_close = (close_et - now_et).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.time_stop_minutes


def is_market_open() -> bool:
    """
    Check whether the NYSE is currently open using pandas_market_calendars
    as the authoritative local source (handles holidays, early closes, weekends).

    Falls back to a Finnhub API check if the calendar check fails for any reason.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        market_open = sched.iloc[0]["market_open"]
        market_close = sched.iloc[0]["market_close"]
        return bool(market_open <= now_utc < market_close)
    except Exception as exc:
        logger.warning("Calendar open check failed: %s — falling back to Finnhub", exc)

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/market-status",
            params={"exchange": "US", "token": cfg.finnhub_api_key},
            timeout=5,
        )
        resp.raise_for_status()
        return bool(resp.json().get("isOpen", False))
    except Exception as exc:
        logger.warning("Finnhub market-status fallback also failed: %s — assuming closed", exc)
        return False


@dataclass
class PriceConfirmation:
    ticker: str
    symbol: str
    current_price: float
    open_price: float
    day_move_pct: float
    recent_move_pct: float      # price vs ~5 min ago (Twelvedata baseline)
    current_volume: int
    avg_volume: int
    volume_ratio: float
    daily_dollar_volume: float | None
    is_confirmed: bool
    reason: str
    reason_code: str            # approved | low_momentum | high_momentum | low_volume | high_volume | dead_cat | no_price_data | illiquid | opening_block | penny_stock


def confirm_price_signal(t212_ticker: str) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing active upward momentum that
    corroborates a bullish news signal.

    Returns None only when a hard data failure makes it impossible to evaluate
    the signal (Finnhub down, Twelvedata down). Confirmed/rejected signals
    are returned as PriceConfirmation with is_confirmed set accordingly.
    """
    symbol = _to_yf_ticker(t212_ticker)

    try:
        # ── Current price via Finnhub REST (real-time, retried) ───────────────
        quote = get_finnhub_quote(symbol)
        if quote is None:
            logger.warning(
                "Price check [%s]: no Finnhub quote available — cannot evaluate signal",
                symbol,
            )
            return None

        current_price = float(quote["c"])
        open_price = float(quote["o"]) if quote.get("o") else current_price
        if open_price == 0:
            open_price = current_price
        day_move_pct = ((current_price - open_price) / open_price) * 100

        # ── Time since open ───────────────────────────────────────────────────
        now_et = datetime.now(_ET)
        market_open_et = now_et.replace(
            hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0
        )
        minutes_since_open = (now_et - market_open_et).total_seconds() / 60

        # Hard opening-auction block. The first cfg.open_block_minutes (default 5)
        # after open are off-limits. GOAI: entire spike was in the 09:30 bar;
        # the system bought at 09:32 into full collapse. Extending to 5 min.
        if minutes_since_open < cfg.open_block_minutes:
            logger.info(
                "Price check [%s]: opening block active (%.1f min since open, block=%d min) — skipping",
                symbol, minutes_since_open, cfg.open_block_minutes,
            )
            return PriceConfirmation(
                ticker=t212_ticker,
                symbol=symbol,
                current_price=current_price,
                open_price=open_price,
                day_move_pct=day_move_pct,
                recent_move_pct=0.0,
                current_volume=0,
                avg_volume=0,
                volume_ratio=0.0,
                daily_dollar_volume=None,
                is_confirmed=False,
                reason=(
                    f"Opening auction block: {minutes_since_open:.1f} min since open "
                    f"(block lasts {cfg.open_block_minutes} min to avoid auction noise)"
                ),
                reason_code="opening_block",
            )

        # ── Penny stock guard ────────────────────────────────────────────────────
        # Stocks below min_stock_price ($2 default) have extreme bid-ask spreads
        # relative to price and are frequently the subject of halt-pump patterns.
        # All Jun 8–11 losses were on stocks priced < $5 at entry.
        if current_price < cfg.min_stock_price:
            reason = (
                f"Penny stock filter: price ${current_price:.4f} "
                f"< ${cfg.min_stock_price:.2f} minimum — extreme spread/slippage risk"
            )
            logger.info("Price check [%s]: rejected — %s", symbol, reason)
            return PriceConfirmation(
                ticker=t212_ticker,
                symbol=symbol,
                current_price=current_price,
                open_price=open_price,
                day_move_pct=day_move_pct,
                recent_move_pct=0.0,
                current_volume=0,
                avg_volume=0,
                volume_ratio=0.0,
                daily_dollar_volume=None,
                is_confirmed=False,
                reason=reason,
                reason_code="penny_stock",
            )

        # ── Momentum baseline via Twelvedata 1-min bars ───────────────────────
        # bar[5] = price ~5 min ago; bar[0] = most recent completed bar.
        # Staleness guard built into get_momentum_baseline() — rejects bars >10 min old.
        past_price, current_bar_price = get_momentum_baseline(symbol)

        if past_price is None:
            # Fall back to Finnhub open price in the early session window
            # (session not yet 15 min old, Twelvedata may not have enough bars)
            if minutes_since_open < 15 and open_price and open_price > 0:
                past_price = open_price
                logger.info(
                    "Price check [%s]: Twelvedata unavailable — using Finnhub open=%.4f as baseline",
                    symbol, open_price,
                )
            else:
                logger.warning(
                    "Price check [%s]: Twelvedata momentum baseline unavailable and not in open window — cannot evaluate",
                    symbol,
                )
                return None

        recent_move_pct = ((current_price - past_price) / past_price) * 100 if past_price else 0.0

        # ── Volume stats via Twelvedata 1-day bars ────────────────────────────
        today_volume, avg_daily_volume, daily_dollar_volume = get_volume_stats(symbol)

        if today_volume is None:
            # Volume data unavailable — use Finnhub quote's own volume field as fallback
            current_volume = int(quote.get("v", 0))
            avg_daily_volume = 0
            volume_ratio = 0.0
            daily_dollar_volume = None
            logger.warning(
                "Price check [%s]: Twelvedata volume unavailable — using Finnhub quote volume=%d (no ratio)",
                symbol, current_volume,
            )
        else:
            current_volume = today_volume
            volume_ratio = (current_volume / avg_daily_volume) if avg_daily_volume > 0 else 0.0

        # ── Evaluate conditions ───────────────────────────────────────────────

        # 1. Dead-cat bounce guard
        dead_cat = day_move_pct < -cfg.max_day_drop_pct
        if dead_cat:
            reason = (
                f"Dead-cat bounce guard: stock is down {day_move_pct:.2f}% on the day "
                f"(max allowed drop: -{cfg.max_day_drop_pct}%) — skipping"
            )
            logger.info("Price check [%s]: rejected — %s", symbol, reason)
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="dead_cat",
            )

        # 2. Liquidity filter — reject stocks with insufficient daily dollar volume.
        # Thin order books cause catastrophic slippage on market sell orders.
        if daily_dollar_volume is not None and daily_dollar_volume < cfg.min_daily_dollar_volume:
            reason = (
                f"Liquidity filter: daily dollar volume ${daily_dollar_volume:,.0f} "
                f"< ${cfg.min_daily_dollar_volume:,.0f} minimum — market orders would cause severe slippage"
            )
            logger.info("Price check [%s]: rejected — %s", symbol, reason)
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="illiquid",
            )

        # 3. Momentum floor check
        momentum_ok = recent_move_pct >= cfg.min_price_move_pct
        if not momentum_ok:
            reason = (
                f"Insufficient recent momentum: {recent_move_pct:+.2f}% "
                f"over last ~5 min "
                f"(threshold: +{cfg.min_price_move_pct}%)"
            )
            logger.info(
                "Price check [%s]: recent=%+.2f%% day=%+.2f%% vol=%.1f× ddv=%s — rejected: low_momentum",
                symbol, recent_move_pct, day_move_pct, volume_ratio,
                f"${daily_dollar_volume:,.0f}" if daily_dollar_volume else "n/a",
            )
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="low_momentum",
            )

        # 3b. Momentum ceiling check — reject stocks that have already moved too far.
        # A recent_move_pct > cfg.max_price_move_pct means we're likely reading a
        # post-halt spike. Circuit-breaker halt articles publish AFTER the 30–120% move;
        # buying here = buying the top. All Jun 8–11 losses triggered this pattern.
        if recent_move_pct > cfg.max_price_move_pct:
            reason = (
                f"Momentum ceiling breached: {recent_move_pct:+.2f}% over last ~5 min "
                f"exceeds max {cfg.max_price_move_pct}% — likely a post-halt article, "
                f"not a live catalyst"
            )
            logger.info(
                "Price check [%s]: recent=%+.2f%% day=%+.2f%% vol=%.1f× ddv=%s — rejected: high_momentum",
                symbol, recent_move_pct, day_move_pct, volume_ratio,
                f"${daily_dollar_volume:,.0f}" if daily_dollar_volume else "n/a",
            )
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="high_momentum",
            )

        # 4. Volume check
        # After the open block (5+ min): require ≥1.5× average daily volume.
        # During the open block window (already rejected above, but keeping logic clean):
        # require ≥0.5× — eliminates zero-volume auction ticks while allowing
        # genuine gap-ups with lower early volume. This is stricter than the old
        # "current_volume > 0" check that let GOAI through at 0.7×.
        if minutes_since_open >= 15:
            volume_ok = volume_ratio >= 1.5
        elif minutes_since_open >= cfg.open_block_minutes:
            # 5–15 min window: require 0.5× minimum
            volume_ok = volume_ratio >= 0.5
        else:
            volume_ok = current_volume > 0  # Shouldn't reach here (opening_block above)

        if not volume_ok:
            if minutes_since_open < 15:
                reason = (
                    f"+{recent_move_pct:.2f}% momentum but volume {volume_ratio:.2f}× avg "
                    f"({current_volume:,} shares) — too thin in early session (need ≥0.5×)"
                )
            else:
                reason = (
                    f"+{recent_move_pct:.2f}% momentum but low volume "
                    f"({volume_ratio:.1f}× avg, threshold 1.5×) — rejected"
                )
            logger.info(
                "Price check [%s]: recent=%+.2f%% day=%+.2f%% vol=%.1f× ddv=%s — rejected: low_volume",
                symbol, recent_move_pct, day_move_pct, volume_ratio,
                f"${daily_dollar_volume:,.0f}" if daily_dollar_volume else "n/a",
            )
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="low_volume",
            )

        # 4b. Volume ceiling check — extreme volume on micro-caps = halt pattern.
        # All Jun 8–11 halt-article trades had volume_ratio > 30×. A genuine
        # momentum catalyst has elevated but not parabolic volume (5–15× is the
        # sweet spot). Above cfg.max_volume_ratio (20×) the risk/reward inverts.
        if volume_ratio > cfg.max_volume_ratio:
            reason = (
                f"Volume ceiling breached: {volume_ratio:.1f}× avg volume "
                f"exceeds max {cfg.max_volume_ratio:.0f}× — "
                f"extreme volume pattern consistent with circuit-breaker halt"
            )
            logger.info(
                "Price check [%s]: recent=%+.2f%% day=%+.2f%% vol=%.1f× ddv=%s — rejected: high_volume",
                symbol, recent_move_pct, day_move_pct, volume_ratio,
                f"${daily_dollar_volume:,.0f}" if daily_dollar_volume else "n/a",
            )
            return PriceConfirmation(
                ticker=t212_ticker, symbol=symbol,
                current_price=current_price, open_price=open_price,
                day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
                current_volume=current_volume, avg_volume=avg_daily_volume or 0,
                volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
                is_confirmed=False, reason=reason, reason_code="high_volume",
            )

        # ── All conditions met — signal confirmed ─────────────────────────────
        ddv_str = f" | ddv=${daily_dollar_volume:,.0f}" if daily_dollar_volume else ""
        reason = (
            f"+{recent_move_pct:.2f}% in last ~5 min "
            f"| {volume_ratio:.1f}× avg volume "
            f"| day: {day_move_pct:+.2f}%"
            f"{ddv_str}"
        )
        logger.info(
            "Price check [%s]: recent=%+.2f%% day=%+.2f%% vol=%.1f× ddv=%s — APPROVED",
            symbol, recent_move_pct, day_move_pct, volume_ratio,
            f"${daily_dollar_volume:,.0f}" if daily_dollar_volume else "n/a",
        )
        return PriceConfirmation(
            ticker=t212_ticker, symbol=symbol,
            current_price=current_price, open_price=open_price,
            day_move_pct=day_move_pct, recent_move_pct=recent_move_pct,
            current_volume=current_volume, avg_volume=avg_daily_volume or 0,
            volume_ratio=volume_ratio, daily_dollar_volume=daily_dollar_volume,
            is_confirmed=True, reason=reason, reason_code="approved",
        )

    except Exception as exc:
        logger.error("Price check failed for %s: %s", symbol, exc, exc_info=True)
        return None


def get_current_price(t212_ticker: str) -> float | None:
    """
    Fast lookup of the latest price for an open position monitor.
    Primary: Finnhub REST quote (real-time, retried).
    Returns None if Finnhub is unavailable — callers must handle this explicitly.
    """
    symbol = _to_yf_ticker(t212_ticker)
    quote = get_finnhub_quote(symbol)
    if quote is not None:
        price = float(quote["c"])
        logger.debug("get_current_price [%s]: %.4f (Finnhub)", symbol, price)
        return price
    # Twelvedata fallback: use most recent 1-min bar close
    try:
        past_price, current_bar_price = get_momentum_baseline(symbol)
        if current_bar_price is not None:
            logger.warning(
                "get_current_price [%s]: Finnhub unavailable — using Twelvedata bar close %.4f",
                symbol, current_bar_price,
            )
            return current_bar_price
    except Exception as exc:
        logger.error("get_current_price [%s]: Twelvedata fallback also failed: %s", symbol, exc)
    logger.error(
        "get_current_price [%s]: both Finnhub and Twelvedata unavailable — returning None",
        symbol,
    )
    return None
