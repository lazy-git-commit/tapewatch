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
import math
import time
import requests
from dataclasses import dataclass
from config.settings import cfg
from market.twelvedata_bars import get_gbp_usd_rate

logger = logging.getLogger(__name__)

_T212_DEMO = "https://demo.trading212.com/api/v0"
_T212_LIVE = "https://live.trading212.com/api/v0"

# symbol → T212 ticker code, e.g. "SUNE" → "JCS_US_EQ"
# T212 keeps original IPO/SPAC codes even after a company changes its exchange ticker,
# so "SUNE_US_EQ" 404s while the instrument lives as "JCS_US_EQ".
# Built once at startup from the instruments metadata endpoint.
_symbol_to_t212: dict[str, str] = {}
# Inverse of the above (T212 code → exchange shortName), rebuilt together with
# it. Used by t212_to_symbol() so price checks query the real exchange symbol.
_t212_to_symbol: dict[str, str] = {}


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
            # Inverse map: T212 code → exchange shortName. Market-data lookups
            # (Finnhub/Twelvedata) need the EXCHANGE symbol; deriving it by
            # stripping "_US_EQ" is lossy whenever T212's code differs from the
            # shortName (observed 2026-07-07: Firefly Aerospace is shortName
            # "FLY" but T212 code "FLY1_US_EQ" — the derived "FLY1" had no data
            # coverage anywhere, so a $13M-NASA-contract signal expired unpriced).
            global _t212_to_symbol
            _t212_to_symbol = {v: k for k, v in mapping.items()}
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


def t212_to_symbol(t212_ticker: str) -> str:
    """
    Convert a T212 instrument code back to the exchange symbol that market-data
    APIs (Finnhub/Twelvedata) understand.

    Prefers the inverse of the instrument map (exact, handles re-used symbols
    like FLY → FLY1_US_EQ and cruft codes like AVAV__US_EQ); falls back to
    stripping everything from the first underscore, which is correct for the
    common AAPL_US_EQ shape and the best available guess before the map is
    built.
    """
    if not t212_ticker:
        return ""  # downstream quote lookups 4xx harmlessly on ""
    mapped = _t212_to_symbol.get(t212_ticker)
    if mapped:
        return mapped
    return t212_ticker.split("_")[0]


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
    try:
        filled_price = float(fill["price"]) if fill.get("price") is not None else None
    except (TypeError, ValueError):
        filled_price = None
    impact = fill.get("walletImpact", {})
    try:
        net_gbp = float(impact["netValue"]) if impact.get("netValue") is not None else None
    except (TypeError, ValueError):
        net_gbp = None
    try:
        fx_rate = float(impact["fxRate"]) if impact.get("fxRate") is not None else None
    except (TypeError, ValueError):
        fx_rate = None
    fees_gbp = 0.0
    for tax in impact.get("taxes", []) or []:
        try:
            fees_gbp += abs(float(tax.get("quantity", 0)))
        except (TypeError, ValueError):
            continue
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


def get_account_summary() -> tuple[float, float] | None:
    """(total_value, free_cash) in one API call — used by the snapshot job."""
    data = _fetch_cash()
    if not data:
        return None
    return float(data.get("total", 0)), float(data.get("free", 0))


def get_broker_positions() -> dict[str, float] | None:
    """
    Return the broker's current open positions as {t212_ticker: quantity}.

    Uses the T212 /equity/portfolio endpoint. Returns None on API failure
    so callers can distinguish "broker has no positions" (empty dict) from
    "broker is unreachable" (None) and avoid false-positive reconciliation
    alerts during transient network errors.
    """
    try:
        data = _get("/equity/portfolio")
        # T212 returns a list of position objects; each has 'ticker' and 'quantity'.
        positions = data if isinstance(data, list) else data.get("positions", [])
        return {
            str(p["ticker"]): float(p["quantity"])
            for p in positions
            if p.get("ticker") and p.get("quantity") is not None
        }
    except Exception as exc:
        logger.warning("get_broker_positions failed: %s — skipping reconciliation", exc)
        return None


