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


# ── GBP/USD live rate ─────────────────────────────────────────────────────────
# Cached with a 60-second TTL: position sizing is called at most once per signal,
# but in burst news days we don't want to burn a credit per signal. The 60s stale
# window is safe — FX intraday moves ~0.1%/min, which is inside the ADV cap's
# safety margin. Falls back to 1.27 (5-year average) if the API is unavailable.

_FX_CACHE: dict = {"rate": None, "ts": 0.0}
_FX_CACHE_TTL = 60.0
_FX_FALLBACK = 1.27


def get_gbp_usd_rate() -> float:
    """Return the live GBP/USD rate, cached for 60 s. Falls back to 1.27."""
    now = time.monotonic()
    if _FX_CACHE["rate"] is not None and now - _FX_CACHE["ts"] < _FX_CACHE_TTL:
        return _FX_CACHE["rate"]
    try:
        resp = requests.get(
            f"{_BASE_URL}/price",
            params={"symbol": "GBP/USD", "apikey": cfg.twelvedata_api_key},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rate = float(data["price"])
        if rate <= 0:
            raise ValueError(f"Twelvedata returned non-positive GBP/USD rate: {rate}")
        _FX_CACHE["rate"] = rate
        _FX_CACHE["ts"] = now
        return rate
    except Exception as exc:
        logger.warning("GBP/USD rate fetch failed: %s — using fallback %.4f", exc, _FX_FALLBACK)
        # Update ts even on failure so we throttle to one retry per TTL window
        # instead of hammering the dead endpoint on every call during an outage.
        _FX_CACHE["ts"] = now
        return _FX_CACHE["rate"] if _FX_CACHE["rate"] is not None else _FX_FALLBACK


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
    """Parse a Twelvedata bar timestamp (ET) to UTC.

    Intraday bars use ``YYYY-MM-DD HH:MM:SS``; daily bars often use
    ``YYYY-MM-DD``. Treat both as ET because Twelvedata timestamps US equity
    bars in the exchange timezone.
    """
    try:
        raw = bar.get("datetime", "")
        fmt = "%Y-%m-%d" if len(raw) == 10 else "%Y-%m-%d %H:%M:%S"
        bar_dt = datetime.strptime(raw, fmt)
        return _ET.localize(bar_dt).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def get_twelvedata_quote(symbol: str, fast: bool = False) -> dict | None:
    """
    Fetch a real-time quote from Twelvedata /quote — the FALLBACK for when
    Finnhub has no coverage of a symbol (small caps, recent IPOs, many names
    Finnhub's free tier simply doesn't carry; observed 2026-06-15: Finnhub
    returned no quote for CUPR/ELAN/WBD/INBX/SAIL while Twelvedata had them all).

    Returns a dict normalised to the SAME keys as get_finnhub_quote() so the
    price-check code can consume either interchangeably:
        c  — current/last price        (Twelvedata "close")
        o  — today's open               (Twelvedata "open")
        pc — previous session close     (Twelvedata "previous_close")
    Plus a passthrough "av" (average_volume) when present.

    Costs 1 Twelvedata credit. Returns None on any error or missing price.

    `fast` (default False) is for time-boxed callers — chiefly the pre-market
    eval window, where every second of retry backoff is a second the gap-and-go
    edge decays, and candidates are evaluated concurrently so blocking one
    thread for 18s on a 429 starves nothing but does waste the window. In fast
    mode there are NO retries and NO sleeps: a 429/5xx/timeout returns None
    immediately (skip this candidate this cycle, re-try on the NEXT cycle, which
    is the retry), and a 404 is treated as terminal either way (the symbol does
    not exist on Twelvedata — see below). Normal (RTH) callers keep the
    full 429/5xx-aware retry behaviour.

    A 404 is ALWAYS terminal, even in non-fast mode: it means Twelvedata has no
    such symbol, so re-requesting it 3× with backoff (the old behaviour: 404 →
    raise_for_status → RequestException → sleep → repeat, ~4.5s of pure dead
    time per call) can never succeed. Returning None on the first 404 is both
    correct and ~3× faster for the no-coverage small-caps this fallback targets.
    """
    url = f"{_BASE_URL}/quote"
    params = {"symbol": symbol, "apikey": cfg.twelvedata_api_key}
    _record_credit_use()
    last_exc: Exception | None = None
    attempts = 1 if fast else 3
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            # 404 = symbol genuinely not on Twelvedata — retrying cannot help.
            if resp.status_code == 404:
                logger.debug("Twelvedata /quote: %s not found (404) — terminal", symbol)
                return None
            if resp.status_code == 429:
                if fast:
                    logger.info(
                        "Twelvedata /quote rate limit for %s — fast mode, skipping "
                        "(retry next cycle)", symbol,
                    )
                    return None
                wait = 1.5 * attempt * 2
                logger.warning(
                    "Twelvedata /quote rate limit for %s (attempt %d/3) — waiting %.1fs",
                    symbol, attempt, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                if fast:
                    return None
                time.sleep(1.5 * attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "error":
                logger.debug(
                    "Twelvedata /quote error for %s: %s",
                    symbol, data.get("message", "unknown")[:80],
                )
                return None
            close = data.get("close")
            if close is None or float(close) == 0:
                return None
            quote = {
                "c": float(close),
                "o": float(data["open"]) if data.get("open") else float(close),
                "pc": float(data["previous_close"]) if data.get("previous_close") else None,
                "av": int(float(data["average_volume"])) if data.get("average_volume") else None,
            }
            logger.debug(
                "Twelvedata /quote [%s]: c=%.4f o=%.4f pc=%s (Finnhub fallback)",
                symbol, quote["c"], quote["o"], quote["pc"],
            )
            return quote
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if fast:
                return None
            time.sleep(1.5 * attempt)
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("Twelvedata /quote malformed data for %s: %s", symbol, exc)
            return None
    logger.warning("Twelvedata /quote: all attempts failed for %s — last: %s", symbol, last_exc)
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
        # No bar is old enough to honor the look-back window (e.g. only a few
        # bars exist right after the open). Fall back to the OLDEST bar we have
        # rather than failing — but only if it is genuinely a different bar
        # from the most recent one. If the oldest available bar IS the most
        # recent (degenerate single-usable-bar case), there is no momentum
        # window at all: return past_price=None so the caller's early-session
        # open-price fallback (or a clean reject) kicks in instead of a
        # spurious 0.00% reading.
        if values[-1] is most_recent:
            logger.debug(
                "Twelvedata [%s]: no bar old enough for a momentum window "
                "(have %d bar(s)) — momentum baseline unavailable",
                symbol, len(values),
            )
            # Still return the current bar price + spread so the caller has them.
            try:
                cur = float(most_recent["close"])
                hi = float(most_recent.get("high", cur))
                lo = float(most_recent.get("low", cur))
                sp = ((hi - lo) / cur) * 100 if cur > 0 else None
            except (KeyError, ValueError, TypeError):
                cur, sp = None, None
            return None, cur, sp
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


def get_session_vwap(symbol: str) -> tuple[float | None, float | None]:
    """
    Compute today's session VWAP and return (vwap, last_price).

    VWAP (Volume-Weighted Average Price) is the institutional fair-value line:
        VWAP = Σ(typical_price × volume) / Σ(volume),  typical = (H+L+C)/3
    accumulated from the session open.

    Why VWAP instead of a fixed % momentum floor (the v15 strategy fix):
      A genuine catalyst on a deep-book large-cap reprices it by well under
      1.5% in 5 minutes (DXCM +0.14%, SNY +0.07% on 2026-06-15 — all rejected
      by the old fixed floor), yet the stock trades and HOLDS above VWAP
      because institutions are net buyers. A fading "gap-and-crap" sits below
      VWAP regardless of its raw % change. Price-vs-VWAP is therefore a
      SIZE-NEUTRAL "is this being accumulated?" signal — the same test works
      for a $2 micro-cap and a $1000 mega-cap. This is the standard
      practitioner confirmation (see docs/algorithm.md research notes).

    Returns (None, None) if data is unavailable. Costs 1 credit.

    Implementation note: we pull up to 390 1-min bars (a full RTH session) and
    accumulate only today's bars. Early in the session there are few bars, which
    is fine — VWAP is simply the average so far.
    """
    values = _get_time_series(symbol, interval="1min", outputsize=390)
    if values is None or len(values) < 1:
        return None, None

    now_et_date = datetime.now(_ET).date()
    cum_pv = 0.0   # Σ typical_price × volume
    cum_v = 0.0    # Σ volume
    last_price: float | None = None

    # values are newest-first; iterate oldest→newest so last_price ends on the
    # most recent bar. Only include bars from today's session.
    for bar in reversed(values):
        bar_dt = _parse_bar_time(bar)
        if bar_dt is None or bar_dt.astimezone(_ET).date() != now_et_date:
            continue
        try:
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            vol = float(bar.get("volume", 0))
        except (KeyError, ValueError, TypeError):
            continue
        typical = (high + low + close) / 3
        cum_pv += typical * vol
        cum_v += vol
        last_price = close

    if cum_v <= 0 or last_price is None:
        # No volume yet (pre-open or dead tape) — VWAP undefined.
        return None, last_price

    vwap = cum_pv / cum_v
    logger.debug("Twelvedata VWAP [%s]: vwap=%.4f last=%.4f", symbol, vwap, last_price)
    return vwap, last_price


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
        today_bar_time = _parse_bar_time(today_bar)
        today_et = datetime.now(_ET).date()
        if today_bar_time is None or today_bar_time.astimezone(_ET).date() != today_et:
            logger.warning(
                "Twelvedata daily bar for %s has not rolled to today yet "
                "(latest=%s, expected=%s) — volume/RVOL unavailable",
                symbol, today_bar.get("datetime", "?"), today_et,
            )
            return None, None, None, None

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
