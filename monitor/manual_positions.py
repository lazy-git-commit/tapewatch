# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
monitor/manual_positions.py
───────────────────────────
Trailing-stop protection for positions YOU opened by hand (v22).

The automated strategy never sees these. This module adopts a position you
bought in the Trading 212 app, rests a stop-loss under it at the broker, and
keeps raising that stop as the price makes new highs — the trailing stop that
Trading 212 does not offer on Invest/ISA accounts.

SAFETY PROPERTIES — the reasons this module may touch a real-money account
──────────────────────────────────────────────────────────────────────────
  1. Only what you list. Tickers in MANUAL_TICKERS are the only ones ever
     adopted, priced, sold or alerted on. An ISA usually holds long-term
     investments as well; those are invisible here.
  2. Sell-side only. Everything runs inside executor.use_account(), where
     _guard_order() refuses every buy before it reaches the broker. A bug in
     this file can close your position early; it cannot open, add to, or
     double one.
  3. Never touches an order it did not place. While a sell order of yours rests
     on the ticker the manager stands by, and if you cancel the bot's stop in
     the app it stays hands-off for _HUMAN_CANCEL_STANDBY_SECONDS.
  4. The stop only ever moves UP — here, and again in SQL (raise_manual_trail).
  5. When unsure, it does nothing. A broker timeout is never read as "you sold
     it", an unknown order state is never read as "filled", and a price that is
     already below the stop when the position is adopted is alerted, not sold
     into.

HOW IT TRAILS
─────────────
    stop = max(average_price × (1 − MANUAL_INITIAL_STOP_PCT),
               highest_confirmed_price × (1 − MANUAL_TRAIL_PCT),
               the previous stop)

  "Confirmed": a new high only counts once two consecutive readings agree on
  it — the high is raised to the LOWER of the two. One bad print (a stale feed,
  a thin book, the noisy first seconds after a fill) would otherwise drag the
  stop up to a price the market never actually held, and the next ordinary
  tick would sell you out. HOG (2026-07-23, see position_monitor.py) is the
  incident that showed how noisy a single quote can be.

  The stop RESTS at the broker, so it fires the instant price touches it with
  no involvement from this process — protection does not depend on our polling
  cadence, our data feeds, or the VM being up. Polling only decides how fast
  the stop is RAISED. If the VM dies, the last stop keeps protecting you; only
  the upward trailing pauses until it is back.

  Raising it is cancel + re-place (T212 has no "modify order"), and for that
  moment no stop rests at all — so it moves only when the new level is at least
  MANUAL_STOP_STEP_PCT higher, and at most once per
  MANUAL_STOP_MIN_MOVE_SECONDS.

WHAT A STOP CANNOT DO — say this to anyone relying on it
───────────────────────────────────────────────────────
  * Gaps. A stop is a trigger, not a guaranteed price: if the stock opens
    below it, it sells at the opening price, which can be far lower.
  * Outside regular hours (09:30–16:00 ET) T212 stops do not execute and our
    feeds carry no prices, so the trail is frozen overnight.
  * Halts. While trading is halted nothing executes; on resumption the stop
    fills wherever the market reopens.
"""

import contextvars
import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytz

from config.settings import cfg
from market.price_check import get_quote_with_fallback
from market.sessions import (
    AFTERHOURS, PREMARKET, REGULAR, get_trading_session, session_bounds,
)
from storage.database import (
    close_trade, get_open_manual_trades, open_manual_trade, raise_manual_trail,
    record_system_event, set_stop_order_id, update_manual_position,
)
from trading.executor import (
    T212HTTPError, _fetch_fill, _parse_fill, _round_price, cancel_order,
    get_broker_portfolio, get_order_status, get_pending_orders, place_stop_order,
    sell, t212_to_symbol, use_account,
)

logger = logging.getLogger(__name__)

_LONDON = pytz.timezone("Europe/London")
_ET = pytz.timezone("America/New_York")

# ── Cadences ──────────────────────────────────────────────────────────────────
# Broker sync ("what do you hold, and which orders are open?"). Two endpoints,
# each limited by T212 to roughly one request per 5 s; 10 s keeps both well
# inside that while still noticing a fill or your own sale quickly.
_SYNC_EVERY_SECONDS = 10.0
# Our resting stop's status. Bookkeeping, not protection — the order protects
# the position whether or not we look at it. Same 15 s as the strategy monitor.
_STATUS_CHECK_EVERY_SECONDS = 15.0
# After a failed stop placement, wait this long before trying again.
_PLACE_RETRY_SECONDS = 10.0
# The pause before retrying a refused GOOD_TILL_CANCEL stop as DAY: the stop
# endpoint allows ~1 request per 2 s, so an immediate retry would be throttled
# and the fallback could never be detected.
_VALIDITY_FALLBACK_PAUSE_SECONDS = 2.1
# De-spiking: two readings further apart than this are not "consecutive".
_DESPIKE_MAX_GAP_SECONDS = 60.0
# Our GOOD_TILL_CANCEL stop showing up cancelled when we did not cancel it means
# a human did, in the app. Stay hands-off this long so the bot never re-places a
# stop underneath someone who is in the middle of selling.
_HUMAN_CANCEL_STANDBY_SECONDS = 600.0
# The max-hold exit waits this long after the open — the opening auction and
# the first minutes print the noisiest prices of the day.
_MAX_HOLD_EXIT_AFTER_OPEN_MINUTES = 15
# Regular hours with no resting stop for this long raises a CRITICAL alert.
_UNPROTECTED_ALERT_AFTER_SECONDS = 120.0
# A brand-new listing may not be covered by the market-data feeds yet. After
# this many consecutive misses the feeds are skipped (the broker's own position
# price is used instead) for _CHAIN_BACKOFF_SECONDS — otherwise every cycle
# would spend a Twelvedata credit asking for a symbol it does not have.
_CHAIN_MAX_MISSES = 3
_CHAIN_BACKOFF_SECONDS = 300.0
# How often a NEW price is read. The resting stop does the protecting; this only
# paces how quickly the stop is raised, so 10 s costs nothing in protection —
# and it matches the broker sync, keeps the manager to ~6 quote calls a minute
# out of the market-data budget the strategy shares, and means every reading is
# a genuinely new observation rather than the same one seen twice.
_PRICE_EVERY_SECONDS = 10.0
# A quote older than this is not acted on (the broker's own price is used
# instead). The coverage check upstream accepts quotes up to 20 minutes old —
# right for "is this instrument priced at all?", far too old to move a stop on.
_MAX_OBS_AGE_SECONDS = 120.0
# A GOOD_TILL_CANCEL stop that has left the book without a visible fill almost
# always FILLED — fill detail lags, and the history lookup is tightly rate
# limited. Wait this long, and for a broker sync taken after the disappearance,
# before concluding it expired instead; replacing it early would rest a new sell
# stop on shares that are already sold.
_GONE_GRACE_SECONDS = 20.0
# Recurring status / warning lines are throttled to one per this many seconds.
_SAY_EVERY_SECONDS = 60.0

_GTC = "GOOD_TILL_CANCEL"
_DAY = "DAY"


@dataclass
class _TradeState:
    """Process-lifetime state for one manual trade. Losing it (a restart) is
    safe by construction: everything that protects money is in the DB or at
    the broker; this only holds pacing and the previous price reading."""
    prev_reading: tuple[float, float, object] | None = None  # (monotonic, price, obs id)
    last_price: float | None = None
    last_observed: float = float("-inf")
    gone_since: float | None = None
    gone_epoch: int = 0
    last_status_check: float = float("-inf")
    partial_fill: bool = False
    last_place_attempt: float = float("-inf")
    last_move: float = float("-inf")
    stop_placed_et_day: str | None = None
    standby_until: float = float("-inf")
    unprotected_since: float | None = None
    chain_misses: int = 0
    chain_retry_at: float = float("-inf")
    requantify: bool = False
    exit_pending: str | None = None
    last_said: dict[str, float] = field(default_factory=dict)


_states: dict[int, _TradeState] = {}
_last_sync = float("-inf")
_broker_positions: dict[str, dict] = {}     # listed ticker → position (last sync)
_foreign_sells: dict[str, list[dict]] = {}  # listed ticker → sell orders not ours
_vanish_suspects: set[int] = set()
_quantity_suspects: dict[int, float] = {}
_said_once: set[str] = set()
_stop_validity = _GTC
_announced_day: str | None = None
# Increments on every SUCCESSFUL broker sync: identifies which snapshot a
# broker-sourced price came from, and whether a sync has happened since.
_sync_epoch = 0
# order id → the validity it was placed with. Read when the order shows up
# cancelled: only a GOOD_TILL_CANCEL stop we placed can have been cancelled by a
# person. Unknown (e.g. after a restart) is read as expiry, so a lost entry
# costs a re-placement, never a stretch without a stop.
_order_validity: dict[str, str] = {}


def reset_state() -> None:
    """Forget all process-lifetime state (tests — see tests/conftest.py)."""
    global _last_sync, _stop_validity, _announced_day, _sync_epoch
    _announced_day = None
    _sync_epoch = 0
    _order_validity.clear()
    _states.clear()
    _broker_positions.clear()
    _foreign_sells.clear()
    _vanish_suspects.clear()
    _quantity_suspects.clear()
    _said_once.clear()
    _last_sync = float("-inf")
    _stop_validity = _GTC


# ── Small helpers ─────────────────────────────────────────────────────────────

def _num(value) -> float | None:
    """A finite float, or None — broker payloads are untrusted input."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _state(trade: dict) -> _TradeState:
    return _states.setdefault(trade["id"], _TradeState())


