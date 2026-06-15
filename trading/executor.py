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


def build_symbol_map(retries: int = 3) -> bool:
    """
    Fetch T212's full instrument catalogue and build a shortName → ticker map.

    T212 keeps the original SPAC/IPO ticker code even after a company changes
    its exchange symbol (e.g. SUNE → JCS_US_EQ after a reverse merger).
    Without this map, ~16% of small-cap tickers 404 when we append _US_EQ.

    Retries with backoff: the metadata endpoint rate-limits aggressively, and
    a single startup 429 used to leave the whole session running on the bad
    `shortName_US_EQ` fallback (observed 2026-06-12). main.py also schedules
    a daily rebuild as a second line of defence.

    Returns True if the map was built, False if all attempts failed.
    """
    global _symbol_to_t212
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{_base_url()}/equity/metadata/instruments",
                headers=_auth_header(),
                timeout=15,
            )
            if resp.status_code == 429:
                # Rate-limited: this endpoint allows ~1 req/30s — wait it out.
                wait = 30 * attempt
                logger.warning(
                    "T212 instrument metadata rate-limited (attempt %d/%d) — waiting %ds",
                    attempt, retries, wait,
                )
                time.sleep(wait)
                continue
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
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(5 * attempt)
    logger.warning(
        "Could not build T212 symbol map after %d attempts (%s) — using "
        "shortName_US_EQ fallback until the daily rebuild",
        retries, last_exc,
    )
    return False


# Exchange prefixes Benzinga attaches to NON-US listings. We only trade US
# equities (T212 ISA, USD instruments), so a tagged ticker carrying any of
# these is a foreign listing we cannot trade — reject it outright rather than
# strip the prefix and accidentally trade a same-named US ticker.
# Observed leak (2026-06-15): "TSX:MDA" reached the price check as "TSX:MDA",
# Finnhub returned no quote, and the candidate burned a 30-min eval window.
_NON_US_EXCHANGE_PREFIXES = (
    "TSX:", "TSXV:", "CVE:", "CNSX:",      # Canada
    "LON:", "LSE:",                         # London
    "ASX:",                                 # Australia
    "HKG:", "SHA:", "SHE:",                 # Hong Kong / China
    "FRA:", "ETR:", "EPA:", "AMS:", "BIT:", "BME:", "STO:",  # Europe
    "TSE:", "TYO:", "KRX:", "NSE:", "BSE:",  # Asia
)


def clean_benzinga_symbol(raw: str) -> str | None:
    """
    Normalise a raw Benzinga ticker tag into a clean US exchange symbol, or
    return None if it is not a tradeable US equity.

    Benzinga tags carry routing/disambiguation cruft that breaks every
    downstream consumer (Finnhub quote, Twelvedata, T212 mapping):

      "TSX:MDA"  → None   — foreign (Toronto) listing, not US-tradeable
      "INBX1"    → "INBX" — trailing digit is Benzinga's collision
                            disambiguation suffix, not part of the symbol
      "BRK.A"    → "BRK.A"— class shares are kept (dot is a real US ticker char)
      "AAPL"     → "AAPL" — already clean

    Returns the cleaned UPPERCASE symbol, or None to drop the tag.
    """
    if not raw:
        return None
    sym = raw.strip().upper()

    # Drop foreign-exchange-prefixed tags entirely — we can't trade them.
    for prefix in _NON_US_EXCHANGE_PREFIXES:
        if sym.startswith(prefix):
            logger.debug("clean_benzinga_symbol: dropping non-US listing %s", raw)
            return None

    # A leftover "EXCH:" prefix we don't explicitly know is still non-US.
    if ":" in sym:
        logger.debug("clean_benzinga_symbol: dropping unknown-exchange tag %s", raw)
        return None

    # Strip a single trailing disambiguation digit that is NOT part of the
    # real symbol (INBX1 → INBX, SAIL1 → SAIL). Guarded carefully:
    #   - only a lone trailing digit (not multi-digit like a units ticker),
    #   - only when the remaining stem is a plausible 2+ char alpha symbol,
    #   so we never mangle legitimate alphanumerics.
    if len(sym) >= 4 and sym[-1].isdigit() and sym[:-1].isalpha():
        stem = sym[:-1]
        logger.debug("clean_benzinga_symbol: %s → %s (stripped disambiguation digit)", raw, stem)
        sym = stem

    return sym or None


