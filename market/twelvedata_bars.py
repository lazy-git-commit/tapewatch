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

Credit model (HARD budget guard — 2026-06-23 credit-collapse fix):
  Twelvedata Basic = 800 credits/day (free/trial tier — we deliberately do NOT
  pay for a larger plan until the strategy is net-profitable). Each symbol in a
  call costs 1 credit. We meter usage in-process (_credit_meter).

  CRITICAL: once the daily budget is spent, every public entry point in this
  module SHORT-CIRCUITS — it returns its "data unavailable" sentinel WITHOUT
  making the HTTP call. Before this guard, an exhausted budget kept calling the
  API anyway, getting 429s, and burning the full 3+6+9=18s retry backoff per
  call; on a busy news day that storm (plus the premarket fan-out draining the
  budget by mid-morning) silently took the system down for NINE consecutive
  sessions, 2026-06-11→06-23, with zero trades and no alert. See
  docs/algorithm.md §"Data-budget collapse" and credits_exhausted() below.

  When credits are exhausted the system has NO way to run its momentum/RVOL/
  VWAP/liquidity gates (Finnhub provides only a quote, never bars), so it CANNOT
  and MUST NOT trade — it fails closed. News scoring is unaffected (Claude +
  Benzinga only) and keeps running so the eval loop still measures classifier
  accuracy on days we can't trade.

Momentum baseline (BY TIMESTAMP, not index):
  /time_series returns bars newest-first. Earlier versions took values[5] as
  "5 minutes ago" — but thin stocks skip minutes (no trades → no bar), so
  values[5] could silently be 15–20 minutes old, stretching the momentum
  window per-stock and corrupting both the momentum floor and ceiling checks.
  We now walk the bars and select the newest bar whose timestamp is at least
  cfg.momentum_lookback_minutes old.

Liquidity (ADV-based, deliberately):
  get_daily_stats() returns avg_dollar_volume = 20-day ADV × last close.
  We do NOT use today's volume × price for the liquidity filter: during a
  halt-spike, today's dollar volume explodes, which would let the illiquidity
  filter PASS exactly the micro-caps it exists to block. Exit slippage is
  governed by the stock's NORMAL book depth, which ADV measures.

Call plan per confirmation (v20):
  get_session_analysis() — 1 credit, one 390-bar 1-min pull → momentum
  baseline (by timestamp, today-only), spread proxy, session volume, VWAP,
  session low/high. get_daily_stats() — 1 credit ONCE per symbol per day
  (cached) → ADV, dollar-ADV, prev close. A re-evaluation retry costs 1
  credit total, down from the 2-3 the old three-function plan re-paid.
"""

import logging
import math
import threading
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import pytz
from config.settings import cfg

logger = logging.getLogger(__name__)


def _safe_float(v) -> float | None:
    """float(v) that returns None for unparseable/non-finite values instead of
    raising — NaN would compare False against every downstream gate threshold."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None

_BASE_URL = "https://api.twelvedata.com"
_TIMEOUT = 8
_ET = pytz.timezone("America/New_York")

# ── Credit metering ───────────────────────────────────────────────────────────
# Grow plan: no daily credit cap. We keep the in-process counter and guard
# active as a sanity backstop (runaway loop, misconfiguration) but set the
# limit high enough that it will never fire under normal operation. The 80%
# warning threshold is similarly raised so it doesn't false-alarm.
# Previously: Basic plan = 800/day; upgraded 2026-06-25 to Grow ($29/month).
_DAILY_CREDIT_LIMIT = 50_000                           # Grow: no hard cap; backstop only
_CREDIT_HEADROOM = 100                                 # headroom on the backstop
_DAILY_CREDIT_SOFT_CAP = _DAILY_CREDIT_LIMIT - _CREDIT_HEADROOM
_CREDIT_WARN_AT = int(_DAILY_CREDIT_LIMIT * 0.8)       # 80% of backstop (40,000)
_credit_meter = {"date": None, "used": 0}
# Per-day latches so the 80% WARNING and the EXHAUSTED transition each log once
# per UTC day rather than on every call (the old EXHAUSTED path logged hundreds
# of times — 120 lines on 2026-06-22 alone). `exhausted_emitted` is separate
# from `exhausted_logged` so a transient DB failure on the first emit doesn't
# permanently suppress the system_events row: the log latches immediately, the
# emit keeps retrying until one succeeds.
_meter_latches = {"date": None, "warned": False, "exhausted_logged": False, "exhausted_emitted": False}
# All reads AND mutations of _credit_meter / _meter_latches happen under this
# lock. The pre-market eval runs confirm_price_signal across an 8-worker thread
# pool (premarket/scanner.py), so each `used += 1` and each latch check-then-set
# is genuinely concurrent — a bare dict op is not guaranteed atomic and the
# check-then-set would otherwise double-fire logs/emits across threads.
_meter_lock = threading.Lock()

