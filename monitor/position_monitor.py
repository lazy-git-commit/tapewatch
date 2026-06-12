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
from market.price_check import get_current_price, is_market_open, minutes_until_close
from trading.executor import (
    sell, get_order_status, cancel_order, _fetch_fill, _parse_fill,
)
from storage.database import (
    get_open_trades, close_trade, set_tp_order_id, touch_heartbeat,
)
from config.settings import cfg

logger = logging.getLogger(__name__)


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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


def _close_as_tp_fill(trade: dict, tp_order_id: str) -> None:
    """
    The resting TP order filled — record the close with real fill data.
    If fill detail is unavailable, fall back to the TP threshold price: a
    limit sell can only fill AT or ABOVE its limit, so this is conservative.
    """
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
        _close_as_tp_fill(trade, tp_order_id)
        return False
    # Unknown state (network error): do NOT sell — the resting order may
    # still be live and a second sell would double-exit. Retry next cycle.
    logger.warning(
        "Monitor [%s] trade=%d: TP order %s in unknown state during cancel — "
        "deferring exit to next cycle",
        trade["ticker"], trade["id"], tp_order_id,
    )
    return False


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
            if status in ("FILLED", "GONE"):
                # Profit side executed at the exchange — just record it.
                try:
                    _close_as_tp_fill(trade, tp_order_id)
                except Exception as exc:
                    logger.error(
                        "monitor_positions: failed to record TP fill for trade %d (%s): %s",
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
        try:
            result = sell(ticker, quantity, sell_price, reason)
        except Exception as exc:
            logger.error(
                "monitor_positions: sell() raised exception for trade %d (%s): %s",
                trade_id, ticker, exc, exc_info=True,
            )
            continue

        if result.success:
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
            # cycle (20s) retries at the then-current price.
            logger.error(
                "Sell not completed for trade %d (%s): %s — will retry next cycle",
                trade_id, ticker, result.error,
            )