def resolve_t212_ticker(exchange_symbol: str) -> str | None:
    """
    Convert a Benzinga exchange symbol (e.g. "SUNE") to the correct T212 ticker
    code. Returns None when the tag is not a tradeable US equity (foreign
    listing, unparseable) — callers must skip those.

    Resolution order:
      1. clean the raw tag (drop foreign listings, strip disambiguation cruft)
      2. look up the cleaned symbol in the T212 shortName→ticker map
         (handles post-SPAC/rename mismatches: SUNE → JCS_US_EQ)
      3. fall back to "<symbol>_US_EQ"
    """
    cleaned = clean_benzinga_symbol(exchange_symbol)
    if cleaned is None:
        return None
    return _symbol_to_t212.get(cleaned, f"{cleaned}_US_EQ")


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


def _delete(path: str) -> dict:
    resp = requests.delete(f"{_base_url()}{path}", headers=_auth_header(), timeout=10)
    if not resp.ok:
        raise Exception(f"HTTP {resp.status_code} - {resp.text}")
    # T212 DELETE may return an empty body on success
    try:
        return resp.json()
    except ValueError:
        return {}


def _round_price(price: float) -> float:
    """Round a limit price to a precision T212 accepts (2dp ≥ $1, 4dp below)."""
    return round(price, 2) if price >= 1 else round(price, 4)


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
    """Poll until the order fill is available. Returns the fill dict or None.

    T212 populates fill data asynchronously — on fast micro-cap orders it can
    take 15-30s after the order is placed. We poll for up to 30s total:
      - First try the individual order endpoint (most reliable, direct by ID)
      - Fall back to history/orders list scan if the direct endpoint 404s
    Logs a warning if fill is still missing after all retries so blank Grafana
    columns are traceable to a specific order ID.
    """
    for attempt in range(10):
        time.sleep(3)
        try:
            # Try direct order lookup first — avoids the list-scan miss
            try:
                item = _get(f"/equity/orders/{order_id}")
                if "fill" in item:
                    return item["fill"]
            except Exception:
                pass
            # Fall back to history list scan
            data = _get("/equity/history/orders?limit=50")
            for item in data.get("items", []):
                if str(item.get("order", {}).get("id")) == order_id and "fill" in item:
                    return item["fill"]
        except Exception as exc:
            logger.warning("Failed to fetch fill for order %s (attempt %d): %s", order_id, attempt + 1, exc)
    logger.warning(
        "Fill data unavailable for order %s after 30s — buy_net_gbp/fx_rate/fees will be NULL in DB",
        order_id,
    )
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


def get_account_summary() -> tuple[float, float] | None:
    """(total_value, free_cash) in one API call — used by the snapshot job."""
    data = _fetch_cash()
    if not data:
        return None
    return float(data.get("total", 0)), float(data.get("free", 0))


