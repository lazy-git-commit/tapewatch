"""
market/price_check.py
──────────────────────
Fetches live price data and checks whether a news signal is confirmed by actual
price movement and elevated volume. This is the last gate before money moves.

Price sources:
  - Current price       — Finnhub REST quote (real-time, <1s latency, retried)
  - Previous close      — Finnhub quote `pc` field (cross-checked w/ Twelvedata)
  - Momentum baseline   — Twelvedata 1-min bars, selected by TIMESTAMP
  - Volume stats        — Twelvedata 1-day bars (20-day ADV)

A signal is confirmed when ALL of the following hold (in evaluation order —
cheapest checks first so we fail fast and spend fewer API credits):

  0. stale_price     — the quote backing the decision must be no older than
                       cfg.max_entry_quote_age_seconds. Runs FIRST because a
                       lagging quote doesn't make the gates below fail, it
                       makes them AGREE — on a market that has moved on
                       (NVT 2026-07-31: a 3-min-old $167.37 confirmed momentum,
                       RVOL and VWAP while the tape traded $165.50 and fell;
                       stopped out 42s after the fill). TRANSIENT.
  1. opening_block   — at least cfg.open_block_minutes after the open. The
                       opening auction produces violent noise (GOAI: entire
                       spike was in the 09:30 bar; bought 09:32 into collapse).
  2. penny_stock     — price >= cfg.min_stock_price. Sub-$5 names carry
                       outsized spread %, halt frequency, and manipulation
                       risk; every Jun 8–11 loss was sub-$5.
  3. wide_spread     — latest 1-min bar range (high−low)/close must be under
                       cfg.max_spread_pct. Bar-range proxy for effective
                       spread (no bid/ask feed available on our data plan).
  4. dead_cat        — not down more than cfg.max_day_drop_pct vs the PREVIOUS
                       CLOSE. Prev close (not today's open) so overnight
                       gap-downs are caught: a stock that gapped −25% and is
                       flat since open is still a falling knife.
  5. extended_move   — not UP more than cfg.max_day_move_pct vs prev close.
                       A stock already up 30%+ on the day has paid out its
                       catalyst; late articles on it are recaps. Closes the
                       hole where a stock up 80% on the day but flat in the
                       last 5 min passed the 5-min momentum ceiling.
  6. illiquid        — 20-day ADV × price >= cfg.min_daily_dollar_volume.
                       ADV-based on purpose: spike-day volume would let halt
                       patterns through (GOAI: $390k ADV → −18.99% stop fill).
  7. low_momentum    — up at least cfg.min_price_move_pct over the momentum
                       look-back window. v15: this is now only a DEAD-TAPE
                       noise floor (default 0.2%); the real accumulation
                       judgement is VWAP (step 10), which is size-neutral.
  8. high_momentum   — but NOT up more than cfg.max_price_move_pct in that
                       window — that is a post-halt spike, not an entry.
                       Runs before VWAP so spikes don't waste a VWAP credit.
  5.5 stale_volume   — RVOL and the day move come from different sources, so
                       when they disagree hard (a big day move on near-zero
                       relative volume) the VOLUME side is lagging, not calm.
                       Defer instead of letting the size-neutral bypass below
                       excuse a reading that was never real (NVT 2026-07-31:
                       +15.59% on the day, RVOL 0.28, while the first minute
                       alone traded ~10% of an average day). TRANSIENT.
  9. low_volume /    — RVOL (time-of-day normalized relative volume) within
     high_volume       [cfg.min_rvol, cfg.max_rvol]. See _expected_volume_
                       fraction() for why raw volume ratios are meaningless
                       without time normalization. FLOOR ONLY has a
                       size-neutral bypass (v20.2): ADV$ >= cfg.
                       rvol_bypass_min_adv_dollar + a held VWAP substitutes
                       for RVOL — a mega-cap's normal book is already huge
                       in dollar terms, so it doesn't need anomalous
                       RELATIVE volume to make a real move (BMY 2026-07-13:
                       +2.1% all session, RVOL never exceeded 0.3, held VWAP
                       throughout, rejected on all 27 re-eval cycles because
                       this gate ran before step 10 ever got a look). The
                       ceiling has no bypass — parabolic volume is the
                       halt-pattern signature regardless of cap size.
 10. below_vwap      — price must hold at/above session VWAP (cfg.
                       require_vwap_confirmation). SIZE-NEUTRAL accumulation
                       test: a deep-book large-cap reprices <1% in 5 min but
                       holds above VWAP when institutions buy; a fading
                       gap-up sits below VWAP regardless of % change. This is
                       the v15 fix for the fixed-% momentum floor rejecting
                       every real large-cap catalyst.
 10.2 overextended   — but NOT more than cfg.max_vwap_extension_pct ABOVE
                       VWAP either: with the stop cfg.stop_loss_pct below
                       entry, an entry further above value than the stop is
                       wide means a routine reversion-to-VWAP stops it out by
                       construction (LEVI +1.9% / CRCL +2.2% above VWAP with
                       a 2% stop — both dead on arrival, 2026-07-09/10).
                       TRANSIENT: the re-eval queue re-checks, so this
                       converts "chase the vertical move now" into "enter on
                       the first pullback into value".
 10.5 exhausted_bounce — price must not have already recovered most of a
                       large intraday round trip (v19.5, LEVI).

Data plan (v20): ONE Twelvedata 1-min session pull feeds momentum, spread,
RVOL, VWAP, extension and exhaustion; the daily series (ADV/prev close) is
fetched once per symbol per day and cached. Steps 1-2 run before any bar
call; a typical re-evaluation costs 1 credit.
"""

import logging
import time
import requests
import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timezone
from dataclasses import dataclass
import pytz
from config.settings import cfg
from market.finnhub_bars import get_finnhub_quote
from market.sessions import (
    get_trading_session, session_bounds, minutes_until_session_end,
    EXTENDED_SESSIONS, REGULAR, AFTERHOURS, PREMARKET,
)
from market.twelvedata_bars import (
    get_twelvedata_quote, get_session_analysis, get_daily_stats,
)
from trading.executor import t212_to_symbol

_NYSE = mcal.get_calendar("NYSE")

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MARKET_OPEN = (9, 30)   # 09:30 ET
_MARKET_CLOSE = (16, 0)  # 16:00 ET


# ── Intraday volume curve ─────────────────────────────────────────────────────
# Equity volume is U-shaped: heavy at the open, dead at lunch, heavy at the
# close. These anchor points give the typical cumulative fraction of a full
# day's volume traded by each time of day (ET); linearly interpolated between
# anchors.
#
# Why it matters: "today's volume >= 1.5× the 20-day FULL-DAY average" is
# nearly impossible at 10:00 and trivially true at 15:45. Normalizing by this
# curve makes the RVOL floor and ceiling mean the same thing all session long.
#
# The 0–150 min anchors were recalibrated 2026-07-08: the original curve
# (textbook big-cap-open-auction shape: 16% traded by minute 30) assumed a
# front-loaded ramp this system's actual catalyst population — small/mid-cap
# names reacting to a news wire, not S&P 500 constituents with pre-positioned
# open-auction flow — doesn't show. Measured directly against real volume on
# 2026-07-08 (BZH, JNJ, CACI, ARQT — a mix of gap-and-go and quiet names): the
# true fraction traded by minute 30 ran 1–4%, not 16%, a 4–14× mismatch that
# pinned RVOL near-zero for the entire pre-market eval window regardless of
# whether the stock was genuinely trading well (BZH finished the day at 4×
# normal volume and was STILL reading RVOL ~0.3 at minute 29). The new anchors
# are a conservative partial correction (roughly 3x less aggressive through
# minute 90, reconverging with the original curve by minute 150 where there
# is no contradicting evidence) — a first-pass empirical fit from a single
# day's data, not a fully validated model. Revisit with more days of measured
# today_volume-vs-avg_daily_volume data as it accumulates.
_VOLUME_CURVE: list[tuple[float, float]] = [
    # (minutes since 09:30 open, cumulative fraction of typical daily volume)
    (0,    0.00),
    (5,    0.015),
    (15,   0.03),
    (30,   0.05),
    (60,   0.11),
    (90,   0.18),
    (150,  0.30),
    (210,  0.42),
    (270,  0.55),
    (330,  0.70),
    (360,  0.80),
    (380,  0.88),
    (390,  1.00),
]


