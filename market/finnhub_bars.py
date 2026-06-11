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
import time
import requests
from config.settings import cfg

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://finnhub.io/api/v1/quote"
_RETRIES = 3
_BASE_BACKOFF = 1.0  # seconds


def get_finnhub_quote(symbol: str) -> dict | None:
    """
    Fetch current quote from Finnhub REST API with exponential-backoff retries.

    Returns dict with keys: c (current), o (open), pc (prev close), t (timestamp).
    Returns None if the quote is unavailable, the symbol is invalid, or all
    retry attempts are exhausted.

    A zero 'c' value means Finnhub has no data for the symbol — returned as None
    so callers don't treat $0 as a valid price.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(
                _QUOTE_URL,
                params={"symbol": symbol, "token": cfg.finnhub_api_key},
                timeout=5,
            )
            # Client errors (4xx) won't self-heal — log and return immediately
            if 400 <= resp.status_code < 500:
                logger.warning(
                    "Finnhub quote HTTP %d for %s — not retrying: %s",
                    resp.status_code, symbol, resp.text[:120],
                )
                return None
            # Server errors (5xx) are transient — retry
            if resp.status_code >= 500:
                wait = _BASE_BACKOFF * (2 ** (attempt - 1))
                logger.warning(
                    "Finnhub quote HTTP %d for %s (attempt %d/%d) — retrying in %.1fs",
                    resp.status_code, symbol, attempt, _RETRIES, wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("c", 0) == 0:
                # Finnhub returns c=0 for unknown/halted symbols
                logger.debug("Finnhub: c=0 for %s — symbol unknown or halted", symbol)
                return None
            logger.debug(
                "Finnhub quote [%s]: c=%.4f o=%.4f pc=%.4f",
                symbol, data["c"], data.get("o", 0), data.get("pc", 0),
            )
            return data
        except requests.exceptions.Timeout:
            wait = _BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "Finnhub quote timeout for %s (attempt %d/%d) — retrying in %.1fs",
                symbol, attempt, _RETRIES, wait,
            )
            last_exc = Exception(f"timeout on attempt {attempt}")
        except requests.exceptions.ConnectionError as exc:
            wait = _BASE_BACKOFF * (2 ** (attempt - 1))
            logger.warning(
                "Finnhub connection error for %s (attempt %d/%d): %s — retrying in %.1fs",
                symbol, attempt, _RETRIES, exc, wait,
            )
            last_exc = exc
        except Exception as exc:
            logger.warning(
                "Finnhub quote unexpected error for %s (attempt %d/%d): %s",
                symbol, attempt, _RETRIES, exc,
            )
            last_exc = exc
            break  # Non-network errors (e.g. JSON decode) won't self-heal
        if attempt < _RETRIES:
            time.sleep(_BASE_BACKOFF * (2 ** (attempt - 1)))
    logger.error(
        "Finnhub: all %d attempts failed for %s — last error: %s",
        _RETRIES, symbol, last_exc,
    )
    return None
