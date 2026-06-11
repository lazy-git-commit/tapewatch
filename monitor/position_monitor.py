"""
monitor/position_monitor.py
────────────────────────────
Continuously monitors all open trades and fires a sell order when any of
the three exit conditions is met:

  1. Take profit  — current price >= buy_price × (1 + take_profit_pct/100)
  2. Stop loss    — current price <= buy_price × (1 - stop_loss_pct/100)
  3. Time stop    — position has been open for >= time_stop_minutes

Runs as a background job via APScheduler (called every 60 seconds).

Price fetch failure policy:
  If get_current_price() returns None (both Finnhub and Twelvedata down),
  we skip the price-based checks (take-profit and stop-loss) for that cycle.
  The time-stop is always evaluated first — this guarantees every position
  exits eventually even if all price feeds are down for an extended period.
  We log a WARNING on each skipped cycle so the user can see the outage.
"""

import logging
from datetime import datetime, timezone
from market.price_check import get_current_price
from trading.executor import sell
from storage.database import get_open_trades, close_trade
from config.settings import cfg

logger = logging.getLogger(__name__)


def _parse_utc(iso_str: str) -> datetime:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_exit_conditions(trade: dict) -> tuple[bool, str, float | None]:
    """
    Evaluate all three exit conditions for a single open trade.

    Returns (should_exit: bool, reason: str, current_price: float | None)

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
                "selling at buy_price=£%.4f for P&L estimate",
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

    # ── Take profit ───────────────────────────────────────────────────────────
    if current_price >= take_profit_threshold:
        logger.info(
            "Monitor [%s] trade=%d: TAKE PROFIT triggered — "
            "price=£%.4f (+%.2f%%) >= threshold=£%.4f",
            ticker, trade["id"], current_price, pct_from_buy, take_profit_threshold,
        )
        return True, "take_profit", current_price

    # ── Stop loss ─────────────────────────────────────────────────────────────
    if current_price <= stop_loss_threshold:
        logger.info(
            "Monitor [%s] trade=%d: STOP LOSS triggered — "
            "price=£%.4f (%.2f%%) <= threshold=£%.4f",
            ticker, trade["id"], current_price, pct_from_buy, stop_loss_threshold,
        )
        return True, "stop_loss", current_price

    logger.debug(
        "Monitor [%s] trade=%d: holding — price=£%.4f (%+.2f%%) "
        "TP=£%.4f SL=£%.4f elapsed=%.1f min",
        ticker, trade["id"], current_price, pct_from_buy,
        take_profit_threshold, stop_loss_threshold, elapsed_minutes,
    )
    return False, "", current_price


def monitor_positions() -> None:
    """
    Called by the scheduler every 60 seconds.
    Checks every open trade and executes sells as needed.
    """
    try:
        open_trades = get_open_trades()
    except Exception as exc:
        logger.error("monitor_positions: failed to fetch open trades from DB: %s", exc)
        return

    if not open_trades:
        return

    logger.info(
        "── Position monitor: %d open position(s) ──────────────────",
        len(open_trades),
    )

    for trade in open_trades:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        quantity = trade["quantity"]
        buy_price = trade["buy_price"]
        buy_time = trade["buy_time"]

        try:
            should_exit, reason, current_price = check_exit_conditions(trade)
        except Exception as exc:
            logger.error(
                "monitor_positions: unhandled error checking exit for trade %d (%s): %s",
                trade_id, ticker, exc, exc_info=True,
            )
            continue

        if not should_exit:
            continue

        # current_price is guaranteed non-None when should_exit is True
        # (time-stop sets it to buy_price as fallback if feeds are down)
        sell_price = current_price if current_price is not None else buy_price

        logger.info(
            "Exit triggered [%s] trade=%d reason=%s "
            "buy=£%.4f current=£%.4f qty=%.4f bought=%s",
            ticker, trade_id, reason, buy_price, sell_price, quantity, buy_time,
        )

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
            logger.error(
                "Sell order FAILED for trade %d (%s): %s — position remains open",
                trade_id, ticker, result.error,
            )