# ── Per-minute rate-limit token bucket ───────────────────────────────────────
# Grow plan = 55 API calls/minute. Token bucket guards the in-process burst
# rate so we never send more than this to the API even under concurrent load
# (e.g. 34 pre-market candidates firing in the first open cycle).
# Previously: Basic = 8/min; raised to 55 on 2026-06-25 Grow upgrade.
_PER_MINUTE_LIMIT = 55
_bucket_lock = threading.Lock()
_bucket_tokens = float(_PER_MINUTE_LIMIT)          # starts full
_bucket_last_refill = time.monotonic()


def _claim_minute_token() -> bool:
    """Claim one per-minute token. Returns True if a token was available, False if rate-limited."""
    global _bucket_tokens, _bucket_last_refill
    with _bucket_lock:
        now = time.monotonic()
        elapsed = now - _bucket_last_refill
        _bucket_tokens = min(
            float(_PER_MINUTE_LIMIT),
            _bucket_tokens + elapsed * (_PER_MINUTE_LIMIT / 60.0),
        )
        _bucket_last_refill = now
        if _bucket_tokens >= 1.0:
            _bucket_tokens -= 1.0
            return True
        return False


def _roll_meter_locked() -> None:
    """Reset counter + latches on a UTC-day change. CALLER MUST HOLD _meter_lock."""
    today = datetime.now(timezone.utc).date()
    if _credit_meter["date"] != today:
        _credit_meter["date"] = today
        _credit_meter["used"] = 0
    if _meter_latches["date"] != today:
        _meter_latches.update(
            date=today, warned=False, exhausted_logged=False, exhausted_emitted=False
        )


def credits_exhausted() -> bool:
    """
    True when the Twelvedata daily budget is spent (at/over the soft cap).

    This is the single gate every public entry point checks BEFORE making a
    network call: when it returns True the call is skipped entirely (no HTTP, no
    retry backoff) and the caller gets its "unavailable" sentinel. That is what
    keeps an exhausted budget from re-triggering the 18s-per-call 429 storm.

    Logs the EXHAUSTED transition once per UTC day and records a system_event so
    the outage is visible to alerting/Grafana instead of being buried in INFO
    spam (observability gap that hid the 9-session drought). The DB emit is
    attempted (outside the lock) until it succeeds, so a momentary DB blip at the
    instant of first exhaustion doesn't lose the alert row for the whole day.
    """
    emit_now = False
    with _meter_lock:
        _roll_meter_locked()
        if _credit_meter["used"] < _DAILY_CREDIT_SOFT_CAP:
            return False
        used = _credit_meter["used"]
        if not _meter_latches["exhausted_logged"]:
            _meter_latches["exhausted_logged"] = True
            logger.error(
                "Twelvedata credit budget EXHAUSTED (%d/%d, soft cap %d) — momentum/"
                "volume/VWAP data unavailable until UTC midnight; the system will keep "
                "scoring news but CANNOT confirm signals, so it will not trade for the "
                "rest of the day",
                used, _DAILY_CREDIT_LIMIT, _DAILY_CREDIT_SOFT_CAP,
            )
        if not _meter_latches["exhausted_emitted"]:
            emit_now = True  # retry the DB emit until one call succeeds
    if emit_now and _emit_credit_exhausted_event():
        with _meter_lock:
            _meter_latches["exhausted_emitted"] = True
    return True