def _expected_volume_fraction(minutes_since_open: float) -> float:
    """
    Return the typical cumulative fraction of a day's volume traded by
    `minutes_since_open` (linear interpolation over _VOLUME_CURVE).

    Floored at 0.04 so RVOL doesn't divide by a near-zero denominator in the
    first minutes after the open (which would make every stock look like 50×).
    """
    m = max(0.0, min(390.0, minutes_since_open))
    for (m0, f0), (m1, f1) in zip(_VOLUME_CURVE, _VOLUME_CURVE[1:]):
        if m0 <= m <= m1:
            # Linear interpolation between the two anchors
            frac = f0 + (f1 - f0) * ((m - m0) / (m1 - m0)) if m1 > m0 else f0
            return max(0.04, frac)
    return 1.0


def compute_rvol(today_volume: int, avg_daily_volume: int, minutes_since_open: float) -> float:
    """
    Time-of-day normalized relative volume:
      rvol = today's cumulative volume
             / (20-day ADV × expected fraction traded by this time of day)

    rvol == 1.0 means "trading exactly like a normal day so far".
    rvol >= cfg.min_rvol confirms genuine participation behind the move;
    rvol >  cfg.max_rvol is the parabolic halt-pattern signature.
    """
    if avg_daily_volume <= 0:
        return 0.0
    expected = avg_daily_volume * _expected_volume_fraction(minutes_since_open)
    return today_volume / expected if expected > 0 else 0.0


def _to_yf_ticker(t212_ticker: str) -> str:
    """
    Convert a T212 code to the exchange symbol Finnhub/Twelvedata understand.

    Delegates to the instrument map's inverse (trading.executor.t212_to_symbol):
    T212 re-uses historical symbols by appending a digit to its own code
    (Firefly Aerospace: exchange symbol "FLY", T212 code "FLY1_US_EQ"), so the
    old suffix-strip derivation produced symbols no data API knows — those
    signals could never be priced and silently expired (observed 2026-07-07:
    two FLY candidates on a $13M NASA contract died as "no coverage").
    """
    return t212_to_symbol(t212_ticker)


def get_quote_with_fallback(
    symbol: str, fast: bool = False, prefer_fresher_than: float | None = None,
) -> dict | None:
    """
    Real-time quote with a two-source fallback chain:
      1. Finnhub /quote  — primary (fastest, generous rate limit)
      2. Twelvedata /quote — fallback when Finnhub has no coverage

    `fast` (default False) propagates to BOTH sources so time-boxed callers
    (the pre-market eval window) never block on retry backoff — one attempt
    per source, no sleeps. RTH callers keep full retries.

    Finnhub's free tier silently omits many small caps and recent IPOs
    (observed 2026-06-15: CUPR/ELAN/WBD/INBX/SAIL all returned no Finnhub
    quote — exactly the small-cap catalysts this strategy targets — while
    Twelvedata carried every one). Returning None from BOTH means the symbol
    is genuinely unpriceable and the signal can't be evaluated.

    Both sources return the same normalised keys (c/o/pc), so callers don't
    need to know which one answered.

    `prefer_fresher_than` (seconds, v21.11) is a SOFT preference, not a filter:
    when the primary's quote is older than this, the fallback is consulted and
    the fresher of the two wins. It never causes None to be returned — deciding
    that a quote is too stale to ACT on belongs to the caller (see
    confirm_price_signal's stale_price gate), because "unusable for an entry"
    and "this ticker has no coverage" must not collapse into the same outcome:
    the latter burns a no-quote strike toward a session blacklist.

    `pc` (previous close) gets special treatment: Finnhub's free tier routinely
    returns pc=0 (or a stale pc) in the first minutes after the open, before its
    daily rollover settles (observed 2026-06-16: OTLK/SLP/SPCB all had a valid
    Finnhub price but pc=0 at 09:30 ET, which terminally rejected every premarket
    candidate as "no prev close"). Finnhub being non-None is therefore NOT enough
    to trust its pc — when pc is missing we backfill it from Twelvedata rather
    than abandoning Finnhub's (otherwise good) real-time price. This is a cheap
    one-credit call only on the names that need it.
    """
    quote = get_finnhub_quote(symbol, fast=fast)
    if quote is not None and _quote_is_stale(symbol, quote, "Finnhub"):
        quote = None  # fall through to Twelvedata exactly as if uncovered
    if quote is not None:
        # v21.11: Finnhub answered and is inside the COVERAGE window, but the
        # caller needs a tighter freshness bar (an entry decision). Give
        # Twelvedata a chance rather than rejecting outright — on 2026-07-31
        # the two providers lagged by different amounts, so the fallback is
        # worth consulting even when the primary technically "answered".
        if _below_freshness_bar(quote, prefer_fresher_than):
            td = get_twelvedata_quote(symbol, fast=fast)
            if td is not None and not _quote_is_stale(symbol, td, "Twelvedata"):
                fh_age, td_age = quote_age_seconds(quote), quote_age_seconds(td)
                if td_age is not None and (fh_age is None or td_age < fh_age):
                    logger.info(
                        "Quote [%s]: Finnhub quote %.0fs old is behind the "
                        "%.0fs entry bar — using fresher Twelvedata quote (%.0fs)",
                        symbol, fh_age or -1, prefer_fresher_than, td_age,
                    )
                    if not (float(td.get("pc") or 0) > 0) and float(quote.get("pc") or 0) > 0:
                        td["pc"] = quote["pc"]   # keep Finnhub's prev close
                    return td
        if not (float(quote.get("pc") or 0) > 0):
            td = get_twelvedata_quote(symbol, fast=fast)
            td_pc = float(td.get("pc") or 0) if td else 0
            if td_pc > 0:
                logger.info(
                    "Quote [%s]: Finnhub pc missing (%.4f) — backfilled prev close "
                    "%.4f from Twelvedata", symbol, float(quote.get("pc") or 0), td_pc,
                )
                quote["pc"] = td_pc
        return quote
    logger.info("Quote [%s]: no Finnhub coverage — trying Twelvedata fallback", symbol)
    td_quote = get_twelvedata_quote(symbol, fast=fast)
    if td_quote is not None and _quote_is_stale(symbol, td_quote, "Twelvedata"):
        return None
    return td_quote


# Maximum age of a quote's own data timestamp before we refuse to treat it as
# a live price. 20 minutes is far outside any liquid name's trade gap during
# RTH, but tolerates thin-but-real tapes just after the opening block.
# Why this exists (2026-07-07, GLASF): Finnhub served a $12.50 last-print all
# afternoon while the real market traded ~$11.50. The frozen quote (a)
# manufactured +2% "momentum" that confirmed the entry, (b) made the monitor
# believe a losing position was +6% up, and (c) priced every exit limit above
# the real book — 459 consecutive unfilled sells until the EOD market flatten.
# A quote that hasn't updated in 20 minutes is not a price, it's a memory.
_QUOTE_MAX_AGE_SECONDS = 20 * 60


