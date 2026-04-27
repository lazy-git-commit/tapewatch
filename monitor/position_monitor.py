"""
monitor/position_monitor.py
────────────────────────────
Continuously monitors all open trades and fires a sell order when any of
the three exit conditions is met:

  1. Take profit  — current price >= buy_price × (1 + take_profit_pct/100)
  2. Stop loss    — current price <= buy_price × (1 - stop_loss_pct/100)
  3. Time stop    — position has been open for >= time_stop_minutes

Runs as a background job via APScheduler (called every 60 seconds).
"""

import logging
from datetime import datetime, timezone, timedelta
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


def check_exit_conditions(trade: dict) -> tuple[bool, str]:
    """
    Evaluate all three exit conditions for a single open trade.

    Returns (should_exit: bool, reason: str)
    """
    ticker = trade["ticker"]
    buy_price = trade["buy_price"]
    buy_time = _parse_utc(trade["buy_time"])
    now = datetime.now(timezone.utc)

    # ── Time stop ─────────────────────────────────────────────────────────────
    elapsed_minutes = (now - buy_time).total_seconds() / 60
    if elapsed_minutes >= cfg.time_stop_minutes:
        return True, "time_stop"

    # ── Fetch current price ───────────────────────────────────────────────────
    current_price = get_current_price(ticker)
    if current_price is None:
        logger.warning("Could not fetch price for %s — skipping exit check", ticker)
        return False, ""

    take_profit_threshold = buy_price * (1 + cfg.take_profit_pct / 100)
    stop_loss_threshold = buy_price * (1 - cfg.stop_loss_pct / 100)

    # ── Take profit ───────────────────────────────────────────────────────────
    if current_price >= take_profit_threshold:
        return True, "take_profit"

    # ── Stop loss ─────────────────────────────────────────────────────────────
    if current_price <= stop_loss_threshold:
        return True, "stop_loss"

    logger.debug(
        "Monitor [%s]: £%.4f | TP=£%.4f | SL=£%.4f | elapsed=%.1f min",
        ticker, current_price, take_profit_threshold, stop_loss_threshold, elapsed_minutes,
    )
    return False, ""


def monitor_positions() -> None:
    """
    Called by the scheduler every 60 seconds.
    Checks every open trade and executes sells as needed.
    """
    open_trades = get_open_trades()
    if not open_trades:
        return

    logger.info("Monitoring %d open position(s)...", len(open_trades))

    for trade in open_trades:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        quantity = trade["quantity"]
        buy_price = trade["buy_price"]

        should_exit, reason = check_exit_conditions(trade)
        if not should_exit:
            continue

        # Fetch the latest price to use as our sell price estimate
        current_price = get_current_price(ticker) or buy_price

        logger.info(
            "Exit triggered [%s] trade_id=%d reason=%s price=£%.4f",
            ticker, trade_id, reason, current_price,
        )

        result = sell(ticker, quantity, current_price, reason)

        if result.success:
            close_trade(trade_id, current_price, reason)
        else:
            logger.error(
                "Sell order FAILED for trade %d (%s): %s",
                trade_id, ticker, result.error,
            )
            # In a real system you might want to alert here (email, SMS, etc.)