def _due(st: _TradeState, key: str, every: float = _SAY_EVERY_SECONDS) -> bool:
    """True at most once per `every` seconds per (trade, key) — log throttle."""
    now = _time.monotonic()
    if now - st.last_said.get(key, float("-inf")) >= every:
        st.last_said[key] = now
        return True
    return False


def _once(key: str) -> bool:
    """True the first time `key` is seen in this process."""
    if key in _said_once:
        return False
    _said_once.add(key)
    return True


def _forget(trade_id: int) -> None:
    _states.pop(trade_id, None)
    _vanish_suspects.discard(trade_id)
    _quantity_suspects.pop(trade_id, None)


def _listed_entry(code: str) -> str | None:
    """The MANUAL_TICKERS entry this broker instrument code matches, or None.

    Matches the T212 code itself (ACME_US_EQ) or its exchange symbol (ACME)."""
    raw = (code or "").strip()
    if not raw:
        return None
    wanted = set(cfg.manual_tickers)
    if raw.upper() in wanted:
        return raw.upper()
    symbol = (t212_to_symbol(raw) or "").upper()
    return symbol if symbol in wanted else None


def _manageable_quantity(pos: dict) -> float:
    """Shares the API can actually sell: shares held inside a Pie cannot be."""
    qty = _num(pos.get("quantity")) or 0.0
    pie = _num(pos.get("pieQuantity")) or 0.0
    return max(0.0, qty - max(0.0, pie))


def _order_quantity(trade: dict) -> float:
    """
    How many shares to put on a sell order: the BROKER's own figure, never the
    stored one when the two describe the same holding.

    trades.quantity is a REAL (32-bit float) column. A fractional holding such
    as 3.14159265 reads back as 3.1415927 — a hair MORE than is held — and the
    broker rejects a sell for more shares than you own. That is exactly what
    happened on the first live position (2026-11-19): the first stop, placed
    from the broker's figure, rested fine; every re-placement, sized from the
    database, was refused, leaving the position with no resting stop.

    When the snapshot and the record genuinely differ (a size change awaiting
    confirmation) the SMALLER is used, so an order can never exceed the holding.
    With no snapshot at all, the stored figure is floored to 6 decimal places,
    which removes the float32 excess for any realistic holding.
    """
    stored = float(trade["quantity"])
    pos = _broker_positions.get(trade["ticker"])
    held = _manageable_quantity(pos) if pos else 0.0
    if held > 0:
        return held if math.isclose(held, stored, rel_tol=0, abs_tol=1e-6) else min(held, stored)
    return math.floor(stored * 1e6) / 1e6


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = _LONDON.localize(dt)  # this codebase stores naive times as London
    return dt.astimezone(timezone.utc)


def _initial_stop(trade: dict) -> float:
    return _round_price(float(trade["buy_price"]) * (1 - cfg.manual_initial_stop_pct / 100))


def _et_day() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


# ── Entry points ──────────────────────────────────────────────────────────────

def manage_manual_positions() -> None:
    """Scheduler job (every MONITOR_INTERVAL_SECONDS). No-op unless
    MANUAL_TICKERS is set."""
    if not cfg.manual_enabled:
        return
    # Run in a COPY of the current context: the account override set inside can
    # never outlive this job on the scheduler's reused worker thread — not even
    # if use_account()'s own restore were somehow skipped. A leaked override
    # would send the next strategy job's calls to the wrong account.
    contextvars.copy_context().run(_cycle)