# ── Volume plausibility cross-check (v21.11) ─────────────────────────────────
# Thresholds for "the volume feed disagrees with the price feed so hard that
# one of them must be lagging". Deliberately conservative so the size-neutral
# RVOL bypass keeps working for its actual purpose — a genuinely quiet
# mega-cap grinding up on ordinary volume (BMY 2026-07-13: +2.1% on the day,
# RVOL ~0.3) stays well inside the day-move threshold and is untouched.
# Only a LARGE move on near-zero relative volume is flagged, because that
# combination is not a market state, it is a data state.
_VOLUME_PLAUSIBILITY_DAY_MOVE_PCT = 5.0   # |day change| at/above this...
_VOLUME_PLAUSIBILITY_MIN_RVOL = 0.5       # ...must not come with RVOL below this


# ── Frozen-feed tripwire (v21.10) ────────────────────────────────────────────
# The staleness check below correctly REFUSES a frozen quote, but each refusal
# was only ever an isolated per-symbol WARNING. On 2026-07-30 Twelvedata served
# a quote frozen at 14:30 ET for 71+ minutes — 23 refusals across the session,
# while a position was open and its take-profit was being polled — and nothing
# counted them, so the operator had no signal that a price feed had died.
# A single stale symbol can be an illiquid name; a STREAK of them (with no
# fresh quote in between) is the provider's cache, not the tape.
#
# Like the Finnhub outage latch, the alert fires at most ONCE PER SOURCE per
# process: system_events already de-dupes to one row per day, and this sits on
# the monitor's 5s price path where record_system_event -> get_conn() can
# retry-with-backoff if the DB is also degraded. One attempt per source keeps
# a database problem from ever slowing the price-fetch loop.
_STALE_QUOTE_ALERT_THRESHOLD = 10

# v21.12 — a streak alone is NOT enough: it must span several DISTINCT symbols.
# 2026-08-04: MZDAY (Mazda's OTC ADR, a security so thin its entire day spanned
# $3.38-$3.55) sat in the re-eval queue and was polled 221 times. Ten of those
# in a row tripped this detector on BOTH providers — Twelvedata at 09:33 ET,
# Finnhub at 09:51 — while the feeds were perfectly healthy: BE was quoted
# correctly two minutes later and traded normally all session.
#
# The false alarm is not merely noise, it closes a loop. quote_feed_degraded()
# suppresses the no-quote strikes that would eventually blacklist a dead ticker
# (deliberately — that is the whole v21.11 fix). So MZDAY tripped the alarm,
# the alarm protected MZDAY from being blacklisted, and MZDAY kept being polled
# to trip the alarm again. All day.
#
# Requiring N distinct symbols keeps the original protection intact: on
# 2026-07-31 the real outage served the previous day's close for EVERY symbol
# asked, SONY included, so it clears a distinct-symbol bar trivially. One dead
# ticker polled in a loop never can.
_STALE_QUOTE_MIN_DISTINCT_SYMBOLS = 3

_stale_quote_streak: dict[str, int] = {}
_stale_quote_symbols: dict[str, set[str]] = {}
_stale_quote_reported: set[str] = set()


def _feed_looks_frozen(source: str) -> bool:
    """A long stale streak spanning enough distinct symbols to be the provider."""
    return (
        _stale_quote_streak.get(source, 0) >= _STALE_QUOTE_ALERT_THRESHOLD
        and len(_stale_quote_symbols.get(source, ())) >= _STALE_QUOTE_MIN_DISTINCT_SYMBOLS
    )


def _note_quote_fresh(source: str) -> None:
    """A usable, current quote arrived — the feed is alive."""
    if _stale_quote_streak.get(source):
        _stale_quote_streak[source] = 0
    # Clear the symbol set with the streak, or a provider that alternates
    # fresh/stale readings would accumulate distinct symbols indefinitely and
    # eventually clear the bar without ever having been frozen.
    if _stale_quote_symbols.get(source):
        _stale_quote_symbols[source] = set()


def _note_quote_stale(source: str, symbol: str, age_minutes: float) -> None:
    """Count a stale reading and shout once when the streak looks systemic."""
    streak = _stale_quote_streak.get(source, 0) + 1
    _stale_quote_streak[source] = streak
    _stale_quote_symbols.setdefault(source, set()).add(symbol)
    if not _feed_looks_frozen(source) or source in _stale_quote_reported:
        return
    _stale_quote_reported.add(source)
    distinct = len(_stale_quote_symbols.get(source, ()))
    logger.error(
        "%s has served %d consecutive STALE quotes across %d DISTINCT symbols "
        "with no fresh one in between (most recent: %s, %.0f min old) — this is "
        "a frozen provider feed, not a quiet ticker. Price-dependent exits "
        "(polled take-profit, polled stop) are degraded until it recovers.",
        source, streak, distinct, symbol, age_minutes,
    )
    try:
        from storage.database import record_system_event
        record_system_event(
            "stale_quote_feed",
            f"{source}: {streak} consecutive stale quotes across {distinct} "
            f"distinct symbols (most recent {symbol}, {age_minutes:.0f} min old)",
        )
    except Exception as exc:
        logger.debug("Could not record stale_quote_feed system_event: %s", exc)


def quote_feed_degraded() -> bool:
    """
    True while ANY quote source is in a live stale streak long enough to look
    like a frozen provider feed rather than a quiet ticker.

    Unlike the once-per-process alert latch above, this reads the CURRENT
    streak — `_note_quote_fresh()` zeroes it the moment a usable quote
    arrives — so it answers "are the feeds degraded right now?".

    Callers use it to avoid punishing a TICKER for a PROVIDER's outage: on
    2026-07-31 both feeds froze at the open and two liquid names (GTES,
    IRMD) were blacklisted for the session as "no coverage" while Finnhub
    was serving the previous day's close for every symbol it was asked
    about, SONY included.

    v21.12: the streak must also span _STALE_QUOTE_MIN_DISTINCT_SYMBOLS —
    otherwise ONE dead ticker polled in a loop declares the provider frozen and
    thereby protects itself from ever being blacklisted (MZDAY, 2026-08-04).
    """
    return any(_feed_looks_frozen(source) for source in _stale_quote_streak)


def quote_age_seconds(quote: dict) -> float | None:
    """Age of a quote's own data timestamp in seconds, or None when it carries
    no usable timestamp (which is NOT the same as "fresh" — callers decide)."""
    ts = quote.get("t") if quote else None
    if not ts:
        return None
    try:
        return time.time() - float(ts)
    except (TypeError, ValueError):
        return None


def _below_freshness_bar(quote: dict, bar_seconds: float | None) -> bool:
    """True when a freshness bar is set and this quote is provably older."""
    if not bar_seconds:
        return False
    age = quote_age_seconds(quote)
    return age is not None and age > bar_seconds


def _quote_is_stale(symbol: str, quote: dict, source: str) -> bool:
    """True when the quote carries a data timestamp older than the max age.

    Quotes without a usable timestamp are NOT treated as stale (fail-open on
    missing metadata — the other gates still apply); the check only fires on
    positive evidence of staleness.
    """
    age = quote_age_seconds(quote)
    if age is None:
        return False
    ts = quote.get("t")
    if age > _QUOTE_MAX_AGE_SECONDS:
        logger.warning(
            "Quote [%s]: %s quote is %.0f min old (last update %s) — treating "
            "as no coverage, not a live price",
            symbol, source, age / 60,
            datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S"),
        )
        _note_quote_stale(source, symbol, age / 60)
        return True
    _note_quote_fresh(source)
    return False


