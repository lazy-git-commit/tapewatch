"""
monitor/position_monitor.py
────────────────────────────
Manages every open position. Runs every cfg.monitor_interval_seconds (20s).

Exit architecture (v14):

  TAKE PROFIT — handled by a RESTING LIMIT ORDER placed at buy time
  (executor.place_take_profit). The exchange fills it the instant price
  touches the target: zero polling latency, zero spread-crossing. The monitor
  only needs to NOTICE the fill (order status check) and close the DB trade.
  If the resting order failed to place, the monitor falls back to the old
  polled TP check for that position.

  STOP LOSS / TIME STOP / EOD FLATTEN — polled here. Before selling, any
  resting TP order MUST be cancelled (T212 has no OCO; the shares are
  reserved by the resting order, and a second sell would be rejected — or
  worse, both could fill). The cancel/fill race is handled explicitly:
  if the cancel fails because the TP filled while we were cancelling,
  the position is closed as a take_profit, not sold twice.

  EOD FLATTEN — all positions are force-closed cfg.eod_flatten_minutes
  before the bell, regardless of P&L. This is a day-trading system: stops
  don't work overnight, and one earnings gap through a held position can
  erase a month of wins.

Price fetch failure policy:
  If get_current_price() returns None (both feeds down), the SL check is
  skipped for that cycle. The time stop is evaluated first and needs no
  price data — every position exits eventually even in a full data outage.
"""

