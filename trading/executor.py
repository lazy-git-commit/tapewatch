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
import time
import requests
from dataclasses import dataclass, field
from config.settings import cfg

logger = logging.getLogger(__name__)

_T212_DEMO = "https://demo.trading212.com/api/v0"
_T212_LIVE = "https://live.trading212.com/api/v0"

# symbol → T212 ticker code, e.g. "SUNE" → "JCS_US_EQ"
# T212 keeps original IPO/SPAC codes even after a company changes its exchange ticker,
# so "SUNE_US_EQ" 404s while the instrument lives as "JCS_US_EQ".
# Built once at startup from the instruments metadata endpoint.
_symbol_to_t212: dict[str, str] = {}


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


def build_symbol_map() -> None:
    """
    Fetch T212's full instrument catalogue and build a shortName → ticker map.

    T212 keeps the original SPAC/IPO ticker code even after a company changes
    its exchange symbol (e.g. SUNE → JCS_US_EQ after a reverse merger).
    Without this map, ~16% of small-cap tickers 404 when we append _US_EQ.

    Called once at startup from main.py. Safe to call again to refresh.
    """
    global _symbol_to_t212
    try:
        resp = requests.get(
            f"{_base_url()}/equity/metadata/instruments",
            headers=_auth_header(),
            timeout=15,
        )
        resp.raise_for_status()
        instruments = resp.json()
        mapping = {}
        for inst in instruments:
            if not isinstance(inst, dict):
                continue
            ticker = inst.get("ticker", "")
            short = inst.get("shortName", "")
            if ticker and short and inst.get("currencyCode") == "USD":
                mapping[short.upper()] = ticker
        _symbol_to_t212 = mapping
        logger.info("T212 symbol map built: %d USD instruments", len(mapping))
    except Exception as exc:
        logger.warning("Could not build T212 symbol map: %s — will use shortName_US_EQ fallback", exc)


def resolve_t212_ticker(exchange_symbol: str) -> str:
    """
    Convert a Benzinga exchange symbol (e.g. "SUNE") to the correct T212 ticker code.
    Uses the pre-built symbol map; falls back to "<symbol>_US_EQ" if not found.
    """
    return _symbol_to_t212.get(exchange_symbol.upper(), f"{exchange_symbol}_US_EQ")


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
    net_gbp: float | None = None
    fx_rate: float | None = None
    fees_gbp: float | None = None


def _fetch_fill(order_id: str) -> dict | None:
    """Poll history/orders until the given order appears with a fill. Returns the fill dict or None."""
    for _ in range(6):
        time.sleep(2)
        try:
            data = _get("/equity/history/orders?limit=20")
            for item in data.get("items", []):
                if str(item.get("order", {}).get("id")) == order_id and "fill" in item:
                    return item["fill"]
        except Exception as exc:
            logger.warning("Failed to fetch fill for order %s: %s", order_id, exc)
    return None


def _parse_fill(fill: dict) -> tuple[float | None, float | None, float | None, float | None]:
    """Extract (filled_price, net_gbp, fx_rate, fees_gbp) from a Trading 212 fill dict."""
    if not fill:
        return None, None, None, None
    filled_price = fill.get("price")
    impact = fill.get("walletImpact", {})
    net_gbp = impact.get("netValue")
    fx_rate = impact.get("fxRate")
    fees_gbp = sum(abs(t.get("quantity", 0)) for t in impact.get("taxes", []))
    return filled_price, net_gbp, fx_rate, fees_gbp


def _fetch_cash() -> dict | None:
    """Fetch /equity/account/cash once and return the raw dict, or None on error."""
    try:
        return _get("/equity/account/cash")
    except Exception as exc:
        logger.error("Failed to fetch T212 cash balance: %s", exc)
        return None


def get_portfolio_value() -> float | None:
    data = _fetch_cash()
    return float(data["total"]) if data else None


def get_available_cash() -> float | None:
    data = _fetch_cash()
    return float(data["free"]) if data else None


