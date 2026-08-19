"""
main.py
────────
Entry point for the momentum trader.

Scheduled jobs:
  1. news_cycle       — every cfg.news_cycle_seconds (v21.18; 10s in
                        production, 60s default). The interval IS our
                        publication→fetch latency: news arrives at random
                        moments, so the average wait is HALF the interval.
                        Only the FETCH runs at this cadence — the re-eval
                        queue, retry queue, pre-market scan and gap-and-go
                        evaluation stay on ~60s spacing via _slow_path_due(),
                        because re-running them costs Twelvedata credits
                        against a fail-closed 55/min bucket.
                        In an entry session (regular hours, and
                        after-hours 16:00–20:00 ET under v21's extended
                        regime): fetch Benzinga → Claude sentiment → price
                        confirmation → risk gates → buy (+ resting stop-loss
                        in RTH — v20; polled both-sides in extended sessions).
                        During the pre-market window: scan news into the
                        at-open watchlist. Overnight (Blue Ocean): nothing —
                        no data coverage, fail closed.
  2. monitor_positions — every cfg.monitor_interval_seconds (default 5s, v20).
                        Notices resting-stop fills, polls take-profit and
                        time-stop, ratchets the stop to breakeven at +1R,
                        EOD-flattens before the close.
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
import math
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
    is_article_seen, set_rejection_reason, clear_rejection, set_stop_order_id,
    touch_heartbeat, count_open_trades, count_trades_today, get_today_realized_pnl,
    update_premarket_candidate, save_snapshot,
    trading_days_since_last_trade, record_system_event,
    get_loss_streak, get_drawdown_from_peak,
)
from news.fetcher import fetch_all_news, NewsItem
from market.price_check import (
    confirm_price_signal, is_too_late_to_buy, PriceConfirmation,
    quote_feed_degraded,
)
from market.sessions import (
    get_trading_session, is_entry_session, REGULAR, EXTENDED_SESSIONS, _ET,
)
from trading.executor import (
    buy, sell, build_symbol_map, place_stop_loss, cancel_order,
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
# overextended joined in v20: "too far above VWAP to place a stop sensibly"
# is a property of THIS MINUTE's price, not of the catalyst — the re-eval
# queue converts it into a first-pullback entry (see price_check gate 10.2).
# opening_block joined in v21.6: it is the ONLY gate whose condition is a
# pure countdown — "N minutes since the session boundary, block lasts
# cfg.open_block_minutes" is guaranteed false a few minutes later, yet it was
# terminal, so a catalyst that printed inside the window was discarded
# outright. Eight signals died this way, four of them in the current
# tradeable-catalyst set: CDNS ×2 (guidance_raise, conf 0.88/0.85) at 4.0 and
# 4.1 min into the after-hours boundary on 2026-07-27 — sixty seconds short —
# plus TXN (2026-07-22) and THRM (2026-07-20). Earnings and guidance print in
# the first minutes after 16:00 ET, which is exactly the window this gate
# covers, so terminal handling here forfeits the densest catalyst window of
# the day. The block itself is sound and unchanged (auction/MOC noise is real);
# only its permanence was wrong.
# stale_price / stale_volume (v21.11) are data states, not market states: the
# feed is behind, not the stock. Both clear on their own within minutes, so
# discarding the signal would throw away a catalyst over a vendor hiccup.
# stale_bars (v21.14.2) is the same kind of data state — the session pull
# succeeded but carries no bar recent enough to measure momentum against. It
# used to return None, which _queue_retry counts as "no provider carries this
# instrument" and blacklists the ticker for the day after two of them: SRRK and
# NVO were both lost that way on 2026-08-10 over a 14-minute-old bar, on two of
# only four regular-hours tradeable candidates that session.
_TRANSIENT_REJECT_CODES = frozenset(
    {"low_volume", "low_momentum", "overextended", "opening_block",
     "stale_price", "stale_volume", "stale_bars"}
)
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
# add it to this set. New articles for blacklisted tickers are skipped before
# price-check (same as a seen-article).
#
# Scope (v21.6): the blackout means "no provider carries this instrument", so
# only a miss in a session where data is genuinely EXPECTED may count toward
# it. Two rules, both bought by the same incident (2026-07-27):
#
#  1. Extended sessions never accrue strikes. Twelvedata serves pre/post bars
#     only on the Pro tier and Finnhub's free quote freezes at the 16:00 close,
#     so an after-hours miss is the NORMAL state of the world here and proves
#     nothing about the ticker. Counting it blacklisted CDNS, SANM, CLS, KFRC,
#     LOKB, TFII, SJW, SUI and LC — every one a liquid large/mid cap with
#     perfect RTH coverage — purely for reporting earnings after the bell.
#  2. The blackout resets on a new ET trading day. The original comment
#     assumed "a daily restart gives a clean slate", but the service is a
#     long-running systemd unit: it had gone six days and 16 tickers without a
#     restart. A per-day reset is what the design always meant, and it bounds
#     the damage of any future false positive to one session.
_NO_QUOTE_BLACKOUT_RETRIES = 2   # strikes before session suppression
_no_quote_ticker_strikes: dict[str, int] = {}   # ticker → consecutive no-data count
_no_quote_blackout: set[str] = set()            # tickers suppressed for this session
_no_quote_blackout_day: str | None = None       # ET date the sets above belong to

# ── Entry slippage instrumentation (v21.13) ──────────────────────────────────
# The gap between the price a signal was APPROVED at and the price we were
# actually FILLED at is money lost before the thesis is even tested, and until
# now it was only recoverable by hand-diffing two log lines against external
# price data.
#
# 2026-08-06 made the case. LAMR: sized at $161.09, filled at $164.30 — +1.99%,
# and 34 seconds elapsed between the two (the other three entries that day filled
# in 3-4s). $164.30 was above LAMR's high for the ENTIRE session. The stop then
# sat 2% below that inflated fill, the stock drifted back to ~$161 (the price we
# actually wanted), and we were stopped out 28 seconds after entry. Of the day's
# £17.96 loss, ~£6 was entry slippage and most of that was this one fill.
#
# Every entry now logs the gap; anything past the alert threshold also raises a
# WARNING and one system_event per day, so a degrading fill path shows up in
# Grafana rather than being reconstructed after the fact.
_ENTRY_SLIPPAGE_ALERT_PCT = 1.0


def _record_entry_slippage(ticker: str, signal_price: float, fill_price: float,
                           elapsed_s: float | None = None) -> None:
    """Log (and alert on) the signal→fill gap. Never raises."""
    try:
        # math.isfinite rejects NaN, which slips past every comparison below
        # (NaN <= 0 is False, `not NaN` is False) and would otherwise log a
        # meaningless "nan%" slippage line.
        if (not signal_price or not fill_price
                or not math.isfinite(float(signal_price))
                or not math.isfinite(float(fill_price))
                or signal_price <= 0 or fill_price <= 0):
            return
        slip_pct = ((fill_price - signal_price) / signal_price) * 100
        took = f" in {elapsed_s:.1f}s" if elapsed_s is not None else ""
        if slip_pct >= _ENTRY_SLIPPAGE_ALERT_PCT:
            logger.warning(
                "Entry slippage [%s]: approved at $%.4f, FILLED at $%.4f%s "
                "(%+.2f%%) — the stop now sits %.2f%% below a price we did not "
                "decide on, so routine reversion to the signal price alone can "
                "stop us out.",
                ticker, signal_price, fill_price, took, slip_pct, slip_pct,
            )
            record_system_event(
                "entry_slippage_high",
                f"{ticker}: approved ${signal_price:.4f} → filled "
                f"${fill_price:.4f} ({slip_pct:+.2f}%){took}",
            )
        else:
            logger.info(
                "Entry slippage [%s]: approved at $%.4f, filled at $%.4f%s (%+.2f%%)",
                ticker, signal_price, fill_price, took, slip_pct,
            )
    except Exception as exc:   # observability must never break the entry path
        logger.debug("Could not record entry slippage for %s: %s", ticker, exc)


def _usable_price(price: float | None) -> float | None:
    """
    Return `price` if it is a real, positive, finite quote — else None.

    The graduated pre-market hand-off builds a synthetic PriceConfirmation with
    current_price=0.0 (it never got a quote), and NaN survives every ordinary
    comparison, so both must be filtered before a price is stored or divided by.
    """
    try:
        if price is None:
            return None
        value = float(price)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _record_wait_cost(ticker: str, signal_price: float | None,
                      fill_price: float) -> None:
    """
    Log the gap between the price when the signal FIRST appeared and the price
    we actually paid. Never raises.

    This is the momentum gate's bill, itemised. The gate is a real filter, but
    it is paid for in entry price: waiting for the tape to confirm has meant
    buying ~2% higher, which leaves the −2% stop at the price the news fired
    at. Persisted alongside this as trades.signal_price so the question is
    answerable from our own rows rather than from forward-return sampling,
    which cannot see the path between its 5/15/60/120-min checkpoints.
    """
    try:
        fill = _usable_price(fill_price)
        if signal_price is None or fill is None:
            return
        wait_pct = ((fill - signal_price) / signal_price) * 100
        if abs(wait_pct) < 0.01:
            return   # confirmed on the first look — nothing was waited for
        logger.info(
            "Wait cost [%s]: signal first seen at $%.4f, entered at $%.4f "
            "(%+.2f%%) — the stop now sits %.2f%% below the fill, i.e. %.2f%% "
            "from the price the catalyst was published at.",
            ticker, signal_price, fill, wait_pct,
            cfg.stop_loss_pct, cfg.stop_loss_pct - wait_pct,
        )
    except Exception as exc:   # observability must never break the entry path
        logger.debug("Could not record wait cost for %s: %s", ticker, exc)


def _reset_no_quote_blackout_if_new_day() -> None:
    """Clear the no-quote blackout when the ET trading day rolls over."""
    global _no_quote_blackout_day
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    if _no_quote_blackout_day == today:
        return
    if _no_quote_blackout:
        logger.info(
            "New trading day (%s) — clearing no-quote blackout (%d ticker(s): %s)",
            today, len(_no_quote_blackout), ", ".join(sorted(_no_quote_blackout)),
        )
    _no_quote_blackout.clear()
    _no_quote_ticker_strikes.clear()
    _no_quote_blackout_day = today


def _queue_retry(item: NewsItem, count_strike: bool = True) -> None:
    key = (item.article_id, item.ticker)
    _retry_queue[key] = {
        "item": item,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=_RETRY_TTL_MINUTES),
    }
    if not count_strike:
        # Extended session: missing bars is expected here (see the note above),
        # so park the signal without moving it toward a blacklist it doesn't
        # deserve.
        logger.info(
            "Signal [%s] parked for retry (no extended-hours price data — not "
            "counted toward the no-quote blackout) — expires in %d min",
            item.ticker, _RETRY_TTL_MINUTES,
        )
        return
    # v21.11: a strike asserts "no provider carries this instrument". While a
    # provider feed is demonstrably frozen, a miss proves nothing about the
    # TICKER, so it must not count toward a session-long blacklist. On
    # 2026-07-31 both feeds served the previous day's close for every symbol
    # they were asked about (SONY included) for the first 40+ minutes of the
    # session, and GTES + IRMD — liquid, fully-covered names — were blacklisted
    # for the day as a result. The frozen-feed tripwire already DETECTS this;
    # the blacklist simply wasn't listening to it.
    if quote_feed_degraded():
        logger.warning(
            "Signal [%s] parked for retry — a quote feed is currently frozen, "
            "so this miss is NOT counted toward the no-quote blackout "
            "(provider outage, not missing ticker coverage)",
            item.ticker,
        )
        return
    # Track consecutive no-data strikes for this ticker.
    strikes = _no_quote_ticker_strikes.get(item.ticker, 0) + 1
    _no_quote_ticker_strikes[item.ticker] = strikes
    if strikes >= _NO_QUOTE_BLACKOUT_RETRIES:
        _no_quote_blackout.add(item.ticker)
        logger.warning(
            "Signal [%s] blacklisted for today — no quote after %d retries "
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


# ── Slow-path throttle (v21.18) ───────────────────────────────────────────────
# Dropping NEWS_CYCLE_SECONDS to 10 makes us see news ~25s sooner. It must not
# also make us do everything else six times as often, because most of the cycle
# is not news discovery and re-running it costs real money and real quota:
#
#   * the re-eval queue re-confirms EVERY parked signal, and each confirmation
#     spends a Twelvedata credit. Twelvedata's Grow plan allows 55 calls/min
#     (`_PER_MINUTE_LIMIT`), and the token bucket FAILS CLOSED — once it is
#     empty, `confirm_price_signal` returns None and nothing can be confirmed
#     at all. Five parked signals at a 10s cadence is 30 credits/min; a dozen
#     would starve the bucket and silently stop us trading. That is a strictly
#     worse outcome than the 25s of latency this change buys.
#   * the retry queue re-attempts tickers that had NO price data, so faster
#     retries mean more calls against exactly the symbols least likely to
#     answer.
#   * the pre-market scan and the gap-and-go evaluation are anchored to
#     30-minute windows and carry their own wall-clock budgets; 10-second
#     granularity buys them nothing.
#
# None of those benefit from sub-minute granularity — only NEWS DISCOVERY does.
# So the fetch runs at the fast cadence and every periodic sub-step keeps its
# original ~60s spacing, whatever NEWS_CYCLE_SECONDS is set to.
_SLOW_PATH_MIN_INTERVAL_SECONDS = 60.0
_slow_path_last_run: dict[str, float] = {}


def _slow_path_due(name: str,
                   min_interval: float = _SLOW_PATH_MIN_INTERVAL_SECONDS) -> bool:
    """
    True when a periodic sub-step is due again, and records the run.

    Uses time.monotonic() rather than wall-clock so an NTP correction or a DST
    shift can't make a step appear overdue by hours (or never due again).
    """
    now = time.monotonic()
    last = _slow_path_last_run.get(name)
    if last is not None and (now - last) < min_interval:
        return False
    _slow_path_last_run[name] = now
    return True


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


def _queue_reeval(item: NewsItem, signal_id: int, signal_price: float | None = None) -> None:
    """
    Park a transiently-rejected signal for periodic re-confirmation.

    `signal_price` (v21.16) is the price observed on the FIRST evaluation of
    this signal — i.e. what the entry would have cost had we not waited. It is
    carried through the queue so the eventual trade row can record it, making
    "what did waiting for confirmation cost us?" a direct measurement instead
    of an inference from 5/15/60/120-min forward-return samples.
    """
    key = (item.article_id, item.ticker)
    if key in _reeval_queue:
        return  # already waiting — keep the original expiry
    _reeval_queue[key] = {
        "item": item,
        "signal_id": signal_id,
        "signal_price": signal_price,
        # When we first looked at this signal — the other half of the wait
        # measurement. signal_price says what the delay cost; this says how long
        # the delay was, so a 2% wait cost over 30s (a fast mover) is
        # distinguishable from 2% over 14 minutes (the queue grinding).
        "first_seen_at": datetime.now(timezone.utc),
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
            conf = confirm_price_signal(item.ticker, catalyst_type=item.catalyst_type)
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
            if _enter_confirmed(item, conf, entry["signal_id"],
                                signal_price=entry.get("signal_price"),
                                first_seen_at=entry.get("first_seen_at")):
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

    # ── Drawdown circuit breaker (v21.17) ────────────────────────────────────
    # The daily kill switch resets at midnight and therefore cannot see a slow
    # bleed. This measures the peak-to-trough decline the strategy is actually
    # in. Fail-OPEN on error, unlike the kill switch: a missing snapshot series
    # is an observability gap, not evidence of loss, and standing the system
    # down on it would repeat the 2026-07-31 failure where an unavailable
    # portfolio value silently halted 44 news cycles.
    if cfg.max_drawdown_pct > 0:
        try:
            dd = get_drawdown_from_peak(cfg.drawdown_lookback_days)
            if dd is not None:
                current, peak, dd_pct = dd
                if dd_pct <= -abs(cfg.max_drawdown_pct):
                    return False, (
                        f"DRAWDOWN BREAKER: equity £{current:,.2f} is {dd_pct:.2f}% "
                        f"below its {cfg.drawdown_lookback_days}-day peak of "
                        f"£{peak:,.2f}, past the −{cfg.max_drawdown_pct}% limit — "
                        f"no new entries (open positions are still managed)"
                    )
        except Exception as exc:
            logger.warning("drawdown check failed (%s) — gate skipped this cycle", exc)

    # ── Losing-streak cooldown (v21.17) ──────────────────────────────────────
    # A run of consecutive stop-outs is information about the TAPE, not about
    # the next signal. Pause briefly rather than spend the rest of the daily
    # trade budget discovering the same thing four more times.
    if cfg.loss_streak_halt > 0:
        try:
            streak, last_sell = get_loss_streak()
            if streak >= cfg.loss_streak_halt and last_sell:
                last_dt = datetime.fromisoformat(str(last_sell))
                if last_dt.tzinfo is None:
                    last_dt = pytz.timezone("Europe/London").localize(last_dt)
                mins = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if mins < cfg.loss_streak_cooldown_minutes:
                    return False, (
                        f"LOSS-STREAK COOLDOWN: {streak} consecutive losing trades, "
                        f"last closed {mins:.0f} min ago — pausing entries for "
                        f"{cfg.loss_streak_cooldown_minutes - mins:.0f} more min"
                    )
        except Exception as exc:
            logger.warning("loss-streak check failed (%s) — gate skipped this cycle", exc)

    return True, ""


# ── Shared entry execution ────────────────────────────────────────────────────

def _execute_entry(item: NewsItem, confirmation: PriceConfirmation, fetched_at: str,
                   entry_path: str | None = None) -> bool:
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
            _queue_reeval(item, signal_id, signal_price=confirmation.current_price)
        return False

    # first_seen_at is left None: this signal confirmed on its FIRST look, so
    # its entry delay is zero by construction. Only the re-eval path, which
    # knows when it parked the signal, passes a real timestamp.
    return _enter_confirmed(item, confirmation, signal_id,
                            signal_price=confirmation.current_price,
                            entry_path=entry_path)


def _entry_reason(confirmation: PriceConfirmation,
                  entry_path: str | None = None) -> str:
    """
    Name the decision path that authorised this entry, for the trade row.

    'momentum_skipped' vs 'momentum_confirmed' is the distinction that makes
    v21.16 falsifiable: both paths can produce a fill on the signal's first
    look with a wait cost of zero, so nothing in the prices tells them apart.
    Without this, "did entering at the signal work?" has no control group.
    """
    if entry_path:
        return entry_path
    return ("momentum_skipped"
            if getattr(confirmation, "momentum_skipped", False)
            else "momentum_confirmed")


def _entry_delay_seconds(first_seen_at) -> int | None:
    """Seconds from the signal's first evaluation to now. None if unknown."""
    if first_seen_at is None:
        return 0        # confirmed on its first look — it never waited
    try:
        delta = (datetime.now(timezone.utc) - first_seen_at).total_seconds()
        return max(0, int(delta))
    except (TypeError, ValueError):
        return None


