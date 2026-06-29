"""
premarket/scanner.py
─────────────────────
Pre-market news pipeline ("gap-and-go", v14).

Why this exists:
  Most genuine catalysts — earnings, FDA decisions, M&A — publish 07:00–09:25
  ET, when the regular-hours pipeline is asleep (news_cycle returns early
  when the market is closed, and its 60s freshness filter would drop the
  articles by the open anyway). Before v14 the system structurally could not
  trade the single richest source of momentum: the overnight catalyst.

Why we do NOT pre-place orders:
  The gap prices the news in BEFORE the open. A pre-placed order executes at
  the opening auction price — buying the entire gap with zero confirmation.
  "Gap-and-crap" (gap up, fade all day) is one of the most common intraday
  patterns; blind at-open buying systematically tops-ticks it. T212 also has
  no market-on-open order type, so a pending order would do exactly this.

What we do instead (the professional version):
  1. SCAN  (premarket_scan, every minute from cfg.premarket_scan_start_et):
     fetch pre-market Benzinga news, score it with the same Claude classifier
     and the same trade gates (confidence / catalyst class / already_moved),
     and store survivors in the premarket_candidates watchlist.
  2. CONFIRM AT THE OPEN (evaluate_premarket_candidates, called by the news
     cycle once the market opens and the opening block has passed):
       - gap gate: current price vs previous close must be inside
         [cfg.min_gap_pct, cfg.max_gap_pct]. Below the band the market
         doesn't believe the catalyst; above it the move is exhausted.
       - full price confirmation: the standard confirm_price_signal() gate
         (momentum, RVOL, liquidity, spread...) must also pass — buyers must
         be following through AFTER the open, not just at the auction.
  3. Candidates that survive are returned to main.py, which executes them
     through the exact same risk gates and buy path as regular-hours signals.

Candidates expire at open+_EVAL_WINDOW_MINUTES (gap-and-go is a first-half-
hour trade) and at the end of the day they were created.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pytz

from config.settings import cfg
from market.price_check import confirm_price_signal, PriceConfirmation
from market.twelvedata_bars import credits_exhausted
from news.fetcher import fetch_all_news, NewsItem
from storage.database import (
    is_premarket_candidate_seen,
    save_premarket_candidate,
    get_pending_premarket_candidates,
    update_premarket_candidate,
    touch_heartbeat,
)

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_LONDON = pytz.timezone("Europe/London")

# Candidates are only evaluated during the first N minutes after the open.
# Past that, the gap-and-go edge is gone — late entries on morning news are
# exactly the "buying the top" failure v13 eliminated intraday.
_EVAL_WINDOW_MINUTES = 30

# Pre-market articles can be up to this old when scanned. The scanner runs
# every minute, so 5 min gives comfortable overlap without re-scoring stale
# news (dedup via premarket_candidates handles repeats anyway).
_SCAN_MAX_AGE_MINUTES = 5.0

# At the open, candidates are price-confirmed CONCURRENTLY rather than one at a
# time. Root cause (2026-06-18 zero-trades incident): serial evaluation of
# ~13 candidates, several with Finnhub-pc=0 + Twelvedata 429/404 retry backoff
# (3+6+9s each), pushed a single eval cycle past 60s — the scanner skipped whole
# minutes and 9 candidates EXPIRED having never been price-checked once. With a
# bounded thread pool + the fast (no-retry) quote path, one slow/dead ticker can
# no longer starve the rest: they all resolve in parallel within the budget, and
# anything that doesn't simply stays pending for next cycle (~60s away, still
# inside the 30-min window). See docs/algorithm.md §7 "Known failure mode".
_EVAL_MAX_WORKERS = 8
# Hard wall-clock ceiling for the parallel confirm phase. Comfortably under the
# 60s news-cycle interval so the scanner always completes a full pass per minute.
_EVAL_CYCLE_BUDGET_SECONDS = 30.0

# Per-candidate strike counter for consecutive no-data cycles.
# After this many consecutive conf=None returns the ticker has no
# Finnhub/Twelvedata coverage — expire it rather than retrying for 30 min.
# The threshold absorbs 1-2 transient token-bucket misses (those resolve fast).
_no_quote_strikes: dict[int, int] = {}
_NO_QUOTE_EXPIRE_AFTER = 3


def _now_et() -> datetime:
    return datetime.now(_ET)


def in_premarket_window() -> bool:
    """
    True when the pre-market scanner should run: a weekday, between
    cfg.premarket_scan_start_et and the 09:30 open (ET).
    Holiday handling is implicit: scanning on a holiday only builds
    candidates that expire unevaluated — harmless, and far simpler than
    wiring the exchange calendar in here.
    """
    if not cfg.premarket_enabled:
        return False
    now = _now_et()
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    try:
        start_h, start_m = (int(x) for x in cfg.premarket_scan_start_et.split(":"))
    except ValueError:
        logger.warning(
            "Invalid PREMARKET_SCAN_START_ET=%r — using 08:00", cfg.premarket_scan_start_et
        )
        start_h, start_m = 8, 0
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return start <= now < open_


def premarket_scan() -> None:
    """
    One pre-market scan cycle: fetch + score recent news, add tradeable
    positives to the watchlist. Called every minute by main.news_cycle while
    in_premarket_window() is True.

    Reuses fetch_all_news — same Claude classifier, same confidence /
    catalyst / already_moved gates as regular hours — with two differences:
    a wider freshness window, and dedup against the premarket_candidates
    table instead of news_signals.
    """
    try:
        touch_heartbeat("premarket_scan")
    except Exception:
        pass

    items: list[NewsItem] = fetch_all_news(
        lookback_minutes=int(_SCAN_MAX_AGE_MINUTES) + 1,
        max_age_minutes=_SCAN_MAX_AGE_MINUTES,
        seen_checker=is_premarket_candidate_seen,
    )
    if not items:
        return

    for item in items:
        try:
            cand_id = save_premarket_candidate(
                article_id=item.article_id,
                ticker=item.ticker,
                headline=item.headline,
                catalyst_type=item.catalyst_type,
                confidence=item.confidence,
                published_at=item.published_at.isoformat(),
                catalyst_magnitude=item.catalyst_magnitude,
            )
            logger.info(
                "Pre-market candidate #%d added: [%s] %s (catalyst=%s conf=%.2f)",
                cand_id, item.ticker, item.headline[:70],
                item.catalyst_type, item.confidence,
            )
        except Exception as exc:
            logger.error(
                "Could not save pre-market candidate %s/%s: %s",
                item.ticker, item.article_id, exc,
            )


def _minutes_since_open() -> float:
    now = _now_et()
    open_ = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return (now - open_).total_seconds() / 60


def _live_candidates(pending: list[dict], minutes_open: float) -> list[dict]:
    """
    Sequential, NO-I/O pre-pass: expire candidates that are stale (created on a
    prior day) or whose 30-min eval window has closed, and return the ones still
    worth price-checking. Runs before the (parallel, I/O-bound) confirm phase so
    we never spend a quote/credit on a candidate that's already expired.
    """
    today_london = datetime.now(_LONDON).date()
    live: list[dict] = []
    for cand in pending:
        # created_at is stored as a London-offset ISO string; parse it back to a
        # London date so midnight-straddling comparisons are correct.
        try:
            created_ts = datetime.fromisoformat(str(cand.get("created_at", "")))
            if created_ts.tzinfo is None:
                created_ts = _LONDON.localize(created_ts)
            created_day = created_ts.astimezone(_LONDON).date()
        except (ValueError, TypeError):
            created_day = None
        if created_day != today_london:
            update_premarket_candidate(cand["id"], "expired", f"stale: created {created_day}")
            continue
        if minutes_open > _EVAL_WINDOW_MINUTES:
            update_premarket_candidate(
                cand["id"], "expired",
                f"eval window closed ({minutes_open:.0f} min after open)",
            )
            continue
        live.append(cand)
    return live


def _apply_confirmation(
    cand: dict, conf: PriceConfirmation | None
) -> tuple[dict, PriceConfirmation] | None:
    """
    Turn one candidate's PriceConfirmation into a verdict: write a terminal
    status to its row and return (cand, conf) if APPROVED, else None. Pure given
    `conf` (no network) — this is the gate logic, unchanged from the original
    serial loop; only the quote fetch that produces `conf` has been parallelized.

    Returning None with NO status write means "stay pending, retry next cycle"
    (data outage, missing prev close, or opening block still active) — all
    transient conditions bounded by the 30-min window expiry in _live_candidates.
    """
    cand_id = cand["id"]
    ticker = cand["ticker"]

    if conf is None:
        # Track consecutive no-data cycles. After _NO_QUOTE_EXPIRE_AFTER strikes
        # the ticker has no Finnhub/Twelvedata coverage — expire it immediately
        # rather than burning API credits for the full 30-min eval window.
        # Token-bucket exhaustion also produces conf=None but resolves within
        # 1-2 cycles; the threshold absorbs those transient misses.
        strikes = _no_quote_strikes.get(cand_id, 0) + 1
        _no_quote_strikes[cand_id] = strikes
        if strikes >= _NO_QUOTE_EXPIRE_AFTER:
            _no_quote_strikes.pop(cand_id, None)
            update_premarket_candidate(
                cand_id, "expired",
                f"no_coverage: no quote after {_NO_QUOTE_EXPIRE_AFTER} consecutive retries",
            )
            logger.info(
                "Pre-market eval [%s]: no quote after %d retries — expiring "
                "(not covered by Finnhub/Twelvedata)",
                ticker, _NO_QUOTE_EXPIRE_AFTER,
            )
            return None
        logger.info(
            "Pre-market eval [%s]: price data unavailable (attempt %d/%d) — retrying next cycle",
            ticker, strikes, _NO_QUOTE_EXPIRE_AFTER,
        )
        return None

    # Any successful price check resets the strike counter.
    _no_quote_strikes.pop(cand_id, None)

    # ── Opening-block guard: must come BEFORE gap_pct ─────────────────────────
    # The opening block early-returns from confirm_price_signal before prev_close
    # is computed, so conf.day_change_pct=None on all opening_block cycles.
    # Checking gap_pct first would misattribute every opening_block cycle as
    # "prev close unavailable" — observed 2026-06-29: all 40 candidates mis-logged
    # for the first 5 minutes after the open, including liquid names like AMGN/PFE.
    if not conf.is_confirmed and conf.reason_code == "opening_block":
        logger.debug(
            "Pre-market eval [%s]: opening block active — re-evaluating after it lifts",
            ticker,
        )
        return None  # stays pending

    # ── Gap gate: vs previous close, gap included ────────────────────────────
    gap_pct = conf.day_change_pct
    if gap_pct is None:
        # Genuine prev-close miss (both Finnhub pc=0 and Twelvedata daily bar
        # unavailable). Transient at open; resolved once TD's daily bar rolls.
        logger.info(
            "Pre-market eval [%s]: prev close unavailable — retrying next cycle", ticker
        )
        return None
    if gap_pct < cfg.min_gap_pct:
        update_premarket_candidate(
            cand_id, "rejected",
            f"gap {gap_pct:+.2f}% < {cfg.min_gap_pct}% — market doesn't believe the catalyst",
        )
        return None
    if gap_pct > cfg.max_gap_pct:
        update_premarket_candidate(
            cand_id, "rejected",
            f"gap {gap_pct:+.2f}% > {cfg.max_gap_pct}% — move exhausted pre-open",
        )
        return None

    # ── Standard confirmation: post-open follow-through required ──────────────
    if not conf.is_confirmed:
        # opening_block already handled above; all remaining rejections are final.
        # Re-evaluating every cycle for 30 min would cost ~60 Twelvedata credits
        # per candidate, and a candidate that fails post-block is gap-and-crap.
        update_premarket_candidate(cand_id, "rejected", f"{conf.reason_code}: {conf.reason}")
        return None

    logger.info(
        "Pre-market candidate APPROVED: [%s] gap=%+.2f%% %s — %s",
        ticker, gap_pct, conf.reason, cand["headline"][:60],
    )
    return (cand, conf)


def evaluate_premarket_candidates() -> list[tuple[dict, PriceConfirmation]]:
    """
    At-open evaluation of the watchlist. Returns the candidates that passed
    BOTH the gap gate and full price confirmation, paired with their
    PriceConfirmation — main.py executes them through its standard risk
    gates and buy path (this module never places orders itself).

    Candidates are price-confirmed CONCURRENTLY (bounded thread pool + fast,
    no-retry quote path) under a hard wall-clock budget, so one slow or
    unpriceable ticker can never starve the window — the failure that produced
    the 2026-06-18 zero-trades day. Anything not resolved within the budget
    stays pending for the next cycle (~60s away, still inside the 30-min window).

    Every candidate's outcome is recorded on its row (status + eval_note) so the
    watchlist is fully auditable after the fact.
    """
    pending = get_pending_premarket_candidates()
    if not pending:
        return []

    # Budget guard: if Twelvedata credits are spent, every confirm_price_signal
    # would return None anyway (the bar calls short-circuit). Don't spin up the
    # thread pool or write any verdict — leave candidates pending so they're
    # re-evaluated for free next cycle (within their 30-min window) or expire via
    # _live_candidates. Trading is suspended for the day; news scoring continues.
    if credits_exhausted():
        logger.warning(
            "Pre-market eval skipped — Twelvedata credit budget exhausted; "
            "%d candidate(s) left pending (no trades until UTC midnight)",
            len(pending),
        )
        return []

    minutes_open = _minutes_since_open()
    live = _live_candidates(pending, minutes_open)
    if not live:
        return []

    # ── Parallel confirm phase ───────────────────────────────────────────────
    # Each confirm_price_signal does its own fast (single-attempt, no-backoff)
    # I/O — worst case ~one HTTP timeout (_TIMEOUT=8s), never the 18s retry storm
    # that broke 2026-06-18. Running them across a small thread pool makes the
    # cycle's wall time ≈ the slowest single candidate, not the sum. The budget
    # is a secondary guard: when it fires we record verdicts for whatever
    # finished and leave the rest pending (no status write) for next cycle.
    #
    # shutdown(wait=False, cancel_futures=True): cancel any not-yet-started
    # futures and DON'T block the cycle on a straggler already mid-call — it
    # finishes harmlessly in a daemon-ish background thread; we simply ignore its
    # result. This keeps the news cycle returning promptly (well under its 60s
    # interval) so the fast candidates' verdicts get acted on immediately.
    confs: dict[int, PriceConfirmation | None] = {}
    workers = min(_EVAL_MAX_WORKERS, len(live))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pmc-eval")
    future_to_cand = {
        pool.submit(confirm_price_signal, cand["ticker"], fast=True): cand
        for cand in live
    }
    try:
        for future in as_completed(future_to_cand, timeout=_EVAL_CYCLE_BUDGET_SECONDS):
            cand = future_to_cand[future]
            try:
                confs[cand["id"]] = future.result()
            except Exception as exc:
                logger.error(
                    "Pre-market eval: confirm_price_signal raised for %s: %s",
                    cand["ticker"], exc,
                )
    except TimeoutError:
        unresolved = [c["ticker"] for c in live if c["id"] not in confs]
        logger.warning(
            "Pre-market eval: %d/%d candidates unresolved within %.0fs budget "
            "(%s) — left pending for next cycle",
            len(unresolved), len(live), _EVAL_CYCLE_BUDGET_SECONDS,
            ",".join(unresolved),
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # ── Verdict phase (sequential DB writes; pure given each conf) ────────────
    approved: list[tuple[dict, PriceConfirmation]] = []
    for cand in live:
        if cand["id"] not in confs:
            continue  # unresolved within budget — stays pending
        result = _apply_confirmation(cand, confs[cand["id"]])
        if result is not None:
            approved.append(result)

    return approved
