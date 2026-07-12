"""
monitor/position_monitor.py
────────────────────────────
Manages every open position. Runs every cfg.monitor_interval_seconds (5s).

Exit architecture (v20 — INVERTED from v14):

  STOP LOSS — handled by a RESTING STOP-MARKET ORDER placed at buy time
  (executor.place_stop_loss). The broker executes the instant price touches
  the stop: zero polling latency on the side where latency costs capital.
  v14-v19 rested the TAKE-PROFIT and polled the stop; the realized record
  proved that backwards — 1 resting-TP fill in 11 trades, versus stop-side
  slippage of −3.4% (VECO), −3.97% (CRCL: falling ~1%/min, the 20s poll gave
  it a 20-second head start) and −18.99% (GOAI) on −2% triggers. T212 has no
  OCO and each sell order reserves its shares, so only ONE side can rest: it
  must be the stop. The monitor only needs to NOTICE the stop fill
  (order-status check) and close the DB trade. If the resting order failed
  to place, the monitor falls back to the old polled stop for that position.

  TAKE PROFIT / TIME STOP / EOD FLATTEN — polled here, at the monitor
  cadence. Before any polled sell the resting stop MUST be cancelled (the
  shares are reserved by it; a second sell would be rejected — or worse,
  both could fill). The cancel/fill race is handled explicitly: if the
  cancel fails because the stop filled while we were cancelling, the
  position is closed as a stop_loss, not sold twice.

  BREAKEVEN RATCHET — once a position is up cfg.ratchet_trigger_pct, the
  resting stop is cancelled and re-placed at breakeven
  (buy × (1 + cfg.ratchet_lock_pct/100)), once per trade. A trade that has
  paid 1R may mean-revert, but it may not turn into a loser. If the
  replacement placement fails the position falls back to a POLLED breakeven
  stop; the armed flag is persisted on the trade row (v20.1) so a restart
  can't regress it, and a crash mid-move self-repairs — the ratchet never
  weakens protection.

  EOD FLATTEN — all positions are force-closed cfg.eod_flatten_minutes
  before the bell, regardless of P&L. This is a day-trading system: stops
  don't work overnight, and one earnings gap through a held position can
  erase a month of wins.

  LEGACY (pre-v20) trades that still carry a resting TP (tp_order_id) are
  managed under the old rules until they close — both regimes coexist
  during the deploy transition.

Price fetch failure policy:
  If get_current_price() returns None (both feeds down), the polled checks
  are skipped for that cycle. With a resting stop the downside is still
  protected broker-side during the outage. The time stop is evaluated first
  and needs no price data — every position exits eventually even in a full
  data outage.
"""

import logging
import time as _time
from datetime import datetime, timezone
import pytz
from market.price_check import get_current_price, is_market_open, minutes_until_close
from trading.executor import (
    sell, get_order_status, cancel_order, _fetch_fill, _parse_fill,
    get_broker_positions, place_stop_loss,
)
from storage.database import (
    get_open_trades, close_trade, set_tp_order_id, set_stop_order_id,
    set_ratchet_armed, touch_heartbeat, record_system_event,
)
from config.settings import cfg

logger = logging.getLogger(__name__)


_LONDON = pytz.timezone("Europe/London")

# ── Stuck-exit escalation ─────────────────────────────────────────────────────
# Bounded-slippage limit sells protect against thin-book collapses (the GOAI
# −19% market fill), but an unfilled limit that is retried forever is the
# OPPOSITE failure: on 2026-07-07 GLASF's exit limit was priced off a frozen
# quote sitting above the real market, and the monitor placed-and-cancelled
# 459 consecutive limit sells over 5h14m while the position sat unmanaged.
# After this many consecutive failed limit attempts for the SAME trade, the
# next attempt goes straight to a market order — execution certainty now beats
# slippage control, exactly like the EOD flatten. The counter resets on any
# successful sell or when the position closes.
_SELL_ESCALATE_AFTER = 3
_sell_fail_counts: dict[int, int] = {}  # trade_id → consecutive failed sell attempts

