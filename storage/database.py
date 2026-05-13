"""
storage/database.py
───────────────────
Initialises the PostgreSQL database and provides typed helpers for:
  - logging news signals
  - recording trades (open + close)
  - portfolio snapshots
"""

import logging
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from config.settings import cfg

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    """Context manager that yields a connection and commits on exit."""
    conn = psycopg2.connect(cfg.db_url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
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
                id          SERIAL PRIMARY KEY,
                ticker      TEXT    NOT NULL,
                headline    TEXT    NOT NULL,
                source      TEXT,
                sentiment   TEXT    NOT NULL,
                confidence  INTEGER NOT NULL,
                acted_on    INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT    NOT NULL
            );

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
            );

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id          SERIAL PRIMARY KEY,
                total_value REAL    NOT NULL,
                cash        REAL,
                snapshot_at TEXT    NOT NULL
            );
            """)
            # Add new columns to existing tables without dropping data
            for col, definition in [
                ("buy_order_id",  "TEXT"),
                ("sell_order_id", "TEXT"),
                ("buy_net_gbp",   "REAL"),
                ("sell_net_gbp",  "REAL"),
                ("buy_fx_rate",   "REAL"),
                ("sell_fx_rate",  "REAL"),
                ("buy_fees_gbp",  "REAL"),
                ("sell_fees_gbp", "REAL"),
            ]:
                cur.execute(
                    f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {definition}"
                )
    logger.info("Database initialised at %s", cfg.db_url.split("@")[-1])


# ── News signals ──────────────────────────────────────────────────────────────

def save_signal(
    ticker: str,
    headline: str,
    source: str,
    sentiment: str,
    confidence: int,
) -> int:
    """Insert a news signal. Returns the new row id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO news_signals
                   (ticker, headline, source, sentiment, confidence, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   RETURNING id""",
                (ticker, headline, source, sentiment, confidence, datetime.utcnow().isoformat()),
            )
            return cur.fetchone()["id"]


def mark_signal_acted_on(signal_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE news_signals SET acted_on = 1 WHERE id = %s", (signal_id,)
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
                 datetime.utcnow().isoformat(),
                 buy_order_id, buy_net_gbp, buy_fx_rate, buy_fees_gbp),
            )
            row_id = cur.fetchone()["id"]
            logger.info("Trade opened: %s × %.4f @ £%.4f", ticker, quantity, buy_price)
            return row_id


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
                "SELECT buy_price, quantity FROM trades WHERE id = %s", (trade_id,)
            )
            row = cur.fetchone()
            if not row:
                logger.error("Trade %d not found", trade_id)
                return

            buy_price = row["buy_price"]
            quantity = row["quantity"]
            pnl = (sell_price - buy_price) * quantity
            pnl_pct = ((sell_price - buy_price) / buy_price) * 100

            cur.execute(
                """UPDATE trades SET
                   sell_price = %s, sell_time = %s, exit_reason = %s,
                   profit_loss = %s, profit_loss_pct = %s, status = 'closed',
                   sell_order_id = %s, sell_net_gbp = %s, sell_fx_rate = %s, sell_fees_gbp = %s
                   WHERE id = %s""",
                (sell_price, datetime.utcnow().isoformat(), exit_reason, pnl, pnl_pct,
                 sell_order_id, sell_net_gbp, sell_fx_rate, sell_fees_gbp, trade_id),
            )
            logger.info(
                "Trade %d closed: reason=%s P&L=£%.2f (%.2f%%)",
                trade_id, exit_reason, pnl, pnl_pct,
            )


def get_open_trades() -> list[dict]:
    """Return all currently open trades as dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM trades WHERE status = 'open'")
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
                        OR buy_time >= (NOW() AT TIME ZONE 'UTC' - make_interval(hours => %s))::TEXT)
                   LIMIT 1""",
                (ticker, cfg.trading_mode, hours),
            )
            return cur.fetchone() is not None


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(total_value: float, cash: Optional[float] = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO portfolio_snapshots (total_value, cash, snapshot_at)
                   VALUES (%s, %s, %s)""",
                (total_value, cash, datetime.utcnow().isoformat()),
            )