def calculate_quantity(
    ticker: str,
    price: float,
    avg_dollar_volume: float | None = None,
) -> tuple[float | None, str | None]:
    """
    Risk-based position sizing. Returns (quantity, error_reason).

    The position size is the MINIMUM of four constraints:
      1. Hard cap        — max_position_size_pct of portfolio value (GBP).
      2. Risk budget     — equity × risk_per_trade_pct / stop_loss_pct (GBP).
      3. Liquidity cap   — max_adv_participation_pct of the stock's average
                           daily dollar volume converted to GBP, so our own
                           exit order can't move the price (GOAI: our market
                           sell alone pushed the fill 11.7% below trigger).
      4. Available cash  (GBP).

    All four constraints are in GBP. The GBP budget is then converted to USD
    using the live GBP/USD rate before dividing by the USD stock price to get
    quantity. This makes the ADV cap and the quantity division mathematically
    consistent regardless of FX moves.
    """
    # A zero/negative/NaN price would make the quantity division meaningless
    # (or a ZeroDivisionError). Upstream normalization should make this
    # impossible, but sizing is where the money moves — belt and braces.
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, f"invalid price {price!r} — refusing to size"
    if not math.isfinite(price) or price <= 0:
        return None, f"invalid price {price!r} — refusing to size"

    try:
        data = _get("/equity/account/cash")
    except Exception as exc:
        reason = f"T212 cash API failed: {exc}"
        logger.error("calculate_quantity for %s: %s", ticker, reason)
        return None, reason

    try:
        portfolio_value = float(data.get("total", 0))
        available_cash = float(data.get("free", 0))
    except (TypeError, ValueError):
        reason = f"malformed T212 cash payload: {str(data)[:120]}"
        logger.error("calculate_quantity for %s: %s", ticker, reason)
        return None, reason
    if not (math.isfinite(portfolio_value) and math.isfinite(available_cash)):
        reason = f"non-finite T212 cash values: {str(data)[:120]}"
        logger.error("calculate_quantity for %s: %s", ticker, reason)
        return None, reason

    if portfolio_value <= 0 or available_cash <= 0:
        reason = f"no funds available (total=£{portfolio_value:.2f} free=£{available_cash:.2f})"
        logger.warning("calculate_quantity for %s: %s", ticker, reason)
        return None, reason

    fx = get_gbp_usd_rate()  # GBP → USD conversion factor

    hard_cap = portfolio_value * (cfg.max_position_size_pct / 100)
    risk_cap = (
        portfolio_value * (cfg.risk_per_trade_pct / 100) / (cfg.stop_loss_pct / 100)
        if cfg.stop_loss_pct > 0 else hard_cap
    )
    constraints = [hard_cap, risk_cap, available_cash]
    if avg_dollar_volume is not None and avg_dollar_volume > 0:
        # Convert USD ADV cap to GBP so all constraints are in the same currency.
        adv_cap_gbp = (avg_dollar_volume * (cfg.max_adv_participation_pct / 100)) / fx
        constraints.append(adv_cap_gbp)

    max_spend_gbp = min(constraints)
    if max_spend_gbp <= 0:
        return None, "position size computed as zero"

    # Convert GBP budget to USD to match the USD stock price, then size.
    max_spend_usd = max_spend_gbp * fx

    # Trading 212 allows at most 4 decimal places for fractional quantities.
    # If the account is too small for even 0.0001 share, do not send a zero
    # quantity order and burn the signal on a broker-side validation error.
    quantity = round(max_spend_usd / price, 4)
    if quantity <= 0:
        return None, (
            f"position size below minimum fractional quantity "
            f"(max_spend=£{max_spend_gbp:.2f} / ${max_spend_usd:.2f}, price=${price:.4f})"
        )
    logger.info(
        "Position size for %s: £%.2f ($%.2f @ fx=%.4f) "
        "(caps: hard=£%.0f risk=£%.0f adv=%s cash=£%.0f) → %.4f shares @ $%.4f",
        ticker, max_spend_gbp, max_spend_usd, fx, hard_cap, risk_cap,
        f"£{adv_cap_gbp:.0f}" if avg_dollar_volume else "n/a",
        available_cash, quantity, price,
    )
    return quantity, None


# ── Order management (v14) ────────────────────────────────────────────────────

