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

Candidates expire at open+_EVAL_WINDOW_MINUTES and at the end of the day
they were created.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pytz

from config.settings import cfg
from market.price_check import (
    confirm_price_signal, PriceConfirmation, quote_feed_degraded,
)
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
# inside the eval window). See docs/algorithm.md §7 "Known failure mode".
_EVAL_MAX_WORKERS = 8
# Hard wall-clock ceiling for the parallel confirm phase. Comfortably under the
# 60s news-cycle interval so the scanner always completes a full pass per minute.
_EVAL_CYCLE_BUDGET_SECONDS = 30.0

# Per-candidate strike counter for consecutive no-data cycles (conf=None).
# After this many consecutive conf=None returns the ticker has no
# Finnhub/Twelvedata coverage — expire it rather than retrying all window.
# The threshold absorbs 1-2 transient token-bucket misses (those resolve fast).
_no_quote_strikes: dict[int, int] = {}
_NO_QUOTE_EXPIRE_AFTER = 3

# Grace window where a no-quote miss doesn't burn a strike at all. Observed
# 2026-07-08: in the first ~90s after the open, Twelvedata served a quote
# timestamped exactly 24h old for ~19 different tickers simultaneously (its
# own snapshot cache not yet rotated for the new session) while Finnhub's
# quote was still genuinely carrying yesterday's close — both sources
# correctly read as "no live coverage yet" for a systemic, predictable,
# self-healing reason unrelated to any single ticker's real coverage. It
# resolved within one cycle every time, but a free strike here preserves the
# full 3-strike budget for tickers with a genuine, not-provider-wide outage.
_OPEN_GRACE_MINUTES = 2.0

# Per-candidate strike counter for consecutive prev-close-unavailable cycles.
# Finnhub returns pc=0 for some tickers; Twelvedata's daily bar can take 1-2
# minutes to roll at 09:30 ET. Both produce gap_pct=None. Without a bound,
# a candidate retries silently for the full 30-min window and expires as
# "eval window closed" — indistinguishable in the log from a genuine timeout.
# After _GAP_PCT_EXPIRE_AFTER strikes, expire with an explicit reason instead.
# 5 cycles = 5 minutes; genuine transient cases (TD bar delay) resolve in 1-2.
_gap_pct_strikes: dict[int, int] = {}
_GAP_PCT_EXPIRE_AFTER = 5

# Rejection codes that describe the tape AT THIS MINUTE rather than a property
# of the instrument or the day: participation can arrive a few minutes after
# the news (RVOL), a 5-min momentum window dips negative on the first
# pullback of a genuine mover, and a price stretched too far above VWAP
# (overextended, v20) pulls back into buyable range within minutes on real
# movers. Candidates rejected with these codes stay pending and re-evaluate
# every cycle until the eval window closes. opening_block joined in v21.6 —
# it is a pure countdown against cfg.open_block_minutes, so it is guaranteed
# to clear on its own within minutes; treating it as terminal discarded
# at-open candidates a minute or two short of the line (see the matching note
# in main.py). Everything else (penny_stock, illiquid, dead_cat,
# extended_move, wide_spread, high_momentum, high_volume, below_vwap,
# exhausted_bounce, insufficient_data) is terminal.
# Must stay in sync with main._TRANSIENT_REJECT_CODES — see the note there on
# why stale_price/stale_volume (v21.11) belong here.
_TRANSIENT_REJECT_CODES = frozenset(
    {"low_volume", "low_momentum", "overextended", "opening_block",
     "stale_price", "stale_volume"}
)


def _clear_strikes(cand_id: int) -> None:
    """Drop both per-candidate strike counters once a candidate reaches ANY
    terminal status (traded/rejected/expired/approved). Entries left behind
    by candidates that expired via _live_candidates (or were rejected before
    striking out) otherwise accumulate for the life of the process."""
    _no_quote_strikes.pop(cand_id, None)
    _gap_pct_strikes.pop(cand_id, None)


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


