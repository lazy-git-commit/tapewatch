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
  Twelvedata Basic = 800 credits/day. Each symbol in a batch call costs 1 credit.
  We never batch (one symbol per confirm_price_signal call), so 1 credit per
  price check. At ~50 checks/day this is well within limits.

Momentum baseline:
  /time_series returns results newest-first (index 0 = most recent completed bar).
  results[-6] (index 5) = the bar from ~5 minutes ago, which is our momentum
  baseline. The current price comes from Finnhub /quote (real-time).

Daily history for volume:
  /time_series with interval=1day&outputsize=21 gives 21 daily bars.
  We skip today's bar (index 0, incomplete) and average the prior 20 for ADV.
"""

import logging
import time
import requests
from datetime import datetime, timezone
import pytz
from config.settings import cfg

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.twelvedata.com"
_TIMEOUT = 8
_ET = pytz.timezone("America/New_York")

# Momentum look-back: how many 1-min bars back is "past_price".
# 6 bars = 5 minutes ago (bar 0 = latest complete bar, bar 5 = 5 min ago).
_MOMENTUM_BARS_BACK = 6


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


def get_momentum_baseline(symbol: str) -> tuple[float | None, float | None]:
    """
    Fetch 1-min intraday bars and return (past_price, current_bar_price).

    past_price     — close of bar from ~5 min ago (used as momentum baseline)
    current_bar_price — close of the most recent completed 1-min bar

    Returns (None, None) if data is unavailable.

    Staleness guard: if the most recent bar's timestamp is >10 minutes old,
    the data is considered stale and (None, None) is returned. This prevents
    the VECO failure mode where yfinance returned a bar from hours ago.
    """
    # outputsize=10 gives 10 bars (10 min of history). We need bar[5] = 5 min ago.
    # Using 10 gives a buffer if the most recent bar is still forming.
    values = _get_time_series(symbol, interval="1min", outputsize=10)
    if values is None or len(values) < _MOMENTUM_BARS_BACK:
        return None, None

    # values[0] = most recent completed bar (newest-first from Twelvedata)
    most_recent = values[0]
    # values[5] = 5 completed bars back = ~5 min ago
    baseline_bar = values[_MOMENTUM_BARS_BACK - 1]

    # Staleness guard: reject if most recent bar timestamp > 10 min old
    try:
        bar_time_str = most_recent.get("datetime", "")
        # Twelvedata returns ET time (e.g. "2026-06-11 09:35:00")
        bar_dt = datetime.strptime(bar_time_str, "%Y-%m-%d %H:%M:%S")
        bar_dt_et = _ET.localize(bar_dt)
        bar_dt_utc = bar_dt_et.astimezone(timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - bar_dt_utc).total_seconds() / 60
        if age_minutes > 10:
            logger.warning(
                "Twelvedata: stale bar for %s — most recent bar is %.1f min old (bar_time=%s)",
                symbol, age_minutes, bar_time_str,
            )
            return None, None
    except (ValueError, TypeError) as exc:
        logger.warning("Twelvedata: could not parse bar timestamp for %s: %s", symbol, exc)
        # Don't reject just because we can't parse the timestamp — proceed

    try:
        current_bar_price = float(most_recent["close"])
        past_price = float(baseline_bar["close"])
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Twelvedata: malformed bar data for %s: %s", symbol, exc)
        return None, None

    logger.debug(
        "Twelvedata [%s]: current_bar=%.4f past_bar=%.4f (5 min ago)",
        symbol, current_bar_price, past_price,
    )
    return past_price, current_bar_price


def get_volume_stats(symbol: str) -> tuple[int | None, int | None, float | None]:
    """
    Fetch 21 daily bars and return (today_volume, avg_daily_volume, daily_dollar_volume).

    today_volume        — today's intraday volume (bar[0], may be incomplete)
    avg_daily_volume    — 20-day average daily volume (bars[1..20])
    daily_dollar_volume — today's dollar volume: today's close × today's volume
                          used for the illiquidity filter (min_daily_dollar_volume)

    Returns (None, None, None) if data is unavailable.

    Note: for daily_dollar_volume we use today's close × today's volume as a
    proxy. For the filter, we project: if 2 hours into session and 200k volume,
    that represents ~25% of the session, so projected full-day is ~800k — still
    useful for filtering micro-caps.
    """
    values = _get_time_series(symbol, interval="1day", outputsize=21)
    if values is None or len(values) < 2:
        return None, None, None

    try:
        today_bar = values[0]
        today_volume = int(float(today_bar.get("volume", 0)))
        today_close = float(today_bar.get("close", 0))

        prior_bars = values[1:]  # up to 20 prior trading days
        prior_volumes = [int(float(b.get("volume", 0))) for b in prior_bars if b.get("volume")]
        avg_daily_volume = int(sum(prior_volumes) / len(prior_volumes)) if prior_volumes else 0

        # Projected daily dollar volume: today's partial volume × today's close
        # We use today's bar as-is — it underestimates early in session, which
        # is conservative (better to under-trade than over-trade illiquid names).
        daily_dollar_volume = today_close * today_volume if today_close > 0 and today_volume > 0 else None

        logger.debug(
            "Twelvedata volume [%s]: today=%d avg20d=%d ddv=$%.0f",
            symbol, today_volume, avg_daily_volume,
            daily_dollar_volume if daily_dollar_volume else 0,
        )
        return today_volume, avg_daily_volume, daily_dollar_volume
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Twelvedata: malformed daily bar data for %s: %s", symbol, exc)
        return None, None, None