def _emit_credit_exhausted_event() -> bool:
    """Record a one-per-day system_event for the credit-exhaustion outage.

    Returns True on success, False if the write failed (so the caller leaves the
    `exhausted_emitted` latch unset and retries on a later call). Best-effort and
    import-local: market.* must not hard-depend on storage.* at import time, and
    an event-log failure must never affect the data path. Called WITHOUT the lock
    held (it does I/O).
    """
    try:
        from storage.database import record_system_event
        record_system_event(
            "twelvedata_credits_exhausted",
            f"{_DAILY_CREDIT_SOFT_CAP}+/{_DAILY_CREDIT_LIMIT} credits used "
            f"— trading suspended until UTC midnight",
        )
        return True
    except Exception as exc:
        logger.debug("Could not record credit-exhaustion system_event: %s", exc)
        return False


def _record_credit_use() -> None:
    """Count one Twelvedata credit and warn (once/day) when approaching the cap.

    Callers must already have checked credits_exhausted() and skipped the call
    when it was True — this only meters calls we actually make. Thread-safe: the
    increment and the warn check-then-set run under _meter_lock so the 8-worker
    pre-market pool can't lose increments or skip the warning.
    """
    with _meter_lock:
        _roll_meter_locked()
        _credit_meter["used"] += 1
        if _credit_meter["used"] >= _CREDIT_WARN_AT and not _meter_latches["warned"]:
            _meter_latches["warned"] = True
            logger.warning(
                "Twelvedata credit budget at %d/%d (80%%) — approaching the daily cap; "
                "price confirmation will stop (and trading will pause) at %d",
                _credit_meter["used"], _DAILY_CREDIT_LIMIT, _DAILY_CREDIT_SOFT_CAP,
            )


def get_credits_used_today() -> int:
    """Expose the in-process credit count (for logging/diagnostics)."""
    with _meter_lock:
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


def _fx_stale_or_fallback() -> float:
    """Last-known rate if we ever had one, else the static fallback."""
    return _FX_CACHE["rate"] if _FX_CACHE["rate"] is not None else _FX_FALLBACK


def get_gbp_usd_rate() -> float:
    """Return the live GBP/USD rate, cached for 60 s. Falls back to 1.27.

    The /price call costs 1 Twelvedata credit like every other endpoint, so it
    runs behind the SAME two gates as the bar/quote calls (it used to bypass
    both, making FX the one unmetered leak in the credit budget and an extra
    unaccounted call against the 55/min bucket). Degradation is graceful
    either way: a stale rate (≤60s+ old) is within the sizing safety margin.
    """
    now = time.monotonic()
    if _FX_CACHE["rate"] is not None and now - _FX_CACHE["ts"] < _FX_CACHE_TTL:
        return _FX_CACHE["rate"]
    if credits_exhausted() or not _claim_minute_token():
        # Serve stale/fallback and push ts forward so we re-check at most once
        # per TTL window instead of on every sizing call.
        _FX_CACHE["ts"] = now
        return _fx_stale_or_fallback()
    _record_credit_use()
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
        return _fx_stale_or_fallback()