def minutes_until_close() -> float | None:
    """
    Minutes until today's actual market close (handles early-close days via
    the exchange calendar). Returns None outside a trading session or if the
    calendar lookup fails. Used by the position monitor's EOD flatten.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return None
        close_utc = sched.iloc[0]["market_close"]
        delta = (close_utc - now_utc).total_seconds() / 60
        return delta if delta > 0 else None
    except Exception as exc:
        logger.warning("minutes_until_close: calendar lookup failed: %s", exc)
        return None


def is_too_late_to_buy(session: str = REGULAR) -> bool:
    """
    Return True if it is too close to the current session's hard exit
    boundary to open a new position.

    regular    — within ENTRY_CUTOFF_MINUTES of today's close (calendar-aware,
                 so early-close days are handled correctly).
    afterhours — within ENTRY_CUTOFF_MINUTES of the extended flatten time
                 (session end − EXTENDED_FLATTEN_BUFFER_MINUTES): a position
                 opened later could neither develop before the flatten nor be
                 exited on a venue we can see (overnight = Blue Ocean, no data).

    ENTRY_CUTOFF_MINUTES is decoupled from the hold (TIME_STOP_MINUTES) as of
    2026-07-20 — see config.settings.entry_cutoff_minutes. It defaults to the
    time-stop value, so behavior is unchanged unless it is set explicitly.
    premarket  — never too late: positions carry into RTH, where the time
                 stop and the EOD flatten manage them normally.

    Returns False outside the named session — the session gate in
    main.news_cycle handles that.
    """
    if session == PREMARKET:
        return False
    if session == AFTERHOURS:
        mins_left = minutes_until_session_end(AFTERHOURS)
        if mins_left is None:
            return False
        tradeable_left = mins_left - cfg.extended_flatten_buffer_minutes
        return tradeable_left <= cfg.entry_cutoff_minutes
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        close_utc = sched.iloc[0]["market_close"]
        minutes_to_close = (close_utc - now_utc).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.entry_cutoff_minutes
    except Exception:
        now_et = datetime.now(_ET)
        close_et = now_et.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
        minutes_to_close = (close_et - now_et).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.entry_cutoff_minutes


def is_market_open() -> bool:
    """
    Check whether the NYSE is currently open using pandas_market_calendars
    as the authoritative local source (handles holidays, early closes, weekends).

    Uses open_at_time() rather than a manual schedule-row comparison. The manual
    approach (market_open <= now_utc < market_close) is fragile: if the
    long-running pmc _NYSE object has stale internal DST state (observed
    2026-06-17: service running since midnight treated EDT open as EST open,
    delaying market detection by 60 min and expiring all pre-market candidates),
    the comparison silently returns False for the entire first hour of trading.
    open_at_time() re-derives the open/close from first principles each call.

    Falls back to a Finnhub API check if the calendar check fails for any reason.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        return bool(_NYSE.open_at_time(sched, now_utc))
    except ValueError as exc:
        # open_at_time() raises ValueError for two distinct reasons:
        #   (a) timestamp outside session window — means not open, return False.
        #   (b) schedule column validation failure (schema/version mismatch) —
        #       a programming error that should fall through to the Finnhub
        #       fallback rather than silently reporting "market closed".
        if "not covered by the schedule" in str(exc):
            return False
        logger.warning("Calendar open check failed (unexpected ValueError): %s — falling back to Finnhub", exc)
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
    ticker: str                     # T212 instrument code, e.g. AAPL_US_EQ
    symbol: str                     # exchange symbol, e.g. AAPL
    current_price: float
    open_price: float
    prev_close: float | None        # previous session close (gap baseline)
    day_move_pct: float             # vs today's open (informational/logging)
    day_change_pct: float | None    # vs PREVIOUS CLOSE — used by filters
    recent_move_pct: float          # vs momentum baseline (~5 min ago)
    current_volume: int
    avg_volume: int
    rvol: float                     # time-of-day normalized relative volume
    avg_dollar_volume: float | None # 20-day ADV × price (liquidity metric)
    spread_proxy_pct: float | None  # latest bar (high−low)/close, %
    is_confirmed: bool
    reason: str
    # approved | opening_block | penny_stock | wide_spread | dead_cat |
    # extended_move | illiquid | low_momentum | high_momentum |
    # low_volume | high_volume | below_vwap | overextended |
    # exhausted_bounce | insufficient_data | no_price_data
    reason_code: str
    vwap: float | None = None       # session VWAP at confirmation (v15)
    session: str = REGULAR          # trading session at confirmation (v21) —
                                    # drives extended-hours order routing,
                                    # half-sizing and the no-resting-stop
                                    # regime downstream


def _reject(base: dict, code: str, reason: str) -> "PriceConfirmation":
    """Build a rejected PriceConfirmation and log it uniformly."""
    logger.info(
        "Price check [%s]: recent=%+.2f%% day_chg=%s rvol=%.1f adv$=%s — rejected: %s (%s)",
        base["symbol"], base.get("recent_move_pct", 0.0),
        f"{base['day_change_pct']:+.2f}%" if base.get("day_change_pct") is not None else "n/a",
        base.get("rvol", 0.0),
        f"${base['avg_dollar_volume']:,.0f}" if base.get("avg_dollar_volume") else "n/a",
        code, reason,
    )
    return PriceConfirmation(**base, is_confirmed=False, reason=reason, reason_code=code)