# ── Breakeven ratchet state (v20, persisted v20.1) ──────────────────────────
# The armed flag lives ON the trade row (trades.ratchet_armed), not in memory:
# an in-memory set silently regressed protection to the original −2% polled
# stop after a service restart when the breakeven re-place had failed. The
# ratchet is also crash-recoverable: it only marks armed AFTER the move
# resolves, so a crash mid-move leaves an un-armed trade that simply
# re-ratchets on the next cycle where price is still above the trigger.

# ── Reconcile throttle ────────────────────────────────────────────────────────
# The monitor runs every 5s (v20), but the /equity/portfolio comparison only
# needs to catch slow drift — throttle it so the faster exit loop doesn't
# multiply T212 API load.
_RECONCILE_EVERY_SECONDS = 60.0
_last_reconcile_ts = 0.0

# ── Resting-order status-check throttle (v20.1) ──────────────────────────────
# Checking the resting order's status is bookkeeping (noticing a fill /
# expiry), not protection — the money is already guarded broker-side. At the
# 5s cadence a per-cycle check bursts 2-3 back-to-back T212 order lookups
# every cycle, courting 429s. 15s keeps fill-notice latency trivial while
# cutting the order-endpoint load 3×. Between checks the order is presumed
# live (the safe assumption — identical to the status=None network-error
# handling). Entries are popped when the trade closes.
_STATUS_CHECK_EVERY_SECONDS = 15.0
_last_status_check: dict[int, float] = {}


def _note_sell_failed(trade_id: int, ticker: str) -> bool:
    """Record a failed sell attempt; True when the next attempt must escalate."""
    fails = _sell_fail_counts.get(trade_id, 0) + 1
    _sell_fail_counts[trade_id] = fails
    if fails == _SELL_ESCALATE_AFTER:
        logger.error(
            "Exit STUCK [%s] trade=%d: %d consecutive limit sells unfilled — "
            "escalating to MARKET order next attempt",
            ticker, trade_id, fails,
        )
        record_system_event(
            "exit_stuck",
            f"{ticker} trade={trade_id}: {fails} consecutive limit sells "
            f"unfilled — escalated to market order",
        )
    return fails >= _SELL_ESCALATE_AFTER


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        # Timestamps are written by _now_london() — treat naive strings as London
        # rather than UTC so a missing offset doesn't introduce a 1-hour error
        # during BST when computing elapsed time against datetime.now(utc).
        dt = _LONDON.localize(dt)
    return dt.astimezone(timezone.utc)


def _stop_threshold(trade: dict) -> float:
    """The polled stop price for a trade: breakeven once ratcheted, else −stop%."""
    buy_price = trade["buy_price"]
    normal = buy_price * (1 - cfg.stop_loss_pct / 100)
    if trade.get("ratchet_armed"):
        return max(normal, buy_price * (1 + cfg.ratchet_lock_pct / 100))
    return normal


