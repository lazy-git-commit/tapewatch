"""
storage/database.py
───────────────────
Initialises the PostgreSQL database and provides typed helpers for:
  - logging news signals
  - recording trades (open + close)
  - portfolio snapshots

Connection retry policy:
  get_conn() retries up to 3 times with 1s back-off on operational errors
  (connection refused, server restart, transient TCP drop). It does NOT
  retry programmer errors (bad SQL, type mismatches).
"""

import logging
import time
import psycopg2
import psycopg2.extras
import psycopg2.errors
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional
import pytz
from config.settings import cfg

_LONDON = pytz.timezone("Europe/London")
_DB_RETRIES = 3
_DB_RETRY_DELAY = 1.0  # seconds


def _now_london() -> str:
    """Current time as an ISO string in London local time (handles BST/GMT automatically)."""
    return datetime.now(_LONDON).isoformat()

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    """
    Context manager that yields a psycopg2 connection, commits on clean exit,
    and rolls back + closes on exception.

    Retries up to _DB_RETRIES times on OperationalError (transient TCP/server
    issues). Raises immediately on all other errors.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _DB_RETRIES + 1):
        try:
            conn = psycopg2.connect(cfg.db_url)
            conn.cursor_factory = psycopg2.extras.RealDictCursor
            break
        except psycopg2.OperationalError as exc:
            last_exc = exc
            if attempt < _DB_RETRIES:
                logger.warning(
                    "DB connection failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, _DB_RETRIES, exc, _DB_RETRY_DELAY,
                )
                time.sleep(_DB_RETRY_DELAY * attempt)
            else:
                logger.error(
                    "DB connection failed after %d attempts: %s", _DB_RETRIES, exc
                )
                raise
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create all tables if they don't exist yet."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS news_signals (
                    id               SERIAL PRIMARY KEY,
                    article_id       TEXT,
                    ticker           TEXT    NOT NULL,
                    headline         TEXT    NOT NULL,
                    source           TEXT,
                    sentiment        TEXT    NOT NULL,
                    confidence       INTEGER NOT NULL,
                    acted_on         INTEGER NOT NULL DEFAULT 0,
                    rejection_reason TEXT,
                    published_at     TEXT,
                    fetched_at       TEXT    NOT NULL,
                    created_at       TEXT    NOT NULL
                )
            """)
            cur.execute("""
                ALTER TABLE news_signals
                ADD COLUMN IF NOT EXISTS rejection_reason TEXT
            """)
            cur.execute("""
                ALTER TABLE news_signals
                ADD COLUMN IF NOT EXISTS rejection_code TEXT
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id              SERIAL PRIMARY KEY,
                    mode            TEXT    NOT NULL DEFAULT 'demo',
                    ticker          TEXT    NOT NULL,
                    signal_id       INTEGER REFERENCES news_signals(id),
                    quantity        REAL    NOT NULL,
                    buy_price       REAL    NOT NULL,
                    sell_price      REAL,
                    buy_time        TEXT    NOT NULL,
                    sell_time       TEXT,
                    exit_reason     TEXT,
                    profit_loss     REAL,
                    profit_loss_pct REAL,
                    status          TEXT    NOT NULL DEFAULT 'open',
                    buy_order_id    TEXT,
                    sell_order_id   TEXT,
                    buy_net_gbp     REAL,
                    sell_net_gbp    REAL,
                    buy_fx_rate     REAL,
                    sell_fx_rate    REAL,
                    buy_fees_gbp    REAL,
                    sell_fees_gbp   REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id          SERIAL PRIMARY KEY,
                    total_value REAL    NOT NULL,
                    cash        REAL,
                    snapshot_at TEXT    NOT NULL
                )
            """)
            # ── v14 migrations ────────────────────────────────────────────────
            # tp_order_id: the resting take-profit LIMIT order placed right
            # after the buy fills. The monitor checks its status each cycle;
            # it must be cancelled before any stop-loss/time-stop sell.
            cur.execute("""
                ALTER TABLE trades
                ADD COLUMN IF NOT EXISTS tp_order_id TEXT
            """)
            # catalyst_type on signals: which catalyst class Claude assigned.
            cur.execute("""
                ALTER TABLE news_signals
                ADD COLUMN IF NOT EXISTS catalyst_type TEXT
            """)
            # catalyst_magnitude: 1–5 relative-to-market-cap impact score.
            cur.execute("""
                ALTER TABLE news_signals
                ADD COLUMN IF NOT EXISTS catalyst_magnitude INTEGER
            """)
            cur.execute("""
                ALTER TABLE sentiment_scores
                ADD COLUMN IF NOT EXISTS catalyst_magnitude INTEGER
            """)
            cur.execute("""
                ALTER TABLE premarket_candidates
                ADD COLUMN IF NOT EXISTS catalyst_magnitude INTEGER
            """)
            # Eval-loop table: EVERY Claude classification (positive, neutral,
            # negative) is stored here; a nightly job fills in forward returns
            # so prompt changes can be measured, not guessed.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sentiment_scores (
                    id             SERIAL PRIMARY KEY,
                    article_id     TEXT,
                    ticker         TEXT    NOT NULL,
                    headline       TEXT,
                    sentiment      TEXT    NOT NULL,
                    confidence     REAL    NOT NULL,
                    catalyst_type  TEXT,
                    already_moved  INTEGER NOT NULL DEFAULT 0,
                    published_at   TEXT,
                    scored_at      TEXT    NOT NULL,
                    fwd_return_5m  REAL,
                    fwd_return_15m REAL,
                    fwd_return_60m REAL,
                    returns_computed_at TEXT
                )
            """)
            # Pre-market watchlist: news scored before the open, evaluated at
            # open + open_block with gap/momentum confirmation. status:
            # pending → traded | rejected | expired.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS premarket_candidates (
                    id            SERIAL PRIMARY KEY,
                    article_id    TEXT,
                    ticker        TEXT    NOT NULL,
                    headline      TEXT,
                    catalyst_type TEXT,
                    confidence    REAL,
                    published_at  TEXT,
                    created_at    TEXT    NOT NULL,
                    status        TEXT    NOT NULL DEFAULT 'pending',
                    eval_note     TEXT
                )
            """)
            # Liveness heartbeat: one row per job, updated every cycle.
            # Grafana alerts when last_beat_at goes stale (see docs/algorithm.md).
            cur.execute("""
                CREATE TABLE IF NOT EXISTS heartbeat (
                    job          TEXT PRIMARY KEY,
                    last_beat_at TEXT NOT NULL
                )
            """)
            # System events: degradation/outage markers the system would
            # otherwise fail SILENTLY on (the 2026-06-23 root cause — 9 sessions
            # of zero trades with green heartbeats). One row per occurrence:
            #   twelvedata_credits_exhausted | claude_outage |
            #   claude_billing_error | claude_auth_error | zero_trade_session
            # Grafana/alerting reads this so a data-pipeline collapse is visible
            # instead of looking like a quiet, healthy market day. Severity is
            # 'warning' | 'critical' so an alert rule can filter.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_events (
                    id          SERIAL PRIMARY KEY,
                    event_type  TEXT NOT NULL,
                    severity    TEXT NOT NULL DEFAULT 'warning',
                    detail      TEXT,
                    created_at  TEXT NOT NULL,
                    event_day   TEXT NOT NULL
                )
            """)
            # Atomic one-row-per-(type, day) dedup: a UNIQUE index + ON CONFLICT
            # in record_system_event closes the SELECT-then-INSERT race where two
            # threads (8-worker premarket pool, or startup racing the cron) both
            # see no row and both insert. event_day is the London calendar date.
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_system_events_type_day
                ON system_events (event_type, event_day)
            """)
    logger.info("Database initialised at %s", cfg.db_url.split("@")[-1])


