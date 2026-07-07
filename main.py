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
    is_article_seen, set_rejection_reason, clear_rejection, set_tp_order_id,
    touch_heartbeat, count_open_trades, count_trades_today, get_today_realized_pnl,
    update_premarket_candidate, save_snapshot,
    trading_days_since_last_trade, record_system_event,
)
from news.fetcher import fetch_all_news, NewsItem
from market.price_check import (
    confirm_price_signal, is_market_open, is_too_late_to_buy, PriceConfirmation,
)
from trading.executor import (
    buy, sell, build_symbol_map, place_take_profit, cancel_order,
    get_portfolio_value, get_account_summary,
)
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

# ── Transient-rejection re-evaluation queue ───────────────────────────────────
# Signals are scored within ~3 minutes of publication — often FASTER than the
# market can express participation. Cumulative-session RVOL barely moves in the
# first minutes after a midday catalyst (the denominator is the whole quiet
# morning), and the 5-min momentum window can read flat before buyers arrive.
# Observed 2026-07-07: VERA (FDA approval, 95% confidence, magnitude 5 — the
# strongest signal of the day) was terminally rejected on RVOL 0.71 measured
# THE MINUTE the news broke; CSCO/BTU/TEVA/RPRX died the same way on flat tape.
# A gate that demands confirmation which can only exist minutes later must
# re-check, not kill. Signals rejected with a TRANSIENT code are parked here
# (their news_signals row keeps the rejection) and re-confirmed each cycle for
# _REEVAL_TTL_MINUTES; if participation arrives, the trade proceeds and the
# row's rejection is cleared. If not, the final rejection stands.
_TRANSIENT_REJECT_CODES = frozenset({"low_volume", "low_momentum"})
_REEVAL_TTL_MINUTES = 15
_reeval_queue: dict[tuple[str, str], dict] = {}  # (article_id, ticker) → {"item", "signal_id", "expires_at"}

# ── Session-level no-quote blackout ──────────────────────────────────────────
# Tickers with no coverage on Finnhub or Twelvedata (e.g. OTC/special-purpose
# instruments like EGGF, OXAC) can loop indefinitely: they pass Claude scoring,
# hit no-data at price-check, enter the retry queue, expire, then re-enter when
# Benzinga publishes a follow-up article about the same event with a new ID
# (which the seen-checker can't recognise as the same ticker). On 2026-06-24,
# EGGF/OXAC looped from 15:38 to past 18:00 with zero upside.
#
# After _NO_QUOTE_BLACKOUT_RETRIES consecutive no-data retries for a ticker, we
# add it to this set for the rest of the session. New articles for blacklisted
# tickers are skipped before price-check (same as a seen-article). The set resets
# on service restart (it's session-scoped, not persistent — a daily restart
# gives a clean slate).
_NO_QUOTE_BLACKOUT_RETRIES = 2   # strikes before permanent session suppression
_no_quote_ticker_strikes: dict[str, int] = {}   # ticker → consecutive no-data count
_no_quote_blackout: set[str] = set()            # tickers suppressed for this session


def _queue_retry(item: NewsItem) -> None:
    key = (item.article_id, item.ticker)
    _retry_queue[key] = {
        "item": item,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_RETRY_TTL_MINUTES),
    }
    # Track consecutive no-data strikes for this ticker.
    strikes = _no_quote_ticker_strikes.get(item.ticker, 0) + 1
    _no_quote_ticker_strikes[item.ticker] = strikes
    if strikes >= _NO_QUOTE_BLACKOUT_RETRIES:
        _no_quote_blackout.add(item.ticker)
        logger.warning(
            "Signal [%s] blacklisted for session — no quote after %d retries "
            "(no Finnhub/Twelvedata coverage); future articles for this ticker suppressed",
            item.ticker, strikes,
        )
    else:
        logger.info(
            "Signal [%s] parked for retry (price data unavailable) — expires in %d min",
            item.ticker, _RETRY_TTL_MINUTES,
        )


def _note_price_data_ok(ticker: str) -> None:
    """
    Reset the no-quote strike counter after ANY successful price check.

    Without this, the strikes were cumulative-per-session, not consecutive:
    two unrelated transient data misses hours apart (e.g. a token-bucket
    minute-limit skip at 14:00 and a Twelvedata blip at 19:00) would
    permanently blacklist a ticker that has perfectly good coverage.
    """
    _no_quote_ticker_strikes.pop(ticker, None)


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


