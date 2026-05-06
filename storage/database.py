"""
storage/database.py
───────────────────
Initialises the SQLite database and provides typed helpers for:
  - logging news signals
  - recording trades (open + close)
  - portfolio snapshots
"""

import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from config.settings import cfg

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    """Context manager that yields a connection and commits on exit."""
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
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
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS news_signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker      TEXT    NOT NULL,
            headline    TEXT    NOT NULL,
            source      TEXT,
            sentiment   TEXT    NOT NULL,   -- BULLISH / BEARISH / NEUTRAL
            confidence  INTEGER NOT NULL,   -- 1–10
            acted_on    INTEGER NOT NULL DEFAULT 0,  -- 1 = triggered a trade
            created_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            mode            TEXT    NOT NULL DEFAULT 'demo',  -- demo / live
            ticker          TEXT    NOT NULL,
            signal_id       INTEGER REFERENCES news_signals(id),
            quantity        REAL    NOT NULL,
            buy_price       REAL    NOT NULL,
            sell_price      REAL,
            buy_time        TEXT    NOT NULL,
            sell_time       TEXT,
            exit_reason     TEXT,           -- take_profit / stop_loss / time_stop / manual
            profit_loss     REAL,           -- populated on close
            profit_loss_pct REAL,
            status          TEXT    NOT NULL DEFAULT 'open'  -- open / closed
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            total_value REAL    NOT NULL,
            cash        REAL,
            snapshot_at TEXT    NOT NULL
        );
        """)
    # Migration: add mode column to existing databases
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        if "mode" not in cols:
            conn.execute("ALTER TABLE trades ADD COLUMN mode TEXT NOT NULL DEFAULT 'demo'")
            logger.info("Migrated trades table: added mode column")

    logger.info("Database initialised at %s", cfg.db_path)


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
        cur = conn.execute(
            """INSERT INTO news_signals
               (ticker, headline, source, sentiment, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, headline, source, sentiment, confidence, datetime.utcnow().isoformat()),
        )
        return cur.lastrowid


def mark_signal_acted_on(signal_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE news_signals SET acted_on = 1 WHERE id = ?", (signal_id,)
        )


# ── Trades ────────────────────────────────────────────────────────────────────

def open_trade(
    ticker: str,
    signal_id: int,
    quantity: float,
    buy_price: float,
) -> int:
    """Record an opening buy. Returns the trade id."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (mode, ticker, signal_id, quantity, buy_price, buy_time, status)
               VALUES (?, ?, ?, ?, ?, ?, 'open')""",
            (cfg.trading_mode, ticker, signal_id, quantity, buy_price, datetime.utcnow().isoformat()),
        )
        logger.info("Trade opened: %s × %.4f @ £%.4f", ticker, quantity, buy_price)
        return cur.lastrowid


def close_trade(
    trade_id: int,
    sell_price: float,
    exit_reason: str,
) -> None:
    """Record the close of an open trade and calculate P&L."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT buy_price, quantity FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        if not row:
            logger.error("Trade %d not found", trade_id)
            return

        buy_price = row["buy_price"]
        quantity = row["quantity"]
        pnl = (sell_price - buy_price) * quantity
        pnl_pct = ((sell_price - buy_price) / buy_price) * 100

        conn.execute(
            """UPDATE trades SET
               sell_price = ?, sell_time = ?, exit_reason = ?,
               profit_loss = ?, profit_loss_pct = ?, status = 'closed'
               WHERE id = ?""",
            (sell_price, datetime.utcnow().isoformat(), exit_reason, pnl, pnl_pct, trade_id),
        )
        logger.info(
            "Trade %d closed: %s reason=%s P&L=£%.2f (%.2f%%)",
            trade_id, exit_reason, exit_reason, pnl, pnl_pct,
        )


def get_open_trades() -> list[dict]:
    """Return all currently open trades as dicts."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open'"
        ).fetchall()
        return [dict(r) for r in rows]


# ── Portfolio snapshots ───────────────────────────────────────────────────────

def save_snapshot(total_value: float, cash: Optional[float] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO portfolio_snapshots (total_value, cash, snapshot_at)
               VALUES (?, ?, ?)""",
            (total_value, cash, datetime.utcnow().isoformat()),
        )
