# Adding a provider

Tapewatch talks to four kinds of external service. Each sits behind a small
contract, and each is selected by one environment variable. Swapping one out
means writing a module that satisfies the contract and changing that variable —
no trading logic changes.

| Kind | What it supplies | Setting | Shipped implementation |
|---|---|---|---|
| **News** | Breaking articles tagged with tickers | `NEWS_PROVIDER` | `benzinga` (via Massive) |
| **Quote** | Current price, previous close, timestamp | `QUOTE_PROVIDER` | `finnhub`, then `twelvedata` as fallback |
| **Bars** | Intraday 1-minute bars and daily aggregates | `BARS_PROVIDER` | `twelvedata` |
| **Broker** | Order placement, positions, cash | `BROKER` | `trading212` |
| **Classifier** | Judges whether an article is a tradeable catalyst | `CLASSIFIER_PROVIDER` | `anthropic`, optional `qwen` shadow |

Provider modules live next to the ones they sit beside — `market/`, `news/`,
`trading/` — and are resolved by name at startup.

---

## The general shape

A provider module is an ordinary Python module exposing a few module-level
functions. There is no base class to inherit and no registration decorator: if
the functions exist with the right names and return the right shapes, it works.

Three rules apply to every provider:

1. **Never raise on an expected failure.** Return `None`. A provider that raises
   on a rate limit will abort a trading cycle; one that returns `None` lets the
   caller degrade gracefully.
2. **Fail closed.** If you cannot answer, say so. Never invent, interpolate or
   substitute a stale value — the system is designed to skip a trade rather than
   act on a guess, and that only works if providers are honest.
3. **Log at `INFO` when something is wrong.** An outage that only logs at
   `DEBUG` is invisible in production. This has cost real diagnosis time here.

---

## Quote provider

The most commonly swapped one. Create `market/<name>_quotes.py`:

```python
def get_quote(symbol: str, fast: bool = False) -> dict | None:
    """
    Current market data for one symbol.

    Args:
        symbol: exchange symbol, e.g. "AAPL"
        fast:   when True, make ONE attempt with no retry/backoff. Used by the
                time-boxed pre-market scan, where a slow ticker must not starve
                the rest of the watchlist.

    Returns a dict, or None if this provider has no data for the symbol:
        {
            "c":  float,          # current price          (required)
            "o":  float,          # today's open            (required)
            "pc": float,          # previous close          (required)
            "t":  float | None,   # unix seconds of the quote (optional but
                                  # strongly recommended — the entry-freshness
                                  # gate cannot run without it)
        }
    """


def auth_ok() -> bool:
    """
    True if credentials are valid. Called once at startup so a bad key is a
    loud failure at boot rather than a silent one at the first trade.
    """
```

**On `t`:** if your provider does not expose a quote timestamp, return `None`
rather than `time.time()`. The freshness gate fails *open* on a missing
timestamp, which is a deliberate choice — but a fabricated one would defeat it
silently. A stale quote that looked fresh caused a real loss here; see
`docs/algorithm.md`.

---

## Bars provider

Create `market/<name>_bars.py`:

```python
def get_session_analysis(symbol: str, fast: bool = False) -> SessionAnalysis | None:
    """
    ONE pull covering the whole trading session, returning the aggregates every
    price gate needs. Deliberately a single call rather than several: it is the
    difference between one API credit per confirmation and three.

    Returns a SessionAnalysis (see market/twelvedata_bars.py) with:
        past_price          price ~MOMENTUM_LOOKBACK_MINUTES ago, chosen BY
                            TIMESTAMP among today's bars only
        current_bar_price   latest bar close
        spread_proxy_pct    (high - low) / close of the latest bar, as %
        session_volume      shares traded so far this session
        vwap                volume-weighted average price
        last_price, session_low, session_high
    """


def get_daily_stats(symbol: str, fast: bool = False) -> tuple | None:
    """Returns (avg_daily_volume, avg_dollar_volume, prev_close) over ~20 days.
    Cache this per symbol per day — it changes once daily, and re-fetching it
    per signal is the easiest way to exhaust an API quota."""


def credits_exhausted() -> bool:
    """True when the daily budget is spent. Checked BEFORE every HTTP call so an
    exhausted quota costs nothing rather than generating a retry storm."""
```

**Select bars by timestamp, never by array index.** Thin stocks skip minutes, so
"the fifth bar back" can silently be twenty minutes old and stretch the momentum
window per-stock. This produced a false momentum reading in production.

---

## News provider

Create `news/<name>_source.py`:

```python
def fetch_articles(lookback_minutes: int = 5) -> list[dict]:
    """
    Recent articles, newest first. Return [] on failure — never raise.

    Each article:
        {
            "id":           str,        # stable unique id, used for dedup
            "title":        str,
            "body":         str,        # may be ""
            "tickers":      list[str],  # exchange symbols, no exchange prefix
            "published_at": datetime,   # timezone-aware
            "source":       str,
        }
    """
```

The `id` must be **stable across polls** — it is the deduplication key, and an
unstable one causes the same article to be classified repeatedly, costing money
and potentially trading the same event twice.