def get_order_status(order_id: str) -> str | None:
    """
    Return the T212 order status (NEW, FILLED, CANCELLED, REJECTED, ...).

    Special values:
      "GONE" — the order 404s on the pending-orders endpoint and the history
               lookup is inconclusive. This is NOT proof of a fill; callers
               that care about fill-vs-expiry must fetch fill detail before
               closing any DB trade.
      None   — network/API error: status UNKNOWN. Callers must NOT treat
               None as filled — closing a DB trade on a transient timeout
               while the real position is still open would desync the book.
    """
    try:
        item = _get(f"/equity/orders/{order_id}")
        return str(item.get("status", "")).upper() or None
    except Exception as exc:
        if "HTTP 404" in str(exc):
            # Pending endpoint 404 means "not live on the book", but that can
            # be a fill, cancellation, expiry, or history pagination miss. Check
            # recent order history before making the caller infer too much.
            try:
                data = _get("/equity/history/orders?limit=50")
                for item in data.get("items", []):
                    order = item.get("order", {})
                    if str(order.get("id") or item.get("id")) != str(order_id):
                        continue
                    status = str(order.get("status") or item.get("status") or "").upper()
                    if "fill" in item or status == "FILLED":
                        return "FILLED"
                    if status:
                        return status
            except Exception as hist_exc:
                logger.warning("get_order_status(%s): history lookup failed after 404: %s", order_id, hist_exc)
                return None
            return "GONE"
        logger.warning("get_order_status(%s): %s", order_id, exc)
        return None


def cancel_order(order_id: str) -> bool:
    """
    Cancel a pending order. Returns True if the broker accepted the cancel.

    False means the order state is unresolved — the caller MUST re-check order
    status/fill detail before placing any competing order. The common benign
    case is "already filled", but network errors and expiry look similar at
    this level.
    """
    try:
        _delete(f"/equity/orders/{order_id}")
        logger.info("Order %s cancelled", order_id)
        return True
    except Exception as exc:
        logger.warning("Cancel failed for order %s: %s", order_id, exc)
        return False