def check_manual_account() -> bool:
    """
    Startup self-check, read-only: can we read the manual account at all?

    A wrong or missing API key has to surface at deploy time, loudly — not at
    the moment a stop needs placing on a falling stock.
    """
    if not cfg.manual_enabled:
        return True

    def _probe():
        with use_account(cfg.manual_account):
            return get_broker_portfolio()

    try:
        portfolio = contextvars.copy_context().run(_probe)
    except Exception as exc:  # a self-check must never be what stops startup
        logger.error("MANUAL POSITIONS: account self-check failed: %s", exc)
        portfolio = None
    if portfolio is None:
        logger.critical(
            "MANUAL POSITIONS: could not read the %s account. Check the API key — "
            "TRADING212_API_KEY / TRADING212_API_KEY_ID must be a key created INSIDE "
            "that account (for an ISA: switch to the ISA in the app, then create it). "
            "Nothing can be adopted or protected until this works.",
            cfg.manual_account.upper(),
        )
        record_system_event(
            "manual_account_unreachable",
            f"{cfg.manual_account} account unreadable at startup — check the API key",
        )
        return False
    held = sorted({c for c in (str(p.get("ticker") or "") for p in portfolio) if _listed_entry(c)})
    # Counts only: listing every holding would copy your portfolio into the log.
    logger.info(
        "MANUAL POSITIONS: %s account reachable — %d position(s), of which listed: %s. "
        "Watching %s | initial stop %.1f%% | trail %.1f%% | step %.2f%% / %ds | max hold %s",
        cfg.manual_account.upper(), len(portfolio), ", ".join(held) or "none yet",
        ",".join(cfg.manual_tickers), cfg.manual_initial_stop_pct, cfg.manual_trail_pct,
        cfg.manual_stop_step_pct, cfg.manual_stop_min_move_seconds,
        f"{cfg.manual_max_hold_days} days" if cfg.manual_max_hold_days > 0 else "off",
    )
    return True


def _cycle() -> None:
    session = get_trading_session()
    if session not in (PREMARKET, REGULAR, AFTERHOURS):
        return  # overnight / closed: no prices, no execution — nothing to do
    with use_account(cfg.manual_account):
        try:
            trades = get_open_manual_trades(cfg.manual_account)
        except Exception as exc:
            logger.error("Manual positions: could not read open manual trades: %s", exc)
            return
        trades = _drop_unlisted(trades)
        try:
            trades = _sync_with_broker(trades)
        except Exception as exc:
            logger.error("Manual positions: broker sync failed: %s", exc, exc_info=True)
        # Stops only execute, and our feeds only price, in regular hours. Out of
        # hours the sync above still notices a sale or a new position.
        if session != REGULAR:
            return
        _announce_session(trades)
        for trade in trades:
            try:
                _manage(trade)
            except Exception as exc:
                logger.error(
                    "Manual positions: managing trade %s (%s) failed: %s",
                    trade.get("id"), trade.get("ticker"), exc, exc_info=True,
                )


def _announce_session(trades: list[dict]) -> None:
    """Once per regular session: say what is being watched — so silence in the
    log before you have bought means "nothing to do", not "not running"."""
    global _announced_day
    day = _et_day()
    if _announced_day == day:
        return
    _announced_day = day
    held = ", ".join(
        f"{t['ticker']} ×{float(t['quantity']):.4f} stop ${_num(t.get('trail_stop_price')) or 0:.2f}"
        f" ({'resting' if t.get('stop_order_id') else 'not yet placed'})"
        for t in trades
    )
    logger.info(
        "MANUAL POSITIONS: regular session open (%s) — %s account, watching %s; held: %s",
        day, cfg.manual_account.upper(), ",".join(cfg.manual_tickers), held or "none yet",
    )


# ── Broker sync: adopt / notice a sale / follow a size change ────────────────

def _drop_unlisted(trades: list[dict]) -> list[dict]:
    """Skip open manual trades whose ticker was removed from MANUAL_TICKERS.

    Removing a ticker is how you take a position back: the manager stops
    touching it at once. It deliberately does NOT cancel its last stop — the
    position keeps that protection until you cancel it yourself."""
    kept = []
    for trade in trades:
        if _listed_entry(trade["ticker"]):
            kept.append(trade)
        elif _once(f"unlisted:{trade['id']}"):
            logger.warning(
                "Manual positions: trade %d (%s) is open but no longer in MANUAL_TICKERS "
                "— NOT managing it. Its last stop order (%s) is left resting at the "
                "broker; cancel it in the app if you no longer want it.",
                trade["id"], trade["ticker"], trade.get("stop_order_id") or "none",
            )
    return kept


def _sync_with_broker(trades: list[dict]) -> list[dict]:
    global _last_sync, _sync_epoch
    now = _time.monotonic()
    if now - _last_sync < _SYNC_EVERY_SECONDS:
        return trades
    _last_sync = now

    portfolio = get_broker_portfolio()
    if portfolio is None:
        # Unreachable is not empty. Treating a timeout as "you sold it" would
        # close the record and stop the trailing on a position you still own.
        return trades
    _sync_epoch += 1
    positions = {str(p["ticker"]): p for p in portfolio if p.get("ticker")}
    managed = {code: pos for code, pos in positions.items() if _listed_entry(code)}
    _broker_positions.clear()
    _broker_positions.update(managed)
    _warn_near_misses(positions)

    orders = get_pending_orders()
    if orders is not None:  # on failure keep the previous picture, never assume "none"
        ours = {str(t["stop_order_id"]) for t in trades if t.get("stop_order_id")}
        _foreign_sells.clear()
        for order in orders:
            code = str(order.get("ticker") or "")
            qty = _num(order.get("quantity"))
            if (code in managed and qty is not None and qty < 0
                    and str(order.get("id")) not in ours):
                _foreign_sells.setdefault(code, []).append(order)

    # A position missing from the account must be missing on TWO syncs running
    # before the record closes: a sale whose fill is still settling, or one odd
    # response, must not end the management of a position you still hold.
    kept, suspects = [], set()
    for trade in trades:
        if trade["ticker"] in positions:
            kept.append(trade)
        elif trade["id"] not in _vanish_suspects:
            logger.info(
                "MANUAL [%s] trade=%d: not in the account this sync — confirming on the next",
                trade["ticker"], trade["id"],
            )
            suspects.add(trade["id"])
            kept.append(trade)
        elif not _resolve_vanished(trade):
            suspects.add(trade["id"])
            kept.append(trade)
    _vanish_suspects.clear()
    _vanish_suspects.update(suspects)

    open_tickers = {t["ticker"] for t in kept}
    for code, pos in managed.items():
        if code in open_tickers:
            continue
        try:
            adopted = _adopt(code, pos)
        except Exception as exc:
            logger.error("Manual positions: could not adopt %s: %s", code, exc, exc_info=True)
            continue
        if adopted is not None:
            kept.append(adopted)

    for trade in kept:
        pos = managed.get(trade["ticker"])
        if pos is not None:
            _check_quantity(trade, pos)
            _claim_own_stop(trade)
    return kept


