"""
tests/conftest.py
─────────────────
Shared pytest fixtures.

The modules under test carry deliberate PROCESS-LIFETIME state: outage
counters, capability latches, and credit meters that are supposed to persist
across calls in the live service. In a test session that same persistence
leaks between tests — one test's simulated failures push a counter toward a
threshold that a later, unrelated test then crosses.

That is not hypothetical: after the v21.10 tripwires were added, accumulated
failures from unrelated Finnhub/quote tests crossed the outage threshold
mid-suite and triggered a REAL `record_system_event()` call, which retries
`get_conn()` with backoff when no database is reachable. The suite still
passed, but went from ~80 seconds to just under 8 hours.

Resetting this state before every test keeps tests independent and fast.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_process_level_state():
    """Clear cross-test module state before each test."""
    import market.finnhub_bars as fh
    import market.price_check as pc
    import monitor.position_monitor as pm
    import trading.executor as ex

    # Finnhub: auth latch + sustained-outage counter (v21.9 / v21.10)
    fh._auth_ok = None
    fh._finnhub_consecutive_failures = 0
    fh._finnhub_outage_reported = False

    # T212 cash-balance cache (v21.11) — a value cached by one test would
    # otherwise be served to the next, hiding whatever _get mock it installed.
    ex._cash_cache = None

    # Frozen-feed tripwire (v21.10)
    pc._stale_quote_streak.clear()
    pc._stale_quote_reported.clear()

    # MFE/MAE excursion cache (v21.10)
    pm._excursion_seen.clear()

    yield
