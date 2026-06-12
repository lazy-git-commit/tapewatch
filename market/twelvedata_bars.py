"""
market/twelvedata_bars.py
──────────────────────────
Intraday OHLCV data from Twelvedata /time_series.
Replaces yfinance as the momentum baseline source.

Why not yfinance?
  yfinance only serves data with a 15-min delay AND sometimes returns stale bars
  from hours ago on high-volume days (VECO root cause: bar from 09:56 returned
  at 11:42 giving false +1.20% momentum signal). Twelvedata Basic plan provides
  near-real-time 1-min bars and is reliable under load.

Credit model:
  Twelvedata Basic = 800 credits/day. Each symbol in a call costs 1 credit.
  We meter usage in-process (_credit_meter) and log a WARNING at 80% so a
  bursty news day doesn't silently exhaust the budget — once credits run out,
  every price check fails and every signal is dropped for the rest of the day.

Momentum baseline (BY TIMESTAMP, not index):
  /time_series returns bars newest-first. Earlier versions took values[5] as
  "5 minutes ago" — but thin stocks skip minutes (no trades → no bar), so
  values[5] could silently be 15–20 minutes old, stretching the momentum
  window per-stock and corrupting both the momentum floor and ceiling checks.
  We now walk the bars and select the newest bar whose timestamp is at least
  cfg.momentum_lookback_minutes old.

Liquidity (ADV-based, deliberately):
  get_volume_stats() returns avg_dollar_volume = 20-day ADV × last close.
  We do NOT use today's volume × price for the liquidity filter: during a
  halt-spike, today's dollar volume explodes, which would let the illiquidity
  filter PASS exactly the micro-caps it exists to block. Exit slippage is
  governed by the stock's NORMAL book depth, which ADV measures.
"""

import logging
import time
import requests
from datetime import datetime, timedelta, timezone
import pytz
from config.settings import cfg

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.twelvedata.com"
_TIMEOUT = 8
_ET = pytz.timezone("America/New_York")

# ── Credit metering ───────────────────────────────────────────────────────────
# Basic plan: 800 credits/day, 1 credit per symbol per call. Resets at UTC
# midnight (Twelvedata's reset). In-process counter — resets on restart, which
# is acceptable: it under-counts, and the purpose is the 80% early warning,
# not exact accounting.
_DAILY_CREDIT_LIMIT = 800
_CREDIT_WARN_FRACTION = 0.8
_credit_meter = {"date": None, "used": 0}


def _record_credit_use() -> None:
    """Count one Twelvedata credit and warn when approaching the daily cap."""
    today = datetime.now(timezone.utc).date()
    if _credit_meter["date"] != today:
        _credit_meter["date"] = today
        _credit_meter["used"] = 0
    _credit_meter["used"] += 1
    used = _credit_meter["used"]
    if used == int(_DAILY_CREDIT_LIMIT * _CREDIT_WARN_FRACTION):
        logger.warning(
            "Twelvedata credit budget at %d/%d (80%%) — price checks will start "
            "failing when the budget is exhausted",
            used, _DAILY_CREDIT_LIMIT,
        )
    elif used >= _DAILY_CREDIT_LIMIT:
        logger.error(
            "Twelvedata credit budget EXHAUSTED (%d/%d) — momentum/volume data "
            "unavailable until UTC midnight",
            used, _DAILY_CREDIT_LIMIT,
        )


def get_credits_used_today() -> int:
    """Expose the in-process credit count (for logging/diagnostics)."""
    today = datetime.now(timezone.utc).date()
    return _credit_meter["used"] if _credit_meter["date"] == today else 0


