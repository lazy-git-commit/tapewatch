"""
market/finnhub_bars.py
───────────────────────
Real-time price data from Finnhub REST API.
Used by price_check.py to get the current price without the 15-min yfinance delay.
"""

import logging
import requests
from config.settings import cfg

logger = logging.getLogger(__name__)

_QUOTE_URL = "https://finnhub.io/api/v1/quote"


def get_finnhub_quote(symbol: str) -> dict | None:
    """
    Fetch current quote from Finnhub REST API.
    Returns dict with keys: c (current), o (open), pc (prev close), t (timestamp).
    Returns None if the quote is unavailable or the symbol is invalid.
    """
    try:
        resp = requests.get(
            _QUOTE_URL,
            params={"symbol": symbol, "token": cfg.finnhub_api_key},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("c", 0) == 0:
            return None
        return data
    except Exception as exc:
        logger.warning("Finnhub quote failed for %s: %s", symbol, exc)
        return None