def check_exit_conditions(
    trade: dict,
    has_resting_tp: bool = False,
    has_resting_stop: bool = False,
) -> tuple[bool, str, float | None]:
    """
    Evaluate polled exit conditions for a single open trade.

    Returns (should_exit: bool, reason: str, current_price: float | None)

    has_resting_tp   — legacy (pre-v20) trades: a resting limit order owns the
                       profit side, so the polled TP branch is SKIPPED (polling
                       it too would double-sell the same shares the moment
                       price touches the target).
    has_resting_stop — v20 trades: a resting stop order owns the loss side, so
                       the polled STOP branch is skipped for the same reason.

    current_price is None only when all price feeds are unavailable.
    Time-stop fires regardless of price feed availability.
    """
    ticker = trade["ticker"]
    buy_price = trade["buy_price"]
    buy_time = _parse_utc(trade["buy_time"])
    now = datetime.now(timezone.utc)

    elapsed_minutes = (now - buy_time).total_seconds() / 60

    # ── Time stop — evaluated FIRST, requires no price data ──────────────────
    if elapsed_minutes >= cfg.time_stop_minutes:
        # Still try to get current price for accurate P&L; if unavailable,
        # fall back to buy_price (will record as 0% P&L — better than not exiting)
        current_price = get_current_price(ticker)
        if current_price is None:
            logger.warning(
                "Monitor [%s] trade=%d: time_stop triggered but price feed down — "
                "selling at buy_price=$%.4f for P&L estimate",
                ticker, trade["id"], buy_price,
            )
            current_price = buy_price
        return True, "time_stop", current_price

    # ── Fetch current price ───────────────────────────────────────────────────
    current_price = get_current_price(ticker)
    if current_price is None:
        # Both Finnhub and Twelvedata unavailable — skip price checks this cycle.
        # Time-stop is still active (handled above) so the position will exit;
        # a resting stop keeps protecting the downside broker-side meanwhile.
        logger.warning(
            "Monitor [%s] trade=%d: price feed unavailable (Finnhub + Twelvedata both down) "
            "— skipping polled TP/SL check this cycle (elapsed=%.1f min, time_stop=%d min)",
            ticker, trade["id"], elapsed_minutes, cfg.time_stop_minutes,
        )
        return False, "", None

    take_profit_threshold = buy_price * (1 + cfg.take_profit_pct / 100)
    stop_loss_threshold = _stop_threshold(trade)
    pct_from_buy = ((current_price - buy_price) / buy_price) * 100

    # ── Take profit (polled — the resting side is the STOP since v20) ────────
    if not has_resting_tp and current_price >= take_profit_threshold:
        logger.info(
            "Monitor [%s] trade=%d: TAKE PROFIT (polled) triggered — "
            "price=$%.4f (+%.2f%%) >= threshold=$%.4f",
            ticker, trade["id"], current_price, pct_from_buy, take_profit_threshold,
        )
        return True, "take_profit", current_price

    # ── Stop loss (fallback path — only when no resting stop is live) ────────
    if not has_resting_stop and current_price <= stop_loss_threshold:
        logger.info(
            "Monitor [%s] trade=%d: STOP LOSS (polled) triggered — "
            "price=$%.4f (%.2f%%) <= threshold=$%.4f%s",
            ticker, trade["id"], current_price, pct_from_buy, stop_loss_threshold,
            " (breakeven ratchet)" if trade.get("ratchet_armed") else "",
        )
        return True, "stop_loss", current_price

    logger.debug(
        "Monitor [%s] trade=%d: holding — price=$%.4f (%+.2f%%) "
        "TP=$%.4f SL=$%.4f elapsed=%.1f min",
        ticker, trade["id"], current_price, pct_from_buy,
        take_profit_threshold, stop_loss_threshold, elapsed_minutes,
    )
    return False, "", current_price


# ── Resting-order bookkeeping (shared by the TP-legacy and stop regimes) ─────

def _resting_kind(trade: dict) -> tuple[str, str] | tuple[None, None]:
    """(kind, order_id) of the trade's resting order — ("stop"|"tp"), or Nones."""
    if trade.get("stop_order_id"):
        return "stop", trade["stop_order_id"]
    if trade.get("tp_order_id"):
        return "tp", trade["tp_order_id"]
    return None, None


def _clear_resting(trade: dict, kind: str) -> None:
    """Clear the resting-order id (DB + in-memory dict) for `kind`."""
    try:
        if kind == "stop":
            set_stop_order_id(trade["id"], None)
        else:
            set_tp_order_id(trade["id"], None)
    except Exception:
        pass
    trade["stop_order_id" if kind == "stop" else "tp_order_id"] = None


def _close_as_resting_fill(trade: dict, order_id: str, kind: str, fill: dict | None = None) -> None:
    """
    The resting order filled at the broker — record the close with real fill
    data. Fallback price when fill detail is unavailable: the order's own
    threshold. For a TP limit that is conservative (limits fill at/above
    their price); for a stop-market it is an ESTIMATE (stops can fill below
    their trigger) — logged as such.
    """
    if fill is None:
        fill = _fetch_fill(order_id)
    filled_price, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
    if kind == "tp":
        reason = "take_profit"
        threshold = trade["buy_price"] * (1 + cfg.take_profit_pct / 100)
    else:
        reason = "stop_loss"
        threshold = _stop_threshold(trade)
    sell_price = filled_price if filled_price is not None else threshold
    if filled_price is None:
        logger.warning(
            "Monitor [%s] trade=%d: resting %s filled but fill detail unavailable — "
            "recording at threshold $%.4f%s",
            trade["ticker"], trade["id"], kind, threshold,
            " (stop-market may have filled lower)" if kind == "stop" else "",
        )
    logger.info(
        "Monitor [%s] trade=%d: resting %s FILLED @ $%.4f",
        trade["ticker"], trade["id"], kind.upper(), sell_price,
    )
    close_trade(
        trade["id"], sell_price, reason,
        sell_order_id=order_id,
        sell_net_gbp=net_gbp, sell_fx_rate=fx_rate, sell_fees_gbp=fees_gbp,
    )
    _sell_fail_counts.pop(trade["id"], None)
    _last_status_check.pop(trade["id"], None)