def calculate_quantity(
    ticker: str,
    price: float,
    avg_dollar_volume: float | None = None,
) -> tuple[float | None, str | None]:
    """
    Risk-based position sizing. Returns (quantity, error_reason).

    The position size is the MINIMUM of four constraints:
      1. Hard cap        — max_position_size_pct of portfolio value.
      2. Risk budget     — equity × risk_per_trade_pct / stop_loss_pct.
                           Sizes the position so a stop-loss hit costs at most
                           risk_per_trade_pct of the account. With the fixed
                           2% stop the hard cap usually binds first; this term
                           becomes active if stops are widened/dynamic.
      3. Liquidity cap   — max_adv_participation_pct of the stock's average
                           daily dollar volume, so our own exit order can't
                           move the price (GOAI: our market sell alone pushed
                           the fill 11.7% below trigger). Approximation: ADV
                           is USD, equity is GBP — the small FX difference is
                           within the cap's safety margin.
      4. Available cash.
    """
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

    hard_cap = portfolio_value * (cfg.max_position_size_pct / 100)
    risk_cap = (
        portfolio_value * (cfg.risk_per_trade_pct / 100) / (cfg.stop_loss_pct / 100)
        if cfg.stop_loss_pct > 0 else hard_cap
    )
    constraints = [hard_cap, risk_cap, available_cash]
    if avg_dollar_volume is not None and avg_dollar_volume > 0:
        constraints.append(avg_dollar_volume * (cfg.max_adv_participation_pct / 100))

    max_spend = min(constraints)
    if max_spend <= 0:
        return None, "position size computed as zero"

    # Trading 212 allows at most 4 decimal places for fractional quantities
    quantity = round(max_spend / price, 4)
    logger.info(
        "Position size for %s: £%.2f (caps: hard=£%.0f risk=£%.0f adv=%s cash=£%.0f) "
        "→ %.4f shares @ $%.4f",
        ticker, max_spend, hard_cap, risk_cap,
        f"£{avg_dollar_volume * cfg.max_adv_participation_pct / 100:.0f}" if avg_dollar_volume else "n/a",
        available_cash, quantity, price,
    )
    return quantity, None


# ── Order management (v14) ────────────────────────────────────────────────────

def get_order_status(order_id: str) -> str | None:
    """
    Return the T212 order status (NEW, FILLED, CANCELLED, REJECTED, ...).

    Special values:
      "GONE" — the order 404s on the pending-orders endpoint, which means it
               left the book (filled and moved to history, normally).
      None   — network/API error: status UNKNOWN. Callers must NOT treat
               None as filled — closing a DB trade on a transient timeout
               while the real position is still open would desync the book.
    """
    try:
        item = _get(f"/equity/orders/{order_id}")
        return str(item.get("status", "")) or None
    except Exception as exc:
        if "HTTP 404" in str(exc):
            return "GONE"
        logger.warning("get_order_status(%s): %s", order_id, exc)
        return None


def cancel_order(order_id: str) -> bool:
    """
    Cancel a pending order. Returns True if cancelled (or already gone),
    False if the cancel failed — the caller MUST then re-check the order
    status, because the most common failure is "already filled" (the
    cancel/fill race the monitor has to handle before placing a stop sell).
    """
    try:
        _delete(f"/equity/orders/{order_id}")
        logger.info("Order %s cancelled", order_id)
        return True
    except Exception as exc:
        logger.warning("Cancel failed for order %s: %s", order_id, exc)
        return False


def place_take_profit(ticker: str, quantity: float, tp_price: float) -> str | None:
    """
    Place a resting LIMIT sell at the take-profit price, immediately after a
    buy fills. This removes ALL polling latency from the profit side: the
    exchange executes the moment the price touches TP, instead of waiting up
    to monitor_interval_seconds and then crossing the spread with a market
    order (the old way realized +3.1% on a +5% target).

    Returns the order id, or None if placement failed — the monitor then
    falls back to polled TP checking for this position, so a failed placement
    degrades gracefully rather than leaving the position unmanaged.
    """
    try:
        order = _post("/equity/orders/limit", {
            "quantity": -quantity,           # negative = sell
            "ticker": ticker,
            "limitPrice": _round_price(tp_price),
            "timeValidity": "DAY",           # EOD flatten covers the close anyway
        })
        order_id = str(order.get("id", "")) or None
        logger.info(
            "Resting TP placed: %s × %.4f @ $%.4f | order_id=%s",
            ticker, quantity, tp_price, order_id,
        )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place resting TP for %s (monitor will poll TP instead): %s",
            ticker, exc,
        )
        return None