def _warn_near_misses(positions: dict[str, dict]) -> None:
    """
    T212 re-uses symbols by appending a digit to its own instrument code
    (exchange FLY = T212 FLY1_US_EQ), and a brand-new listing may not be in the
    instrument map yet — so a listed symbol can fail to match its own position.
    Name that likely candidate, and only that one: logging every unmatched
    holding would copy your whole portfolio into the service log.
    """
    for code in positions:
        if _listed_entry(code):
            continue
        stem = code.upper().split("_")[0]
        for entry in cfg.manual_tickers:
            if "_" in entry or not stem.startswith(entry):
                continue
            if stem[len(entry):].isdigit() and _once(f"near:{code}"):
                logger.warning(
                    "Manual positions: you hold %s, which looks like MANUAL_TICKERS entry "
                    "%s under Trading 212's own code — it was NOT adopted. If it is the "
                    "position you meant, add %s to MANUAL_TICKERS exactly.",
                    code, entry, code,
                )


def _adopt(code: str, pos: dict) -> dict | None:
    qty = _manageable_quantity(pos)
    avg = _num(pos.get("averagePrice"))
    if qty <= 0 or avg is None or avg <= 0:
        if _once(f"unadoptable:{code}"):
            logger.error(
                "Manual positions: %s is listed and held but cannot be adopted (sellable "
                "quantity %.4f, average price %r). Shares inside a Pie cannot be sold by "
                "API orders — buy it directly, not in a Pie.",
                code, qty, pos.get("averagePrice"),
            )
        return None
    pie = _num(pos.get("pieQuantity")) or 0.0
    if pie > 0 and _once(f"pie:{code}"):
        logger.warning(
            "Manual positions: %.4f share(s) of %s are inside a Pie and are NOT protected "
            "— only the %.4f held directly are.", pie, code, qty,
        )
    initial = _round_price(avg * (1 - cfg.manual_initial_stop_pct / 100))
    bought = _parse_time(pos.get("initialFillDate")) or datetime.now(timezone.utc)
    buy_time = bought.astimezone(_LONDON).isoformat()
    trade_id = open_manual_trade(
        code, qty, avg, mode=cfg.manual_account, buy_time=buy_time,
        trail_high_price=avg, trail_stop_price=initial,
    )
    logger.warning(
        "MANUAL POSITION ADOPTED [%s] trade=%d: %.4f shares @ avg $%.4f — initial stop "
        "$%.2f (−%.1f%%), then trailing %.1f%% below the highest confirmed price. "
        "Managed SELL-SIDE ONLY in the %s account.",
        code, trade_id, qty, avg, initial, cfg.manual_initial_stop_pct,
        cfg.manual_trail_pct, cfg.manual_account.upper(),
    )
    record_system_event(
        "manual_position_adopted",
        f"{code} qty={qty:.4f} avg=${avg:.4f} initial_stop=${initial:.2f} "
        f"trail={cfg.manual_trail_pct}%",
    )
    return {
        "id": trade_id, "ticker": code, "quantity": qty, "buy_price": avg,
        "buy_time": buy_time, "stop_order_id": None, "trail_high_price": avg,
        "trail_stop_price": initial, "status": "open", "origin": "manual",
        "mode": cfg.manual_account,
    }


def _check_quantity(trade: dict, pos: dict) -> None:
    """Follow a size change you made in the app (bought more / sold part)."""
    qty = _manageable_quantity(pos)
    held = float(trade["quantity"])
    if qty <= 0 or math.isclose(qty, held, rel_tol=0, abs_tol=1e-6):
        _quantity_suspects.pop(trade["id"], None)
        return
    seen = _quantity_suspects.get(trade["id"])
    if seen is None or not math.isclose(seen, qty, rel_tol=0, abs_tol=1e-6):
        _quantity_suspects[trade["id"]] = qty  # confirm next sync: a fill may be in flight
        return
    _quantity_suspects.pop(trade["id"], None)
    st = _state(trade)
    if st.partial_fill:
        return  # our own stop is mid-execution — let it finish
    avg = _num(pos.get("averagePrice")) or float(trade["buy_price"])
    update_manual_position(trade["id"], qty, avg)
    logger.warning(
        "MANUAL [%s] trade=%d: position size changed %.4f → %.4f (avg $%.4f) — the stop "
        "will be re-placed for the new size",
        trade["ticker"], trade["id"], held, qty, avg,
    )
    trade["quantity"] = qty
    trade["buy_price"] = avg
    st.requantify = True


def _claim_own_stop(trade: dict) -> None:
    """
    After a crash between placing a stop and recording its id, our own order
    looks exactly like one of yours, and would park the manager in stand-by for
    good. A resting sell STOP on this ticker, for this quantity, at exactly the
    stop level recorded BEFORE placement (see _place_and_record) can only be
    ours — claim it back.
    """
    if trade.get("stop_order_id"):
        return
    stop = _num(trade.get("trail_stop_price"))
    orders = _foreign_sells.get(trade["ticker"]) or []
    if stop is None or not orders:
        return
    for order in list(orders):
        price = _num(order.get("stopPrice"))
        qty = _num(order.get("quantity"))
        if (price is not None and qty is not None and order.get("limitPrice") is None
                and math.isclose(price, _round_price(stop), rel_tol=0, abs_tol=0.005)
                and math.isclose(-qty, float(trade["quantity"]), rel_tol=0, abs_tol=1e-6)):
            order_id = str(order.get("id"))
            set_stop_order_id(trade["id"], order_id)
            trade["stop_order_id"] = order_id
            orders.remove(order)
            if not orders:
                _foreign_sells.pop(trade["ticker"], None)
            logger.warning(
                "MANUAL [%s] trade=%d: recovered the bot's own resting stop %s at $%.2f "
                "(its id was not recorded before a restart)",
                trade["ticker"], trade["id"], order_id, price,
            )
            return