def _handle_gone_resting_order(trade: dict, order_id: str, kind: str) -> bool:
    """
    Resolve a resting order that disappeared from the pending-order endpoint.

    T212 returns 404 for orders that are no longer live. That is usually a
    fill, but DAY orders can also expire/cancel. We only close the DB trade
    when fill detail exists; otherwise we clear the stale order id and let
    the normal polled exits manage the still-open position.

    Returns True when the trade was closed, False when monitoring should
    continue without a resting order.
    """
    fill = _fetch_fill(order_id)
    if fill:
        _close_as_resting_fill(trade, order_id, kind, fill=fill)
        return True
    logger.warning(
        "Monitor [%s] trade=%d: resting %s order %s is gone but no fill was found — "
        "treating it as expired/cancelled and reverting to polled exits",
        trade["ticker"], trade["id"], kind, order_id,
    )
    _clear_resting(trade, kind)
    return False


def _cancel_resting_before_sell(trade: dict) -> bool:
    """
    Cancel the trade's resting order (stop or legacy TP) so its reserved
    shares are free to sell.

    Returns True if it is safe to proceed with the sell.
    Returns False if the resting order turned out to be FILLED during the
    cancel (the race) — in that case the trade has already been closed here
    with the resting order's own reason and there is nothing left to sell —
    or if the order state is unknown (defer to next cycle).
    """
    kind, order_id = _resting_kind(trade)
    if not order_id:
        return True

    if cancel_order(order_id):
        _clear_resting(trade, kind)
        return True

    # Cancel failed — the dominant cause is that the order filled while we
    # were cancelling. Re-check before doing anything irreversible.
    status = get_order_status(order_id)
    if status == "FILLED":
        _close_as_resting_fill(trade, order_id, kind)
        return False
    if status == "GONE":
        if _handle_gone_resting_order(trade, order_id, kind):
            return False
        return True  # expired, not filled — safe to sell
    # Unknown state (network error): do NOT sell — the resting order may
    # still be live and a second sell would double-exit. Retry next cycle.
    logger.warning(
        "Monitor [%s] trade=%d: resting %s order %s in unknown state during cancel — "
        "deferring exit to next cycle",
        trade["ticker"], trade["id"], kind, order_id,
    )
    return False