def place_stop_loss(ticker: str, quantity: float, stop_price: float) -> str | None:
    """
    Place a resting STOP (stop-market) sell at the broker, immediately after
    a buy fills.

    v20 exit inversion — WHY THE STOP RESTS AND THE TP IS POLLED:
    T212 has no OCO, and every sell order reserves the shares it covers, so
    only ONE closing order can rest at a time. v14-v19 rested the TAKE-PROFIT
    limit and polled the stop every 20s. The realized record proves that was
    backwards: 1 resting-TP fill in 11 trades, versus repeated stop-side
    slippage where the 20s poll + limit-retry ladder turned a −2% trigger
    into −3.4% (VECO), −3.97% (CRCL: price fell ~1%/min; the poll alone gave
    it a 20s head start) and −18.99% (GOAI). A missed TP costs opportunity;
    a slow stop costs capital on EVERY fast reversal. The stop now executes
    broker-side with zero polling latency; the monitor polls the TP instead
    (at the 5s monitor cadence), cancelling the stop before selling.

    Stop-market, not stop-limit, on purpose: when the stop triggers, the book
    is moving against us — execution certainty is the point of the order.
    The ADV liquidity floor (min_daily_dollar_volume) bounds the expected
    slippage; GOAI-class fills came from names that gate now rejects.

    Returns the order id, or None if placement failed — the monitor then
    falls back to polled stop checking for this position, so a failed
    placement degrades gracefully rather than leaving the position unmanaged.
    """
    try:
        order = _post("/equity/orders/stop", {
            "quantity": -quantity,           # negative = sell
            "ticker": ticker,
            "stopPrice": _round_price(stop_price),
            "timeValidity": "DAY",           # EOD flatten covers the close anyway
        })
        order_id = str(order.get("id", "")) or None
        logger.info(
            "Resting STOP placed: %s × %.4f @ $%.4f | order_id=%s",
            ticker, quantity, stop_price, order_id,
        )
        return order_id
    except Exception as exc:
        logger.warning(
            "Could not place resting stop for %s (monitor will poll the stop instead): %s",
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
        # instrument allows. The error detail carries the maximum allowed
        # precision. Retry once with the quantity FLOORED to that precision:
        #   - floored, not rounded — rounding half-up can exceed the cash/ADV
        #     budget the sizing just computed (73.995 → 74 shares we can't afford);
        #   - the allowed precision is extracted as the last integer anywhere in
        #     the detail string rather than `detail.split()[-1]`, because T212's
        #     wording varies ("invalid quantity precision 2" vs "…precision: 2.")
        #     and a parse failure here used to abort the retry entirely —
        #     production lost 6 fully-confirmed entries to this class of failure
        #     (RCAT/ONDS/CELZ/VOYG/VERU/BCDA, 2026-05-28→06-05). If no integer is
        #     found, fall back to whole shares (precision 0) — every instrument
        #     accepts integer quantities.
        if "quantity-precision-mismatch" in exc_str:
            import json as _json
            import math as _math
            import re as _re
            try:
                allowed: int | None = None
                try:
                    body = exc_str.split(" - ", 1)[1]
                    detail = str(_json.loads(body).get("detail", ""))
                    nums = _re.findall(r"\d+", detail)
                    if nums:
                        allowed = int(nums[-1])
                except Exception:
                    allowed = None
                if allowed is None or not (0 <= allowed <= 4):
                    allowed = 0
                scale = 10 ** allowed
                # +1e-9 guards against float representation error flooring
                # 73.46 → 73.45; quantities carry at most 4dp by construction.
                quantity = _math.floor(quantity * scale + 1e-9) / scale
                if quantity <= 0:
                    return OrderResult(
                        success=False, ticker=ticker, quantity=quantity,
                        price=price, order_id=None,
                        error=f"quantity rounds to zero at broker precision {allowed}",
                    )
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
        # Fill-vs-signal sanity check. A large gap means the quote that
        # confirmed the signal did not reflect the real market (stale/OTC
        # print) — the momentum that justified the entry may be fictional.
        # Observed 2026-07-07: GLASF confirmed at $12.50, filled at $11.79
        # (−5.7%); the quote then stayed frozen at $12.50 all afternoon.
        if filled_price is not None and price > 0:
            slippage_pct = (filled_price - price) / price * 100
            if abs(slippage_pct) > 3.0:
                logger.warning(
                    "BUY [%s] filled %.2f%% away from signal price ($%.4f vs "
                    "$%.4f) — quote likely stale or book very thin; position "
                    "risk is NOT what the signal implied",
                    ticker, slippage_pct, filled_price, price,
                )
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


def sell(ticker: str, quantity: float, price: float, reason: str, *, force_market: bool = False) -> OrderResult:
    """
    Close a position with BOUNDED slippage.

    Instead of a pure market order, we place a marketable LIMIT sell at
    (price × (1 − sell_limit_slack_pct%)). Because the limit sits below the
    current price it fills immediately in any normal book — but in a thin or
    collapsing book it caps the damage at the slack instead of chasing the
    bid down (GOAI: market sell filled −18.99% on a −2% stop trigger).

    force_market=True bypasses the limit order entirely (used for EOD flatten
    and emergency exits where execution certainty beats slippage control).
    EOD flatten passes reason="eod_flatten"; emergency DB-failure exits pass
    reason="db_record_failed" with force_market=True — keeping the reason
    accurate for logs and reporting while still routing through market order.

    Fill handling for non-market exits:
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
    used_market_fallback = force_market or reason == "eod_flatten"
    try:
        if used_market_fallback:
            order = _post("/equity/orders/market", {"quantity": -quantity, "ticker": ticker})
            order_id = str(order.get("id", ""))
        else:
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

        if not order_id:
            return OrderResult(
                success=False, ticker=ticker, quantity=quantity,
                price=price, order_id=None, error="broker response missing order id",
            )

        # ── Wait for the fill ────────────────────────────────────────────────
        # Poll status first (fast, definitive), then fetch fill details.
        filled = used_market_fallback  # market orders: assume fill, fetch details
        if not used_market_fallback:
            for _ in range(10):  # up to ~20s
                time.sleep(2)
                status = get_order_status(order_id)
                # "GONE" usually means the order left the pending book; we
                # still fetch fill detail below before trusting the price.
                # None = NETWORK ERROR, status unknown — keep polling.
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

            # Cancel failed. Re-check: if the order filled during the cancel,
            # record it; otherwise do not pretend it filled.
            status = get_order_status(order_id)
            if status in ("FILLED", "GONE"):
                logger.info("SELL [%s] cancel/fill race — treating order %s as filled", ticker, order_id)
            else:
                return OrderResult(
                    success=False, ticker=ticker, quantity=quantity,
                    price=price, order_id=order_id,
                    error=f"limit sell cancel failed; state={status or 'unknown'}",
                )

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
