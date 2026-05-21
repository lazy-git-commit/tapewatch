"""
main.py
────────
Entry point for the momentum trader.

Starts two scheduled jobs:
  1. news_cycle  — runs every minute during market hours
                   fetches Benzinga signals → Claude sentiment → price check → buy
                   sleeps until the next NYSE open when the market is closed
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
from datetime import datetime, timezone
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

from config.settings import cfg
from storage.database import init_db, save_signal, mark_signal_acted_on, open_trade, was_recently_traded, is_article_seen
from news.fetcher import fetch_all_news
from market.price_check import confirm_price_signal, is_market_open, is_too_late_to_buy, next_market_open
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


_scheduler: BackgroundScheduler | None = None  # set in main() before first use


def _reschedule_for_market_open() -> None:
    """Switch the news_cycle job to fire once at the next market open.
    Uses add_job with replace_existing=True because APScheduler removes a
    DateTrigger job after it fires — reschedule_job would raise JobLookupError."""
    if _scheduler is None or not _scheduler.running:
        return
    open_utc = next_market_open()
    _scheduler.add_job(
        news_cycle,
        trigger=DateTrigger(run_date=open_utc, timezone=pytz.utc),
        id="news_cycle",
        name="News → Price Check → Buy",
        misfire_grace_time=30,
        replace_existing=True,
    )
    logger.info(
        "Market closed — news cycle paused until %s UTC",
        open_utc.strftime("%Y-%m-%d %H:%M"),
    )


def _reschedule_for_interval() -> None:
    """Switch the news_cycle job back to 1-minute polling."""
    if _scheduler is None or not _scheduler.running:
        return
    _scheduler.add_job(
        news_cycle,
        trigger=IntervalTrigger(minutes=1),
        id="news_cycle",
        name="News → Price Check → Buy",
        misfire_grace_time=30,
        replace_existing=True,
    )
    logger.info("Market open — news cycle resumed (1-min interval)")


def news_cycle() -> None:
    """
    The main trading pipeline — runs on a schedule.

      1. Fetch and score positive signals from Benzinga via Claude
      2. Confirm price movement via yfinance
      3. Execute a buy via Trading 212 if confirmed

    When the market is closed, reschedules itself to fire at the next NYSE open
    instead of polling every minute.
    """
    logger.info("── News cycle starting ──────────────────────────────────")

    if not is_market_open():
        open_utc = next_market_open()
        logger.info(
            "Market closed — next open at %s UTC",
            open_utc.strftime("%Y-%m-%d %H:%M"),
        )
        _reschedule_for_market_open()
        return

    # If we were sleeping until market open, switch back to 1-min polling
    job = _scheduler.get_job("news_cycle")
    if job and not isinstance(job.trigger, IntervalTrigger):
        _reschedule_for_interval()

    if is_too_late_to_buy():
        logger.info(
            "Too close to market close to open new positions (time stop: %d min) — skipping cycle.",
            cfg.time_stop_minutes,
        )
        return

    fetched_at = datetime.now(timezone.utc).isoformat()
    news_items = fetch_all_news(lookback_minutes=5)
    if not news_items:
        logger.info("No new articles found.")
        return

    logger.info("%d article(s) to evaluate.", len(news_items))

    for item in news_items:
        logger.info("Signal [%s] %.0f%% confidence: %s", item.ticker, item.confidence * 100, item.headline)

        # Skip articles already processed in a previous cycle
        if is_article_seen(item.article_id):
            logger.debug("Skipping %s — article %s already processed", item.ticker, item.article_id)
            continue

        # Skip if already holding or bought in the last 24 hours
        if was_recently_traded(item.ticker):
            logger.info("Skipping %s — already traded within the last 24 hours", item.ticker)
            continue

        # Save signal to DB — map Claude confidence (0–1) to 1–10 scale
        confidence_scaled = max(1, min(10, round(item.confidence * 10)))
        signal_id = save_signal(
            ticker=item.ticker,
            headline=item.headline,
            source=item.source,
            sentiment="BULLISH",
            confidence=confidence_scaled,
            article_id=item.article_id,
            published_at=item.published_at.isoformat(),
            fetched_at=fetched_at,
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
            buy_order_id=result.order_id,
            buy_net_gbp=result.net_gbp,
            buy_fx_rate=result.fx_rate,
            buy_fees_gbp=result.fees_gbp,
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
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=pytz.utc)

    _scheduler.add_job(
        news_cycle,
        trigger=IntervalTrigger(minutes=1),
        id="news_cycle",
        name="News → Price Check → Buy",
        misfire_grace_time=30,
    )

    _scheduler.add_job(
        monitor_positions,
        trigger=IntervalTrigger(seconds=60),
        id="monitor",
        name="Position monitor",
        misfire_grace_time=30,
    )

    # Run once immediately — this also sets the right trigger if market is closed
    logger.info("Running initial news cycle...")
    news_cycle()

    _scheduler.start()
    logger.info("Scheduler running. News cycle every 1 min. Press Ctrl+C to stop.")

    # Keep the main thread alive; handle Ctrl+C gracefully
    def _shutdown(sig, frame):
        logger.info("Shutting down...")
        _scheduler.shutdown(wait=False)
        print("\n" + generate_report())
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