def _maybe_ratchet_stop(trade: dict, current_price: float | None) -> None:
    """
    Move the resting stop to breakeven once the position is up
    cfg.ratchet_trigger_pct — once per trade.

    A trade that has paid one full risk unit must not be allowed to turn into
    a loser: the most common non-winner shape in this system's history is
    "up nicely, then bled out through the stop" (AVGO/MRVL/IBM class). The
    cancel/fill race is handled like every other resting-order interaction.
    If the replacement stop cannot be placed the position falls back to a
    POLLED breakeven stop (the armed flag drives _stop_threshold), so the
    ratchet can only ever tighten protection, never lose it.

    Crash-safety (v20.1): the armed flag is persisted on the trade row, and
    only AFTER the move resolves. A crash between cancel and re-place leaves
    an un-armed trade with no resting stop — which this function repairs on
    the next cycle price is still above the trigger, by placing a fresh
    breakeven stop directly (no old order to cancel). Protection can dip to
    the polled −2% stop for one cycle, never disappear, never stay regressed.
    """
    trade_id = trade["id"]
    if current_price is None or trade.get("ratchet_armed"):
        return
    if trade.get("tp_order_id"):
        return  # legacy pre-v20 trade — never mix a stop into the TP regime
    buy_price = trade["buy_price"]
    if current_price < buy_price * (1 + cfg.ratchet_trigger_pct / 100):
        return

    old_order_id = trade.get("stop_order_id")
    breakeven = buy_price * (1 + cfg.ratchet_lock_pct / 100)
    logger.info(
        "Monitor [%s] trade=%d: +%.2f%% ≥ ratchet trigger %.2f%% — moving stop "
        "to breakeven $%.4f",
        trade["ticker"], trade_id,
        (current_price - buy_price) / buy_price * 100,
        cfg.ratchet_trigger_pct, breakeven,
    )

    if old_order_id:
        if cancel_order(old_order_id):
            _clear_resting(trade, "stop")
        else:
            status = get_order_status(old_order_id)
            if status == "FILLED":
                # Whipsaw: price hit the stop while we saw it above the trigger.
                _close_as_resting_fill(trade, old_order_id, "stop")
                return
            if status == "GONE":
                if _handle_gone_resting_order(trade, old_order_id, "stop"):
                    return  # filled → trade closed
                # expired/cancelled — id cleared; fall through and place fresh.
            else:
                logger.warning(
                    "Monitor [%s] trade=%d: could not cancel stop %s for ratchet "
                    "(state unknown) — retrying next cycle",
                    trade["ticker"], trade_id, old_order_id,
                )
                return

    new_order_id = place_stop_loss(trade["ticker"], trade["quantity"], breakeven)
    if new_order_id:
        try:
            set_stop_order_id(trade_id, new_order_id)
            trade["stop_order_id"] = new_order_id
        except Exception as exc:
            logger.error(
                "Could not store ratcheted stop id %s for trade %d: %s — cancelling it; "
                "polled breakeven stop takes over",
                new_order_id, trade_id, exc,
            )
            cancel_order(new_order_id)
            new_order_id = None
    if not new_order_id:
        logger.error(
            "Monitor [%s] trade=%d: breakeven stop placement FAILED — falling back "
            "to POLLED breakeven stop",
            trade["ticker"], trade_id,
        )
    # Arm LAST, and durably: from here the polled threshold is breakeven even
    # across restarts, whether or not the resting re-place succeeded.
    try:
        set_ratchet_armed(trade_id)
    except Exception as exc:
        logger.warning("set_ratchet_armed failed for trade %d: %s", trade_id, exc)
    trade["ratchet_armed"] = 1


def _reconcile_positions(db_open_trades: list[dict]) -> None:
    """
    Compare DB-open trades against the broker's live portfolio. Log any
    divergence at CRITICAL level — these require manual investigation.

    Never auto-reconciles: a transient /equity/portfolio timeout looks
    identical to "broker has no positions" — auto-closing on that would
    flatten real positions. Alerting and letting a human decide is safer.

    Throttled to once per _RECONCILE_EVERY_SECONDS: the 5s monitor cadence
    (v20) exists for exit latency, not for multiplying portfolio calls.
    """
    global _last_reconcile_ts
    now = _time.monotonic()
    if now - _last_reconcile_ts < _RECONCILE_EVERY_SECONDS:
        return
    _last_reconcile_ts = now

    broker_positions = get_broker_positions()
    if broker_positions is None:
        # API failure — skip this cycle rather than false-alerting.
        return

    db_tickers = {trade["ticker"] for trade in db_open_trades}
    broker_tickers = set(broker_positions.keys())

    # Phantom: DB says open, broker says flat.
    for trade in db_open_trades:
        ticker = trade["ticker"]
        if ticker not in broker_tickers:
            logger.critical(
                "RECONCILIATION: trade %d (%s qty=%.4f) is OPEN in DB but NOT in "
                "broker portfolio — possible missed close_trade() after a sell. "
                "Manual review required: either close DB record if position is "
                "truly flat, or investigate if the sell failed silently.",
                trade["id"], ticker, trade["quantity"],
            )

    # Orphan: broker says open, DB says flat.
    for ticker, qty in broker_positions.items():
        if ticker not in db_tickers:
            logger.critical(
                "RECONCILIATION: broker holds %.4f of %s but NO open trade in DB "
                "— possible failed open_trade() after a buy fill, or manual trade. "
                "Manual review required: close the position manually if it is an "
                "orphan, or add a DB record if it was a manual entry.",
                qty, ticker,
            )


