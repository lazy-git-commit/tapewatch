"""
main.py
────────
Entry point for the momentum trader.

Scheduled jobs:
  1. news_cycle       — every minute. During market hours: fetch Benzinga →
                        Claude sentiment → price confirmation → risk gates →
                        buy + resting take-profit. During the pre-market
                        window: scan news into the at-open watchlist.
  2. monitor_positions — every cfg.monitor_interval_seconds (20s). Manages
                        resting TP fills, stop-loss, time-stop, EOD flatten.
  3. forward_returns  — nightly at 22:30 UTC. Fills 5/15/60-min forward
                        returns for every Claude classification (eval loop).
  4. symbol_map_rebuild — daily at 08:00 UTC. Refreshes the T212 symbol map
                        (startup build retries, but a daily refresh also
                        picks up newly listed instruments).

Portfolio risk gates (applied before EVERY entry, RTH or pre-market):
  - daily kill switch  — realized P&L today worse than −MAX_DAILY_LOSS_PCT%
                         of portfolio → no new entries until tomorrow
  - max open positions — at most MAX_OPEN_POSITIONS concurrent
  - max trades per day — at most MAX_TRADES_PER_DAY entries per day

Usage:
  python main.py

Press Ctrl+C to stop gracefully.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.settings import cfg
from storage.database import (
    init_db, save_signal, mark_signal_acted_on, open_trade, was_recently_traded,
    is_article_seen, set_rejection_reason, set_tp_order_id, touch_heartbeat,
    count_open_trades, count_trades_today, get_today_realized_pnl,
    update_premarket_candidate,
)
from news.fetcher import fetch_all_news, NewsItem
from market.price_check import (
    confirm_price_signal, is_market_open, is_too_late_to_buy, PriceConfirmation,
)
from trading.executor import buy, build_symbol_map, place_take_profit, get_portfolio_value
from monitor.position_monitor import monitor_positions
from premarket.scanner import in_premarket_window, premarket_scan, evaluate_premarket_candidates
from analysis.forward_returns import compute_forward_returns
from reporting.report import generate_report

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s  %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("apscheduler").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


# ── Transient-failure retry queue ─────────────────────────────────────────────
# When confirm_price_signal() returns None (data outage, not a rejection), the
# signal must be retried — but the fetcher's freshness filter would drop the
# article next cycle (it's >60s old by then), which used to silently kill the
# signal forever (observed: SPCX IPO, 2026-06-12). Already-scored signals are
# parked here and retried at the top of each cycle, bypassing the fetch path.
# Entries expire after _RETRY_TTL_MINUTES: a 5-minute-old momentum signal is
# stale by definition.
_RETRY_TTL_MINUTES = 5
_retry_queue: dict[tuple[str, str], dict] = {}  # (article_id, ticker) → {"item", "expires_at"}


def _queue_retry(item: NewsItem) -> None:
    key = (item.article_id, item.ticker)
    _retry_queue[key] = {
        "item": item,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_RETRY_TTL_MINUTES),
    }
    logger.info(
        "Signal [%s] parked for retry (price data unavailable) — expires in %d min",
        item.ticker, _RETRY_TTL_MINUTES,
    )


def _drain_retry_queue() -> list[NewsItem]:
    """Pop all unexpired retry entries; expired ones are dropped with a log."""
    now = datetime.now(timezone.utc)
    items: list[NewsItem] = []
    for key in list(_retry_queue.keys()):
        entry = _retry_queue.pop(key)
        if entry["expires_at"] < now:
            logger.info(
                "Retry for [%s] expired without price data — signal dropped",
                entry["item"].ticker,
            )
            continue
        items.append(entry["item"])
    return items


# ── Portfolio-level risk gates ────────────────────────────────────────────────

def _risk_gates_pass() -> tuple[bool, str]:
    """
    Pre-entry portfolio checks. Returns (ok, reason_if_blocked).
    Fail-open on DB errors for the counters (a DB hiccup shouldn't freeze
    trading) but fail-CLOSED on the kill switch (if we can't verify today's
    P&L, the safe assumption after a losing streak is to stand down).
    """
    # Daily kill switch — the one control that must never fail open.
    try:
        realized = get_today_realized_pnl()
        if realized < 0:
            portfolio = get_portfolio_value()
            if portfolio is None:
                return False, "kill-switch check impossible (portfolio value unavailable) — standing down"
            loss_limit = portfolio * (cfg.max_daily_loss_pct / 100)
            if abs(realized) >= loss_limit:
                return False, (
                    f"DAILY KILL SWITCH: realized P&L today £{realized:.2f} breaches "
                    f"−{cfg.max_daily_loss_pct}% of portfolio (£{loss_limit:.2f}) — "
                    f"no new entries until tomorrow"
                )
    except Exception as exc:
        return False, f"kill-switch check failed ({exc}) — standing down"

    try:
        n_open = count_open_trades()
        if n_open >= cfg.max_open_positions:
            return False, f"max open positions reached ({n_open}/{cfg.max_open_positions})"
    except Exception as exc:
        logger.warning("count_open_trades failed (%s) — gate skipped this cycle", exc)

    try:
        n_today = count_trades_today()
        if n_today >= cfg.max_trades_per_day:
            return False, f"max trades per day reached ({n_today}/{cfg.max_trades_per_day})"
    except Exception as exc:
        logger.warning("count_trades_today failed (%s) — gate skipped this cycle", exc)

    return True, ""


# ── Shared entry execution ────────────────────────────────────────────────────

def _execute_entry(item: NewsItem, confirmation: PriceConfirmation, fetched_at: str) -> bool:
    """
    Execute one confirmed entry: save the signal, run the buy, place the
    resting take-profit, record the trade. Used by both the regular-hours
    path and the pre-market path so risk handling never diverges.
    Returns True if a position was opened.
    """
    confidence_scaled = max(1, min(10, round(item.confidence * 10)))
    try:
        signal_id = save_signal(
            ticker=item.ticker,
            headline=item.headline,
            source=item.source,
            sentiment="positive",
            confidence=confidence_scaled,
            article_id=item.article_id,
            published_at=item.published_at.isoformat(),
            fetched_at=fetched_at,
            catalyst_type=item.catalyst_type,
        )
    except Exception as exc:
        logger.error("save_signal failed for %s: %s — skipping trade", item.ticker, exc)
        return False

    if not confirmation.is_confirmed:
        logger.info(
            "Signal rejected [%s] code=%s: %s",
            item.ticker, confirmation.reason_code, confirmation.reason,
        )
        try:
            set_rejection_reason(signal_id, confirmation.reason, confirmation.reason_code)
        except Exception as exc:
            logger.warning("set_rejection_reason failed for signal %d: %s", signal_id, exc)
        return False

    logger.info(
        "Signal approved [%s] @ $%.4f — %s",
        item.ticker, confirmation.current_price, confirmation.reason,
    )

    # ── Buy (liquidity-aware sizing via ADV) ──────────────────────────────────
    try:
        result = buy(item.ticker, confirmation.current_price, confirmation.avg_dollar_volume)
    except Exception as exc:
        logger.error("buy() raised unexpectedly for %s: %s", item.ticker, exc, exc_info=True)
        try:
            set_rejection_reason(signal_id, f"buy raised exception: {exc}", "buy_failed")
        except Exception:
            pass
        return False

    if not result.success:
        logger.error("Buy order failed [%s]: %s", item.ticker, result.error)
        try:
            set_rejection_reason(signal_id, f"buy order failed: {result.error}", "buy_failed")
        except Exception as exc:
            logger.warning("set_rejection_reason failed after buy failure: %s", exc)
        return False

    # ── Record trade ──────────────────────────────────────────────────────────
    try:
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
    except Exception as exc:
        logger.error(
            "open_trade() failed for %s after successful buy order %s: %s "
            "— trade executed but NOT recorded in DB",
            item.ticker, result.order_id, exc,
        )
        return False

    # ── Resting take-profit ───────────────────────────────────────────────────
    # Placed at the exchange so the profit side has zero polling latency.
    # If placement fails, the monitor's polled TP covers this position instead.
    tp_price = result.price * (1 + cfg.take_profit_pct / 100)
    tp_order_id = place_take_profit(item.ticker, result.quantity, tp_price)
    if tp_order_id:
        try:
            set_tp_order_id(trade_id, tp_order_id)
        except Exception as exc:
            logger.error(
                "Could not store tp_order_id %s for trade %d: %s — monitor will "
                "treat this as polled-TP",
                tp_order_id, trade_id, exc,
            )

    logger.info(
        "Trade #%d opened: %s × %.6f @ $%.4f | net=£%.2f fx=%.4f fees=£%.2f | "
        "order=%s | resting_tp=%s",
        trade_id, item.ticker, result.quantity, result.price,
        result.net_gbp or 0, result.fx_rate or 0, result.fees_gbp or 0,
        result.order_id, tp_order_id or "polled",
    )
    return True


def _candidate_to_news_item(cand: dict) -> NewsItem:
    """Reconstruct a NewsItem from a premarket_candidates row for _execute_entry."""
    london = pytz.timezone("Europe/London")
    try:
        published_at = datetime.fromisoformat(str(cand["published_at"]))
    except (ValueError, TypeError):
        published_at = datetime.now(london)
    return NewsItem(
        article_id=str(cand.get("article_id") or ""),
        ticker=cand["ticker"],
        headline=cand.get("headline") or "",
        body="",
        source="benzinga-premarket",
        published_at=published_at,
        sentiment="positive",
        confidence=float(cand.get("confidence") or 0.7),
        catalyst_type=cand.get("catalyst_type") or "other",
        already_moved=False,
    )


def news_cycle() -> None:
    """
    The main pipeline — runs every minute on a fixed IntervalTrigger.

    Market open:   pre-market candidates (if any, inside their window) →
                   retry queue → fresh Benzinga signals. Each goes through
                   risk gates → price confirmation → buy + resting TP.
    Pre-market:    scan news into the watchlist (premarket/scanner.py).
    Otherwise:     return immediately.

    Never reschedules its own trigger — that avoids the APScheduler race
    where a job replacing itself mid-execution is silently dropped.
    """
    cycle_start = datetime.now(pytz.timezone("Europe/London"))
    logger.info(
        "── News cycle starting [%s] ─────────────────────────────",
        cycle_start.strftime("%H:%M:%S"),
    )

    try:
        touch_heartbeat("news_cycle")
    except Exception:
        pass  # liveness reporting must never block trading

    if not is_market_open():
        # Pre-market window: build the at-open watchlist instead of trading.
        if in_premarket_window():
            logger.info("Pre-market window — scanning for overnight catalysts")
            try:
                premarket_scan()
            except Exception as exc:
                logger.error("premarket_scan failed: %s", exc, exc_info=True)
        else:
            logger.info("Market closed — skipping cycle")
        return

    if is_too_late_to_buy():
        logger.info(
            "Too close to market close to open new positions (time_stop=%d min) — skipping cycle",
            cfg.time_stop_minutes,
        )
        return

    fetched_at = cycle_start.isoformat()

    # ── Portfolio risk gates (one check per cycle — applies to all entries) ──
    gates_ok, gate_reason = _risk_gates_pass()
    if not gates_ok:
        logger.warning("Risk gate active: %s — no new entries this cycle", gate_reason)
        logger.info("── News cycle complete ──────────────────────────────────")
        return

    # ── 1. Pre-market candidates (first 30 min after open only) ──────────────
    try:
        for cand, conf in evaluate_premarket_candidates():
            if was_recently_traded(cand["ticker"]):
                update_premarket_candidate(cand["id"], "rejected", "24h ticker cooldown")
                continue
            item = _candidate_to_news_item(cand)
            opened = _execute_entry(item, conf, fetched_at)
            update_premarket_candidate(
                cand["id"], "traded" if opened else "rejected",
                None if opened else "buy failed or signal save failed",
            )
            # Re-check gates after each entry so a fill can't blow the caps.
            gates_ok, gate_reason = _risk_gates_pass()
            if not gates_ok:
                logger.warning("Risk gate tripped mid-cycle: %s", gate_reason)
                return
    except Exception as exc:
        logger.error("Pre-market candidate evaluation failed: %s", exc, exc_info=True)

    # ── 2. Retry queue (already-scored signals that hit a data outage) ───────
    # ── 3. Fresh signals from Benzinga ────────────────────────────────────────
    retry_items = _drain_retry_queue()
    try:
        news_items = fetch_all_news(lookback_minutes=2)
    except Exception as exc:
        logger.error("news_cycle: fetch_all_news raised unexpectedly: %s", exc, exc_info=True)
        news_items = []

    all_items = retry_items + news_items
    if not all_items:
        logger.info("No positive signals this cycle.")
        logger.info("── News cycle complete ──────────────────────────────────")
        return

    logger.info(
        "%d signal(s) to evaluate (%d retry, %d fresh).",
        len(all_items), len(retry_items), len(news_items),
    )

    for item in all_items:
        logger.info(
            "Signal [%s] %.0f%% confidence catalyst=%s: %s",
            item.ticker, item.confidence * 100, item.catalyst_type, item.headline,
        )

        # Dedup: (article, ticker) already processed in a previous cycle.
        if is_article_seen(item.article_id, item.ticker):
            logger.debug(
                "Skipping %s — article %s already processed", item.ticker, item.article_id,
            )
            continue

        # 24h per-ticker cooldown (open position or traded recently).
        if was_recently_traded(item.ticker):
            logger.info("Skipping %s — 24h ticker cooldown active", item.ticker)
            continue

        # Price confirmation — runs before the DB write so a transient data
        # failure doesn't permanently mark the pair as seen.
        try:
            confirmation = confirm_price_signal(item.ticker)
        except Exception as exc:
            logger.error(
                "confirm_price_signal raised for %s: %s", item.ticker, exc, exc_info=True,
            )
            continue

        if confirmation is None:
            # Data outage (not a rejection) — park for retry; the fetcher's
            # freshness filter can't see queue entries, so they survive.
            _queue_retry(item)
            continue

        opened = _execute_entry(item, confirmation, fetched_at)

        if opened:
            # Re-check portfolio gates after every fill.
            gates_ok, gate_reason = _risk_gates_pass()
            if not gates_ok:
                logger.warning("Risk gate tripped mid-cycle: %s — stopping entries", gate_reason)
                break

    elapsed_ms = (datetime.now(pytz.timezone("Europe/London")) - cycle_start).total_seconds() * 1000
    logger.info(
        "── News cycle complete [%.0fms] ──────────────────────────────",
        elapsed_ms,
    )


def _read_version() -> dict:
    """Read VERSION file written by GitHub Actions at deploy time."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "VERSION")) as f:
            return dict(line.strip().split("=", 1) for line in f if "=" in line)
    except FileNotFoundError:
        return {}