def buy(ticker: str, price: float, avg_dollar_volume: float | None = None) -> OrderResult:
    quantity, err = calculate_quantity(ticker, price, avg_dollar_volume)
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
            "BUY executed: %s × %.4f @ $%.4f | net=£%.2f fx=%.4f fees=£%.2f | order_id=%s",
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
    """
    Close a position with BOUNDED slippage.

    Instead of a pure market order, we place a marketable LIMIT sell at
    (price × (1 − sell_limit_slack_pct%)). Because the limit sits below the
    current price it fills immediately in any normal book — but in a thin or
    collapsing book it caps the damage at the slack instead of chasing the
    bid down (GOAI: market sell filled −18.99% on a −2% stop trigger).

    Fill handling:
      - FILLED within the poll window → success with real fill data.
      - Unfilled after the window → cancel and report failure; the monitor
        keeps the position open and retries next cycle (20s later) at the
        then-current price. An unfilled retry beats an unbounded fill.
      - Cancel fails (the cancel/fill race — order filled while we were
        cancelling) → re-check status; if FILLED treat as success.
      - Limit placement itself rejected → fall back to a market order.
        An exit we can always execute matters more than slippage protection.
    """
    limit_price = _round_price(price * (1 - cfg.sell_limit_slack_pct / 100))
    order_id: str | None = None
    used_market_fallback = False
    try:
        try:
            order = _post("/equity/orders/limit", {
                "quantity": -quantity,
                "ticker": ticker,
                "limitPrice": limit_price,
                "timeValidity": "DAY",
            })
            order_id = str(order.get("id", ""))
        except Exception as limit_exc:
            # Limit rejected (precision, instrument restrictions, ...) —
            # fall back to market so the position is never stuck unmanaged.
            logger.warning(
                "SELL limit placement failed for %s (%s) — falling back to market order",
                ticker, limit_exc,
            )
            order = _post("/equity/orders/market", {"quantity": -quantity, "ticker": ticker})
            order_id = str(order.get("id", ""))
            used_market_fallback = True

        # ── Wait for the fill ────────────────────────────────────────────────
        # Poll status first (fast, definitive), then fetch fill details.
        filled = used_market_fallback  # market orders: assume fill, fetch details
        if not used_market_fallback:
            for _ in range(10):  # up to ~20s
                time.sleep(2)
                status = get_order_status(order_id)
                # "GONE" = order left the pending book (filled → history).
                # None = NETWORK ERROR, status unknown — keep polling. Treating
                # None as filled would record the trade closed in the DB while
                # the real order may still be live on the book (position desync).
                if status in ("FILLED", "GONE"):
                    filled = True
                    break
                if status in ("CANCELLED", "REJECTED"):
                    return OrderResult(
                        success=False, ticker=ticker, quantity=quantity,
                        price=price, order_id=order_id,
                        error=f"limit sell {status}",
                    )

        if not filled:
            # Book never reached our limit — cancel and let the monitor retry.
            if cancel_order(order_id):
                logger.warning(
                    "SELL [%s] limit $%.4f unfilled after 20s — cancelled, monitor "
                    "will retry next cycle at current price",
                    ticker, limit_price,
                )
                return OrderResult(
                    success=False, ticker=ticker, quantity=quantity,
                    price=price, order_id=order_id,
                    error="limit sell unfilled — cancelled for retry",
                )
            # Cancel failed → most likely filled while cancelling. Fall through
            # and treat as filled; fill fetch below confirms the price.
            logger.info("SELL [%s] cancel/fill race — treating order %s as filled", ticker, order_id)

        fill = _fetch_fill(order_id)
        filled_price, net_gbp, fx_rate, fees_gbp = _parse_fill(fill)
        actual_price = filled_price if filled_price is not None else price
        logger.info(
            "SELL executed: %s × %.4f @ $%.4f | net=£%.2f fx=%.4f fees=£%.2f | reason=%s | order_id=%s%s",
            ticker, quantity, actual_price,
            net_gbp or 0, fx_rate or 0, fees_gbp or 0, reason, order_id,
            " (market fallback)" if used_market_fallback else "",
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
            price=price, order_id=order_id, error=str(exc),
        )