def calculate_quantity(ticker: str, price: float) -> tuple[float | None, str | None]:
    """Returns (quantity, error_reason). error_reason is None on success."""
    try:
        data = _get("/equity/account/cash")
    except Exception as exc:
        reason = f"T212 cash API failed: {exc}"
        logger.error("calculate_quantity for %s: %s", ticker, reason)
        return None, reason

    portfolio_value = float(data.get("total", 0))
    available_cash = float(data.get("free", 0))

    if portfolio_value <= 0 or available_cash <= 0:
        reason = f"no funds available (total=£{portfolio_value:.2f} free=£{available_cash:.2f})"
        logger.warning("calculate_quantity for %s: %s", ticker, reason)
        return None, reason

    max_spend = min(
        portfolio_value * (cfg.max_position_size_pct / 100),
        available_cash,
    )

    # Trading 212 allows at most 4 decimal places for fractional quantities
    quantity = round(max_spend / price, 4)
    logger.info(
        "Position size for %s: £%.2f → %.4f shares @ £%.4f",
        ticker, max_spend, quantity, price,
    )
    return quantity, None


def buy(ticker: str, price: float) -> OrderResult:
    quantity, err = calculate_quantity(ticker, price)
    if quantity is None:
        return OrderResult(
            success=False, ticker=ticker, quantity=0,
            price=price, order_id=None, error=err or "Could not calculate position size",
        )

    try:
        order = _post("/equity/orders/market", {"quantity": quantity, "ticker": ticker})
    except Exception as exc:
        exc_str = str(exc)
        # T212 rejects orders when our quantity has more decimal places than the
        # instrument allows. The error says "invalid quantity precision N" where N
        # is the maximum allowed. Retry once with the correct rounding.
        if "quantity-precision-mismatch" in exc_str:
            import json as _json
            try:
                body = exc_str.split(" - ", 1)[1]
                allowed = int(_json.loads(body)["detail"].split()[-1])
                quantity = round(quantity, allowed)
                logger.info(
                    "Retrying BUY %s with precision=%d → quantity=%s",
                    ticker, allowed, quantity,
                )
                order = _post("/equity/orders/market", {"quantity": quantity, "ticker": ticker})
            except Exception as retry_exc:
                logger.error("BUY failed for %s after precision retry: %s", ticker, retry_exc)
                return OrderResult(
                    success=False, ticker=ticker, quantity=quantity,
                    price=price, order_id=None, error=str(retry_exc),
                )
        else:
            logger.error("BUY failed for %s: %s", ticker, exc)
            return OrderResult(
                success=False, ticker=ticker, quantity=quantity,
                price=price, order_id=None, error=exc_str,
            )

    try:
        order_id = str(order.get("id", ""))
        fill = _fetch_fill(order_id)
        filled_price, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
        actual_price = filled_price if filled_price is not None else price
        logger.info(
            "BUY executed: %s × %.4f @ £%.4f | net=£%.2f fx=%.4f fees=£%.2f | order_id=%s",
            ticker, quantity, actual_price,
            net_gbp or 0, fx_rate or 0, fees_gbp or 0, order_id,
        )
        return OrderResult(
            success=True, ticker=ticker, quantity=quantity,
            price=actual_price, order_id=order_id, error=None,
            net_gbp=net_gbp, fx_rate=fx_rate, fees_gbp=fees_gbp,
        )
    except Exception as exc:
        logger.error("BUY post-order processing failed for %s: %s", ticker, exc)
        return OrderResult(
            success=False, ticker=ticker, quantity=quantity,
            price=price, order_id=None, error=str(exc),
        )


def sell(ticker: str, quantity: float, price: float, reason: str) -> OrderResult:
    try:
        order = _post("/equity/orders/market", {"quantity": -quantity, "ticker": ticker})
        order_id = str(order.get("id", ""))
        fill = _fetch_fill(order_id)
        filled_price, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
        actual_price = filled_price if filled_price is not None else price
        logger.info(
            "SELL executed: %s × %.4f @ £%.4f | net=£%.2f fx=%.4f fees=£%.2f | reason=%s | order_id=%s",
            ticker, quantity, actual_price,
            net_gbp or 0, fx_rate or 0, fees_gbp or 0, reason, order_id,
        )
        return OrderResult(
            success=True, ticker=ticker, quantity=quantity,
            price=actual_price, order_id=order_id, error=None,
            net_gbp=net_gbp, fx_rate=fx_rate, fees_gbp=fees_gbp,
        )
    except Exception as exc:
        logger.error("SELL failed for %s: %s", ticker, exc)
        return OrderResult(
            success=False, ticker=ticker, quantity=quantity,
            price=price, order_id=None, error=str(exc),
        )
