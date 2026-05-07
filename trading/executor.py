"""
trading/executor.py
────────────────────
Wraps the Trading 212 REST API directly to execute buy and sell orders.

Authentication: Basic base64(API_KEY_ID:API_KEY) per Trading 212 docs.

In DEMO mode:  calls demo.trading212.com (practice account).
In LIVE mode:  calls live.trading212.com (real ISA — use with care).

Position sizing: each trade uses at most cfg.max_position_size_pct of
the portfolio's total value, capped to available cash.
"""

import base64
import logging
import requests
from dataclasses import dataclass
from config.settings import cfg

logger = logging.getLogger(__name__)

_T212_DEMO = "https://demo.trading212.com/api/v0"
_T212_LIVE = "https://live.trading212.com/api/v0"


def _base_url() -> str:
    return _T212_LIVE if cfg.is_live else _T212_DEMO


def _auth_header() -> dict:
    if cfg.is_live:
        key_id, key = cfg.trading212_api_key_id, cfg.trading212_api_key
    else:
        key_id, key = cfg.trading212_demo_api_key_id, cfg.trading212_demo_api_key
    credentials = f"{key_id}:{key}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


def _get(path: str) -> dict:
    resp = requests.get(f"{_base_url()}{path}", headers=_auth_header(), timeout=10)
    if not resp.ok:
        raise Exception(f"HTTP {resp.status_code} - {resp.text}")
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = requests.post(
        f"{_base_url()}{path}",
        headers={**_auth_header(), "Content-Type": "application/json"},
        json=payload,
        timeout=10,
    )
    if not resp.ok:
        raise Exception(f"HTTP {resp.status_code} - {resp.text}")
    return resp.json()


@dataclass
class OrderResult:
    success: bool
    ticker: str
    quantity: float
    price: float
    order_id: str | None
    error: str | None


def get_portfolio_value() -> float | None:
    if not cfg.is_live:
        return cfg.demo_portfolio_value
    try:
        data = _get("/equity/account/cash")
        return float(data.get("total", 0))
    except Exception as exc:
        logger.error("Failed to fetch portfolio value: %s", exc)
        return None


def get_available_cash() -> float | None:
    if not cfg.is_live:
        return cfg.demo_portfolio_value
    try:
        data = _get("/equity/account/cash")
        return float(data.get("free", 0))
    except Exception as exc:
        logger.error("Failed to fetch cash balance: %s", exc)
        return None


def calculate_quantity(ticker: str, price: float) -> float | None:
    portfolio_value = get_portfolio_value()
    available_cash = get_available_cash()

    if portfolio_value is None or available_cash is None:
        return None

    max_spend = min(
        portfolio_value * (cfg.max_position_size_pct / 100),
        available_cash,
    )

    if max_spend <= 0:
        logger.warning("No funds available to buy %s", ticker)
        return None

    # Trading 212 supports fractional shares — round to 6 decimal places
    quantity = round(max_spend / price, 6)
    logger.info(
        "Position size for %s: £%.2f → %.6f shares @ £%.4f",
        ticker, max_spend, quantity, price,
    )
    return quantity


def buy(ticker: str, price: float) -> OrderResult:
    quantity = calculate_quantity(ticker, price)
    if quantity is None:
        return OrderResult(
            success=False, ticker=ticker, quantity=0,
            price=price, order_id=None, error="Could not calculate position size",
        )

    try:
        order = _post("/equity/orders/market", {"quantity": quantity, "ticker": ticker})
        order_id = str(order.get("id", ""))
        logger.info("BUY executed: %s × %.6f | order_id=%s", ticker, quantity, order_id)
        return OrderResult(
            success=True, ticker=ticker, quantity=quantity,
            price=price, order_id=order_id, error=None,
        )
    except Exception as exc:
        logger.error("BUY failed for %s: %s", ticker, exc)
        return OrderResult(
            success=False, ticker=ticker, quantity=quantity,
            price=price, order_id=None, error=str(exc),
        )


def sell(ticker: str, quantity: float, price: float, reason: str) -> OrderResult:
    try:
        order = _post("/equity/orders/market", {"quantity": -quantity, "ticker": ticker})
        order_id = str(order.get("id", ""))
        logger.info(
            "SELL executed: %s × %.6f | reason=%s | order_id=%s",
            ticker, quantity, reason, order_id,
        )
        return OrderResult(
            success=True, ticker=ticker, quantity=quantity,
            price=price, order_id=order_id, error=None,
        )
    except Exception as exc:
        logger.error("SELL failed for %s: %s", ticker, exc)
        return OrderResult(
            success=False, ticker=ticker, quantity=quantity,
            price=price, order_id=None, error=str(exc),
        )
