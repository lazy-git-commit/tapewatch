"""
trading/executor.py
────────────────────
Wraps trading212-connector to execute buy and sell orders.

In DEMO mode:  calls the Trading 212 practice account API. Position sizing
               uses DEMO_PORTFOLIO_VALUE from .env instead of the API balance.
In LIVE mode:  calls your real ISA account — use with care.

Position sizing: each trade uses at most cfg.max_position_size_pct of
the portfolio's total value, capped to available cash.
"""

import logging
from dataclasses import dataclass
from trading212 import Client
from config.settings import cfg

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    ticker: str
    quantity: float
    price: float
    order_id: str | None
    error: str | None


def _get_client() -> Client:
    """Return a configured Trading 212 client in the correct mode."""
    domain = "live.trading212.com" if cfg.is_live else "demo.trading212.com"
    return Client(cfg.trading212_api_key, domain=domain)


def get_portfolio_value() -> float | None:
    """
    Return the total account value.
    In demo mode uses DEMO_PORTFOLIO_VALUE from config to avoid needing
    a funded practice account.
    """
    if not cfg.is_live:
        return cfg.demo_portfolio_value

    try:
        client = _get_client()
        account = client.get_account_cash()
        return float(account.get("total", 0))
    except Exception as exc:
        logger.error("Failed to fetch portfolio value: %s", exc)
        return None


def get_available_cash() -> float | None:
    """
    Return available cash.
    In demo mode uses DEMO_PORTFOLIO_VALUE as the available balance.
    """
    if not cfg.is_live:
        return cfg.demo_portfolio_value

    try:
        client = _get_client()
        account = client.get_account_cash()
        return float(account.get("free", 0))
    except Exception as exc:
        logger.error("Failed to fetch cash balance: %s", exc)
        return None


def calculate_quantity(ticker: str, price: float) -> float | None:
    """
    Work out how many shares to buy based on position size rules.

    Uses the smaller of:
      - cfg.max_position_size_pct % of total portfolio value
      - available cash
    """
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
    """Place a market buy order for the calculated position size."""
    quantity = calculate_quantity(ticker, price)
    if quantity is None:
        return OrderResult(
            success=False, ticker=ticker, quantity=0,
            price=price, order_id=None, error="Could not calculate position size",
        )

    try:
        client = _get_client()
        order = client.place_market_order(quantity=quantity, ticker=ticker)
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
    """Place a market sell order for an open position."""
    try:
        client = _get_client()
        order = client.place_market_order(quantity=-quantity, ticker=ticker)  # negative = sell
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
