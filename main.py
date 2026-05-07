"""
main.py
────────
Entry point for the momentum trader.

Starts two scheduled jobs:
  1. news_cycle  — runs every cfg.news_poll_interval_minutes minutes
                   fetches Benzinga signals → price check → buy
  2. monitor_job — runs every 60 seconds
                   checks open positions → sell if exit condition met

Usage:
  python main.py

Press Ctrl+C to stop gracefully.
"""

import logging
import signal
import sys
import time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import cfg
from storage.database import init_db, save_signal, mark_signal_acted_on, open_trade, was_recently_traded
from news.fetcher import fetch_all_news
from market.price_check import confirm_price_signal, is_market_open
from trading.executor import buy
from monitor.position_monitor import monitor_positions
from reporting.report import generate_report

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def news_cycle() -> None:
    """
    The main trading pipeline — runs on a schedule.

      1. Fetch signals from Benzinga (WIIM + general news)
      2. Confirm price movement via yfinance
      3. Execute a buy via Trading 212 if confirmed
    """
    logger.info("── News cycle starting ──────────────────────────────────")

    if not is_market_open():
        logger.info("Market is closed — skipping cycle.")
        return

    news_items = fetch_all_news(lookback_hours=1)
    if not news_items:
        logger.info("No new articles found.")
        return

    logger.info("%d article(s) to evaluate.", len(news_items))

    for item in news_items:
        source_tag = "WIIM" if item.is_wiim else "news"
        logger.info(
            "Signal [%s][%s]: %s",
            item.ticker, source_tag, item.headline,
        )

        # Skip if already holding or bought in the last 24 hours
        if was_recently_traded(item.ticker):
            logger.info("Skipping %s — already traded within the last 24 hours", item.ticker)
            continue

        # Save signal to DB
        signal_id = save_signal(
            ticker=item.ticker,
            headline=item.headline,
            source=item.source,
            sentiment="BULLISH",
            confidence=9 if item.is_wiim else 7,
        )

        # Price confirmation
        confirmation = confirm_price_signal(item.ticker)
        if confirmation is None or not confirmation.is_confirmed:
            reason = confirmation.reason if confirmation else "price data unavailable"
            logger.info("Signal rejected for %s: %s", item.ticker, reason)
            continue

        logger.info("Signal approved for %s — placing buy order: %s", item.ticker, confirmation.reason)

        # Execute buy
        result = buy(item.ticker, confirmation.current_price)
        if not result.success:
            logger.error("Buy order failed for %s: %s", item.ticker, result.error)
            continue

        # Record trade in DB
        trade_id = open_trade(
            ticker=item.ticker,
            signal_id=signal_id,
            quantity=result.quantity,
            buy_price=result.price,
        )
        mark_signal_acted_on(signal_id)

        logger.info(
            "Trade #%d opened: %s × %.6f @ £%.4f",
            trade_id, item.ticker, result.quantity, result.price,
        )

    logger.info("── News cycle complete ──────────────────────────────────")


def main() -> None:
    logger.info("=" * 60)
    logger.info("  MOMENTUM TRADER STARTING")
    logger.info("  Mode: %s", cfg.trading_mode.upper())
    logger.info("  Blocklist: %s", ", ".join(cfg.blocklist) if cfg.blocklist else "none")
    logger.info("=" * 60)

    # Validate config and initialise DB
    cfg.validate()
    init_db()

    # Print current report on startup
    print(generate_report())

    # ── Scheduler ─────────────────────────────────────────────────────────────
    scheduler = BackgroundScheduler(timezone=pytz.utc)

    scheduler.add_job(
        news_cycle,
        trigger=IntervalTrigger(minutes=cfg.news_poll_interval_minutes),
        id="news_cycle",
        name="News → Price Check → Buy",
        misfire_grace_time=60,
    )

    scheduler.add_job(
        monitor_positions,
        trigger=IntervalTrigger(seconds=60),
        id="monitor",
        name="Position monitor",
        misfire_grace_time=30,
    )

    # Run once immediately so you see it working straight away
    logger.info("Running initial news cycle...")
    news_cycle()

    scheduler.start()
    logger.info(
        "Scheduler running. News cycle every %d min. Press Ctrl+C to stop.",
        cfg.news_poll_interval_minutes,
    )

    # Keep the main thread alive; handle Ctrl+C gracefully
    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        scheduler.shutdown(wait=False)
        print("\n" + generate_report())
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
