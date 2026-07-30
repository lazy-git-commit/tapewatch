"""
market/finnhub_bars.py
───────────────────────
Real-time price data from Finnhub REST API.
Used by price_check.py to get the current price without the 15-min data delay.

Retry policy:
  - 3 attempts with 1s / 2s / 4s exponential back-off
  - Retries on Timeout, ConnectionError, and HTTP 5xx
  - Does NOT retry HTTP 4xx (bad symbol, auth failure — won't self-heal)
  - Returns None after all attempts exhausted so callers can fall back gracefully
"""

import logging
import math
import time
import requests
from config.settings import cfg

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_RETRIES = 3
_BASE_BACKOFF = 1.0  # seconds

# ── Auth/entitlement failure latch ───────────────────────────────────────────
# A 401/403 means the API key is invalid/revoked or the plan doesn't cover
# this endpoint — a SYSTEMIC failure affecting every symbol, not "this ticker
# has no coverage." Without this latch, a revoked key looks identical to N
# independent per-symbol "no Finnhub data" misses, each just a per-call
# WARNING — exactly the misclassification shape that got nine liquid tickers
# permanently blacklisted on the Twelvedata side (CLAUDE.md v21.6). Mirrors
# twelvedata_bars.py's _prepost_supported/_note_prepost_denied latch.
# None = not yet seen a definitive auth failure, False = latched.
_auth_ok: bool | None = None


def finnhub_auth_ok() -> bool:
    """False once a 401/403 has been seen this process — a systemic failure,
    not per-symbol coverage. Callers can use this to avoid counting a
    downstream miss as a no-quote strike while Finnhub is broken account-wide."""
    return _auth_ok is not False


def _note_auth_failure(status_code: int, detail: str) -> None:
    """Latch the auth/entitlement failure and record it once, loudly."""
    global _auth_ok
    if _auth_ok is False:
        return
    _auth_ok = False
    logger.error(
        "Finnhub API key appears invalid or revoked (HTTP %d) — every symbol "
        "will fail this way until FINNHUBIO_API_KEY is fixed, not just the one "
        "that triggered this. Provider said: %s",
        status_code, detail,
    )
    try:
        from storage.database import record_system_event
        record_system_event(
            "finnhub_auth_failure",
            f"HTTP {status_code}: {detail[:200]}",
        )
    except Exception as exc:
        logger.debug("Could not record finnhub_auth_failure system_event: %s", exc)


def _safe_float(v) -> float | None:
    """float(v) that returns None for unparseable/non-finite values instead
    of raising. NaN matters: NaN compares False against every gate threshold,
    so a NaN price would silently pass penny/dead-cat/extended-move checks."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _normalize_quote(symbol: str, data) -> dict | None:
    """
    Validate and coerce a raw /quote payload into {"c", "o", "pc", "t"}.

    The old code returned Finnhub's dict as-is after checking `c == 0`, which
    let c=None, c="abc", c=-5 and c=NaN through to price math and gate
    comparisons. The current price must be a positive finite number or the
    quote is worthless; o/pc degrade to 0 (callers treat 0 as "missing");
    a bad `t` degrades to None (staleness check fails open on it) rather
    than discarding an otherwise good quote.
    """
    if not isinstance(data, dict):
        logger.warning("Finnhub: non-dict quote payload for %s — ignoring", symbol)
        return None
    c = _safe_float(data.get("c"))
    if c is None or c <= 0:
        # Finnhub returns c=0 for unknown/halted symbols; None/garbage/negative
        # all mean the same thing to us: no usable price.
        logger.debug("Finnhub: unusable c=%r for %s — no quote", data.get("c"), symbol)
        return None
    o = _safe_float(data.get("o"))
    pc = _safe_float(data.get("pc"))
    t = _safe_float(data.get("t"))
    return {
        "c": c,
        "o": o if o is not None and o > 0 else 0,
        "pc": pc if pc is not None and pc > 0 else 0,
        "t": int(t) if t is not None and t > 0 else None,
    }


def get_finnhub_quote(symbol: str, fast: bool = False) -> dict | None:
    """
    Fetch current quote from Finnhub REST API with exponential-backoff retries.

    Returns dict with keys: c (current), o (open), pc (prev close), t (timestamp).
    Returns None if the quote is unavailable, the symbol is invalid, or all
    retry attempts are exhausted.

    A zero 'c' value means Finnhub has no data for the symbol — returned as None
    so callers don't treat $0 as a valid price.

    `fast` (default False) is for time-boxed callers — the pre-market eval
    window. In fast mode there is exactly ONE attempt and NO sleeps: a
    timeout/5xx returns None immediately and the next cycle is the retry.
    Without this, the premarket path's "no retry backoff" contract held for
    every Twelvedata call but not for the PRIMARY quote source: a slow/down
    Finnhub could hold a pool thread for ~17s (3×5s timeouts + 1+2s sleeps)
    inside the 30s eval budget — the same starvation class fixed for
    Twelvedata on 2026-06-23.
    """
    attempts = 1 if fast else _RETRIES
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(
                _QUOTE_URL,
                params={"symbol": symbol, "token": cfg.finnhub_api_key},
                timeout=5,
            )
            # 401/403 is a systemic auth/entitlement failure (bad or revoked
            # key), not "this symbol has no coverage" — latch it distinctly so
            # it isn't logged (and counted downstream) identically to a
            # per-symbol 404/422.
            if resp.status_code in (401, 403):
                _note_auth_failure(resp.status_code, resp.text[:200])
                return None
            # Other client errors (4xx) won't self-heal — log and return immediately
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "Finnhub quote HTTP %d for %s — not retrying: %s",
                    resp.status_code, symbol, resp.text[:120],
                )
                return None
            # Server errors (5xx) are transient — retry (fast mode: give up now)
            if resp.status_code >= 500:
                if fast:
                    logger.warning(
                        "Finnhub quote HTTP %d for %s — fast mode, skipping (retry next cycle)",
                        resp.status_code, symbol,
                    )
                    return None
                wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "Finnhub quote HTTP %d for %s (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code, symbol, attempt, attempts, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            quote = _normalize_quote(symbol, resp.json())
            if quote is None:
                return None
            logger.debug(
                "Finnhub quote [%s]: c=%.4f o=%.4f pc=%.4f",
                symbol, quote["c"], quote["o"], quote["pc"],
            )
            return quote
        except requests.exceptions.Timeout:
            logger.warning(
                "Finnhub quote timeout for %s (attempt %d/%d)", symbol, attempt, attempts,
            )
            last_exc = Exception(f"timeout on attempt {attempt}")
        except requests.exceptions.ConnectionError as exc:
            logger.warning(
                "Finnhub connection error for %s (attempt %d/%d): %s",
                symbol, attempt, attempts, exc,
            )
            last_exc = exc
        except Exception as exc:
            logger.warning(
                "Finnhub quote unexpected error for %s (attempt %d/%d): %s",
                symbol, attempt, attempts, exc,
            )
            last_exc = exc
            break  # Non-network errors (e.g. JSON decode) won't self-heal
        if attempt < attempts:
            time.sleep(_BASE_BACKOFF * (2 ** (attempt - 1)))
    logger.error(
        "Finnhub: all %d attempt(s) failed for %s — last error: %s",
        attempts, symbol, last_exc,
    )
    return None