def _resolve_vanished(trade: dict) -> bool:
    """The position has been absent for two syncs running. True when closed."""
    order_id = trade.get("stop_order_id")
    if order_id:
        status = get_order_status(order_id)
        if status is None:
            return False  # can't tell whether our stop filled — never guess
        if status in ("FILLED", "GONE", "PARTIALLY_FILLED"):
            # Our stop left the book AND the shares left the account: that is
            # our stop executing, even when its fill detail has not arrived.
            # (A sale of yours would show the stop CANCELLED, not gone — T212
            # will not sell shares a resting order has reserved.)
            _close_from_fill(trade, order_id, _fetch_fill(order_id), fetched=True)
            return True
        if status not in ("CANCELLED", "REJECTED", "GONE", "PARTIALLY_FILLED"):
            # Still pending against shares that no longer exist. It cannot
            # execute (an ISA cannot go short) but it should not linger.
            cancel_order(order_id)
    st = _states.get(trade["id"])
    estimate = (
        (st.last_price if st and st.last_price else None)
        or _num(trade.get("trail_stop_price")) or float(trade["buy_price"])
    )
    logger.warning(
        "MANUAL [%s] trade=%d: the position is no longer in the account and the bot's "
        "stop did not fill — recording it as closed by you (manual_exit) at an ESTIMATED "
        "$%.4f. Your real sale price is in your Trading 212 history.",
        trade["ticker"], trade["id"], estimate,
    )
    close_trade(trade["id"], estimate, "manual_exit")
    _announce_close(trade, estimate, "manual_exit")
    _forget(trade["id"])
    return True


# ── Closing ───────────────────────────────────────────────────────────────────

def _announce_close(trade: dict, price: float, reason: str) -> None:
    buy = float(trade["buy_price"])
    high = _num(trade.get("trail_high_price")) or buy
    pct = (price - buy) / buy * 100 if buy > 0 else 0.0
    logger.warning(
        "MANUAL POSITION CLOSED [%s] trade=%d: %s @ $%.4f — %+.2f%% vs your average "
        "$%.4f (highest confirmed price $%.4f)",
        trade["ticker"], trade["id"], reason, price, pct, buy, high,
    )
    record_system_event(
        "manual_position_closed",
        f"{trade['ticker']} trade={trade['id']} {reason} @ ${price:.4f} ({pct:+.2f}%)",
    )


def _close_from_fill(trade: dict, order_id: str, fill: dict | None = None,
                     *, fetched: bool = False) -> None:
    """Our resting stop executed at the broker — record it with the real fill.

    `fetched=True` means the caller already asked for the fill: _fetch_fill()
    blocks for up to 30 s, so asking twice would stall every listed position for
    a minute exactly when one of them has just sold."""
    if fill is None and not fetched:
        fill = _fetch_fill(order_id)
    filled, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
    price = filled if filled is not None else (
        _num(trade.get("trail_stop_price")) or float(trade["buy_price"])
    )
    if filled is None:
        logger.warning(
            "MANUAL [%s] trade=%d: stop %s filled but fill detail is unavailable — "
            "recording at the stop level $%.4f (a stop-market order can fill lower)",
            trade["ticker"], trade["id"], order_id, price,
        )
    close_trade(
        trade["id"], price, "trailing_stop", sell_order_id=order_id,
        sell_net_gbp=net_gbp, sell_fx_rate=fx_rate, sell_fees_gbp=fees_gbp,
    )
    _announce_close(trade, price, "trailing_stop")
    _forget(trade["id"])


def _clear_stop(trade: dict) -> None:
    try:
        set_stop_order_id(trade["id"], None)
    except Exception as exc:
        logger.error(
            "Manual positions: could not clear the stop id on trade %d: %s",
            trade["id"], exc,
        )
    trade["stop_order_id"] = None


def _gone_resolution(trade: dict, st: _TradeState, now: float) -> bool | None:
    """
    Our stop has left the book and no fill is visible. Returns None while that
    is unresolved (do nothing on this position), or False once it is known to
    have EXPIRED — its id is then cleared and a new stop may be placed.

    Most likely it filled and the fill detail is late, so the default is to wait.
    It only counts as expired once _GONE_GRACE_SECONDS have passed AND a broker
    sync taken after the disappearance still shows every share held. If the
    shares are gone instead, the sync's vanish path closes the trade as the stop
    filling (_resolve_vanished) — with the stop id still attached, so the fill
    is attributed to it rather than recorded as a sale of yours.
    """
    if st.gone_since is None:
        st.gone_since, st.gone_epoch = now, _sync_epoch
        logger.warning(
            "MANUAL [%s] trade=%d: stop %s left the book with no fill visible yet — most "
            "likely it FILLED. Placing nothing else until the account confirms the "
            "shares are still held.",
            trade["ticker"], trade["id"], trade.get("stop_order_id"),
        )
        return None
    pos = _broker_positions.get(trade["ticker"])
    still_held = (pos is not None
                  and _manageable_quantity(pos) >= float(trade["quantity"]) - 1e-6)
    if now - st.gone_since >= _GONE_GRACE_SECONDS and _sync_epoch > st.gone_epoch and still_held:
        logger.warning(
            "MANUAL [%s] trade=%d: stop %s expired without filling (all %.4f shares are "
            "still held) — placing a new one",
            trade["ticker"], trade["id"], trade.get("stop_order_id"), float(trade["quantity"]),
        )
        _clear_stop(trade)
        st.gone_since = None
        return False
    return None


def _release_stop(trade: dict, st: _TradeState) -> bool:
    """
    Cancel our resting stop so its shares are free to sell or re-stop.

    True: no order of ours rests any more — safe to proceed. False: stop
    processing this cycle — the stop FILLED during the cancel (the trade has been
    closed here), it left the book without a visible fill (probably filled — see
    _gone_resolution), or its state is unknown. In each of those cases a second
    sell order on top of it could sell shares that are already gone.
    """
    order_id = trade.get("stop_order_id")
    if not order_id:
        return True
    if cancel_order(order_id):
        _clear_stop(trade)
        return True
    status = get_order_status(order_id)
    if status in ("FILLED", "GONE", "PARTIALLY_FILLED"):
        fill = _fetch_fill(order_id)
        if fill or status == "FILLED":
            _close_from_fill(trade, order_id, fill, fetched=True)
            return False
        if status == "GONE":
            _gone_resolution(trade, st, _time.monotonic())
        return False
    if status in ("CANCELLED", "REJECTED"):
        _clear_stop(trade)
        return True
    logger.warning(
        "MANUAL [%s] trade=%d: could not cancel stop %s (state %s) — leaving it in "
        "place and retrying next cycle",
        trade["ticker"], trade["id"], order_id, status,
    )
    return False