def _candidate_rank(cand: dict) -> tuple:
    """Sort key for choosing between candidates naming the SAME ticker."""
    try:
        conf = float(cand.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    try:
        mag = int(cand.get("catalyst_magnitude") or 0)
    except (TypeError, ValueError):
        mag = 0
    # Highest confidence wins, then magnitude, then the newest row — a later
    # article about the same event is usually the more complete one.
    return (conf, mag, cand.get("id") or 0)


def _dedupe_by_ticker(live: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Collapse candidates that name the same ticker. Returns (kept, superseded).

    One corporate event routinely produces several Benzinga articles, and the
    scanner stores one candidate PER ARTICLE. Nothing downstream collapsed them,
    so a single event was evaluated, approved and BOUGHT once per article.

    2026-08-26: Bath & Body Works published three guidance articles before the
    open (conf 0.75 / 0.85 / 0.90). All three became candidates, all three
    approved, and all three fired a buy for LB_US_EQ within seconds — six order
    requests including the precision retries. T212 rate-limited us and every one
    failed with HTTP 429, the first 429s in nine days. Had they succeeded we
    would instead have opened three positions in one stock: the 24-hour ticker
    cooldown only engages once a trade is RECORDED, which these were racing.

    Deduping here — in the sequential, no-I/O pre-pass — also stops us spending
    a quote credit per duplicate. Across all history 594 candidates covered only
    550 unique ticker-days.
    """
    best: dict[str, dict] = {}
    superseded: list[dict] = []
    for cand in live:
        ticker = cand.get("ticker")
        incumbent = best.get(ticker)
        if incumbent is None:
            best[ticker] = cand
            continue
        if _candidate_rank(cand) > _candidate_rank(incumbent):
            superseded.append(incumbent)
            best[ticker] = cand
        else:
            superseded.append(cand)
    return list(best.values()), superseded


def _live_candidates(
    pending: list[dict], minutes_open: float
) -> tuple[list[dict], list[dict]]:
    """
    Sequential, NO-I/O pre-pass: expire candidates that are stale (created on a
    prior day) or whose eval window has closed, and return the ones still
    worth price-checking. Runs before the (parallel, I/O-bound) confirm phase so
    we never spend a quote/credit on a candidate that's already expired.

    Returns (live, graduated):
      live      — still inside the 30-min gap-and-go window, worth a price
                  check this cycle.
      graduated — window closed while STILL PENDING (never confirmed, never
                  terminally rejected — only transient low_volume/low_momentum
                  misses). The gap-and-go edge (buying the open-auction
                  reaction) is gone, but the underlying catalyst may still be
                  developing; the caller hands these to the same standing
                  re-evaluation queue regular-hours signals use, rather than
                  discarding a still-live catalyst just because it missed a
                  30-minute cutoff. Stale (prior-day) candidates are NOT
                  graduated — they're just dead.
    """
    today_london = datetime.now(_LONDON).date()
    live: list[dict] = []
    graduated: list[dict] = []
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
            _clear_strikes(cand["id"])
            continue
        if minutes_open > _EVAL_WINDOW_MINUTES:
            update_premarket_candidate(
                cand["id"], "expired",
                f"eval window closed ({minutes_open:.0f} min after open) — "
                f"handed off to standard momentum re-check",
            )
            _clear_strikes(cand["id"])
            graduated.append(cand)
            continue
        live.append(cand)

    # One event, one trade. Duplicate candidates for the same ticker are
    # retired here rather than each racing the others to the broker.
    live, superseded = _dedupe_by_ticker(live)
    for dupe in superseded:
        keeper = next((c for c in live if c.get("ticker") == dupe.get("ticker")), None)
        keeper_id = keeper.get("id") if keeper else "?"
        logger.info(
            "Pre-market dedupe [%s]: candidate #%s superseded by #%s "
            "(same ticker, same event, higher confidence)",
            dupe.get("ticker"), dupe.get("id"), keeper_id,
        )
        try:
            update_premarket_candidate(
                dupe["id"], "rejected",
                f"duplicate ticker — superseded by candidate #{keeper_id}",
            )
            _clear_strikes(dupe["id"])
        except Exception as exc:
            logger.warning(
                "Could not retire duplicate candidate #%s: %s", dupe.get("id"), exc,
            )
    return live, graduated


def _apply_confirmation(
    cand: dict, conf: PriceConfirmation | None, minutes_open: float = 999.0
) -> tuple[dict, PriceConfirmation] | None:
    """
    Turn one candidate's PriceConfirmation into a verdict: write a terminal
    status to its row and return (cand, conf) if APPROVED, else None. Pure given
    `conf` (no network) — this is the gate logic, unchanged from the original
    serial loop; only the quote fetch that produces `conf` has been parallelized.

    Returning None with NO status write means "stay pending, retry next cycle"
    (data outage, missing prev close, or opening block still active) — all
    transient conditions bounded by the eval window expiry in _live_candidates.
    """
    cand_id = cand["id"]
    ticker = cand["ticker"]

    if conf is None:
        if minutes_open < _OPEN_GRACE_MINUTES:
            logger.info(
                "Pre-market eval [%s]: price data unavailable (opening grace "
                "period, %.1f min since open) — retrying next cycle, no strike",
                ticker, minutes_open,
            )
            return None
        # v21.11: while a provider feed is demonstrably frozen, a miss says
        # nothing about THIS candidate's coverage — so don't strike it toward
        # the "no_coverage" expiry. 2026-07-31: four of nine candidates (GTES,
        # MOG, IRMD ×2) expired as "no quote after 3 consecutive retries"
        # during a session-open outage in which BOTH providers served the
        # previous day's close for every symbol, SONY included. Those were
        # tradeable names discarded for a vendor problem.
        if quote_feed_degraded():
            logger.warning(
                "Pre-market eval [%s]: price data unavailable but a quote feed "
                "is currently frozen — retrying next cycle, no strike "
                "(provider outage, not missing coverage)",
                ticker,
            )
            return None
        # Track consecutive no-data cycles. After _NO_QUOTE_EXPIRE_AFTER strikes
        # the ticker has no Finnhub/Twelvedata coverage — expire it immediately
        # rather than burning API credits for the full eval window.
        # Token-bucket exhaustion also produces conf=None but resolves within
        # 1-2 cycles; the threshold absorbs those transient misses.
        strikes = _no_quote_strikes.get(cand_id, 0) + 1
        _no_quote_strikes[cand_id] = strikes
        if strikes >= _NO_QUOTE_EXPIRE_AFTER:
            _clear_strikes(cand_id)
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
            "Pre-market eval [%s]: still in opening block — retrying next cycle",
            ticker,
        )
        return None  # stays pending

    # ── Transient rejections: participation hasn't arrived YET ────────────────
    # RVOL and the momentum floor are measurements of the tape AT THIS MINUTE.
    # At the open they routinely fail for 1-5 minutes on genuine movers (volume
    # data lags, the first pullback dips the 5-min move negative) and then pass.
    # Observed 2026-07-07: AGIO gapped +11.1% on an FDA catalyst with +3.79%
    # follow-through and was terminally rejected at minute 5 on a lagged RVOL
    # of 0.40 — it never got a second look. These stay PENDING and re-evaluate
    # every cycle; the eval window in _live_candidates bounds the retries.
    if not conf.is_confirmed and conf.reason_code in _TRANSIENT_REJECT_CODES:
        logger.info(
            "Pre-market eval [%s]: %s (%s) — transient, retrying next cycle",
            ticker, conf.reason_code, conf.reason[:80],
        )
        return None  # stays pending

    # ── Terminal rejections: record the REAL reason immediately ──────────────
    # penny_stock and wide_spread fire before prev_close is computed, so
    # day_change_pct is None. The old flow fell through to the gap_pct=None
    # strike counter and (after 5 wasted eval cycles) recorded "prev close
    # unavailable" — observed 2026-07-07: PLUG was a $2.65 penny-stock reject
    # every single cycle but its row says "no previous close after 5 retries".
    # A rejection that isn't transient is final regardless of the gap.
    if not conf.is_confirmed:
        _clear_strikes(cand_id)
        update_premarket_candidate(cand_id, "rejected", f"{conf.reason_code}: {conf.reason}")
        return None

    # ── Gap gate: vs previous close, gap included ────────────────────────────
    gap_pct = conf.day_change_pct
    if gap_pct is None:
        # Finnhub returns pc=0 for some tickers; Twelvedata's daily bar can
        # take 1-2 minutes to roll at open. Both produce gap_pct=None. Bounded
        # by _GAP_PCT_EXPIRE_AFTER so the candidate doesn't retry silently for
        # the full 30-min window (observed 2026-06-30: 12 candidates expired
        # as "eval window closed" solely because of persistent gap_pct=None).
        strikes = _gap_pct_strikes.get(cand_id, 0) + 1
        _gap_pct_strikes[cand_id] = strikes
        if strikes >= _GAP_PCT_EXPIRE_AFTER:
            _clear_strikes(cand_id)
            update_premarket_candidate(
                cand_id, "expired",
                f"prev_close: no previous close after {_GAP_PCT_EXPIRE_AFTER} consecutive retries",
            )
            logger.info(
                "Pre-market eval [%s]: prev close unavailable after %d retries — expiring "
                "(Finnhub pc=0 and Twelvedata daily bar not yet available)",
                ticker, _GAP_PCT_EXPIRE_AFTER,
            )
            return None
        logger.info(
            "Pre-market eval [%s]: prev close unavailable (attempt %d/%d) — retrying next cycle",
            ticker, strikes, _GAP_PCT_EXPIRE_AFTER,
        )
        return None

    # Successful prev-close retrieval resets the strike counter.
    _gap_pct_strikes.pop(cand_id, None)

    if gap_pct < cfg.min_gap_pct:
        _clear_strikes(cand_id)
        update_premarket_candidate(
            cand_id, "rejected",
            f"gap {gap_pct:+.2f}% < {cfg.min_gap_pct}% — market doesn't believe the catalyst",
        )
        return None
    if gap_pct > cfg.max_gap_pct:
        _clear_strikes(cand_id)
        update_premarket_candidate(
            cand_id, "rejected",
            f"gap {gap_pct:+.2f}% > {cfg.max_gap_pct}% — move exhausted pre-open",
        )
        return None

    # Confirmation itself passed (all rejection shapes — opening block,
    # transient, terminal — returned above), and the gap is inside the band.
    _clear_strikes(cand_id)
    logger.info(
        "Pre-market candidate APPROVED: [%s] gap=%+.2f%% %s — %s",
        ticker, gap_pct, conf.reason, cand["headline"][:60],
    )
    return (cand, conf)


def evaluate_premarket_candidates() -> tuple[list[tuple[dict, PriceConfirmation]], list[dict]]:
    """
    At-open evaluation of the watchlist. Returns (approved, graduated):
      approved  — passed BOTH the gap gate and full price confirmation, paired
                  with their PriceConfirmation — main.py executes them through
                  its standard risk gates and buy path (this module never
                  places orders itself).
      graduated — still-PENDING candidates whose 30-min gap-and-go window just
                  closed (see _live_candidates) — main.py hands these to the
                  standard regular-hours re-evaluation queue instead of
                  discarding a catalyst that simply hasn't confirmed yet.

    Candidates are price-confirmed CONCURRENTLY (bounded thread pool + fast,
    no-retry quote path) under a hard wall-clock budget, so one slow or
    unpriceable ticker can never starve the window — the failure that produced
    the 2026-06-18 zero-trades day. Anything not resolved within the budget
    stays pending for the next cycle (~60s away, still inside the eval window).

    Every candidate's outcome is recorded on its row (status + eval_note) so the
    watchlist is fully auditable after the fact.
    """
    pending = get_pending_premarket_candidates()
    if not pending:
        return [], []

    # Budget guard: if Twelvedata credits are spent, every confirm_price_signal
    # would return None anyway (the bar calls short-circuit). Don't spin up the
    # thread pool or write any verdict — leave candidates pending so they're
    # re-evaluated for free next cycle (within their eval window) or expire via
    # _live_candidates. Trading is suspended for the day; news scoring continues.
    if credits_exhausted():
        logger.warning(
            "Pre-market eval skipped — Twelvedata credit budget exhausted; "
            "%d candidate(s) left pending (no trades until UTC midnight)",
            len(pending),
        )
        return [], []

    minutes_open = _minutes_since_open()
    live, graduated = _live_candidates(pending, minutes_open)
    if not live:
        return [], graduated

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
    # NOTE (v21.16): `catalyst_type` is deliberately NOT passed here, so the
    # momentum floor stays enforced on the gap-and-go path. The buy-at-signal
    # measurement that motivates cfg.skip_momentum_catalysts was made on
    # regular-hours news signals, where the catalyst has just published and the
    # tape has not reacted yet. A gap candidate is the opposite case by
    # construction — the move ALREADY happened overnight, which is what put it
    # on the watchlist — so "don't wait for the move" does not transfer, and
    # the gap band is not a substitute for it. Candidates that graduate into
    # the regular-hours re-eval queue do get the skip, because at that point
    # they are being evaluated as ordinary intraday signals.
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
        result = _apply_confirmation(cand, confs[cand["id"]], minutes_open)
        if result is not None:
            approved.append(result)

    return approved, graduated