# ── News signals ──────────────────────────────────────────────────────────────

def is_article_seen(article_id: str, ticker: str) -> bool:
    """Return True if this (article, ticker) pair has already been processed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM news_signals WHERE article_id = %s AND ticker = %s LIMIT 1",
                (article_id, ticker),
            )
            return cur.fetchone() is not None


def save_signal(
    ticker: str,
    headline: str,
    source: str,
    sentiment: str,
    confidence: int,
    article_id: str | None = None,
    published_at: str | None = None,
    fetched_at: str | None = None,
    catalyst_type: str | None = None,
    catalyst_magnitude: int | None = None,
) -> int:
    """Insert a news signal. Returns the new row id."""
    now = _now_london()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO news_signals
                   (article_id, ticker, headline, source, sentiment, confidence,
                    published_at, fetched_at, created_at, catalyst_type,
                    catalyst_magnitude)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (article_id, ticker, headline, source, sentiment, confidence,
                 published_at, fetched_at or now, now, catalyst_type,
                 catalyst_magnitude),
            )
            return cur.fetchone()["id"]


def mark_signal_acted_on(signal_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE news_signals SET acted_on = 1 WHERE id = %s", (signal_id,)
            )


def set_rejection_reason(signal_id: int, reason: str, code: str | None = None) -> None:
    """Store the rejection reason and optional short code for a signal that was not traded."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE news_signals SET rejection_reason = %s, rejection_code = %s WHERE id = %s",
                (reason, code, signal_id),
            )