def _queue_reeval(item: NewsItem, signal_id: int) -> None:
    """Park a transiently-rejected signal for periodic re-confirmation."""
    key = (item.article_id, item.ticker)
    if key in _reeval_queue:
        return  # already waiting — keep the original expiry
    _reeval_queue[key] = {
        "item": item,
        "signal_id": signal_id,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_REEVAL_TTL_MINUTES),
    }
    logger.info(
        "Signal [%s] parked for re-evaluation (transient rejection) — "
        "re-checking every cycle for up to %d min",
        item.ticker, _REEVAL_TTL_MINUTES,
    )


def _process_reeval_queue() -> int:
    """
    Re-confirm every parked transiently-rejected signal. Returns positions opened.

    Outcomes per entry:
      confirmed        → clear the row's rejection, enter the trade, unpark.
      transient again  → keep waiting (row keeps its latest rejection).
      terminal reject  → record the new reason, unpark.
      data outage      → keep waiting (bounded by expiry).
      expired          → drop; the last recorded rejection stands.
    """
    now = datetime.now(timezone.utc)
    opened_count = 0
    for key in list(_reeval_queue.keys()):
        entry = _reeval_queue[key]
        item: NewsItem = entry["item"]
        if entry["expires_at"] < now:
            _reeval_queue.pop(key)
            logger.info(
                "Re-eval window closed for [%s] — participation never arrived; "
                "final rejection stands",
                item.ticker,
            )
            continue

        gates_ok, gate_reason = _risk_gates_pass()
        if not gates_ok:
            logger.info("Re-eval paused: %s", gate_reason)
            break

        if was_recently_traded(item.ticker):
            _reeval_queue.pop(key)
            logger.info("Re-eval dropped for [%s] — ticker traded since parking", item.ticker)
            continue

        try:
            conf = confirm_price_signal(item.ticker)
        except Exception as exc:
            logger.error("Re-eval confirm raised for %s: %s", item.ticker, exc, exc_info=True)
            continue
        if conf is None:
            continue  # data miss this cycle — expiry bounds the wait

        if conf.is_confirmed:
            _reeval_queue.pop(key)
            try:
                clear_rejection(entry["signal_id"])
            except Exception as exc:
                logger.warning("clear_rejection failed for signal %d: %s", entry["signal_id"], exc)
            logger.info(
                "Re-eval CONFIRMED [%s] — participation arrived within the window: %s",
                item.ticker, conf.reason,
            )
            if _enter_confirmed(item, conf, entry["signal_id"]):
                opened_count += 1
            continue

        try:
            set_rejection_reason(entry["signal_id"], conf.reason, conf.reason_code)
        except Exception as exc:
            logger.warning("set_rejection_reason failed for signal %d: %s", entry["signal_id"], exc)
        if conf.reason_code not in _TRANSIENT_REJECT_CODES:
            _reeval_queue.pop(key)
            logger.info(
                "Re-eval terminal rejection [%s] code=%s: %s",
                item.ticker, conf.reason_code, conf.reason,
            )
        else:
            logger.debug(
                "Re-eval still transient [%s] code=%s — waiting", item.ticker, conf.reason_code,
            )
    return opened_count


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
            catalyst_magnitude=item.catalyst_magnitude,
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
        # Transient tape states (participation not arrived yet) get re-checked
        # for the next _REEVAL_TTL_MINUTES rather than dying at first sight.
        if confirmation.reason_code in _TRANSIENT_REJECT_CODES:
            _queue_reeval(item, signal_id)
        return False

    return _enter_confirmed(item, confirmation, signal_id)