def confirm_price_signal(t212_ticker: str, fast: bool = False) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing active, tradeable upward momentum
    that corroborates a bullish news signal.

    Returns None only when a hard data failure makes it impossible to evaluate
    the signal (Finnhub down, Twelvedata down). Confirmed/rejected signals
    are returned as PriceConfirmation with is_confirmed set accordingly.

    `fast` (default False) is for the time-boxed pre-market eval window: it makes
    EVERY Twelvedata call non-blocking (no retry backoff) — the quote fallback/
    backfill AND the momentum/volume/VWAP bar pulls. Before 2026-06-23 only the
    quote was fast while the three bar calls still did the full 3+6+9s backoff,
    so a single slow ticker could consume the scanner's 30s wall-clock budget and
    starve the rest of the watchlist (the candidates simply expired unevaluated —
    26 of 37 on 2026-06-23). Fast misses return None and are retried next cycle.

    When Twelvedata's daily credit budget is exhausted, the bar calls return None
    without any HTTP request (see twelvedata_bars.credits_exhausted): this
    function then returns None ("can't evaluate"), the signal is parked/retried,
    and — because confirmation is impossible without bars — no trade is opened.
    Failing closed here is correct: Finnhub gives a quote but never the momentum/
    RVOL/VWAP/liquidity bars the gates need, so there is no safe way to trade
    blind. The system keeps scoring news regardless (eval loop continues).
    """
    symbol = _to_yf_ticker(t212_ticker)

    # ── Session context (v21) ─────────────────────────────────────────────────
    # Extended sessions (premarket/afterhours) run a stricter gate variant:
    # session-anchored bars, no RVOL band (RTH curve doesn't apply), higher
    # liquidity floor, tighter spread. Overnight never reaches this function
    # (main.news_cycle gates on session), but if it somehow did, the anchored
    # bar pull would return nothing and the signal would defer — fail closed.
    session = get_trading_session()
    extended = session in EXTENDED_SESSIONS
    bounds = session_bounds(session) if extended else None
    anchor_utc = bounds[0] if bounds else None

    try:
        # ── Current price + previous close (Finnhub → Twelvedata fallback) ──
        quote = get_quote_with_fallback(
            symbol, fast=fast, prefer_fresher_than=cfg.max_entry_quote_age_seconds,
        )
        if quote is None:
            logger.warning(
                "Price check [%s]: no quote from Finnhub or Twelvedata — cannot evaluate signal",
                symbol,
            )
            return None

        current_price = float(quote["c"])
        open_price = float(quote["o"]) if quote.get("o") else current_price
        if open_price == 0:
            open_price = current_price
        # `pc` = previous close (from whichever quote source answered —
        # Finnhub or the Twelvedata fallback both populate this key). The
        # baseline for gap/day-change math: using today's open instead
        # silently ignores overnight gaps in both directions (the dead-cat hole).
        quote_prev_close = float(quote.get("pc") or 0) or None
        day_move_pct = ((current_price - open_price) / open_price) * 100

        # ── Time since session start ─────────────────────────────────────────
        now_et = datetime.now(_ET)
        if extended and anchor_utc is not None:
            minutes_since_open = (
                datetime.now(timezone.utc) - anchor_utc
            ).total_seconds() / 60
        else:
            market_open_et = now_et.replace(
                hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0
            )
            minutes_since_open = (now_et - market_open_et).total_seconds() / 60

        # Template for every PriceConfirmation built before later data arrives.
        base = dict(
            ticker=t212_ticker, symbol=symbol,
            current_price=current_price, open_price=open_price,
            prev_close=quote_prev_close,
            day_move_pct=day_move_pct, day_change_pct=None,
            recent_move_pct=0.0, current_volume=0, avg_volume=0,
            rvol=0.0, avg_dollar_volume=None, spread_proxy_pct=None,
            session=session,
        )

        # ── 0. Entry-price freshness (v21.11) ────────────────────────────────
        # Runs FIRST because it invalidates everything downstream: current
        # price, the day-move maths, and (via the bar pull that follows) the
        # momentum/RVOL/VWAP readings all describe whatever moment this quote
        # actually belongs to. A lagging quote does not make the gates fail —
        # it makes them agree, on a market that no longer exists.
        #
        # TRANSIENT: a feed running behind catches up within minutes, so this
        # goes to the re-eval queue rather than discarding the signal. It is
        # deliberately NOT a None return: "coverage exists but is lagging" must
        # not be recorded as "this ticker is unpriceable", which strikes the
        # ticker toward a session-long blacklist (2026-07-31, GTES/IRMD).
        # RTH only. In an extended session the quote is EXPECTED to lag (the
        # 16:00 official close is served for minutes into after-hours) and the
        # block below deliberately substitutes the fresher anchored bar close
        # as "now" — so an age test on the quote here would reject exactly the
        # signals that substitution exists to rescue. In regular hours no such
        # substitution happens: current_price IS the quote, so its age is the
        # age of the price we would trade on.
        entry_quote_age = quote_age_seconds(quote)
        if (
            not extended
            and entry_quote_age is not None
            and entry_quote_age > cfg.max_entry_quote_age_seconds
        ):
            return _reject(
                base, "stale_price",
                f"Entry-price freshness: quote is {entry_quote_age:.0f}s old "
                f"(max {cfg.max_entry_quote_age_seconds}s for an entry) — every "
                f"gate below would be computed from a market that has already "
                f"moved on",
            )

        # ── 1. Opening block ─────────────────────────────────────────────────
        # Costs nothing to check; rejects before any Twelvedata credit is spent.
        # Applies at every session boundary: the 09:30 opening auction AND the
        # first minutes after 16:00, where closing-auction unwind and MOC spill
        # print noise that looks like momentum.
        if minutes_since_open < cfg.open_block_minutes:
            return _reject(
                base, "opening_block",
                f"Session-start block: {minutes_since_open:.1f} min since "
                f"{session} session start (block lasts {cfg.open_block_minutes} "
                f"min to avoid auction/boundary noise)",
            )

        # ── 2. Penny stock floor ─────────────────────────────────────────────
        if current_price < cfg.min_stock_price:
            return _reject(
                base, "penny_stock",
                f"Penny stock filter: price ${current_price:.4f} "
                f"< ${cfg.min_stock_price:.2f} minimum — spread/halt/manipulation risk",
            )

        # ── Session analysis (ONE Twelvedata 1-min pull — v20) ───────────────
        # Momentum baseline, spread proxy, session volume, VWAP and session
        # low/high all come from the same 390-bar series. The old plan fetched
        # them across three sequential calls (2-3 credits + round trips per
        # confirmation, re-paid on every re-eval retry).
        # v21: extended sessions pull prepost bars ANCHORED at the session
        # start, so every aggregate measures the post-boundary regime only —
        # for an after-hours catalyst the accumulation test runs against the
        # after-hours VWAP, not a full-day VWAP dominated by pre-news RTH tape.
        sa = get_session_analysis(
            symbol, fast=fast, include_extended=extended, anchor_utc=anchor_utc,
        )

        past_price = sa.past_price if sa else None
        spread_proxy_pct = sa.spread_proxy_pct if sa else None
        base["spread_proxy_pct"] = spread_proxy_pct

        # ── Extended-session price freshness (v21) ───────────────────────────
        # Outside RTH the quote sources update irregularly (Finnhub can serve
        # the 16:00 official close for minutes after a catalyst starts moving
        # the extended tape). When the newest anchored bar is FRESHER than the
        # quote's own data timestamp, the bar close is the better "now" price.
        if extended and sa is not None and sa.last_price and sa.newest_bar_utc:
            quote_ts = quote.get("t")
            bar_epoch = sa.newest_bar_utc.timestamp()
            if not quote_ts or bar_epoch > float(quote_ts):
                logger.info(
                    "Price check [%s]: %s bar close %.4f is fresher than the "
                    "quote (%.4f) — using bar price",
                    symbol, session, sa.last_price, current_price,
                )
                current_price = sa.last_price
                base["current_price"] = current_price
                base["day_move_pct"] = (
                    ((current_price - open_price) / open_price) * 100
                    if open_price else 0.0
                )

        if past_price is None:
            if extended:
                # No open-auction price exists as a fallback baseline in an
                # extended session, and a catalyst that is genuinely moving
                # the extended tape prints bars within minutes. Defer — the
                # retry/re-eval queues are the retry.
                logger.info(
                    "Price check [%s]: no %s-session momentum window yet — "
                    "deferring (bars will exist within minutes if the "
                    "catalyst is real)",
                    symbol, session,
                )
                return None
            # Early-session fallback: not enough today-bars for a momentum
            # window in the first ~15 min; the official open is a fair baseline.
            if minutes_since_open < 15 and open_price and open_price > 0:
                past_price = open_price
                logger.info(
                    "Price check [%s]: no momentum window yet — using open=%.4f as baseline",
                    symbol, open_price,
                )
            elif sa is not None:
                # v21.14.2: a REJECTION, not a None — but ONLY when the session
                # pull itself succeeded.
                #
                # The distinction is the whole point. `sa is None` means
                # Twelvedata gave us nothing and a strike is justified (that is
                # the EGGF/OXAC loop the blackout was built for, and it still
                # returns None below). `sa is not None` with no usable
                # `past_price` means the provider ANSWERED — the log line
                # immediately above is "stale bar … session aggregates kept" —
                # and we simply lack a bar recent enough to measure a momentum
                # window against. That happens on any name whose minute stream
                # has gaps and says nothing whatsoever about coverage.
                #
                # Returning None for that second case meant "no provider
                # carries this instrument", which is what main._queue_retry
                # counts strikes against; two strikes blacklist the ticker for
                # the rest of the day.
                #
                # 2026-08-10: SRRK (Scholar Rock, fda_approval conf 0.75) and
                # NVO were both blacklisted for the day with "no
                # Finnhub/Twelvedata coverage" over a bar 14.4 minutes old, on
                # two of only four regular-hours tradeable-catalyst candidates
                # that session. Both are liquid, fully-covered listings.
                #
                # This is the fourth instance of one bug: a strike asserts "no
                # provider carries this instrument", so only a miss that
                # actually proves that may count one (cf. v21.6 extended
                # sessions, v21.11 frozen feeds, v21.12 the detector itself).
                # A stale bar proves the opposite. Routed through the normal
                # transient path instead: parked in the 15-min re-eval queue,
                # no strike, and — unlike a None — it leaves a news_signals row
                # with a reason_code, so the next occurrence is queryable
                # rather than journal-only.
                return _reject(
                    base, "stale_bars",
                    f"Momentum baseline unavailable: no recent {symbol} minute "
                    "bar to measure against (feed gap, not missing coverage) "
                    "— re-checking while the signal is fresh",
                )
            else:
                # The session pull returned nothing at all. This IS a hard data
                # failure, and a strike toward the no-quote blackout is the
                # correct response — unchanged.
                logger.warning(
                    "Price check [%s]: no session data and not in open window "
                    "— cannot evaluate",
                    symbol,
                )
                return None

        recent_move_pct = ((current_price - past_price) / past_price) * 100 if past_price else 0.0
        base["recent_move_pct"] = recent_move_pct

        # ── 3. Spread proxy ──────────────────────────────────────────────────
        # No bid/ask feed on our data plan, so the latest 1-min bar's range is
        # the proxy. Permissive default in RTH — only the truly untradeable get
        # cut. Extended sessions use the tighter ceiling: a wide extended-hours
        # bar is the thin-book signature, and every exit out here is polled.
        spread_ceiling = cfg.extended_max_spread_pct if extended else cfg.max_spread_pct
        if spread_proxy_pct is not None and spread_proxy_pct > spread_ceiling:
            return _reject(
                base, "wide_spread",
                f"Spread proxy {spread_proxy_pct:.2f}% (last bar range) exceeds "
                f"{spread_ceiling}% ({session}) — effective spread would eat the edge",
            )

        # ── Daily stats: ADV + prev close (cached per symbol per day) ────────
        daily = get_daily_stats(symbol, fast=fast)

        # FAIL-CLOSED if ADV data is missing entirely: without avg_dollar_volume
        # we cannot run the liquidity gate, and without avg_daily_volume we
        # cannot run RVOL — both are non-negotiable risk controls.
        if daily is None:
            logger.warning(
                "Price check [%s]: volume/liquidity data unavailable — cannot run "
                "liquidity or RVOL gates; deferring (will retry)",
                symbol,
            )
            return None
        avg_daily_volume, avg_dollar_volume, td_prev_close = daily

        # Prefer Finnhub's prev close (real-time source); Twelvedata's daily
        # bar is the backup when the quote lacks `pc`.
        prev_close = quote_prev_close or td_prev_close
        base["prev_close"] = prev_close
        day_change_pct = (
            ((current_price - prev_close) / prev_close) * 100 if prev_close else None
        )
        base["day_change_pct"] = day_change_pct

        # RVOL numerator = today's session volume from the minute bars — the
        # CURRENT source (the daily bar's volume field trails the session by
        # minutes; its lag forced the v19.2 "rescue" second fetch, now gone).
        session_volume = sa.session_volume if sa else None
        if session_volume is None:
            logger.warning(
                "Price check [%s]: session volume unavailable — RVOL gate "
                "deferred; continuing with dead_cat/extended_move/liquidity checks",
                symbol,
            )
            current_volume = 0
            rvol = 0.0
        else:
            current_volume = session_volume
            rvol = compute_rvol(current_volume, avg_daily_volume or 0, minutes_since_open)

        base.update(
            current_volume=current_volume,
            avg_volume=avg_daily_volume or 0,
            rvol=rvol,
            avg_dollar_volume=avg_dollar_volume,
        )


        # ── 4. Dead-cat guard (vs prev close) ────────────────────────────────
        # If prev close is unavailable from both sources, fall back to the
        # open-based day move rather than silently disabling the guard —
        # it misses overnight gaps but still catches intraday knives.
        drop_metric = day_change_pct if day_change_pct is not None else day_move_pct
        if drop_metric < -cfg.max_day_drop_pct:
            baseline_name = "prev close" if day_change_pct is not None else "today's open (prev close unavailable)"
            return _reject(
                base, "dead_cat",
                f"Dead-cat guard: {drop_metric:.2f}% vs {baseline_name} "
                f"(max allowed drop −{cfg.max_day_drop_pct}%) — bullish news on a "
                f"falling knife is a bounce, not a trend",
            )

        # ── 5. Extended-move ceiling (vs prev close, gap included) ───────────
        if day_change_pct is not None and day_change_pct > cfg.max_day_move_pct:
            return _reject(
                base, "extended_move",
                f"Extended-move ceiling: {day_change_pct:+.2f}% vs prev close exceeds "
                f"+{cfg.max_day_move_pct}% — catalyst already paid out, entries here "
                f"buy exhaustion",
            )

        # ── 5.5 Volume plausibility cross-check (v21.11) ─────────────────────
        # RVOL and the day move come from DIFFERENT sources (session minute-bar
        # volume vs quote price against prev close), so they can disagree — and
        # when they disagree this hard, the VOLUME side is lagging, not calm.
        # A stock cannot reprice several percent on a fraction of its normal
        # volume; the shares had to trade for the price to get there.
        #
        # 2026-07-31, NVT: +15.59% on the day with RVOL reported as 0.28, while
        # the real tape printed 191k shares in the FIRST MINUTE — about 10% of
        # NVT's entire average day. Because that reading looked low, it
        # triggered the size-neutral bypass in step 9 (which exists for
        # genuinely quiet mega-caps) and waved the trade through on
        # participation evidence that was never there.
        #
        # Deliberately placed AFTER the extended-move ceiling: a move too big
        # to trade is a permanent verdict and must stay terminal, not be
        # downgraded to this transient one and re-queued forever.
        # Deferring is the only safe response — VWAP comes from the same bars,
        # so an implausible volume reading impugns the accumulation test too.
        if (
            not extended
            and session_volume is not None
            and day_change_pct is not None
            and abs(day_change_pct) >= _VOLUME_PLAUSIBILITY_DAY_MOVE_PCT
            and rvol < _VOLUME_PLAUSIBILITY_MIN_RVOL
        ):
            return _reject(
                base, "stale_volume",
                f"Volume/price disagreement: {day_change_pct:+.2f}% on the day "
                f"but RVOL only {rvol:.2f} — a move that size cannot happen on "
                f"{rvol:.0%} of normal volume, so the volume feed is behind "
                f"(RVOL and VWAP both unusable this cycle)",
            )

        # ── 6. Liquidity floor (ADV-based) ───────────────────────────────────
        # Extended sessions demand the institutional-depth floor: the tape is
        # a fraction of RTH depth, every exit is polled (no resting stop), so
        # only names whose NORMAL book is enormous are worth touching.
        liquidity_floor = (
            max(cfg.min_daily_dollar_volume, cfg.extended_min_adv_dollar)
            if extended else cfg.min_daily_dollar_volume
        )
        if avg_dollar_volume is not None and avg_dollar_volume < liquidity_floor:
            return _reject(
                base, "illiquid",
                f"Liquidity filter: avg daily dollar volume ${avg_dollar_volume:,.0f} "
                f"< ${liquidity_floor:,.0f} ({session}) — normal book too thin for a "
                f"clean exit",
            )

        # ── 7. Momentum noise floor ──────────────────────────────────────────
        # With VWAP confirmation on (step 9), this only rejects dead-flat tape —
        # a catalyst that produced literally no move. The "is it being
        # accumulated?" judgement is VWAP's job, not a fixed % threshold's.
        if recent_move_pct < cfg.min_price_move_pct:
            # Same gate, two distinct market states worth telling apart in the
            # logs: flat tape (catalyst ignored so far — retriable) vs tape
            # actively moving AGAINST the signal (observed 2026-07-07: DOCN
            # −8.14% logged as "dead tape", which buried what actually happened).
            if recent_move_pct < -cfg.min_price_move_pct:
                detail = (
                    f"Tape moving against the signal: {recent_move_pct:+.2f}% over last "
                    f"~{cfg.momentum_lookback_minutes} min — sellers in control despite "
                    f"the positive catalyst"
                )
            else:
                detail = (
                    f"Dead tape: {recent_move_pct:+.2f}% over last "
                    f"~{cfg.momentum_lookback_minutes} min (need +{cfg.min_price_move_pct}% to "
                    f"confirm the catalyst moved the stock at all)"
                )
            return _reject(base, "low_momentum", detail)

        # ── 8. Momentum ceiling ──────────────────────────────────────────────
        # A post-halt spike is also far ABOVE VWAP, but it would pass the
        # below_vwap floor — this ceiling names the pattern explicitly.
        if recent_move_pct > cfg.max_price_move_pct:
            return _reject(
                base, "high_momentum",
                f"Momentum ceiling: {recent_move_pct:+.2f}% in "
                f"~{cfg.momentum_lookback_minutes} min exceeds +{cfg.max_price_move_pct}% "
                f"— post-halt spike, not a live catalyst",
            )

        # ── 9. RVOL band ─────────────────────────────────────────────────────
        # Enforced whenever a volume measurement exists. (The old "skip when
        # volume is unmeasured" bypass meant the tickers with the WORST data
        # got a free pass on the participation gate — GLASF traded on RVOL 0.0
        # while liquid names were being rejected.)
        volume_measured = session_volume is not None
        vwap = sa.vwap if sa else None
        if vwap is not None and vwap > 0:
            base["vwap"] = vwap
        if extended:
            # RVOL's time-of-day curve is calibrated on the RTH volume shape —
            # at 17:00 it has no meaning. The extended participation test is
            # ABSOLUTE: dollars actually printed in this session since its
            # start. Transient (low_volume) — the re-eval queue re-checks as
            # the post-catalyst tape builds.
            if volume_measured:
                session_dollars = current_volume * current_price
                if session_dollars < cfg.extended_min_session_dollar_volume:
                    return _reject(
                        base, "low_volume",
                        f"{session} session has printed only "
                        f"${session_dollars:,.0f} since the session start "
                        f"(< ${cfg.extended_min_session_dollar_volume:,.0f}) — "
                        f"no real extended-hours participation yet",
                    )
        elif avg_daily_volume and avg_daily_volume > 0 and volume_measured:
            if rvol < cfg.min_rvol:
                # Size-neutral bypass (v20.2): a mega/large-cap doesn't need
                # anomalous RELATIVE volume to make a real move — its normal
                # book is already enormous in dollar terms. A held VWAP
                # (institutions net buying, independent of raw % change) is
                # the same size-neutral evidence step 10 uses below, just
                # consulted here so the RVOL floor can't veto it first. See
                # cfg.rvol_bypass_min_adv_dollar (2026-07-13: BMY, ADV$ $752M,
                # +2.1% all session, RVOL never exceeded 0.3, held VWAP
                # throughout — rejected low_volume on all 27 re-eval cycles).
                rvol_bypass = (
                    avg_dollar_volume is not None
                    and avg_dollar_volume >= cfg.rvol_bypass_min_adv_dollar
                    and vwap is not None and vwap > 0
                    and current_price >= vwap * (1 - cfg.vwap_tolerance_pct / 100)
                )
                if not rvol_bypass:
                    return _reject(
                        base, "low_volume",
                        f"RVOL {rvol:.2f} below {cfg.min_rvol} — price move lacks real "
                        f"participation (time-normalized vs 20-day avg)",
                    )
                logger.info(
                    "Price check [%s]: RVOL %.2f below %.1f but ADV$ $%s >= "
                    "$%s bypass floor and VWAP held ($%.4f) — size-neutral "
                    "participation confirmed without a relative-volume spike",
                    symbol, rvol, cfg.min_rvol, f"{avg_dollar_volume:,.0f}",
                    f"{cfg.rvol_bypass_min_adv_dollar:,.0f}", vwap,
                )
            if rvol > cfg.max_rvol:
                return _reject(
                    base, "high_volume",
                    f"RVOL {rvol:.1f} above {cfg.max_rvol} — parabolic volume is the "
                    f"halt-pattern signature, not a tradeable catalyst",
                )

        # ── 10. VWAP confirmation (size-neutral accumulation test) ───────────
        # Price held at/above session VWAP = institutions are net buyers,
        # independent of the raw % change. This is what lets a deep-book
        # large-cap catalyst through (it sits above VWAP even at +0.2%) while
        # rejecting a fading gap-up (below VWAP regardless of % change).
        # Research basis: PEAD literature + VWAP-reclaim practitioner playbooks
        # (citations in docs/algorithm.md). The VWAP comes from the same
        # session-analysis pull as everything else (v20) — no extra credit.
        vwap_passed = False
        session_low = sa.session_low if sa else None
        session_high = sa.session_high if sa else None
        if cfg.require_vwap_confirmation:
            if vwap is not None and vwap > 0:
                # Accept at/above VWAP minus a small tolerance (handles a fresh
                # reclaim on the current bar).
                vwap_floor = vwap * (1 - cfg.vwap_tolerance_pct / 100)
                if current_price < vwap_floor:
                    return _reject(
                        base, "below_vwap",
                        f"Below VWAP: price ${current_price:.4f} < VWAP ${vwap:.4f} "
                        f"(tol {cfg.vwap_tolerance_pct}%) — being distributed, not "
                        f"accumulated; gap-and-crap risk",
                    )
                vwap_passed = True
            else:
                # VWAP unavailable (no volume yet / data gap). Not necessarily
                # fatal on its own — but see the degraded-data check below.
                logger.info(
                    "Price check [%s]: VWAP unavailable — confirming on momentum + RVOL only",
                    symbol,
                )

        # ── 10.2. VWAP extension ceiling — never park the stop beyond value ──
        # The stop-loss sits cfg.stop_loss_pct below entry. If entry is MORE
        # than that above VWAP, a routine mean-reversion to VWAP — the base
        # case for any extended stock, even in a healthy uptrend — hits the
        # stop before any continuation can play out: the trade is structurally
        # dead on arrival. Both 2026-07 losses had exactly this geometry
        # (LEVI entered +1.9% above VWAP, CRCL +2.2%, stop 2% — VWAP itself
        # sat at/below the stop). Professional playbooks phrase it as "don't
        # chase — buy the first pullback into value"; mechanically that is
        # this gate + the re-eval queue: an overextended reject is TRANSIENT,
        # so the signal re-checks every cycle and enters IF AND WHEN price
        # pulls back toward VWAP with the catalyst still confirmed.
        # cfg.max_vwap_extension_pct defaults to 1.5 (= stop 2.0 minus margin).
        # Deliberately INDEPENDENT of require_vwap_confirmation: the geometry
        # argument holds whenever a VWAP exists, and disabling the
        # accumulation test must not silently disable the chasing protection.
        if (
            vwap is not None and vwap > 0
            and current_price > vwap * (1 + cfg.max_vwap_extension_pct / 100)
        ):
            ext_pct = (current_price - vwap) / vwap * 100
            return _reject(
                base, "overextended",
                f"Price ${current_price:.4f} is {ext_pct:+.2f}% above VWAP "
                f"${vwap:.4f} (max {cfg.max_vwap_extension_pct}%) — a routine "
                f"reversion to value would hit the {cfg.stop_loss_pct}% stop; "
                f"waiting for the first pullback instead of chasing",
            )

        # ── 10.5. Intraday exhaustion (chasing an already-completed round trip) ─
        # day_change_pct only sees distance from YESTERDAY's close;
        # recent_move_pct only sees the last ~5 minutes. Neither can see the
        # SHAPE of today's own session. 2026-07-09: LEVI gapped down as much as
        # -7.8% at the open on an earnings beat ("sell the news"), then clawed
        # all the way back to +2.3% by the time this gate would have run —
        # within 15 cents of the exact high of the day, three minutes before
        # the actual peak. Every other gate read clean; the trade faded for
        # the rest of the session. session_low/session_high come from the same
        # session-analysis pull as everything else — no extra credit. Both
        # conditions must hold: the day's range must be large enough to be a
        # real round trip (not noise), and price must already sit deep inside
        # the recovered portion of it.
        if (
            cfg.require_exhaustion_check
            and session_low is not None and session_high is not None
            and session_high > session_low
        ):
            day_range_pct = (session_high - session_low) / session_low * 100
            if day_range_pct >= cfg.exhaustion_min_range_pct:
                recovered_frac = (current_price - session_low) / (session_high - session_low)
                if recovered_frac >= cfg.exhaustion_recovery_threshold:
                    return _reject(
                        base, "exhausted_bounce",
                        f"Price has recovered {recovered_frac:.0%} of today's "
                        f"{day_range_pct:.1f}% low-to-high range (low ${session_low:.2f} "
                        f"→ high ${session_high:.2f}) — chasing the tail of a bounce, "
                        f"not a fresh move",
                    )

        # ── Degraded-data floor: at least ONE participation gate must PASS ───
        # Each fallback above is individually reasonable (no session volume →
        # defer RVOL; no VWAP → skip it; thin early tape → open-price momentum
        # baseline). Their CONJUNCTION is not: with RVOL unmeasurable AND VWAP
        # unavailable, "confirmation" has degraded to a single possibly stale
        # quote. That is exactly how GLASF traded on 2026-07-07 — the one
        # candidate with the worst data was the only one that passed, because
        # bad data disabled the gates that would have stopped it. Require
        # positive evidence of participation from at least one source.
        if not volume_measured and not vwap_passed:
            return _reject(
                base, "insufficient_data",
                "No volume measurement (daily bar not rolled, no session minute "
                "bars) and no VWAP — cannot verify participation; refusing to "
                "confirm on a bare quote",
            )

        # ── All conditions met — signal confirmed ─────────────────────────────
        adv_str = f" | adv$={avg_dollar_volume:,.0f}" if avg_dollar_volume else ""
        participation_str = (
            f"| {session} ${current_volume * current_price:,.0f} printed "
            if extended else f"| RVOL {rvol:.1f} "
        )
        reason = (
            f"+{recent_move_pct:.2f}% in ~{cfg.momentum_lookback_minutes} min "
            f"{participation_str}"
            f"| day {day_change_pct:+.2f}% vs prev close"
            f"{adv_str}"
        ) if day_change_pct is not None else (
            f"+{recent_move_pct:.2f}% {participation_str}{adv_str}"
        )
        logger.info(
            "Price check [%s]: session=%s recent=%+.2f%% day_chg=%s rvol=%.1f adv$=%s — APPROVED",
            symbol, session, recent_move_pct,
            f"{day_change_pct:+.2f}%" if day_change_pct is not None else "n/a",
            rvol,
            f"${avg_dollar_volume:,.0f}" if avg_dollar_volume else "n/a",
        )
        return PriceConfirmation(**base, is_confirmed=True, reason=reason, reason_code="approved")

    except Exception as exc:
        logger.error("Price check failed for %s: %s", symbol, exc, exc_info=True)
        return None


# The bars-based price fallback pulls a full 390-bar session series (1 credit).
# The monitor calls get_current_price every 5s per open position, so during a
# quote outage an unthrottled fallback would fire ~12×/min/position — enough
# to saturate the 55/min Twelvedata token bucket and starve signal
# confirmation. Throttled per symbol: between attempts the monitor gets None,
# which it already handles safely (time-stop unaffected; the resting stop
# keeps protecting the downside broker-side throughout).
_BARS_FALLBACK_EVERY_SECONDS = 30.0
_last_bars_fallback: dict[str, float] = {}


def get_current_price(t212_ticker: str) -> float | None:
    """
    Fast lookup of the latest price for the open-position monitor.
    Primary: Finnhub REST quote; fallback Twelvedata /quote — both in fast
    (single-attempt) mode: at the monitor's 5s cadence the NEXT CYCLE is the
    retry, and in-call backoff sleeps would overrun the cycle interval.
    Last resort: most recent Twelvedata 1-min bar close, throttled to one
    attempt per _BARS_FALLBACK_EVERY_SECONDS per symbol (see above).
    Returns None if all are unavailable — callers must handle this explicitly.
    """
    symbol = _to_yf_ticker(t212_ticker)
    # v21: outside RTH the position monitor still needs live prices. The
    # quote chain is tried first regardless (its staleness guard rejects a
    # frozen 16:00 close); the bars fallback must pull prepost bars in an
    # extended session or it would return the RTH close as "current".
    session = get_trading_session()
    extended = session in EXTENDED_SESSIONS
    quote = get_quote_with_fallback(symbol, fast=True)
    if quote is not None:
        price = float(quote["c"])
        logger.debug("get_current_price [%s]: %.4f", symbol, price)
        return price
    now = time.monotonic()
    if now - _last_bars_fallback.get(symbol, 0.0) < _BARS_FALLBACK_EVERY_SECONDS:
        return None
    _last_bars_fallback[symbol] = now
    try:
        sa = get_session_analysis(symbol, fast=True, include_extended=extended)
        if sa is not None and sa.last_price is not None:
            logger.warning(
                "get_current_price [%s]: quotes unavailable — using Twelvedata bar close %.4f",
                symbol, sa.last_price,
            )
            return sa.last_price
    except Exception as exc:
        logger.error("get_current_price [%s]: Twelvedata fallback also failed: %s", symbol, exc)
    logger.error(
        "get_current_price [%s]: both Finnhub and Twelvedata unavailable — returning None",
        symbol,
    )
    return None