def _nightly_forward_returns() -> None:
    """Nightly eval-loop job — see analysis/forward_returns.py."""
    try:
        touch_heartbeat("forward_returns")
        compute_forward_returns()
    except Exception as exc:
        logger.error("Nightly forward-returns job failed: %s", exc, exc_info=True)


def main() -> None:
    version = _read_version()
    logger.info("=" * 60)
    logger.info("  MOMENTUM TRADER STARTING")
    logger.info("  Mode: %s", cfg.trading_mode.upper())
    logger.info("  Blocklist: %s", ", ".join(cfg.blocklist) if cfg.blocklist else "none")
    if version:
        logger.info("  Deployed: %s", version.get("deployed_at", "unknown"))
        logger.info("  Commit:   %s", version.get("commit", "unknown")[:12])
    logger.info("=" * 60)

    # Validate config and initialise DB
    cfg.validate()
    init_db()
    build_symbol_map()

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
        trigger=IntervalTrigger(seconds=cfg.monitor_interval_seconds),
        id="monitor",
        name="Position monitor",
        misfire_grace_time=15,
    )

    # Nightly eval loop: forward returns for every Claude classification.
    # 22:30 UTC is after the US close year-round (21:00 BST / 20:00 GMT close).
    _scheduler.add_job(
        _nightly_forward_returns,
        trigger=CronTrigger(hour=22, minute=30, day_of_week="mon-fri"),
        id="forward_returns",
        name="Eval loop: forward returns",
        misfire_grace_time=3600,
    )

    # Daily symbol-map refresh (also picks up new listings).
    _scheduler.add_job(
        build_symbol_map,
        trigger=CronTrigger(hour=8, minute=0, day_of_week="mon-fri"),
        id="symbol_map_rebuild",
        name="T212 symbol map rebuild",
        misfire_grace_time=3600,
    )

    # Run once immediately on startup
    logger.info("Running initial news cycle...")
    news_cycle()

    _scheduler.start()
    logger.info(
        "Scheduler running. News cycle every 1 min, monitor every %ds. Press Ctrl+C to stop.",
        cfg.monitor_interval_seconds,
    )
    logger.info("Jobs scheduled: %s", [j.id for j in _scheduler.get_jobs()])

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
