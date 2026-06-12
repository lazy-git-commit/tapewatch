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
from datetime import datetime

import pytz

from config.settings import cfg
from market.price_check import confirm_price_signal, PriceConfirmation
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

# Candidates are only evaluated during the first N minutes after the open.
# Past that, the gap-and-go edge is gone — late entries on morning news are
# exactly the "buying the top" failure v13 eliminated intraday.
_EVAL_WINDOW_MINUTES = 30

# Pre-market articles can be up to this old when scanned. The scanner runs
# every minute, so 5 min gives comfortable overlap without re-scoring stale
# news (dedup via premarket_candidates handles repeats anyway).
_SCAN_MAX_AGE_MINUTES = 5.0


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


def evaluate_premarket_candidates() -> list[tuple[dict, PriceConfirmation]]:
    """
    At-open evaluation of the watchlist. Returns the candidates that passed
    BOTH the gap gate and full price confirmation, paired with their
    PriceConfirmation — main.py executes them through its standard risk
    gates and buy path (this module never places orders itself).

    Every candidate's outcome is recorded on its row (status + eval_note)
    so the watchlist is fully auditable after the fact.
    """
    pending = get_pending_premarket_candidates()
    if not pending:
        return []

    minutes_open = _minutes_since_open()
    today_str = _now_et().strftime("%Y-%m-%d")
    approved: list[tuple[dict, PriceConfirmation]] = []

    for cand in pending:
        cand_id = cand["id"]
        ticker = cand["ticker"]

        # ── Expire stale candidates ──────────────────────────────────────────
        # (a) created on a previous day — the catalyst is old news now;
        # (b) evaluation window closed — gap-and-go is a first-30-min trade.
        created_day = str(cand.get("created_at", ""))[:10]
        if created_day != today_str:
            update_premarket_candidate(cand_id, "expired", f"stale: created {created_day}")
            continue
        if minutes_open > _EVAL_WINDOW_MINUTES:
            update_premarket_candidate(
                cand_id, "expired",
                f"eval window closed ({minutes_open:.0f} min after open)",
            )
            continue

        # ── Price confirmation (also yields the gap via day_change_pct) ─────
        try:
            conf = confirm_price_signal(ticker)
        except Exception as exc:
            logger.error("Pre-market eval: confirm_price_signal raised for %s: %s", ticker, exc)
            continue  # leave pending — retried next cycle inside the window

        if conf is None:
            # Data outage — leave pending and retry next cycle. The window
            # expiry above bounds how long we keep trying.
            logger.info("Pre-market eval [%s]: price data unavailable — retrying next cycle", ticker)
            continue

        # ── Gap gate: vs previous close, gap included ────────────────────────
        gap_pct = conf.day_change_pct
        if gap_pct is None:
            update_premarket_candidate(cand_id, "rejected", "no prev close — gap unknown")
            continue
        if gap_pct < cfg.min_gap_pct:
            update_premarket_candidate(
                cand_id, "rejected",
                f"gap {gap_pct:+.2f}% < {cfg.min_gap_pct}% — market doesn't believe the catalyst",
            )
            continue
        if gap_pct > cfg.max_gap_pct:
            update_premarket_candidate(
                cand_id, "rejected",
                f"gap {gap_pct:+.2f}% > {cfg.max_gap_pct}% — move exhausted pre-open",
            )
            continue

        # ── Standard confirmation: post-open follow-through required ────────
        if not conf.is_confirmed:
            update_premarket_candidate(
                cand_id, "rejected", f"{conf.reason_code}: {conf.reason}"
            )
            continue

        logger.info(
            "Pre-market candidate APPROVED: [%s] gap=%+.2f%% %s — %s",
            ticker, gap_pct, conf.reason, cand["headline"][:60],
        )
        approved.append((cand, conf))

    return approved