# ── Trades ────────────────────────────────────────────────────────────────────

def open_trade(
    ticker: str,
    signal_id: int,
    quantity: float,
    buy_price: float,
    buy_order_id: str | None = None,
    buy_net_gbp: float | None = None,
    buy_fx_rate: float | None = None,
    buy_fees_gbp: float | None = None,
) -> int:
    """Record an opening buy. Returns the trade id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trades
                   (mode, ticker, signal_id, quantity, buy_price, buy_time, status,
                    buy_order_id, buy_net_gbp, buy_fx_rate, buy_fees_gbp)
                   VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s)
                   RETURNING id""",
                (cfg.trading_mode, ticker, signal_id, quantity, buy_price,
                 _now_london(),
                 buy_order_id, buy_net_gbp, buy_fx_rate, buy_fees_gbp),
            )
            row_id = cur.fetchone()["id"]
            # buy_price is in USD (T212 quotes US equities in USD); GBP cash
            # impact is tracked separately in buy_net_gbp.
            logger.info("Trade opened: %s × %.4f @ $%.4f", ticker, quantity, buy_price)
            return row_id


def _pnl_from_cashflows(
    buy_net_gbp: float,
    sell_net_gbp: float,
) -> tuple[float, float]:
    """
    Return (pnl_gbp, pnl_pct) from broker cash-flow values.

    Trading APIs differ on sign convention: some report a buy as a positive
    cost, others as a negative wallet impact. For a long-only close, the only
    invariant we can rely on is economic direction: buy = cost, sell =
    proceeds. Using absolute values keeps the daily kill switch correct under
    either convention while preserving the raw broker values in the DB.
    """
    cost = abs(float(buy_net_gbp))
    proceeds = abs(float(sell_net_gbp))
    pnl = proceeds - cost
    pnl_pct = (pnl / cost) * 100 if cost > 0 else 0.0
    return pnl, pnl_pct


def close_trade(
    trade_id: int,
    sell_price: float,
    exit_reason: str,
    sell_order_id: str | None = None,
    sell_net_gbp: float | None = None,
    sell_fx_rate: float | None = None,
    sell_fees_gbp: float | None = None,
) -> None:
    """Record the close of an open trade and calculate P&L."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT buy_price, quantity, buy_net_gbp FROM trades WHERE id = %s", (trade_id,)
            )
            row = cur.fetchone()
            if not row:
                logger.error("Trade %d not found", trade_id)
                return

            buy_price = row["buy_price"]
            quantity = row["quantity"]
            buy_net_gbp_stored = row["buy_net_gbp"]

            # Use real GBP cash flows when available; fall back to USD price diff.
            # Cash-flow signs are normalized in _pnl_from_cashflows(); this is
            # critical for the daily kill switch because broker APIs often
            # report buys as negative wallet impacts and sells as positive ones.
            if sell_net_gbp is not None and buy_net_gbp_stored is not None:
                pnl, pnl_pct = _pnl_from_cashflows(buy_net_gbp_stored, sell_net_gbp)
            else:
                pnl = (sell_price - buy_price) * quantity
                pnl_pct = ((sell_price - buy_price) / buy_price) * 100

            cur.execute(
                """UPDATE trades SET
                   sell_price = %s, sell_time = %s, exit_reason = %s,
                   profit_loss = %s, profit_loss_pct = %s, status = 'closed',
                   sell_order_id = %s, sell_net_gbp = %s, sell_fx_rate = %s, sell_fees_gbp = %s
                   WHERE id = %s""",
                (sell_price, _now_london(), exit_reason, pnl, pnl_pct,
                 sell_order_id, sell_net_gbp, sell_fx_rate, sell_fees_gbp, trade_id),
            )
            logger.info(
                "Trade %d closed: reason=%s P&L=£%.2f (%.2f%%)",
                trade_id, exit_reason, pnl, pnl_pct,
            )


def get_open_trades() -> list[dict]:
    """Return currently open trades for the active trading mode only."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM trades WHERE status = 'open' AND mode = %s",
                (cfg.trading_mode,),
            )
            return [dict(r) for r in cur.fetchall()]