def _enter_confirmed(item: NewsItem, confirmation: PriceConfirmation, signal_id: int) -> bool:
    """Buy + resting TP + trade record for an already-confirmed, saved signal."""
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
    # A buy that is not represented in the DB is an unmanaged live position.
    # If the insert fails after the broker filled us, flatten immediately; the
    # spread loss is preferable to an invisible position with no stop/EOD logic.
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
    except Exception as exc:
        logger.error(
            "open_trade() failed for %s after successful buy order %s: %s "
            "— trade executed but NOT recorded in DB; attempting emergency flatten",
            item.ticker, result.order_id, exc,
        )
        try:
            flatten = sell(item.ticker, result.quantity, result.price, "db_record_failed", force_market=True)
            if flatten.success:
                logger.critical(
                    "Emergency flatten succeeded for unrecorded %s position "
                    "(sell_order=%s)",
                    item.ticker, flatten.order_id,
                )
            else:
                logger.critical(
                    "Emergency flatten FAILED for unrecorded %s position: %s — "
                    "manual broker reconciliation required",
                    item.ticker, flatten.error,
                )
        except Exception as flatten_exc:
            logger.critical(
                "Emergency flatten raised for unrecorded %s position: %s — "
                "manual broker reconciliation required",
                item.ticker, flatten_exc, exc_info=True,
            )
        return False

    try:
        mark_signal_acted_on(signal_id)
    except Exception as exc:
        # The trade row is the source of truth for position management. A failed
        # acted_on flag should not abort TP placement or leave the position less
        # protected.
        logger.warning("mark_signal_acted_on failed for signal %d: %s", signal_id, exc)

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
                "not know about the resting order; cancelling it now",
                tp_order_id, trade_id, exc,
            )
            if cancel_order(tp_order_id):
                logger.warning(
                    "Untracked resting TP %s for trade %d cancelled; monitor will use polled TP",
                    tp_order_id, trade_id,
                )
                tp_order_id = None
            else:
                logger.critical(
                    "Could not cancel untracked TP order %s for trade %d — "
                    "manual broker reconciliation required before any stop sell",
                    tp_order_id, trade_id,
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
        if published_at.tzinfo is None:
            published_at = london.localize(published_at)
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
        # Required NewsItem field since v15.8. Omitting it raised TypeError in
        # every premarket-approval execution (main.news_cycle caught it, aborting
        # the whole loop) — the root cause of the 2026-06-11→07-06 zero-trade
        # drought. Stored on the candidate row at scan time; default 1 (noise)
        # only as a can't-crash fallback for legacy rows written before v15.8.
        catalyst_magnitude=int(cand.get("catalyst_magnitude") or 1),
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

    # ── 2. Re-eval queue (transient tape rejections awaiting participation) ──
    reeval_opened = _process_reeval_queue()
    if reeval_opened:
        gates_ok, gate_reason = _risk_gates_pass()
        if not gates_ok:
            logger.warning("Risk gate tripped after re-eval entries: %s", gate_reason)
            return

    # ── 3. Retry queue (already-scored signals that hit a data outage) ───────
    # ── 4. Fresh signals from Benzinga ────────────────────────────────────────
    retry_items = _drain_retry_queue()
    try:
        # Lookback 5 min > the 3-min freshness window: articles the Benzinga
        # feed indexes late (or that land while a cycle overruns) still get
        # fetched; the fetcher's session dedup keeps Claude costs flat.
        news_items = fetch_all_news(lookback_minutes=5)
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

    # Funnel counters — make the tradeable-signal → trade attrition visible in
    # one line per cycle. On 2026-06-15, 25 gate-passing positives produced 0
    # trades and it took a manual DB dig to see WHY each one dropped. This
    # surfaces the leak in the logs (and is cheap — just ints).
    funnel = {"evaluated": 0, "already_seen": 0, "blackout": 0, "cooldown": 0,
              "no_price_data": 0, "rejected": 0, "opened": 0}

    for item in all_items:
        funnel["evaluated"] += 1
        logger.info(
            "Signal [%s] %.0f%% confidence catalyst=%s: %s",
            item.ticker, item.confidence * 100, item.catalyst_type, item.headline,
        )

        # Dedup: (article, ticker) already processed in a previous cycle.
        if is_article_seen(item.article_id, item.ticker):
            funnel["already_seen"] += 1
            logger.debug(
                "Skipping %s — article %s already processed", item.ticker, item.article_id,
            )
            continue

        # Session no-quote blackout: ticker has no Finnhub/Twelvedata coverage —
        # price-check will always return None; suppress to avoid looping all day.
        if item.ticker in _no_quote_blackout:
            funnel["blackout"] += 1
            logger.debug("Skipping %s — session no-quote blackout", item.ticker)
            continue

        # 24h per-ticker cooldown (open position or traded recently).
        if was_recently_traded(item.ticker):
            funnel["cooldown"] += 1
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
            funnel["no_price_data"] += 1
            _queue_retry(item)
            continue

        # Price data answered — strikes must be CONSECUTIVE to blacklist.
        _note_price_data_ok(item.ticker)

        opened = _execute_entry(item, confirmation, fetched_at)
        funnel["opened" if opened else "rejected"] += 1

        if opened:
            # Re-check portfolio gates after every fill.
            gates_ok, gate_reason = _risk_gates_pass()
            if not gates_ok:
                logger.warning("Risk gate tripped mid-cycle: %s — stopping entries", gate_reason)
                break

    # One-line funnel summary whenever anything was evaluated.
    if funnel["evaluated"]:
        logger.info(
            "Signal funnel: %d evaluated → %d seen, %d blackout, %d cooldown, "
            "%d no-data, %d rejected, %d OPENED",
            funnel["evaluated"], funnel["already_seen"], funnel["blackout"],
            funnel["cooldown"], funnel["no_price_data"], funnel["rejected"],
            funnel["opened"],
        )

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


def check_zero_trade_drought() -> None:
    """
    Alert when ZERO_TRADE_ALERT_SESSIONS+ consecutive trading days pass with no
    trades. This is the tripwire for the silent-failure class that ran undetected
    for nine sessions in June 2026 (a Twelvedata credit collapse): the service was
    up, heartbeats green, news scored — but no signal could ever be confirmed, so
    nothing traded and nothing alerted.

    It only RAISES AN ALERT — it never stands the system down. A multi-day drought
    can be a legitimately bad tape, so a human looks and decides; record_system_event
    de-dupes to one row per day so this won't spam.
    """
    try:
        idle = trading_days_since_last_trade()
    except Exception as exc:
        logger.warning("zero-trade drought check failed: %s", exc)
        return
    if idle is None or idle < cfg.zero_trade_alert_sessions:
        return
    logger.critical(
        "ZERO-TRADE DROUGHT: %d consecutive trading sessions with no trades "
        "(threshold %d). Service is up and scoring news — investigate whether the "
        "price-confirmation/data pipeline is silently failing (Twelvedata credits, "
        "Claude outage) vs a genuinely untradeable tape. See system_events.",
        idle, cfg.zero_trade_alert_sessions,
    )
    record_system_event(
        "zero_trade_session",
        f"{idle} consecutive trading sessions with no trades "
        f"(threshold {cfg.zero_trade_alert_sessions})",
    )


def _nightly_forward_returns() -> None:
    """Nightly eval-loop job — see analysis/forward_returns.py."""
    try:
        touch_heartbeat("forward_returns")
        compute_forward_returns()
    except Exception as exc:
        logger.error("Nightly forward-returns job failed: %s", exc, exc_info=True)


def portfolio_snapshot() -> None:
    """
    Record (total_value, cash) to portfolio_snapshots — the data behind
    Grafana's "Portfolio Value Over Time" panel. Only during market hours:
    the value doesn't move while the market is closed, and flat weekend
    lines just compress the interesting part of the chart.

    (save_snapshot() existed since v1 but nothing ever called it — the
    Grafana panel was empty from day one.)
    """
    if not is_market_open():
        return
    try:
        summary = get_account_summary()
        if summary is None:
            logger.warning("portfolio_snapshot: account summary unavailable — skipping")
            return
        total, free = summary
        save_snapshot(total, free)
        logger.debug("Portfolio snapshot: total=£%.2f cash=£%.2f", total, free)
    except Exception as exc:
        logger.error("portfolio_snapshot failed: %s", exc)


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

    # Zero-trade drought tripwire. 21:30 UTC is after the US close year-round, so
    # "today" is already settled when it runs. Weekdays only — a Saturday alert
    # would just re-report Friday's idle count.
    _scheduler.add_job(
        check_zero_trade_drought,
        trigger=CronTrigger(hour=21, minute=30, day_of_week="mon-fri"),
        id="zero_trade_drought",
        name="Zero-trade drought alert",
        misfire_grace_time=3600,
    )

    # Portfolio value snapshots for the Grafana time-series panel.
    # Every 5 min; the job itself returns immediately when the market is closed.
    _scheduler.add_job(
        portfolio_snapshot,
        trigger=IntervalTrigger(minutes=5),
        id="portfolio_snapshot",
        name="Portfolio value snapshot",
        misfire_grace_time=60,
    )

    # Surface an in-progress drought immediately on startup, not only at 21:30.
    check_zero_trade_drought()

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