def _get_time_series(
    symbol: str,
    interval: str,
    outputsize: int,
    retries: int = 3,
    retry_delay: float = 1.5,
    fast: bool = False,
) -> list[dict] | None:
    """
    Call Twelvedata /time_series. Returns the 'values' list (newest first) or None.

    Retries on transient network errors and HTTP 5xx. Does NOT retry 4xx
    (invalid symbol, auth failure) — these won't self-heal.

    `fast` (default False) is for time-boxed callers — chiefly the pre-market
    eval window, where every second of retry backoff is a second the gap-and-go
    edge decays and candidates are evaluated concurrently under a wall-clock
    budget. In fast mode there are NO retries and NO sleeps: a 429/5xx/timeout
    returns None immediately (skip this candidate this cycle; the next cycle is
    the retry). This matches the no-retry contract already on get_twelvedata_
    quote() — before 2026-06-23 the fast path covered only the quote, while the
    momentum/volume/VWAP calls (which all route through here) still did the full
    3+6+9s backoff, so a single slow ticker could blow the 30s budget and starve
    the rest. See premarket/scanner.evaluate_premarket_candidates.

    Budget guard: returns None WITHOUT any HTTP call when the daily credit budget
    is exhausted (credits_exhausted()). This is what stops the 18s-per-call 429
    storm once the budget is spent.
    """
    if credits_exhausted():
        return None
    if not _claim_minute_token():
        logger.info(
            "Twelvedata per-minute rate limit for %s — skipping (retry next cycle)", symbol,
        )
        return None

    url = f"{_BASE_URL}/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": cfg.twelvedata_api_key,
    }
    _record_credit_use()
    attempts = 1 if fast else retries
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code == 429:
                if fast:
                    logger.info(
                        "Twelvedata rate limit for %s — fast mode, skipping (retry next cycle)",
                        symbol,
                    )
                    return None
                # Rate limited — wait longer before retry
                wait = retry_delay * attempt * 2
                logger.warning(
                    "Twelvedata rate limit for %s (attempt %d/%d) — waiting %.1fs",
                    symbol, attempt, attempts, wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                if fast:
                    return None
                wait = retry_delay * attempt
                logger.warning(
                    "Twelvedata HTTP %d for %s (attempt %d/%d) — waiting %.1fs",
                    resp.status_code, symbol, attempt, attempts, wait,
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
            # Must be a non-empty LIST: a dict/string here would make callers
            # index into it (`values[0]`) with garbage semantics instead of
            # failing cleanly at the source.
            if not isinstance(values, list) or not values:
                logger.warning(
                    "Twelvedata: empty or malformed values for %s interval=%s",
                    symbol, interval,
                )
                return None
            return values
        except requests.exceptions.Timeout:
            logger.warning(
                "Twelvedata timeout for %s (attempt %d/%d)", symbol, attempt, attempts
            )
            last_exc = Exception(f"timeout after {_TIMEOUT}s")
            if fast:
                return None
        except requests.exceptions.ConnectionError as exc:
            logger.warning(
                "Twelvedata connection error for %s (attempt %d/%d): %s",
                symbol, attempt, attempts, exc,
            )
            last_exc = exc
            if fast:
                return None
        except Exception as exc:
            logger.warning(
                "Twelvedata unexpected error for %s (attempt %d/%d): %s",
                symbol, attempt, attempts, exc,
            )
            last_exc = exc
            if fast:
                return None
        if attempt < attempts:
            time.sleep(retry_delay * attempt)
    logger.error(
        "Twelvedata: all %d attempts failed for %s — last error: %s",
        attempts, symbol, last_exc,
    )
    return None


def _parse_bar_time(bar: dict) -> datetime | None:
    """Parse a Twelvedata bar timestamp (ET) to UTC.

    Intraday bars use ``YYYY-MM-DD HH:MM:SS``; daily bars often use
    ``YYYY-MM-DD``. Treat both as ET because Twelvedata timestamps US equity
    bars in the exchange timezone.
    """
    # isinstance guard: a scalar/null smuggled into the values array must
    # parse as "no timestamp" (bar gets skipped by every caller) rather than
    # AttributeError out of the whole series.
    if not isinstance(bar, dict):
        return None
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
    if credits_exhausted():
        return None
    if not _claim_minute_token():
        logger.info(
            "Twelvedata per-minute rate limit for %s /quote — skipping (retry next cycle)",
            symbol,
        )
        return None
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
            # Defensive coercion field-by-field: one malformed secondary field
            # (a garbage previous_close or timestamp) must degrade to None for
            # THAT field, not discard an otherwise good quote. Only an unusable
            # close (missing / non-numeric / non-positive / NaN) kills it.
            close_f = _safe_float(data.get("close"))
            if close_f is None or close_f <= 0:
                return None
            open_f = _safe_float(data.get("open"))
            pc_f = _safe_float(data.get("previous_close"))
            av_f = _safe_float(data.get("average_volume"))
            ts_f = _safe_float(data.get("timestamp"))
            quote = {
                "c": close_f,
                "o": open_f if open_f is not None and open_f > 0 else close_f,
                "pc": pc_f if pc_f is not None and pc_f > 0 else None,
                "av": int(av_f) if av_f is not None and av_f > 0 else None,
                # Unix seconds of the quote's own data time — lets callers apply
                # the same staleness test as Finnhub's "t" (see
                # price_check._quote_is_stale; a frozen quote must not be
                # treated as a live price).
                "t": int(ts_f) if ts_f is not None and ts_f > 0 else None,
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


@dataclass
class SessionAnalysis:
    """Everything the entry gates need from today's 1-min bars — ONE pull.

    v20 consolidation: momentum baseline, spread proxy, session volume, VWAP
    and session low/high were previously three separate /time_series calls
    (get_momentum_baseline + get_volume_stats + get_session_volume_and_vwap)
    made sequentially per confirmation — 2-3 credits and 2-3 HTTP round-trips
    per signal, re-paid in full by every re-eval retry. All of them are
    derivable from the same 390-bar 1-min series. One pull now feeds every
    gate; the daily series (ADV/prev-close, immutable intraday) is cached per
    day in get_daily_stats. A re-evaluation now costs 1 credit, not 3.
    """
    past_price: float | None        # close of newest TODAY bar ≥ lookback old
    current_bar_price: float | None # close of the newest TODAY bar
    spread_proxy_pct: float | None  # (high−low)/close of the newest bar, %
    session_volume: int | None      # Σ volume of today's bars (current, unlike
                                    # the daily bar's lagging volume field)
    vwap: float | None              # session VWAP (typical price, volume-wtd)
    last_price: float | None        # close of the newest TODAY bar
    session_low: float | None       # min(low) of today's bars
    session_high: float | None      # max(high) of today's bars


def get_session_analysis(symbol: str, fast: bool = False) -> SessionAnalysis | None:
    """
    One 1-min-bars pull (1 credit) → SessionAnalysis for TODAY's session.

    Momentum baseline is selected BY TIMESTAMP among today's bars only: the
    newest bar at least cfg.momentum_lookback_minutes old. Restricting the
    walk to today's session fixes a long-standing subtle bug in the old
    get_momentum_baseline: right after the open, "the newest bar ≥5 min old"
    could be YESTERDAY'S 15:59 bar, silently turning the overnight gap into
    fake 5-minute momentum. If no today-bar is old enough (first minutes of
    the session), past_price is None and the caller falls back to the
    official open price.

    Staleness guard (VECO, 2026-06-05): if the newest today-bar is >10 min
    old, past/current/spread are returned as None — a bar from an hour ago is
    not "momentum". The cumulative session aggregates (volume, VWAP, low,
    high) are still returned: they are true as of the last print, and the
    volume undercount can only make RVOL conservative (a transient
    low_volume reject that self-heals via the re-eval queue).

    session_volume — Σ volume of today's minute bars. v20: this is THE
    primary RVOL numerator. The old daily-bar `today_volume` trailed the live
    session by minutes (worst at the open — ZTS read RVOL 0.07 and AGIO 0.40
    minutes after gapping on real catalysts, 2026-07-07), which forced a
    "rescue" second fetch of exactly this series. Minute bars are current;
    the rescue dance and its failure class are gone. Known caveat: minute-bar
    volume can undercount the consolidated tape, so RVOL reads conservative —
    and low_volume is a TRANSIENT reject, so a false low re-checks in minutes.

    session_low/session_high — min/max of today's bars (v19.5 exhaustion
    gate: LEVI gapped −7.8% and clawed back to +2.3% by entry; endpoint
    measures couldn't see the round trip).

    Returns None when bar data is unavailable or today has no bars yet.
    """
    values = _get_time_series(symbol, interval="1min", outputsize=390, fast=fast)
    if values is None or len(values) < 1:
        return None

    now_utc = datetime.now(timezone.utc)
    now_et_date = datetime.now(_ET).date()

    cum_pv = 0.0   # Σ typical_price × volume
    cum_v = 0.0    # Σ volume
    newest_bar: dict | None = None
    newest_time: datetime | None = None
    baseline_price: float | None = None
    session_low: float | None = None
    session_high: float | None = None
    last_price: float | None = None
    cutoff = now_utc - timedelta(minutes=cfg.momentum_lookback_minutes)

    # values are newest-first; iterate oldest→newest so last_price/newest_bar
    # end on the most recent TODAY bar. Bars from prior sessions are skipped —
    # for the aggregates AND the momentum baseline (see docstring).
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
        cum_pv += ((high + low + close) / 3) * vol
        cum_v += vol
        last_price = close
        session_low = low if session_low is None else min(session_low, low)
        session_high = high if session_high is None else max(session_high, high)
        newest_bar, newest_time = bar, bar_dt
        # Oldest→newest walk: keep overwriting until we pass the cutoff, so
        # this ends on the NEWEST bar that is still ≥ lookback old.
        if bar_dt <= cutoff:
            baseline_price = close

    if newest_bar is None:
        logger.debug("Twelvedata [%s]: no bars for today's session yet", symbol)
        return None

    vwap = (cum_pv / cum_v) if cum_v > 0 else None

    # ── Momentum staleness guard ─────────────────────────────────────────────
    current_bar_price: float | None = last_price
    spread_proxy_pct: float | None = None
    age_minutes = (now_utc - newest_time).total_seconds() / 60
    if age_minutes > 10:
        logger.warning(
            "Twelvedata: stale bar for %s — newest today-bar is %.1f min old "
            "(bar_time=%s); momentum unavailable, session aggregates kept",
            symbol, age_minutes, newest_bar.get("datetime", "?"),
        )
        baseline_price = None
        current_bar_price = None
    else:
        try:
            hi = float(newest_bar.get("high", last_price))
            lo = float(newest_bar.get("low", last_price))
            if last_price and last_price > 0:
                spread_proxy_pct = ((hi - lo) / last_price) * 100
        except (ValueError, TypeError):
            spread_proxy_pct = None

    return SessionAnalysis(
        past_price=baseline_price,
        current_bar_price=current_bar_price,
        spread_proxy_pct=spread_proxy_pct,
        session_volume=int(cum_v),
        vwap=vwap,
        last_price=last_price,
        session_low=session_low,
        session_high=session_high,
    )


# ── Daily stats (ADV / prev close) — cached per symbol per day ───────────────
# ADV, dollar-ADV and the previous close come from COMPLETED sessions: they
# cannot change intraday, so re-fetching them on every confirmation attempt
# (the old get_volume_stats call) was pure waste — a re-evaluating signal
# re-paid 1 credit + 1 HTTP round-trip per retry for numbers that were
# identical all day. Successful results are cached until the ET date rolls.
_daily_stats_cache: dict[tuple[str, str], tuple[int, float | None, float | None]] = {}
_daily_stats_lock = threading.Lock()


def get_daily_stats(
    symbol: str, fast: bool = False
) -> tuple[int, float | None, float | None] | None:
    """
    (avg_daily_volume, avg_dollar_volume, prev_close) for `symbol`, cached per
    ET calendar day. Fetches 21 daily bars on the first call of the day
    (1 credit); every later call for the same symbol is free.

    avg_daily_volume  — 20-day average share volume (completed sessions only;
                        today's partial bar, when present, is excluded).
    avg_dollar_volume — ADV × most recent completed close. THE liquidity
                        metric: normal book depth, immune to spike-day
                        volume inflation.
    prev_close        — most recent completed session's close (gap baseline
                        backup when the quote's `pc` is missing).

    Returns None if data is unavailable (failures are NOT cached — the next
    attempt retries the fetch).
    """
    key = (symbol, datetime.now(_ET).date().isoformat())
    with _daily_stats_lock:
        if key in _daily_stats_cache:
            return _daily_stats_cache[key]
        # Drop entries from prior days so the cache can't grow unbounded.
        for stale in [k for k in _daily_stats_cache if k[1] != key[1]]:
            _daily_stats_cache.pop(stale, None)

    values = _get_time_series(symbol, interval="1day", outputsize=21, fast=fast)
    if values is None or len(values) < 2:
        return None

    try:
        today_bar_time = _parse_bar_time(values[0])
        today_et = datetime.now(_ET).date()
        # Exclude today's partial bar from the ADV window when it has rolled.
        prior_bars = (
            values[1:]
            if today_bar_time is not None
            and today_bar_time.astimezone(_ET).date() == today_et
            else values
        )

        prior_volumes = [int(float(b.get("volume", 0))) for b in prior_bars if b.get("volume")]
        avg_daily_volume = int(sum(prior_volumes) / len(prior_volumes)) if prior_volumes else 0
        prev_close = float(prior_bars[0].get("close", 0)) or None
        avg_dollar_volume = (
            avg_daily_volume * prev_close
            if avg_daily_volume > 0 and prev_close else None
        )
    # AttributeError included: a scalar smuggled into the bar list raises it
    # from `.get` — same malformed-payload class, same fail-closed answer.
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        logger.warning("Twelvedata: malformed daily bar data for %s: %s", symbol, exc)
        return None

    result = (avg_daily_volume, avg_dollar_volume, prev_close)
    with _daily_stats_lock:
        _daily_stats_cache[key] = result
    logger.debug(
        "Twelvedata daily stats [%s]: avg20d=%d adv$=%.0f prev_close=%s (cached for today)",
        symbol, avg_daily_volume, avg_dollar_volume or 0, prev_close,
    )
    return result