def was_recently_traded(ticker: str, hours: int = 24) -> bool:
    """Return True if this ticker has an open trade or was bought within the last `hours`."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM trades
                   WHERE ticker = %s
                   AND mode = %s
                   AND (status = 'open'
                        OR buy_time::timestamptz >= (NOW() - make_interval(hours => %s)))
                   LIMIT 1""",
                (ticker, cfg.trading_mode, hours),
            )
            return cur.fetchone() is not None


# ── Risk-control queries (v14) ────────────────────────────────────────────────
# All three back the portfolio-level gates in main.py: max open positions,
# max trades per day, and the daily loss kill switch.

def count_open_trades() -> int:
    """Number of currently open positions in the active trading mode."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status = 'open' AND mode = %s",
                (cfg.trading_mode,),
            )
            return int(cur.fetchone()["n"])


def count_trades_today() -> int:
    """Number of positions opened today (London calendar day, matching buy_time storage)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) AS n FROM trades
                   WHERE mode = %s
                     AND (buy_time::timestamptz AT TIME ZONE 'Europe/London')::date =
                         (NOW() AT TIME ZONE 'Europe/London')::date""",
                (cfg.trading_mode,),
            )
            return int(cur.fetchone()["n"])


_NYSE_CALENDAR = None  # lazily built once; mcal.get_calendar loads the full holiday set


def _nyse_calendar():
    """Module-cached NYSE calendar (mcal.get_calendar is expensive; build once)."""
    global _NYSE_CALENDAR
    if _NYSE_CALENDAR is None:
        import pandas_market_calendars as mcal
        _NYSE_CALENDAR = mcal.get_calendar("NYSE")
    return _NYSE_CALENDAR


def trading_days_since_last_trade() -> int | None:
    """
    Count COMPLETED NYSE sessions strictly after the most recent trade's entry.

    Backs the zero-trade-drought alert: a long stretch with signals flowing but
    no NEW entries is the silent-failure signature (2026-06-23: 9 sessions, last
    trade 2026-06-10). Returns 0 when the last entry was today or no session has
    closed since it, a positive count for an idle streak, and None when there are
    no trades at all (fresh DB — nothing to compare against).

    NOTE: this counts from the last ENTRY (buy_time), so a position bought N
    sessions ago and still held open is reported as an N-session entry-drought —
    that is intentional (the alert is about entries drying up, not about whether
    any position is held).

    Off-by-one safety (the reason this counts CLOSED sessions, not calendar
    days): the startup call can run at any hour, including between London midnight
    and the NYSE close, when the London calendar date is already "tomorrow" while
    today's US session has not finished. Bounding by sessions whose market_close
    is in the past avoids counting a not-yet-finished session as elapsed.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT MAX((buy_time::timestamptz AT TIME ZONE 'Europe/London')::date) AS d
                   FROM trades WHERE mode = %s AND buy_time IS NOT NULL""",
                (cfg.trading_mode,),
            )
            row = cur.fetchone()
    last = row["d"] if row else None
    if last is None:
        return None
    try:
        import pandas as pd
        now_utc = pd.Timestamp.now(tz="UTC")
        # Look from the day after the last entry through ~2 weeks ahead (covers any
        # realistic drought + holidays); end-bound generously, then filter by close.
        start = (last + timedelta(days=1)).isoformat()
        end = (now_utc.date() + timedelta(days=1)).isoformat()
        sched = _nyse_calendar().schedule(start_date=start, end_date=end)
        if sched.empty:
            return 0
        # Count only sessions whose close has already passed — a session still in
        # progress (or yet to open) is not an "elapsed" idle session.
        closed = sched[sched["market_close"] <= now_utc]
        return int(len(closed))
    except Exception as exc:
        logger.debug("trading_days_since_last_trade: calendar calc failed: %s", exc)
        return None


def get_today_realized_pnl() -> float:
    """
    Sum of realized P&L (GBP) for trades CLOSED today. Backs the daily kill
    switch: once this drops below −(max_daily_loss_pct% of portfolio), no new
    positions are opened until tomorrow. Realized-only by design — unrealized
    swings on open positions shouldn't toggle the switch on and off.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT COALESCE(SUM(profit_loss), 0) AS pnl FROM trades
                   WHERE mode = %s
                     AND status = 'closed'
                     AND sell_time IS NOT NULL
                     AND (sell_time::timestamptz AT TIME ZONE 'Europe/London')::date =
                         (NOW() AT TIME ZONE 'Europe/London')::date""",
                (cfg.trading_mode,),
            )
            return float(cur.fetchone()["pnl"])


def set_tp_order_id(trade_id: int, tp_order_id: str | None) -> None:
    """Attach (or clear) the resting take-profit limit order id on a trade."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET tp_order_id = %s WHERE id = %s",
                (tp_order_id, trade_id),
            )