def _enter_confirmed(item: NewsItem, confirmation: PriceConfirmation, signal_id: int,
                     signal_price: float | None = None,
                     first_seen_at=None,
                     entry_path: str | None = None) -> bool:
    """
    Buy + resting stop + trade record for an already-confirmed, saved signal.

    `signal_price` (v21.16) is the price at the FIRST evaluation of this
    signal, which for a re-queued signal is minutes older than the price we
    are about to buy at. Persisted on the trade row so the cost of waiting is
    measurable per trade rather than argued from a backtest.

    `first_seen_at` / `entry_path` (v21.16) record HOW the entry happened —
    how long it waited, and which gate path let it through. See _entry_reason.
    """
    # v21: extended-session entries route through T212's extendedHours market
    # orders, at cfg.extended_size_factor size, and get NO resting stop —
    # T212 stop orders execute in regular hours only, so a stop placed now
    # would reserve the shares while protecting nothing; the monitor polls
    # both sides at its 5s cadence instead and flattens before the overnight
    # handoff.
    extended = confirmation.session in EXTENDED_SESSIONS
    entry_reason = _entry_reason(confirmation, entry_path)
    if entry_reason == "momentum_skipped":
        logger.info(
            "Entry path [%s]: MOMENTUM FLOOR SKIPPED — entering at %+.2f%% "
            "(floor is +%.2f%%) because catalyst=%s is in "
            "SKIP_MOMENTUM_CATALYSTS. Recorded on the trade row as "
            "entry_reason='momentum_skipped'.",
            item.ticker, confirmation.recent_move_pct,
            cfg.min_price_move_pct, item.catalyst_type,
        )
    logger.info(
        "Signal approved [%s] @ $%.4f (session=%s) — %s",
        item.ticker, confirmation.current_price, confirmation.session,
        confirmation.reason,
    )

    # ── Buy (liquidity-aware sizing via ADV) ──────────────────────────────────
    # A signal here has already cleared every gate (catalyst, confidence,
    # price/momentum/VWAP/liquidity) — the broker call is the last step, not
    # a filter. quantity == 0 with an order_id of None means calculate_quantity
    # failed BEFORE any order reached T212 (see buy()'s early return) — safe
    # to retry once, since nothing was placed. A failure with a non-empty
    # quantity/order_id means the broker was already contacted; retrying that
    # risks a double order, so it is never retried here.
    result = None
    buy_started_at = time.monotonic()
    for attempt in range(2):
        try:
            result = buy(
                item.ticker, confirmation.current_price,
                confirmation.avg_dollar_volume, extended=extended,
            )
        except Exception as exc:
            logger.error("buy() raised unexpectedly for %s: %s", item.ticker, exc, exc_info=True)
            try:
                set_rejection_reason(signal_id, f"buy raised exception: {exc}", "buy_failed")
            except Exception:
                pass
            return False

        if result.success:
            break
        pre_broker_failure = result.order_id is None and result.quantity == 0
        if attempt == 0 and pre_broker_failure:
            logger.warning(
                "Buy sizing failed [%s] before reaching the broker (%s) — "
                "retrying once", item.ticker, result.error,
            )
            continue
        break

    if not result.success:
        # v21.17: an entry LIMIT that never filled is not a failure — it is the
        # feature working. The market did not come back to a price we were
        # willing to pay, so we own nothing and have lost nothing. Park the
        # signal for re-confirmation instead of recording a terminal rejection;
        # abandoning it here would throw away the upside the limit protects.
        if getattr(result, "unfilled", False):
            logger.info(
                "Entry limit not filled [%s] — market never reached our price. "
                "Signal parked for re-evaluation rather than dropped: %s",
                item.ticker, result.error,
            )
            try:
                set_rejection_reason(
                    signal_id, f"entry limit unfilled: {result.error}",
                    "entry_limit_unfilled",
                )
            except Exception as exc:
                logger.warning("set_rejection_reason failed after unfilled limit: %s", exc)
            _queue_reeval(item, signal_id, signal_price=signal_price)
            return False

        logger.error("Buy order failed [%s]: %s", item.ticker, result.error)
        try:
            set_rejection_reason(signal_id, f"buy order failed: {result.error}", "buy_failed")
        except Exception as exc:
            logger.warning("set_rejection_reason failed after buy failure: %s", exc)
        return False

    # What did the decision price cost us against the price we actually got?
    _record_entry_slippage(
        item.ticker, confirmation.current_price, result.price,
        time.monotonic() - buy_started_at,
    )
    # And what did WAITING cost us — the price when the signal first appeared
    # vs the price we actually paid? For a signal confirmed on its first look
    # these are the same number and the log is a no-op; for one that sat in the
    # re-eval queue it is the entire cost of the momentum gate.
    clean_signal_price = _usable_price(signal_price)
    _record_wait_cost(item.ticker, clean_signal_price, result.price)

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
            session=confirmation.session,
            signal_price=clean_signal_price,
            entry_reason=entry_reason,
            entry_momentum_pct=confirmation.recent_move_pct,
            entry_delay_seconds=_entry_delay_seconds(first_seen_at),
        )
    except Exception as exc:
        logger.error(
            "open_trade() failed for %s after successful buy order %s: %s "
            "— trade executed but NOT recorded in DB; attempting emergency flatten",
            item.ticker, result.order_id, exc,
        )
        try:
            flatten = sell(
                item.ticker, result.quantity, result.price, "db_record_failed",
                force_market=True, extended=extended,
            )
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

    # ── Resting stop-loss (v20 exit inversion) ───────────────────────────────
    # The LOSS side rests at the broker — zero polling latency exactly where
    # latency costs capital (a fast reversal). The profit side is polled by
    # the monitor at its 5s cadence instead; T212 has no OCO and each sell
    # reserves the shares, so only one side can rest — it must be the stop
    # (see executor.place_stop_loss for the realized-slippage evidence).
    # If placement fails, the monitor's polled stop covers this position.
    stop_price = result.price * (1 - cfg.stop_loss_pct / 100)
    stop_order_id = (
        place_stop_loss(item.ticker, result.quantity, stop_price)
        if cfg.resting_stop_enabled and not extended else None
    )
    if extended:
        logger.info(
            "Trade [%s]: extended session (%s) — no resting stop (T212 stops "
            "execute RTH-only); monitor polls both sides at %ds cadence",
            item.ticker, confirmation.session, cfg.monitor_interval_seconds,
        )
    if stop_order_id:
        try:
            set_stop_order_id(trade_id, stop_order_id)
        except Exception as exc:
            logger.error(
                "Could not store stop_order_id %s for trade %d: %s — monitor will "
                "not know about the resting order; cancelling it now",
                stop_order_id, trade_id, exc,
            )
            if cancel_order(stop_order_id):
                logger.warning(
                    "Untracked resting stop %s for trade %d cancelled; monitor will poll the stop",
                    stop_order_id, trade_id,
                )
                stop_order_id = None
            else:
                logger.critical(
                    "Could not cancel untracked stop order %s for trade %d — "
                    "manual broker reconciliation required before any polled sell",
                    stop_order_id, trade_id,
                )

    logger.info(
        "Trade #%d opened: %s × %.6f @ $%.4f | net=£%.2f fx=%.4f fees=£%.2f | "
        "order=%s | resting_stop=%s | tp=polled",
        trade_id, item.ticker, result.quantity, result.price,
        result.net_gbp or 0, result.fx_rate or 0, result.fees_gbp or 0,
        result.order_id, stop_order_id or "polled",
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


def _enter_premarket_approved(approved: list, fetched_at: str) -> bool:
    """
    Enter every approved gap-and-go candidate. Returns False when a portfolio
    risk gate tripped mid-loop and the rest of the news cycle must be abandoned.

    Extracted from news_cycle() in v21.16 so the `entry_path="premarket_gap"`
    label is testable at its call site. It is not decoration: the gap path is a
    different strategy from an intraday news entry — the move already happened
    overnight, which is what put the candidate on the watchlist — so pooling it
    into either momentum bucket would corrupt the v21.16 comparison. A call site
    that silently stops passing it produces plausible-looking rows in the wrong
    group, which is worse than a crash.

    Each candidate is handled in its own try/except so a bug triggered by one
    candidate's data (e.g. an unexpected field shape) can't silently drop every
    candidate queued behind it with no DB trace of what happened to them. This
    is the same shape of bug that caused a 2026-06-11→07-06 zero-trade drought:
    a single unhandled TypeError inside this loop used to abort the whole batch
    with one generic log line and no per-candidate status update, leaving the
    rest stuck "pending" indefinitely.
    """
    for cand, conf in approved:
        try:
            if was_recently_traded(cand["ticker"]):
                update_premarket_candidate(cand["id"], "rejected", "24h ticker cooldown")
                continue
            item = _candidate_to_news_item(cand)
            opened = _execute_entry(item, conf, fetched_at,
                                    entry_path="premarket_gap")
            update_premarket_candidate(
                cand["id"], "traded" if opened else "rejected",
                None if opened else "buy failed or signal save failed",
            )
        except Exception as exc:
            logger.error(
                "Pre-market candidate %s (id=%s) failed: %s",
                cand.get("ticker"), cand.get("id"), exc, exc_info=True,
            )
            try:
                update_premarket_candidate(cand["id"], "rejected", f"unhandled error: {exc}")
            except Exception:
                pass
            continue
        # Re-check gates after each entry so a fill can't blow the caps.
        gates_ok, gate_reason = _risk_gates_pass()
        if not gates_ok:
            logger.warning("Risk gate tripped mid-cycle: %s", gate_reason)
            return False
    return True


def news_cycle() -> None:
    """
    The main pipeline — runs every cfg.news_cycle_seconds (v21.18).

    Only news DISCOVERY benefits from a fast cadence. Every periodic sub-step
    is throttled to ~60s by _slow_path_due(), so lowering the interval buys
    latency without multiplying quota consumption.

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

    # ── Session gate (v21) ────────────────────────────────────────────────────
    # regular            → full pipeline (unchanged).
    # premarket          → watchlist scan (always); direct entries only when
    #                      PREMARKET_TRADING_ENABLED (default off — the
    #                      at-open gap-and-go eval is the pre-market strategy).
    # afterhours         → full pipeline in the extended regime when enabled:
    #                      this is where FDA/guidance catalysts actually print.
    # overnight / closed → nothing. Blue Ocean is invisible to our data
    #                      providers; no bars → no confirmation → no trade.
    session = get_trading_session()

    # The no-quote blackout is a one-day judgement, not a permanent one — roll
    # it over before any ticker is tested against it (v21.6).
    _reset_no_quote_blackout_if_new_day()

    if (session == "premarket" and in_premarket_window()
            and _slow_path_due("premarket_scan")):
        logger.info("Pre-market window — scanning for overnight catalysts")
        try:
            premarket_scan()
        except Exception as exc:
            logger.error("premarket_scan failed: %s", exc, exc_info=True)

    if not is_entry_session(session):
        if session != "premarket":
            logger.info("No entries in session=%s — skipping cycle", session)
        return

    if is_too_late_to_buy(session):
        logger.info(
            "Too close to the %s session's hard exit boundary to open new "
            "positions (time_stop=%d min) — skipping cycle",
            session, cfg.time_stop_minutes,
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
    # Regular session only: the gap-and-go evaluation is anchored to the
    # 09:30 open. In extended sessions there is nothing here to evaluate.
    try:
        approved, graduated = (
            evaluate_premarket_candidates()
            if session == REGULAR and _slow_path_due("premarket_eval")
            else ([], [])
        )
    except Exception as exc:
        # Nothing to iterate if the evaluation call itself blew up — this one
        # legitimately aborts the whole premarket step for the cycle.
        logger.error("Pre-market candidate evaluation failed: %s", exc, exc_info=True)
        approved, graduated = [], []

    if not _enter_premarket_approved(approved, fetched_at):
        return

    # Candidates whose 30-min gap-and-go window closed still PENDING (never
    # confirmed, never terminally rejected) are hand off to the same
    # standing re-evaluation queue regular-hours signals use, rather than
    # discarded. A synthetic transient PriceConfirmation routes them through
    # the existing _execute_entry -> _queue_reeval path unchanged (see
    # 2026-07-08 post-mortem: KGS/ARQT/AYA/URGN all drifted 1-3% higher
    # over the rest of the session after their premarket window expired,
    # with no mechanism to ever look at them again).
    for cand in graduated:
        try:
            if was_recently_traded(cand["ticker"]):
                continue
            item = _candidate_to_news_item(cand)
            handoff_conf = PriceConfirmation(
                ticker=item.ticker, symbol=cand["ticker"],
                current_price=0.0, open_price=0.0, prev_close=None,
                day_move_pct=0.0, day_change_pct=None, recent_move_pct=0.0,
                current_volume=0, avg_volume=0, rvol=0.0,
                avg_dollar_volume=None, spread_proxy_pct=None,
                is_confirmed=False,
                reason=(
                    "Gap-and-go eval window closed without a confirmed move — "
                    "handed off to standard momentum re-check for the rest of "
                    "the session"
                ),
                reason_code="low_momentum",
            )
            _execute_entry(item, handoff_conf, fetched_at)
        except Exception as exc:
            logger.error(
                "Pre-market candidate hand-off %s (id=%s) failed: %s",
                cand.get("ticker"), cand.get("id"), exc, exc_info=True,
            )
            continue

    # ── 2. Re-eval queue (transient tape rejections awaiting participation) ──
    # Throttled to the slow path: each parked signal costs a Twelvedata credit
    # per re-confirmation, and the 15-minute window does not need 10-second
    # granularity. See _slow_path_due().
    reeval_opened = _process_reeval_queue() if _slow_path_due("reeval") else 0
    if reeval_opened:
        gates_ok, gate_reason = _risk_gates_pass()
        if not gates_ok:
            logger.warning("Risk gate tripped after re-eval entries: %s", gate_reason)
            return

    # ── 3. Retry queue (already-scored signals that hit a data outage) ───────
    # ── 4. Fresh signals from Benzinga ────────────────────────────────────────
    retry_items = _drain_retry_queue() if _slow_path_due("retry") else []
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
            confirmation = confirm_price_signal(
                item.ticker, catalyst_type=item.catalyst_type,
            )
        except Exception as exc:
            logger.error(
                "confirm_price_signal raised for %s: %s", item.ticker, exc, exc_info=True,
            )
            continue

        if confirmation is None:
            # Data outage (not a rejection) — park for retry; the fetcher's
            # freshness filter can't see queue entries, so they survive.
            # In an extended session missing bars is the expected state, not
            # evidence the ticker is uncovered — don't let it earn a strike.
            funnel["no_price_data"] += 1
            _queue_retry(item, count_strike=session not in EXTENDED_SESSIONS)
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
    # v21: snapshot during extended sessions too — with 24/5 positions the
    # portfolio value moves outside RTH.
    if get_trading_session() not in ("regular", "premarket", "afterhours"):
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


def setup_scheduler() -> BackgroundScheduler:
    """
    Build the scheduler with every job registered, and return it unstarted.

    Extracted from main() in v21.18 so the news cycle's cadence and its
    overlap protection are testable. At the old fixed 1-minute interval
    max_instances/coalesce were academic; at 10 seconds they are load-bearing
    (3 of 325 measured RTH cycles exceeded 10s), and a guarantee nothing
    asserts is a guarantee that quietly disappears.
    """
    scheduler = BackgroundScheduler(timezone=pytz.utc)

    # v21.18: cadence is configurable so the poll interval — which IS our
    # publication→fetch latency, ~half the interval on average — can be tuned
    # without a code change.
    #
    # max_instances=1 and coalesce=True are set EXPLICITLY rather than left to
    # APScheduler's defaults, because at a 10s interval they stop being
    # theoretical. Measured RTH cycles run p50 1s / p90 4s, but 3 of 325 today
    # exceeded 10s. Two concurrent news cycles would race the re-eval queue and
    # the retry queue (plain dicts, not thread-safe) and could double-enter a
    # signal; skipping the overlapping run is the only safe resolution.
    # coalesce collapses a backlog into one run instead of firing a burst.
    scheduler.add_job(
        news_cycle,
        trigger=IntervalTrigger(seconds=cfg.news_cycle_seconds),
        id="news_cycle",
        name="News → Price Check → Buy",
        misfire_grace_time=max(15, cfg.news_cycle_seconds // 2),
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        monitor_positions,
        trigger=IntervalTrigger(seconds=cfg.monitor_interval_seconds),
        id="monitor",
        name="Position monitor",
        misfire_grace_time=15,
    )

    # Nightly eval loop: forward returns for every Claude classification.
    # 22:30 UTC is after the US close year-round (21:00 BST / 20:00 GMT close).
    scheduler.add_job(
        _nightly_forward_returns,
        trigger=CronTrigger(hour=22, minute=30, day_of_week="mon-fri"),
        id="forward_returns",
        name="Eval loop: forward returns",
        misfire_grace_time=3600,
    )

    # Daily symbol-map refresh (also picks up new listings).
    scheduler.add_job(
        build_symbol_map,
        trigger=CronTrigger(hour=8, minute=0, day_of_week="mon-fri"),
        id="symbol_map_rebuild",
        name="T212 symbol map rebuild",
        misfire_grace_time=3600,
    )

    # Zero-trade drought tripwire. 21:30 UTC is after the US close year-round, so
    # "today" is already settled when it runs. Weekdays only — a Saturday alert
    # would just re-report Friday's idle count.
    scheduler.add_job(
        check_zero_trade_drought,
        trigger=CronTrigger(hour=21, minute=30, day_of_week="mon-fri"),
        id="zero_trade_drought",
        name="Zero-trade drought alert",
        misfire_grace_time=3600,
    )

    # Portfolio value snapshots for the Grafana time-series panel.
    # Every 5 min; the job itself returns immediately when the market is closed.
    scheduler.add_job(
        portfolio_snapshot,
        trigger=IntervalTrigger(minutes=5),
        id="portfolio_snapshot",
        name="Portfolio value snapshot",
        misfire_grace_time=60,
    )

    return scheduler


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
    _scheduler = setup_scheduler()

    # Surface an in-progress drought immediately on startup, not only at 21:30.
    check_zero_trade_drought()

    # Run once immediately on startup
    logger.info("Running initial news cycle...")
    news_cycle()

    _scheduler.start()
    logger.info(
        "Scheduler running. News cycle every %ds (expected publication→fetch "
        "latency ~%.0fs), slow path every %ds, monitor every %ds. "
        "Press Ctrl+C to stop.",
        cfg.news_cycle_seconds, cfg.news_cycle_seconds / 2,
        int(_SLOW_PATH_MIN_INTERVAL_SECONDS), cfg.monitor_interval_seconds,
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