def _get_time_series(
    symbol: str,
    interval: str,
    outputsize: int,
    retries: int = 3,
    retry_delay: float = 1.5,
) -> list[dict] | None:
    """
    Call Twelvedata /time_series. Returns the 'values' list (newest first) or None.

    Retries on transient network errors and HTTP 5xx. Does NOT retry 4xx
    (invalid symbol, auth failure) — these won't self-heal.
    """
    url = f"{_BASE_URL}/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": cfg.twelvedata_api_key,
    }
    _record_credit_use()
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code == 429:
                # Rate limited — wait longer before retry
                wait = retry_delay * attempt * 2
                logger.warning(
                    "Twelvedata rate limit for %s (attempt %d/%d) — waiting %.1fs",
                    symbol, attempt, retries, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = retry_delay * attempt
                logger.warning(
                    "Twelvedata HTTP %d for %s (attempt %d/%d) — waiting %.1fs",
                    resp.status_code, symbol, attempt, retries, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "error":
                # API-level errors (bad symbol, no data) — don't retry
                logger.warning(
                    "Twelvedata API error for %s: %s",
                    symbol, data.get("message", "unknown"),
                )
                return None
            values = data.get("values", [])
            if not values:
                logger.warning("Twelvedata: empty values for %s interval=%s", symbol, interval)
                return None
            return values
        except requests.exceptions.Timeout:
            logger.warning(
                "Twelvedata timeout for %s (attempt %d/%d)", symbol, attempt, retries
            )
            last_exc = Exception(f"timeout after {_TIMEOUT}s")
        except requests.exceptions.ConnectionError as exc:
            logger.warning(
                "Twelvedata connection error for %s (attempt %d/%d): %s",
                symbol, attempt, retries, exc,
            )
            last_exc = exc
        except Exception as exc:
            logger.warning(
                "Twelvedata unexpected error for %s (attempt %d/%d): %s",
                symbol, attempt, retries, exc,
            )
            last_exc = exc
        if attempt < retries:
            time.sleep(retry_delay * attempt)
    logger.error(
        "Twelvedata: all %d attempts failed for %s — last error: %s",
        retries, symbol, last_exc,
    )
    return None


def _parse_bar_time(bar: dict) -> datetime | None:
    """Parse a Twelvedata bar timestamp (ET, 'YYYY-MM-DD HH:MM:SS') to UTC."""
    try:
        bar_dt = datetime.strptime(bar.get("datetime", ""), "%Y-%m-%d %H:%M:%S")
        return _ET.localize(bar_dt).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def get_momentum_baseline(symbol: str) -> tuple[float | None, float | None, float | None]:
    """
    Fetch 1-min intraday bars and return
    (past_price, current_bar_price, spread_proxy_pct).

    past_price        — close of the newest bar that is at least
                        cfg.momentum_lookback_minutes old (selected by
                        TIMESTAMP, see module docstring)
    current_bar_price — close of the most recent completed 1-min bar
    spread_proxy_pct  — (high − low) / close of the most recent bar, in %.
                        We have no bid/ask feed; the latest bar's range is a
                        usable proxy for effective spread + microstructure
                        noise. Used by the wide_spread entry filter.

    Returns (None, None, None) if data is unavailable.

    Staleness guard: if the most recent bar's timestamp is >10 minutes old,
    the data is considered stale and rejected. This prevents the VECO failure
    mode where a feed returned a bar from hours ago.
    """
    # Fetch enough bars to cover the look-back window even if some minutes
    # are missing (thin stocks skip bars when no trades print).
    outputsize = max(10, cfg.momentum_lookback_minutes * 3)
    values = _get_time_series(symbol, interval="1min", outputsize=outputsize)
    if values is None or len(values) < 2:
        return None, None, None

    now_utc = datetime.now(timezone.utc)
    most_recent = values[0]

    # ── Staleness guard ───────────────────────────────────────────────────────
    most_recent_time = _parse_bar_time(most_recent)
    if most_recent_time is not None:
        age_minutes = (now_utc - most_recent_time).total_seconds() / 60
        if age_minutes > 10:
            logger.warning(
                "Twelvedata: stale bar for %s — most recent bar is %.1f min old (bar_time=%s)",
                symbol, age_minutes, most_recent.get("datetime", "?"),
            )
            return None, None, None

    # ── Baseline selection by timestamp ───────────────────────────────────────
    # Walk newest→oldest and take the first bar at least lookback_minutes old.
    # This keeps the momentum window honest on stocks with missing bars.
    cutoff = now_utc - timedelta(minutes=cfg.momentum_lookback_minutes)
    baseline_bar = None
    for bar in values[1:]:
        bar_time = _parse_bar_time(bar)
        if bar_time is not None and bar_time <= cutoff:
            baseline_bar = bar
            break
    if baseline_bar is None:
        # All bars are newer than the cutoff (e.g. right after the open) —
        # fall back to the oldest bar we have rather than failing entirely.
        baseline_bar = values[-1]

    try:
        current_bar_price = float(most_recent["close"])
        past_price = float(baseline_bar["close"])
        bar_high = float(most_recent.get("high", current_bar_price))
        bar_low = float(most_recent.get("low", current_bar_price))
        spread_proxy_pct = (
            ((bar_high - bar_low) / current_bar_price) * 100
            if current_bar_price > 0 else None
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Twelvedata: malformed bar data for %s: %s", symbol, exc)
        return None, None, None

    logger.debug(
        "Twelvedata [%s]: current_bar=%.4f baseline=%.4f (bar_time=%s) spread_proxy=%.2f%%",
        symbol, current_bar_price, past_price,
        baseline_bar.get("datetime", "?"), spread_proxy_pct or 0,
    )
    return past_price, current_bar_price, spread_proxy_pct


def get_volume_stats(symbol: str) -> tuple[int | None, int | None, float | None, float | None]:
    """
    Fetch 21 daily bars and return
    (today_volume, avg_daily_volume, avg_dollar_volume, prev_close).

    today_volume      — today's cumulative volume so far (bar[0], partial)
    avg_daily_volume  — 20-day average daily share volume (bars[1..20])
    avg_dollar_volume — ADV × most recent prior close. THE liquidity metric:
                        measures normal book depth, immune to spike-day
                        volume inflation (see module docstring).
    prev_close        — previous session's close, used by price_check for
                        gap/day-change calculations.

    Returns (None, None, None, None) if data is unavailable.
    """
    values = _get_time_series(symbol, interval="1day", outputsize=21)
    if values is None or len(values) < 2:
        return None, None, None, None

    try:
        today_bar = values[0]
        today_volume = int(float(today_bar.get("volume", 0)))

        prior_bars = values[1:]  # up to 20 prior trading days
        prior_volumes = [int(float(b.get("volume", 0))) for b in prior_bars if b.get("volume")]
        avg_daily_volume = int(sum(prior_volumes) / len(prior_volumes)) if prior_volumes else 0

        # Previous close = close of the most recent COMPLETED session (bar[1]).
        prev_close = float(prior_bars[0].get("close", 0)) or None

        # ADV-based dollar volume — normal liquidity, not spike-day liquidity.
        avg_dollar_volume = (
            avg_daily_volume * prev_close
            if avg_daily_volume > 0 and prev_close else None
        )

        logger.debug(
            "Twelvedata volume [%s]: today=%d avg20d=%d adv$=%.0f prev_close=%s",
            symbol, today_volume, avg_daily_volume,
            avg_dollar_volume or 0, prev_close,
        )
        return today_volume, avg_daily_volume, avg_dollar_volume, prev_close
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Twelvedata: malformed daily bar data for %s: %s", symbol, exc)
        return None, None, None, None