# ── Sentiment eval loop (v14) ─────────────────────────────────────────────────

def save_sentiment_scores(rows: list[dict]) -> None:
    """
    Persist a batch of Claude classifications (one row per article+ticker).
    Called for EVERY scored article regardless of sentiment — this is the
    dataset the nightly forward-returns job and prompt evaluation run on.
    """
    if not rows:
        return
    now = _now_london()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO sentiment_scores
                   (article_id, ticker, headline, sentiment, confidence,
                    catalyst_type, already_moved, catalyst_magnitude,
                    published_at, scored_at)
                   VALUES (%(article_id)s, %(ticker)s, %(headline)s, %(sentiment)s,
                           %(confidence)s, %(catalyst_type)s, %(already_moved)s,
                           %(catalyst_magnitude)s, %(published_at)s, %(scored_at)s)""",
                [{**r, "already_moved": int(r.get("already_moved", False)),
                  "catalyst_magnitude": r.get("catalyst_magnitude"),
                  "scored_at": now}
                 for r in rows],
            )


def get_scores_missing_returns(limit: int = 500) -> list[dict]:
    """Scored articles that don't yet have forward returns computed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM sentiment_scores
                   WHERE returns_computed_at IS NULL
                   ORDER BY id ASC LIMIT %s""",
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]


# Deploy date of the forward-return open-anchoring fix. Rows computed BEFORE
# this date by the old publish-time anchoring recorded exact-zero returns for
# every out-of-session article (both window endpoints resolved to the same
# first RTH bar). returns_computed_at is ISO text, so comparing against this
# date prefix is lexicographically correct.
_FWD_RETURN_FIX_DEPLOYED = "2026-07-03"


def reset_contaminated_forward_returns(cutoff: str = _FWD_RETURN_FIX_DEPLOYED) -> int:
    """
    One-time repair for the pre-fix anchoring bug: null out returns for rows
    whose 5/15/60-min forward returns are ALL exactly 0.0 and were computed
    before the fix deployed, so the nightly job recomputes them with the
    corrected open-anchoring. Genuine all-zero rows are vanishingly rare (a
    stock printing identical closes across all three windows) and recomputing
    them is harmless. Self-limiting: recomputed rows carry a fresh
    returns_computed_at past the cutoff and never match again; rows already
    older than yfinance's ~30-day 1-min window resolve to NULL and are marked
    computed, ending their participation.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sentiment_scores
                   SET fwd_return_5m = NULL, fwd_return_15m = NULL,
                       fwd_return_60m = NULL, returns_computed_at = NULL
                   WHERE returns_computed_at IS NOT NULL
                     AND returns_computed_at < %s
                     AND fwd_return_5m = 0
                     AND fwd_return_15m = 0
                     AND fwd_return_60m = 0""",
                (cutoff,),
            )
            return cur.rowcount


def update_forward_returns(score_id: int, r5: float | None, r15: float | None, r60: float | None) -> None:
    """Fill in the 5/15/60-min forward returns for one scored article."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE sentiment_scores
                   SET fwd_return_5m = %s, fwd_return_15m = %s, fwd_return_60m = %s,
                       returns_computed_at = %s
                   WHERE id = %s""",
                (r5, r15, r60, _now_london(), score_id),
            )


# ── Pre-market candidates (v14) ───────────────────────────────────────────────

def is_premarket_candidate_seen(article_id: str, ticker: str) -> bool:
    """Dedup predicate for the pre-market scanner (mirrors is_article_seen)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM premarket_candidates WHERE article_id = %s AND ticker = %s LIMIT 1",
                (article_id, ticker),
            )
            return cur.fetchone() is not None


