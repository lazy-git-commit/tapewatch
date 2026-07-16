"""
market/sessions.py
───────────────────
Trading-session classification for the T212 24/5 world (v21).

T212's 24/5 offering splits the US trading day into four sessions (ET):

    premarket   04:00 – 09:30   (major-exchange extended tape)
    regular     09:30 – 16:00   (NYSE calendar; early closes at 13:00)
    afterhours  16:00 – 20:00   (major-exchange extended tape)
    overnight   20:00 – 04:00   (Blue Ocean ATS — a DIFFERENT venue)

This module answers "which session is it right now?" from the NYSE calendar
(pandas_market_calendars — holidays and early closes handled), with the
extended boundaries derived from the calendar's own open/close:

    premarket  = market_open − 5h30m … market_open      (04:00 on normal days)
    afterhours = market_close … market_close + 4h        (20:00 normal,
                                                          17:00 on 13:00
                                                          early closes —
                                                          matching the SIP
                                                          extended session)

WHY OVERNIGHT IS CLASSIFIED BUT NEVER TRADED: Blue Ocean prices are not
carried by either of our data providers (Finnhub, Twelvedata) — during
20:00–04:00 we are data-blind. No bars → no confirmation → no trade is the
standing fail-closed contract (docs/algorithm.md §7), and the position
monitor force-flattens everything before the after-hours session ends so no
position is ever exposed to a venue we cannot see.

The RTH-only is_market_open() in market/price_check.py is untouched — every
call site that means "regular hours" keeps meaning that. This module is the
single source of truth for the wider session question.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import pandas_market_calendars as mcal
import pytz

from config.settings import cfg

logger = logging.getLogger(__name__)

_NYSE = mcal.get_calendar("NYSE")
_ET = pytz.timezone("America/New_York")

# Session labels (plain strings, not an Enum, to match the codebase's
# reason_code convention — they end up in logs and PriceConfirmation).
REGULAR = "regular"
PREMARKET = "premarket"
AFTERHOURS = "afterhours"
OVERNIGHT = "overnight"
CLOSED = "closed"

# Sessions in which we hold/manage positions (overnight deliberately absent).
EXTENDED_SESSIONS = frozenset({PREMARKET, AFTERHOURS})

_PREMARKET_LEAD = timedelta(hours=5, minutes=30)   # 09:30 − 5:30 = 04:00 ET
_AFTERHOURS_TAIL = timedelta(hours=4)              # 16:00 + 4:00 = 20:00 ET


def _rth_bounds(now_utc: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(market_open, market_close) UTC for `now_utc`'s ET calendar day, or
    None when that day is not a trading day / the calendar lookup fails."""
    try:
        day = now_utc.tz_convert(_ET).strftime("%Y-%m-%d")
        sched = _NYSE.schedule(day, day)
        if sched.empty:
            return None
        return sched.iloc[0]["market_open"], sched.iloc[0]["market_close"]
    except Exception as exc:
        logger.warning("sessions: calendar lookup failed: %s", exc)
        return None


def get_trading_session(now_utc: pd.Timestamp | None = None) -> str:
    """
    Classify the current instant into one of the five session labels.

    Fail-safe: any calendar failure returns CLOSED — the same "when in doubt,
    don't trade" posture as every other gate. (is_market_open() keeps its own
    independent Finnhub fallback for the regular-hours question, so a calendar
    outage degrades extended-hours trading to RTH-only rather than opening a
    blind window.)
    """
    if now_utc is None:
        now_utc = pd.Timestamp.now("UTC")
    elif now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")

    bounds = _rth_bounds(now_utc)
    if bounds is None:
        return CLOSED
    open_, close_ = bounds

    if open_ <= now_utc < close_:
        return REGULAR
    if open_ - _PREMARKET_LEAD <= now_utc < open_:
        return PREMARKET
    if close_ <= now_utc < close_ + _AFTERHOURS_TAIL:
        return AFTERHOURS
    if now_utc < open_ - _PREMARKET_LEAD:
        # 00:00–04:00 ET on a trading day — the tail of the overnight session.
        return OVERNIGHT
    # After the after-hours tail on a trading day: overnight only if there is
    # another trading day tomorrow (Friday night rolls into the weekend).
    try:
        next_day = (now_utc.tz_convert(_ET) + timedelta(days=1)).strftime("%Y-%m-%d")
        if not _NYSE.schedule(next_day, next_day).empty:
            return OVERNIGHT
    except Exception as exc:
        logger.warning("sessions: next-day calendar lookup failed: %s", exc)
    return CLOSED


def session_bounds(
    session: str, now_utc: pd.Timestamp | None = None
) -> tuple[datetime, datetime] | None:
    """
    (start, end) of `session` on the current ET trading day, as tz-aware
    datetimes. Only meaningful for premarket / regular / afterhours; returns
    None for overnight/closed or when the day has no session.
    """
    if now_utc is None:
        now_utc = pd.Timestamp.now("UTC")
    elif now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    bounds = _rth_bounds(now_utc)
    if bounds is None:
        return None
    open_, close_ = bounds
    if session == REGULAR:
        return open_.to_pydatetime(), close_.to_pydatetime()
    if session == PREMARKET:
        return (open_ - _PREMARKET_LEAD).to_pydatetime(), open_.to_pydatetime()
    if session == AFTERHOURS:
        return close_.to_pydatetime(), (close_ + _AFTERHOURS_TAIL).to_pydatetime()
    return None


def minutes_until_session_end(
    session: str, now_utc: pd.Timestamp | None = None
) -> float | None:
    """Minutes until `session` ends today, or None when not in that session."""
    if now_utc is None:
        now_utc = pd.Timestamp.now("UTC")
    elif now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    bounds = session_bounds(session, now_utc)
    if bounds is None:
        return None
    start, end = bounds
    if not (start <= now_utc.to_pydatetime() < end):
        return None
    return (end - now_utc.to_pydatetime()).total_seconds() / 60


def is_entry_session(session: str) -> bool:
    """
    True when NEW positions may be opened in `session` under the current
    config. Regular hours are always tradeable; extended sessions require
    both the master switch and their per-session toggle. Overnight is never
    tradeable — we have no data for the venue (see module docstring).
    """
    if session == REGULAR:
        return True
    if not cfg.extended_hours_enabled:
        return False
    if session == AFTERHOURS:
        return cfg.afterhours_trading_enabled
    if session == PREMARKET:
        return cfg.premarket_trading_enabled
    return False


def is_manage_session(session: str) -> bool:
    """
    True when open positions can be actively managed (priced and sold) in
    `session`. Broader than is_entry_session: a position must remain
    manageable in an extended session even if its entry toggle was flipped
    off mid-day — the master switch is what reflects the broker capability.
    """
    if session == REGULAR:
        return True
    return session in EXTENDED_SESSIONS and cfg.extended_hours_enabled