def monitor_positions() -> None:
    """
    Called by the scheduler every cfg.monitor_interval_seconds.
    Checks every open trade and executes exits as needed.
    """
    try:
        touch_heartbeat("monitor")
    except Exception:
        pass  # liveness reporting must never break position management

    try:
        open_trades = get_open_trades()
    except Exception as exc:
        logger.error("monitor_positions: failed to fetch open trades from DB: %s", exc)
        return

    if not open_trades:
        return

    # ── Broker reconciliation (throttled) ─────────────────────────────────────
    # The DB is the system's source of truth for position management, but it
    # can drift from the broker after a DB write failure or a kill -9.
    # Phantom (DB-open, broker-flat) and orphan (broker-open, DB-flat)
    # divergences are logged CRITICAL; never auto-reconciled (a transient API
    # error would make every DB-open trade look like a phantom).
    _reconcile_positions(open_trades)

    # ── Market-hours guard ────────────────────────────────────────────────────
    # Selling into a closed market queues orders blind into the next open.
    # With the EOD flatten below this should never trigger; if it does, shout.
    if not is_market_open():
        logger.error(
            "monitor_positions: %d position(s) OPEN while market is CLOSED — "
            "EOD flatten was missed (service down at close?). Positions carry "
            "overnight gap risk and will be closed at the next open.",
            len(open_trades),
        )
        return

    # ── EOD flatten window? ───────────────────────────────────────────────────
    mins_to_close = minutes_until_close()
    eod_flatten = mins_to_close is not None and mins_to_close <= cfg.eod_flatten_minutes
    if eod_flatten:
        logger.info(
            "── EOD flatten: %.1f min to close — force-closing %d position(s) ──",
            mins_to_close, len(open_trades),
        )

    logger.info(
        "── Position monitor: %d open position(s) ──────────────────",
        len(open_trades),
    )

    for trade in open_trades:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        quantity = trade["quantity"]
        buy_price = trade["buy_price"]

        # ── 1. Resting-order bookkeeping (stop for v20 trades, TP legacy) ────
        # Status checks are throttled to _STATUS_CHECK_EVERY_SECONDS: between
        # checks the order is presumed live (same safe assumption as the
        # status=None network-error path) — fills are still noticed within
        # 15s, and the T212 order endpoint isn't hit 3× per 5s cycle.
        kind, order_id = _resting_kind(trade)
        has_resting_stop = False
        has_resting_tp = False
        now_mono = _time.monotonic()
        status_due = (
            now_mono - _last_status_check.get(trade_id, 0.0)
            >= _STATUS_CHECK_EVERY_SECONDS
        )
        if order_id and not status_due:
            has_resting_stop = kind == "stop"
            has_resting_tp = kind == "tp"
        elif order_id:
            _last_status_check[trade_id] = now_mono
            status = get_order_status(order_id)
            if status == "FILLED":
                # The resting side executed at the broker — just record it.
                try:
                    _close_as_resting_fill(trade, order_id, kind)
                except Exception as exc:
                    logger.error(
                        "monitor_positions: failed to record resting %s fill for trade %d (%s): %s",
                        kind, trade_id, ticker, exc, exc_info=True,
                    )
                continue
            elif status == "GONE":
                # Disappeared is not automatically filled; verify fill detail
                # before closing the DB trade.
                try:
                    if _handle_gone_resting_order(trade, order_id, kind):
                        continue
                except Exception as exc:
                    logger.error(
                        "monitor_positions: failed to resolve gone %s order for trade %d (%s): %s",
                        kind, trade_id, ticker, exc, exc_info=True,
                    )
                    continue
            elif status in ("CANCELLED", "REJECTED"):
                # Resting order died (e.g. DAY validity expired) — fall back
                # to the polled check for this position from now on.
                logger.warning(
                    "Monitor [%s] trade=%d: resting %s %s is %s — reverting to polled %s",
                    ticker, trade_id, kind, order_id, status, kind,
                )
                _clear_resting(trade, kind)
            elif status is None:
                # Unknown (network error): assume it is still live so we
                # don't double-sell; skip the corresponding polled branch.
                has_resting_stop = kind == "stop"
                has_resting_tp = kind == "tp"
            else:
                # NEW / CONFIRMED / etc. — resting fine.
                has_resting_stop = kind == "stop"
                has_resting_tp = kind == "tp"

        # ── 2. Decide whether to exit ────────────────────────────────────────
        if eod_flatten:
            should_exit, reason = True, "eod_flatten"
            current_price = get_current_price(ticker) or buy_price
        else:
            try:
                should_exit, reason, current_price = check_exit_conditions(
                    trade,
                    has_resting_tp=has_resting_tp,
                    has_resting_stop=has_resting_stop,
                )
            except Exception as exc:
                logger.error(
                    "monitor_positions: unhandled error checking exit for trade %d (%s): %s",
                    trade_id, ticker, exc, exc_info=True,
                )
                continue

        if not should_exit:
            # ── 2b. Breakeven ratchet ────────────────────────────────────────
            # Called unconditionally: the function self-guards (already armed,
            # legacy TP regime, below trigger), and running it even when NO
            # resting stop exists is what repairs a crash that landed between
            # the ratchet's cancel and re-place — it places a fresh breakeven
            # stop directly.
            try:
                _maybe_ratchet_stop(trade, current_price)
            except Exception as exc:
                logger.error(
                    "monitor_positions: ratchet failed for trade %d (%s): %s",
                    trade_id, ticker, exc, exc_info=True,
                )
            continue

        # ── 3. Free the shares: cancel the resting order (handles the race) ─
        try:
            if not _cancel_resting_before_sell(trade):
                continue  # resting order filled during cancel — already closed, or deferred
        except Exception as exc:
            logger.error(
                "monitor_positions: resting-order cancel handling failed for trade %d (%s): %s",
                trade_id, ticker, exc, exc_info=True,
            )
            continue

        sell_price = current_price if current_price is not None else buy_price

        logger.info(
            "Exit triggered [%s] trade=%d reason=%s buy=$%.4f current=$%.4f qty=%.4f",
            ticker, trade_id, reason, buy_price, sell_price, quantity,
        )

        # ── 4. Sell (bounded-slippage limit, market fallback inside sell()) ──
        # EOD flatten must use a market order — execution certainty beats
        # slippage control at the close. Pass force_market=True explicitly so
        # the routing doesn't rely on the "eod_flatten" string literal alone.
        # Stuck-exit escalation: after _SELL_ESCALATE_AFTER consecutive
        # unfilled limit attempts, this trade also goes market (see top of file).
        escalate = _sell_fail_counts.get(trade_id, 0) >= _SELL_ESCALATE_AFTER
        try:
            result = sell(
                ticker, quantity, sell_price, reason,
                force_market=(reason == "eod_flatten" or escalate),
            )
        except Exception as exc:
            logger.error(
                "monitor_positions: sell() raised exception for trade %d (%s): %s",
                trade_id, ticker, exc, exc_info=True,
            )
            continue

        if result.success:
            _sell_fail_counts.pop(trade_id, None)
            _last_status_check.pop(trade_id, None)
            try:
                close_trade(
                    trade_id, result.price, reason,
                    sell_order_id=result.order_id,
                    sell_net_gbp=result.net_gbp,
                    sell_fx_rate=result.fx_rate,
                    sell_fees_gbp=result.fees_gbp,
                )
            except Exception as exc:
                logger.error(
                    "monitor_positions: close_trade() failed for trade %d (%s) "
                    "after successful sell — trade may appear open in DB: %s",
                    trade_id, ticker, exc, exc_info=True,
                )
        else:
            # Unfilled bounded-limit sells land here by design — the next
            # cycle retries at the then-current price, and repeated
            # failures escalate to a market order via _note_sell_failed.
            will_escalate = _note_sell_failed(trade_id, ticker)
            logger.error(
                "Sell not completed for trade %d (%s): %s — will retry next cycle%s",
                trade_id, ticker, result.error,
                " AS MARKET ORDER" if will_escalate else "",
            )