import logging
from datetime import datetime, timezone
import pytz
from market.price_check import get_current_price, is_market_open, minutes_until_close
from trading.executor import (
    sell, get_order_status, cancel_order, _fetch_fill, _parse_fill,
    get_broker_positions,
)
from storage.database import (
    get_open_trades, close_trade, set_tp_order_id, touch_heartbeat,
    record_system_event,
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


def check_exit_conditions(trade: dict, has_resting_tp: bool = False) -> tuple[bool, str, float | None]:
    """
    Evaluate polled exit conditions for a single open trade.

    Returns (should_exit: bool, reason: str, current_price: float | None)

    has_resting_tp — when True, the take-profit branch is SKIPPED: a resting
    limit order owns the profit side, and polling it too would double-sell
    the same shares the moment price touches the target.

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
        # Time-stop is still active (handled above) so the position will exit.
        logger.warning(
            "Monitor [%s] trade=%d: price feed unavailable (Finnhub + Twelvedata both down) "
            "— skipping TP/SL check this cycle (elapsed=%.1f min, time_stop=%d min)",
            ticker, trade["id"], elapsed_minutes, cfg.time_stop_minutes,
        )
        return False, "", None

    take_profit_threshold = buy_price * (1 + cfg.take_profit_pct / 100)
    stop_loss_threshold = buy_price * (1 - cfg.stop_loss_pct / 100)
    pct_from_buy = ((current_price - buy_price) / buy_price) * 100

    # ── Take profit (fallback path only — see has_resting_tp docstring) ──────
    if not has_resting_tp and current_price >= take_profit_threshold:
        logger.info(
            "Monitor [%s] trade=%d: TAKE PROFIT (polled) triggered — "
            "price=$%.4f (+%.2f%%) >= threshold=$%.4f",
            ticker, trade["id"], current_price, pct_from_buy, take_profit_threshold,
        )
        return True, "take_profit", current_price

    # ── Stop loss ─────────────────────────────────────────────────────────────
    if current_price <= stop_loss_threshold:
        logger.info(
            "Monitor [%s] trade=%d: STOP LOSS triggered — "
            "price=$%.4f (%.2f%%) <= threshold=$%.4f",
            ticker, trade["id"], current_price, pct_from_buy, stop_loss_threshold,
        )
        return True, "stop_loss", current_price

    logger.debug(
        "Monitor [%s] trade=%d: holding — price=$%.4f (%+.2f%%) "
        "TP=$%.4f SL=$%.4f elapsed=%.1f min",
        ticker, trade["id"], current_price, pct_from_buy,
        take_profit_threshold, stop_loss_threshold, elapsed_minutes,
    )
    return False, "", current_price


def _close_as_tp_fill(trade: dict, tp_order_id: str, fill: dict | None = None) -> None:
    """
    The resting TP order filled — record the close with real fill data.
    If fill detail is unavailable, fall back to the TP threshold price: a
    limit sell can only fill AT or ABOVE its limit, so this is conservative.
    """
    if fill is None:
        fill = _fetch_fill(tp_order_id)
    filled_price, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
    tp_threshold = trade["buy_price"] * (1 + cfg.take_profit_pct / 100)
    sell_price = filled_price if filled_price is not None else tp_threshold
    if filled_price is None:
        logger.warning(
            "Monitor [%s] trade=%d: TP filled but fill detail unavailable — "
            "recording at limit price $%.4f",
            trade["ticker"], trade["id"], tp_threshold,
        )
    logger.info(
        "Monitor [%s] trade=%d: resting TP FILLED @ $%.4f",
        trade["ticker"], trade["id"], sell_price,
    )
    close_trade(
        trade["id"], sell_price, "take_profit",
        sell_order_id=tp_order_id,
        sell_net_gbp=net_gbp, sell_fx_rate=fx_rate, sell_fees_gbp=fees_gbp,
    )


def _handle_gone_tp_order(trade: dict, tp_order_id: str) -> bool:
    """
    Resolve a TP order that disappeared from the pending-order endpoint.

    T212 returns 404 for orders that are no longer live. That is usually a
    fill, but DAY orders can also expire/cancel. We only close the DB trade as
    take_profit when fill detail exists; otherwise we clear the stale TP id and
    let the normal polled exits manage the still-open position.

    Returns True when the trade was closed, False when monitoring should
    continue without a resting TP.
    """
    fill = _fetch_fill(tp_order_id)
    if fill:
        _close_as_tp_fill(trade, tp_order_id, fill=fill)
        return True
    logger.warning(
        "Monitor [%s] trade=%d: TP order %s is gone but no fill was found — "
        "treating it as expired/cancelled and reverting to polled exits",
        trade["ticker"], trade["id"], tp_order_id,
    )
    try:
        set_tp_order_id(trade["id"], None)
    except Exception:
        pass
    trade["tp_order_id"] = None
    return False


def _cancel_tp_before_sell(trade: dict) -> bool:
    """
    Cancel the resting TP order so its reserved shares are free to sell.

    Returns True if it is safe to proceed with the sell.
    Returns False if the TP turned out to be FILLED during the cancel (the
    race) — in that case the trade has already been closed as take_profit
    here and there is nothing left to sell.
    """
    tp_order_id = trade.get("tp_order_id")
    if not tp_order_id:
        return True

    if cancel_order(tp_order_id):
        set_tp_order_id(trade["id"], None)
        return True

    # Cancel failed — the dominant cause is that the order filled while we
    # were cancelling. Re-check before doing anything irreversible.
    status = get_order_status(tp_order_id)
    if status in ("FILLED", "GONE"):
        if status == "FILLED":
            _close_as_tp_fill(trade, tp_order_id)
        elif _handle_gone_tp_order(trade, tp_order_id):
            return False
        else:
            return True
        return False
    # Unknown state (network error): do NOT sell — the resting order may
    # still be live and a second sell would double-exit. Retry next cycle.
    logger.warning(
        "Monitor [%s] trade=%d: TP order %s in unknown state during cancel — "
        "deferring exit to next cycle",
        trade["ticker"], trade["id"], tp_order_id,
    )
    return False


def _reconcile_positions(db_open_trades: list[dict]) -> None:
    """
    Compare DB-open trades against the broker's live portfolio. Log any
    divergence at CRITICAL level — these require manual investigation.

    Never auto-reconciles: a transient /equity/portfolio timeout looks
    identical to "broker has no positions" — auto-closing on that would
    flatten real positions. Alerting and letting a human decide is safer.
    """
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

    # ── Broker reconciliation ─────────────────────────────────────────────────
    # The DB is the system's source of truth for position management, but it
    # can drift from the broker after a DB write failure or a kill -9. Comparing
    # the two every cycle catches two dangerous divergences:
    #
    #   Phantom (DB-open, broker-flat): close_trade() failed after a successful
    #   sell. The monitor will keep trying to exit a position that no longer
    #   exists. We log CRITICAL — manual review required (we never auto-close
    #   these because a transient /portfolio API error looks identical).
    #
    #   Orphan (broker-open, DB-flat): buy() succeeded but open_trade() failed,
    #   and the emergency flatten also failed. An unmanaged live position with no
    #   stop or EOD logic. We log CRITICAL — manual intervention required.
    #
    # We only alert, never auto-reconcile, because a transient API error would
    # make every DB-open trade look like a phantom and trigger mass closes.
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
        tp_order_id = trade.get("tp_order_id")

        # ── 1. Resting TP bookkeeping ────────────────────────────────────────
        has_resting_tp = False
        if tp_order_id:
            status = get_order_status(tp_order_id)
            if status == "FILLED":
                # Profit side executed at the exchange — just record it.
                try:
                    _close_as_tp_fill(trade, tp_order_id)
                except Exception as exc:
                    logger.error(
                        "monitor_positions: failed to record TP fill for trade %d (%s): %s",
                        trade_id, ticker, exc, exc_info=True,
                    )
                continue
            elif status == "GONE":
                # Disappeared is not automatically filled; verify fill detail
                # before closing the DB trade as a take-profit.
                try:
                    if _handle_gone_tp_order(trade, tp_order_id):
                        continue
                    tp_order_id = None
                except Exception as exc:
                    logger.error(
                        "monitor_positions: failed to resolve gone TP order for trade %d (%s): %s",
                        trade_id, ticker, exc, exc_info=True,
                    )
                    continue
            elif status in ("CANCELLED", "REJECTED"):
                # Resting order died (e.g. DAY validity expired) — fall back
                # to polled TP for this position from now on.
                logger.warning(
                    "Monitor [%s] trade=%d: resting TP %s is %s — reverting to polled TP",
                    ticker, trade_id, tp_order_id, status,
                )
                try:
                    set_tp_order_id(trade_id, None)
                except Exception:
                    pass
                trade["tp_order_id"] = None
            elif status is None:
                # Unknown (network error): assume it is still live so we
                # don't double-sell; skip polled TP this cycle.
                has_resting_tp = True
            else:
                has_resting_tp = True  # NEW / CONFIRMED / etc. — resting fine

        # ── 2. Decide whether to exit ────────────────────────────────────────
        if eod_flatten:
            should_exit, reason = True, "eod_flatten"
            current_price = get_current_price(ticker) or buy_price
        else:
            try:
                should_exit, reason, current_price = check_exit_conditions(
                    trade, has_resting_tp=has_resting_tp
                )
            except Exception as exc:
                logger.error(
                    "monitor_positions: unhandled error checking exit for trade %d (%s): %s",
                    trade_id, ticker, exc, exc_info=True,
                )
                continue

        if not should_exit:
            continue

        # ── 3. Free the shares: cancel resting TP (handles the fill race) ───
        try:
            if not _cancel_tp_before_sell(trade):
                continue  # TP filled during cancel — already closed, or deferred
        except Exception as exc:
            logger.error(
                "monitor_positions: TP cancel handling failed for trade %d (%s): %s",
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
            # cycle (20s) retries at the then-current price, and repeated
            # failures escalate to a market order via _note_sell_failed.
            will_escalate = _note_sell_failed(trade_id, ticker)
            logger.error(
                "Sell not completed for trade %d (%s): %s — will retry next cycle%s",
                trade_id, ticker, result.error,
                " AS MARKET ORDER" if will_escalate else "",
            )