Ticker cleaning (stripping exchange prefixes, disambiguation digits) is handled
downstream; return whatever the source gives you.

---

## Broker

The largest contract, and the one to be most careful with. Create
`trading/<name>_broker.py`:

```python
def buy(ticker, price, avg_dollar_volume=None, extended=False) -> OrderResult
def sell(ticker, quantity, price, reason, force_market=False, extended=False) -> OrderResult
def place_stop_loss(ticker, quantity, stop_price) -> OrderResult
def cancel_order(order_id: str) -> bool
def get_order_status(order_id: str) -> str | None
def get_portfolio_value() -> float | None
def get_open_positions() -> list[dict]
def build_symbol_map(retries: int = 3) -> bool
```

Three requirements that are easy to get wrong and expensive when you do:

**`get_order_status` must distinguish "gone" from "unknown."** Return the string
`"GONE"` when the broker reports the order no longer exists (typically a 404 —
it filled and moved to history), and `None` when the *lookup itself* failed
(network error, timeout). Callers must never treat `None` as filled. Conflating
these means a position can be believed closed while it is still open.

**Never retry after the broker has seen the order.** A failure *before* the
request reached the broker is safe to retry. A failure *after* is not — you risk
a duplicate order. `OrderResult` distinguishes these by convention:
`order_id is None and quantity == 0` means nothing was placed.

**Set `OrderResult.unfilled = True`** when a limit order rested and never filled
and you cancelled it. That is not a failure — it means the market never reached
your price — and the caller re-queues the signal rather than discarding it.

---

## Classifier

Create `news/<name>_classifier.py`:

```python
def score_articles(articles: list[dict]) -> dict[str, dict]:
    """
    Classify a batch. Returns {article_id: classification}.

    Each classification:
        {
            "sentiment":          "positive" | "neutral" | "negative",
            "confidence":         float,   # 0.0-1.0
            "catalyst_type":      str,     # one of CATALYST_TYPES
            "already_moved":      bool,    # did the move pre-date the article?
            "catalyst_magnitude": int,     # 1-5
        }

    Return {} on failure — never raise, and never guess. An article with no
    classification is simply retried next cycle.
    """
```

**Validate, do not clamp.** If a model returns a confidence of 4.5 when the
range is 0–1, reject the record. Clamping it to 1.0 silently converts a
malformed response into a maximally confident trading signal — which is the
worst possible failure mode.

**Batch and bound the output budget.** Chunk large batches; a response truncated
at the token ceiling returns success with an empty result, which is
indistinguishable from a genuine empty answer unless you check the stop reason.
That cost this project several days of misdiagnosis.

---

## Worked example: adding Polygon as a quote provider

```python
# market/polygon_quotes.py
"""Polygon.io quote provider. See docs/PROVIDERS.md for the contract."""

import logging
import os
import requests

logger = logging.getLogger(__name__)
_KEY = os.getenv("POLYGON_API_KEY", "")
_BASE = "https://api.polygon.io"


def auth_ok() -> bool:
    if not _KEY:
        logger.error("POLYGON_API_KEY is not set — quote provider unavailable")
        return False
    try:
        r = requests.get(f"{_BASE}/v1/marketstatus/now",
                         params={"apiKey": _KEY}, timeout=10)
        return r.ok
    except Exception as exc:
        logger.error("Polygon auth check failed: %s", exc)
        return False


def get_quote(symbol: str, fast: bool = False) -> dict | None:
    attempts = 1 if fast else 3
    for attempt in range(attempts):
        try:
            r = requests.get(f"{_BASE}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}",
                             params={"apiKey": _KEY}, timeout=10)
            if r.status_code == 404:
                return None                      # no coverage — not an error
            r.raise_for_status()
            t = r.json().get("ticker", {})
            day, prev = t.get("day", {}), t.get("prevDay", {})
            price = t.get("lastTrade", {}).get("p") or day.get("c")
            if not price or not prev.get("c"):
                return None                      # incomplete — fail closed
            return {
                "c":  float(price),
                "o":  float(day.get("o") or price),
                "pc": float(prev["c"]),
                "t":  t.get("updated", 0) / 1e9 or None,   # ns → s
            }
        except Exception as exc:
            if attempt == attempts - 1:
                logger.warning("Polygon quote failed for %s: %s", symbol, exc)
                return None
    return None
```

Then set `QUOTE_PROVIDER=polygon` and `POLYGON_API_KEY=...` in `.env`.

---

## Testing your provider

The test suite runs with **no credentials and no network**, and yours must too.
Mock at the HTTP boundary:

```python
from unittest.mock import patch

def test_missing_coverage_returns_none_not_an_exception():
    import market.polygon_quotes as p
    with patch.object(p.requests, "get") as get:
        get.return_value.status_code = 404
        assert p.get_quote("NOSUCH") is None


def test_incomplete_response_fails_closed():
    import market.polygon_quotes as p
    with patch.object(p.requests, "get") as get:
        get.return_value.ok = True
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"ticker": {"day": {"c": 10.0}}}
        assert p.get_quote("ACME") is None      # no prevDay → must not guess
```

Then confirm your tests actually fail when the behaviour is removed — see
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) on mutation testing.

Pull requests adding providers are very welcome.