def _exit(trade: dict, st: _TradeState, price: float) -> None:
    """Sell the whole position at market. Retried every cycle until it lands."""
    reason = st.exit_pending or "trailing_stop"
    if not _release_stop(trade, st):
        return
    result = sell(trade["ticker"], _order_quantity(trade), price, reason, force_market=True)
    if not result.success:
        logger.error(
            "MANUAL [%s] trade=%d: %s sell FAILED (%s) — retrying next cycle. Until it "
            "succeeds the position has NO resting stop.",
            trade["ticker"], trade["id"], reason, result.error,
        )
        return
    close_trade(
        trade["id"], result.price, reason, sell_order_id=result.order_id,
        sell_net_gbp=result.net_gbp, sell_fx_rate=result.fx_rate,
        sell_fees_gbp=result.fees_gbp,
    )
    _announce_close(trade, result.price, reason)
    _forget(trade["id"])


# ── The per-cycle trailing logic (regular hours only) ─────────────────────────

def _check_resting_stop(trade: dict, st: _TradeState, now: float) -> bool | None:
    """
    Bookkeeping for our resting stop. Returns True (a stop is resting), False
    (none — one should be placed), or None (stop processing this trade this
    cycle: it closed, its stop is mid-execution, or you have just taken over).
    """
    order_id = trade.get("stop_order_id")
    if not order_id:
        return False
    if st.gone_since is not None:
        return _gone_resolution(trade, st, now)
    if now - st.last_status_check < _STATUS_CHECK_EVERY_SECONDS:
        return None if st.partial_fill else True
    st.last_status_check = now
    status = get_order_status(order_id)
    st.partial_fill = status == "PARTIALLY_FILLED"
    if status is None:
        return True  # unknown: presume it is still protecting — never double-sell
    if status == "FILLED":
        _close_from_fill(trade, order_id)
        return None
    if st.partial_fill:
        if _due(st, "partial"):
            logger.warning(
                "MANUAL [%s] trade=%d: the stop is PARTIALLY filled — leaving it to finish",
                trade["ticker"], trade["id"],
            )
        return None
    if status == "GONE":
        fill = _fetch_fill(order_id)
        if fill:
            _close_from_fill(trade, order_id, fill, fetched=True)
            return None
        return _gone_resolution(trade, st, now)
    if status in ("CANCELLED", "REJECTED", "REPLACED"):
        _clear_stop(trade)
        if status == "CANCELLED" and _cancelled_by_human(order_id, st):
            st.standby_until = now + _HUMAN_CANCEL_STANDBY_SECONDS
            logger.warning(
                "MANUAL [%s] trade=%d: the bot's stop %s was CANCELLED outside the bot — "
                "assuming you are managing this position yourself. Hands off for %d min; "
                "the bot then re-places its stop if you still hold the position and have "
                "no sell order of your own on it.",
                trade["ticker"], trade["id"], order_id,
                int(_HUMAN_CANCEL_STANDBY_SECONDS // 60),
            )
            record_system_event(
                "manual_stop_cancelled_externally",
                f"{trade['ticker']} trade={trade['id']} stop {order_id} cancelled outside the bot",
            )
            return None
        logger.warning(
            "MANUAL [%s] trade=%d: stop %s is %s — placing a new one",
            trade["ticker"], trade["id"], order_id, status,
        )
        return False
    return True  # NEW / CONFIRMED / LOCAL / CANCELLING …: still on the book


def _cancelled_by_human(order_id: str, st: _TradeState) -> bool:
    """
    Was this cancellation a person's doing? Decided by the validity the ORDER
    was placed with, not by the process-wide setting — which resets on restart
    and would misread yesterday's expired DAY stop as your cancellation, leaving
    the position with no stop for ten minutes at the open.

    GOOD_TILL_CANCEL: never expires, so yes. DAY: also expires at every close,
    so only a same-day cancel during regular hours counts. Unknown (placed
    before a restart): read as expiry — a needless re-placement is the cheap
    mistake; a stretch with no stop is the expensive one.
    """
    validity = _order_validity.get(str(order_id))
    if validity is None:
        return False
    if validity == _GTC:
        return True
    return st.stop_placed_et_day == _et_day() and get_trading_session() == REGULAR


def _observe(trade: dict, st: _TradeState, now: float) -> tuple[float, object] | None:
    """
    A price to act on, and the identity of the observation it came from — or
    None when nothing usable is available.

    Primary: the market-data quote (timestamped). A quote older than
    _MAX_OBS_AGE_SECONDS is not acted on. Fallback: the broker's own price for
    the position from the last sync, which covers a listing the feeds do not
    carry yet. The identity — the quote's own timestamp, or the sync it came
    from — lets the de-spike rule refuse to count one observation twice.
    """
    if now >= st.chain_retry_at:
        quote = get_quote_with_fallback(t212_to_symbol(trade["ticker"]), fast=True)
        price = _num(quote.get("c")) if isinstance(quote, dict) else None
        if price is not None and price > 0:
            st.chain_misses = 0
            ts = _num(quote.get("t"))
            if ts is None or _time.time() - ts <= _MAX_OBS_AGE_SECONDS:
                return price, ("feed", ts if ts is not None else now)
            if _due(st, "stale"):
                logger.warning(
                    "MANUAL [%s]: the market-data quote is %.0f s old — not acting on it; "
                    "using the broker's own price instead",
                    trade["ticker"], _time.time() - ts,
                )
        else:
            st.chain_misses += 1
            if st.chain_misses >= _CHAIN_MAX_MISSES:
                st.chain_misses = 0
                st.chain_retry_at = now + _CHAIN_BACKOFF_SECONDS
                logger.warning(
                    "MANUAL [%s]: the market-data feeds have no price for this instrument "
                    "(a new listing may not be covered yet) — using the broker's own "
                    "position price for the next %d min",
                    trade["ticker"], int(_CHAIN_BACKOFF_SECONDS // 60),
                )
    pos = _broker_positions.get(trade["ticker"])
    broker = _num(pos.get("currentPrice")) if pos else None
    if broker is not None and broker > 0:
        return broker, ("broker", _sync_epoch)
    return None


def _confirmed_high_candidate(st: _TradeState, price: float, obs_id: object,
                              now: float) -> float | None:
    """The lower of this reading and the previous one, if they are consecutive
    AND genuinely different observations.

    A single spike between two normal readings can never raise the high: the
    lower of (normal, spike) is normal, and so is the lower of (spike, normal).
    A genuine move up holds across two readings and counts one cycle later.

    "Different observations" matters: the broker's price only refreshes with
    each sync, and a frozen feed repeats its last quote. Reading one bad print
    twice would make min(spike, spike) = spike, and the stop would be raised to
    a price the market never held — permanently, since the trail only goes up."""
    prev = st.prev_reading
    if prev is not None and prev[2] == obs_id:
        return None  # the same observation seen again proves nothing
    st.prev_reading = (now, price, obs_id)
    if prev is None or now - prev[0] > _DESPIKE_MAX_GAP_SECONDS:
        return None
    return min(prev[1], price)


def _max_hold_due(trade: dict) -> bool:
    days = cfg.manual_max_hold_days
    if days <= 0:
        return False
    bought = _parse_time(trade.get("buy_time"))
    if bought is None:
        return False
    now_utc = datetime.now(timezone.utc)
    if now_utc - bought < timedelta(days=days):
        return False
    bounds = session_bounds(REGULAR)
    if bounds is None:
        return False
    return now_utc >= bounds[0] + timedelta(minutes=_MAX_HOLD_EXIT_AFTER_OPEN_MINUTES)


def _is_order_refusal(exc: Exception | None) -> bool:
    """The broker rejected the ORDER ITSELF (HTTP 400) — as opposed to a
    throttle, an outage, an auth problem or a timeout, which say nothing about
    whether a GOOD_TILL_CANCEL stop is acceptable."""
    return isinstance(exc, T212HTTPError) and exc.status_code == 400


def _place_stop(ticker: str, quantity: float, level: float) -> tuple[str | None, str]:
    """
    Place a resting stop. Returns (order_id or None, validity used).

    GOOD_TILL_CANCEL first. DAY only if the broker REFUSED the GTC order itself
    (HTTP 400) and then accepted the same stop as DAY — that is evidence GTC is
    unsupported, and the process switches for good. Any other failure (a 429
    right after cancelling the old stop, a 5xx, a timeout) is just retried as
    GTC next cycle: switching on a passing blip would leave every later stop
    expiring at 16:00, i.e. no protection overnight or at the next open.
    """
    global _stop_validity
    validity = _stop_validity
    order_id, exc = place_stop_order(ticker, quantity, level, validity)
    if order_id is None and validity == _GTC and _is_order_refusal(exc):
        _time.sleep(_VALIDITY_FALLBACK_PAUSE_SECONDS)
        order_id, day_exc = place_stop_order(ticker, quantity, level, _DAY)
        if order_id is not None:
            _stop_validity = validity = _DAY
            logger.warning(
                "Manual positions: the broker refused a GOOD_TILL_CANCEL stop (%s) but "
                "accepted DAY — stops will be re-placed at each regular-session open. "
                "Between sessions, and if this VM is down at the open, there is NO "
                "resting stop.", exc,
            )
        else:
            # Both refused: the ORDER is the problem, not its validity. Say what
            # the broker said — without it, "could not place" is undiagnosable.
            logger.error(
                "Manual positions: the broker refused a stop for %s × %.8f @ $%.2f as "
                "both GTC (%s) and DAY (%s)",
                ticker, quantity, level, exc, day_exc,
            )
    elif order_id is None:
        logger.warning("Manual positions: stop placement for %s failed: %s", ticker, exc)
    return order_id, validity


def _place_and_record(trade: dict, st: _TradeState, level: float, now: float) -> bool:
    """Rest a stop at `level` and record it. False when placement failed."""
    # The level is persisted BEFORE placing, so that if we crash after the
    # broker accepts the order but before its id is stored, the order can be
    # recognised as ours on restart by its exact price (_claim_own_stop).
    raise_manual_trail(trade["id"], stop=level)
    trade["trail_stop_price"] = max(_num(trade.get("trail_stop_price")) or level, level)
    st.last_place_attempt = now
    level = trade["trail_stop_price"]
    order_id, validity = _place_stop(trade["ticker"], _order_quantity(trade), level)
    if order_id is None:
        logger.error(
            "MANUAL [%s] trade=%d: could not place the stop at $%.2f — falling back to a "
            "POLLED stop (sold at market if the price falls through it while this process "
            "runs) and retrying in %ds",
            trade["ticker"], trade["id"], level, int(_PLACE_RETRY_SECONDS),
        )
        return False
    try:
        set_stop_order_id(trade["id"], order_id)
    except Exception as exc:
        logger.error(
            "MANUAL [%s] trade=%d: stop %s placed but its id could not be recorded (%s) — "
            "it will be recognised on the next sync",
            trade["ticker"], trade["id"], order_id, exc,
        )
    trade["stop_order_id"] = order_id
    st.last_status_check = now
    st.last_move = now
    st.requantify = False
    st.unprotected_since = None
    st.stop_placed_et_day = _et_day()
    st.gone_since = None
    _order_validity[str(order_id)] = validity
    logger.info(
        "MANUAL [%s] trade=%d: resting STOP at $%.2f for %.4f shares (%s) — order %s",
        trade["ticker"], trade["id"], level, float(trade["quantity"]), validity, order_id,
    )
    return True


def _move_stop(trade: dict, st: _TradeState, target: float, price: float, now: float) -> None:
    old = _num(trade.get("trail_stop_price")) or target
    logger.info(
        "MANUAL [%s] trade=%d: moving stop $%.2f -> $%.2f (%s; highest confirmed $%.4f, "
        "price $%.4f, trail %.1f%%)",
        trade["ticker"], trade["id"], old, target,
        f"re-sizing for {float(trade['quantity']):.4f} shares" if st.requantify else "new high",
        _num(trade.get("trail_high_price")) or float(trade["buy_price"]), price,
        cfg.manual_trail_pct,
    )
    if not _release_stop(trade, st):
        return
    st.last_move = now
    if price <= target:
        # The price fell through the NEW level while we were moving the old one.
        st.exit_pending = "trailing_stop"
        _exit(trade, st, price)
        return
    _place_and_record(trade, st, target, now)


def _note_unprotected(trade: dict, st: _TradeState, now: float, live: bool) -> None:
    if live:
        st.unprotected_since = None
        return
    if st.unprotected_since is None:
        st.unprotected_since = now
        return
    if now - st.unprotected_since >= _UNPROTECTED_ALERT_AFTER_SECONDS and _due(st, "unprotected"):
        logger.critical(
            "MANUAL [%s] trade=%d: NO resting stop for %.0f s during regular hours — the "
            "position is protected only by polling, and only while this process runs",
            trade["ticker"], trade["id"], now - st.unprotected_since,
        )
        record_system_event(
            "manual_stop_unprotected",
            f"{trade['ticker']} trade={trade['id']} has had no resting stop for "
            f"{now - st.unprotected_since:.0f}s",
        )


def _alert_below_stop(trade: dict, st: _TradeState, price: float, level: float) -> None:
    if not _due(st, "below"):
        return
    logger.critical(
        "MANUAL [%s] trade=%d: price $%.4f is already BELOW the stop level $%.2f, so no "
        "stop order can be placed (a sell stop above the market is not a valid order). "
        "The bot will NOT sell into this on its own; it rests the stop as soon as the "
        "price is back above $%.2f. Sell it yourself if you want out.",
        trade["ticker"], trade["id"], price, level, level,
    )
    record_system_event(
        "manual_below_stop",
        f"{trade['ticker']} trade={trade['id']} price ${price:.4f} below stop ${level:.2f}",
    )


def _log_status(trade: dict, st: _TradeState, price: float, high: float) -> None:
    if not _due(st, "status"):
        return
    buy = float(trade["buy_price"])
    stop = _num(trade.get("trail_stop_price")) or _initial_stop(trade)
    order_id = trade.get("stop_order_id")
    logger.info(
        "MANUAL [%s] trade=%d: price $%.4f (%+.2f%% vs avg $%.4f) | highest confirmed "
        "$%.4f | stop $%.2f, %.2f%% below the price (%s)",
        trade["ticker"], trade["id"], price, (price - buy) / buy * 100 if buy else 0.0,
        buy, high, stop, (price - stop) / price * 100,
        f"resting, order {order_id}" if order_id else "POLLED — no resting order",
    )


def _manage(trade: dict) -> None:
    st = _state(trade)
    now = _time.monotonic()
    code = trade["ticker"]

    # An exit already decided on keeps being retried until it lands.
    if st.exit_pending:
        obs = _observe(trade, st, now)
        price = ((obs[0] if obs else None) or st.last_price
                 or _num(trade.get("trail_stop_price")) or float(trade["buy_price"]))
        _exit(trade, st, price)
        return

    live = _check_resting_stop(trade, st, now)
    if live is None:
        return

    # Price-dependent decisions only on a NEW observation, at most every
    # _PRICE_EVERY_SECONDS. Between them there is nothing new to decide on —
    # the resting stop is what protects the position meanwhile.
    if now - st.last_observed < _PRICE_EVERY_SECONDS:
        _note_unprotected(trade, st, now, live)
        return
    st.last_observed = now
    obs = _observe(trade, st, now)
    if obs is None:
        _note_unprotected(trade, st, now, live)
        if _due(st, "noprice"):
            logger.warning(
                "MANUAL [%s] trade=%d: no price available this cycle — %s",
                code, trade["id"],
                "the resting stop keeps protecting the position" if live else
                "NO resting stop and no price: unprotected until a price is available",
            )
        return
    price, obs_id = obs
    prev_price, st.last_price = st.last_price, price

    # The highest confirmed price is tracked even while standing by, so that
    # when control comes back the trail resumes from the real high.
    high = _num(trade.get("trail_high_price")) or float(trade["buy_price"])
    candidate = _confirmed_high_candidate(st, price, obs_id, now)
    if candidate is not None and candidate > high:
        high = candidate
        raise_manual_trail(trade["id"], high=high)
        trade["trail_high_price"] = high

    if now < st.standby_until:
        if _due(st, "standby"):
            logger.warning(
                "MANUAL [%s] trade=%d: standing by — you cancelled the bot's stop; "
                "hands off for %.0f more min",
                code, trade["id"], (st.standby_until - now) / 60,
            )
        return
    if _foreign_sells.get(code):
        if _due(st, "foreign"):
            ids = ", ".join(str(o.get("id")) for o in _foreign_sells[code])
            logger.warning(
                "MANUAL [%s] trade=%d: a sell order you placed is resting on this position "
                "(%s) — the bot will not place or move its stop while it exists. Cancel "
                "yours to hand control back.",
                code, trade["id"], ids,
            )
            record_system_event(
                "manual_standby_user_order",
                f"{code} trade={trade['id']} standing by for your sell order(s) {ids}",
            )
        return

    if _max_hold_due(trade):
        logger.warning(
            "MANUAL [%s] trade=%d: held for %d+ days (MANUAL_MAX_HOLD_DAYS) — selling at market",
            code, trade["id"], cfg.manual_max_hold_days,
        )
        st.exit_pending = "max_hold"
        _exit(trade, st, price)
        return

    stop = _num(trade.get("trail_stop_price")) or _initial_stop(trade)
    # _initial_stop() is included so that adding shares at a higher price
    # lifts the floor with your new average; it can never lower anything.
    target = max(stop, _initial_stop(trade),
                 _round_price(high * (1 - cfg.manual_trail_pct / 100)))

    if live:
        st.unprotected_since = None
        stepped = target > stop and target >= stop * (1 + cfg.manual_stop_step_pct / 100)
        paced = now - st.last_move >= cfg.manual_stop_min_move_seconds
        if st.requantify or (stepped and paced):
            _move_stop(trade, st, target, price, now)
    elif price <= target:
        if prev_price is not None and prev_price > target:
            # Watched it fall THROUGH the level with no resting order to catch it.
            logger.warning(
                "MANUAL [%s] trade=%d: price $%.4f fell through the stop $%.2f with no "
                "resting order — selling at market (polled stop)",
                code, trade["id"], price, target,
            )
            st.exit_pending = "trailing_stop"
            _exit(trade, st, price)
            return
        # Already below the level when we first looked (e.g. adopted late):
        # alert, never sell into it unprompted.
        _alert_below_stop(trade, st, price, target)
        _note_unprotected(trade, st, now, live=False)
    else:
        if now - st.last_place_attempt >= _PLACE_RETRY_SECONDS:
            _place_and_record(trade, st, target, now)
        _note_unprotected(trade, st, now, live=bool(trade.get("stop_order_id")))
    if trade["id"] in _states:  # not closed during this cycle
        _log_status(trade, st, price, high)
