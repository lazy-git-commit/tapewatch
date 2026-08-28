# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _no_real_db_observability():
    """
    Stub the two best-effort observability writers for every test.

    Both are called from deep inside the news path and both open a real
    connection. With no database reachable, `get_conn()` retries with backoff —
    which is how the v21.10 tripwires once turned an 80-second suite into an
    8-hour one (see the module docstring).

    v21.14 reintroduced the same hazard from a new direction:
    `record_classifier_call()` fires on EVERY Claude call, so every scoring test
    paid three connection retries. That alone took the suite from 7m52s to
    17m37s, and — because `patch("news.fetcher.time.sleep")` patches the shared
    `time` module object rather than a fetcher-local name — the DB's retry
    sleeps were also counted by tests asserting on backoff behaviour, failing
    them for the wrong reason.

    The shadow dispatch is stubbed for a different and worse reason: NETWORK,
    not latency. `_batch_score_sentiment` fires `shadow_score()` before the
    Claude cooldown check, so every scoring test — including the cooldown tests
    that used to return before any dispatch — would submit a real, billable
    request to Alibaba Model Studio on a fire-and-forget thread that outlives
    the test, on any machine with QWEN_* in its `.env` (which is exactly the
    machine used to populate the deploy secrets). `cfg` is read at import, so
    `shadow_enabled()` is True there. Patching the name as bound in
    `news.fetcher` leaves `news.shadow_classifier` itself fully testable.

    Tests that want to assert on these calls patch them locally, which takes
    precedence over this fixture.
    """
    with patch("storage.database.record_classifier_call"), \
         patch("storage.database.save_qwen_scores", return_value=0), \
         patch("news.fetcher.shadow_score"):
        yield


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

    # Frozen-feed tripwire (v21.10; distinct-symbol set added v21.12)
    pc._stale_quote_streak.clear()
    pc._stale_quote_symbols.clear()
    pc._stale_quote_reported.clear()

    # MFE/MAE excursion cache (v21.10)
    pm._excursion_seen.clear()

    # Claude empty-batch streak + cooldown (v21.13) — a streak left by one test
    # would push an unrelated later test into a cooldown it never triggered.
    import news.fetcher as nf
    nf._consecutive_empty_batches = 0
    nf._claude_cooldown = None

    # Shadow-mode module state (v21.14). _pending is decremented on a background
    # thread, so a test that leaves a job in flight can strand the counter and
    # make a later test silently DROP its batch instead of dispatching it.
    import news.shadow_classifier as sc
    sc._pending = 0
    sc._dropped.clear()
    sc._client = None
    sc._unavailable_logged = False

    # Slow-path throttle (v21.18). news_cycle() runs the re-eval queue, retry
    # queue and both pre-market steps at most once per 60s regardless of the
    # poll interval. That timer is process-lifetime state, so the FIRST test to
    # drive news_cycle() consumes the slot and every later test silently gets
    # the throttled path — which is how a pre-market batch test started
    # asserting against an evaluation that never ran.
    import main as _main
    _main._slow_path_last_run.clear()

    yield