def save_premarket_candidate(
    article_id: str, ticker: str, headline: str,
    catalyst_type: str, confidence: float, published_at: str,
    catalyst_magnitude: int | None = None,
) -> int:
    """Add a scored pre-market article to the at-open watchlist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO premarket_candidates
                   (article_id, ticker, headline, catalyst_type, confidence,
                    published_at, created_at, status, catalyst_magnitude)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                   RETURNING id""",
                (article_id, ticker, headline, catalyst_type, confidence,
                 published_at, _now_london(), catalyst_magnitude),
            )
            return cur.fetchone()["id"]


def get_pending_premarket_candidates() -> list[dict]:
    """All watchlist entries awaiting at-open evaluation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM premarket_candidates WHERE status = 'pending' ORDER BY confidence DESC"
            )
            return [dict(r) for r in cur.fetchall()]


def update_premarket_candidate(candidate_id: int, status: str, eval_note: str | None = None) -> None:
    """Mark a candidate traded / rejected / expired after at-open evaluation."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE premarket_candidates SET status = %s, eval_note = %s WHERE id = %s",
                (status, eval_note, candidate_id),
            )


# ── Heartbeat (v14) ───────────────────────────────────────────────────────────

def touch_heartbeat(job: str) -> None:
    """
    Record liveness for a scheduler job. The 2026-06-11 outage (18h crash
    loop) went unnoticed because nothing watched the process — Grafana now
    alerts when this timestamp goes stale (query in docs/algorithm.md).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO heartbeat (job, last_beat_at) VALUES (%s, %s)
                   ON CONFLICT (job) DO UPDATE SET last_beat_at = EXCLUDED.last_beat_at""",
                (job, _now_london()),
            )


# ── System events (v17: outage/degradation observability) ──────────────────────

# Event types that mean the system CANNOT trade even though it's "up". These are
# the silent-failure class — recording them is what turns a 9-session zero-trade
# drought into something an alert fires on. Kept here so callers use stable names.
_CRITICAL_EVENT_TYPES = {
    "twelvedata_credits_exhausted",
    "claude_billing_error",
    "claude_auth_error",
    "zero_trade_session",
}


def record_system_event(event_type: str, detail: str = "") -> None:
    """
    Append a system event (outage / degradation marker). Best-effort: any failure
    is swallowed so observability can never break the trading/news path.

    Severity is derived from the event type — billing/auth/credit-exhaustion are
    'critical' (no self-heal, needs a human), everything else 'warning'.
    De-dupes within the same London day: at most one row per (event_type, day),
    enforced ATOMICALLY by a UNIQUE(event_type, event_day) index + ON CONFLICT
    DO NOTHING, so concurrent callers (8-worker premarket pool, or startup racing
    the cron) can't both insert.
    """
    severity = "critical" if event_type in _CRITICAL_EVENT_TYPES else "warning"
    try:
        now = _now_london()
        event_day = now[:10]  # 'YYYY-MM-DD' prefix of the London ISO ts
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO system_events
                           (event_type, severity, detail, created_at, event_day)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (event_type, event_day) DO NOTHING""",
                    (event_type, severity, detail[:500], now, event_day),
                )
                inserted = cur.rowcount > 0
        if inserted:
            logger.info("system_event recorded: [%s/%s] %s", severity, event_type, detail[:120])
    except Exception as exc:
        logger.debug("record_system_event(%s) failed: %s", event_type, exc)


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(total_value: float, cash: Optional[float] = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO portfolio_snapshots (total_value, cash, snapshot_at)
                   VALUES (%s, %s, %s)""",
                (total_value, cash, _now_london()),
            )

