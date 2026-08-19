"""
v21.18 — configurable news poll cadence, and the throttle that makes it safe.

The change itself is one line (an IntervalTrigger reading a config value). The
part worth testing is everything that stops a 6x faster cycle from becoming a
6x more expensive one:

  * Twelvedata's Grow plan allows 55 calls/minute and the bucket FAILS CLOSED —
    once empty, confirm_price_signal() returns None and NOTHING can be
    confirmed. The re-eval queue re-confirms every parked signal each cycle, so
    at a 10s cadence a dozen parked signals would starve the bucket and
    silently stop us trading. That is strictly worse than the ~25s of latency
    the change buys.
  * Two overlapping news cycles would race the re-eval and retry queues (plain
    dicts, not thread-safe) and could double-enter a signal.

So: only the FETCH runs fast; every periodic sub-step keeps ~60s spacing, and
the scheduler is explicitly forbidden from running two cycles at once.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestPollCadenceIsConfigurable:

    def test_default_is_the_historical_sixty_seconds(self):
        # A missing env var must not silently change trading behaviour.
        import os
        from config.settings import Settings
        old = os.environ.pop("NEWS_CYCLE_SECONDS", None)
        try:
            assert Settings().news_cycle_seconds == 60
        finally:
            if old is not None:
                os.environ["NEWS_CYCLE_SECONDS"] = old

    def test_env_var_overrides_it(self):
        import os
        from config.settings import Settings
        old = os.environ.get("NEWS_CYCLE_SECONDS")
        os.environ["NEWS_CYCLE_SECONDS"] = "10"
        try:
            assert Settings().news_cycle_seconds == 10
        finally:
            if old is None:
                os.environ.pop("NEWS_CYCLE_SECONDS", None)
            else:
                os.environ["NEWS_CYCLE_SECONDS"] = old

    @staticmethod
    def _keys():
        return dict(trading212_demo_api_key="k", trading212_demo_api_key_id="k",
                    trading212_api_key="k", trading212_api_key_id="k",
                    benzinga_api_key="k", finnhub_api_key="k",
                    twelvedata_api_key="k", anthropic_api_key="k")

    @pytest.mark.parametrize("seconds", [0, 1, 4, 301, 3600])
    def test_out_of_range_cadence_is_refused_at_startup(self, seconds):
        # Below ~5s we would spend the day skipping runs (measured cycles reach
        # 12s); above 300s the 3-minute freshness filter discards articles
        # before we ever look at them.
        from config.settings import cfg
        with patch.multiple(cfg, news_cycle_seconds=seconds, **self._keys()):
            with pytest.raises(ValueError, match="NEWS_CYCLE_SECONDS"):
                cfg.validate()

    @pytest.mark.parametrize("seconds", [5, 10, 60, 300])
    def test_sane_cadences_are_accepted(self, seconds):
        from config.settings import cfg
        with patch.multiple(cfg, news_cycle_seconds=seconds, **self._keys()):
            cfg.validate()


class TestSlowPathThrottle:
    """Only news DISCOVERY benefits from a fast cadence."""

    def setup_method(self):
        import main
        main._slow_path_last_run.clear()

    teardown_method = setup_method

    def test_first_call_runs_then_immediately_throttles(self):
        import main
        assert main._slow_path_due("reeval") is True
        assert main._slow_path_due("reeval") is False
        assert main._slow_path_due("reeval") is False

    def test_runs_again_once_the_interval_has_passed(self):
        import main
        assert main._slow_path_due("reeval") is True
        # Simulate 61s of monotonic time passing.
        main._slow_path_last_run["reeval"] -= 61.0
        assert main._slow_path_due("reeval") is True

    def test_steps_are_throttled_independently(self):
        import main
        assert main._slow_path_due("reeval") is True
        assert main._slow_path_due("retry") is True
        assert main._slow_path_due("premarket_eval") is True
        assert main._slow_path_due("reeval") is False

    def test_six_fast_cycles_produce_exactly_one_slow_run(self):
        # The whole point: at a 10s cadence, six cycles pass in one minute and
        # the expensive work must happen once, not six times.
        import main
        ran = sum(1 for _ in range(6) if main._slow_path_due("reeval"))
        assert ran == 1

    def test_uses_monotonic_time_not_the_wall_clock(self):
        # An NTP correction or a DST shift must not make a step look overdue by
        # hours, nor freeze it forever.
        import main
        with patch.object(main.time, "monotonic", return_value=1000.0):
            assert main._slow_path_due("x") is True
            assert main._slow_path_due("x") is False
        with patch.object(main.time, "monotonic", return_value=1061.0):
            assert main._slow_path_due("x") is True


class TestCycleWiring:
    """The throttle is worthless if the cycle stops consulting it."""

    def test_the_expensive_steps_are_gated(self):
        import inspect
        import main
        src = inspect.getsource(main.news_cycle)
        for step, guard in (
            ("_process_reeval_queue()", '_slow_path_due("reeval")'),
            ("_drain_retry_queue()", '_slow_path_due("retry")'),
            ("evaluate_premarket_candidates()", '_slow_path_due("premarket_eval")'),
            ("premarket_scan()", '_slow_path_due("premarket_scan")'),
        ):
            assert step in src, f"{step} vanished from news_cycle"
            assert guard in src, (
                f"{step} is no longer throttled by {guard} — at a 10s cadence "
                f"this multiplies Twelvedata credit use six-fold against a "
                f"fail-closed 55/min bucket"
            )

    def test_the_fetch_itself_is_NOT_throttled(self):
        # Throttling the fetch would defeat the entire change.
        import inspect
        import main
        src = inspect.getsource(main.news_cycle)
        line = next(l for l in src.splitlines() if "fetch_all_news(" in l)
        assert "_slow_path_due" not in line, (
            "the news fetch must run at the FAST cadence — it is the only part "
            "of the cycle that latency reduction actually buys"
        )


class TestSchedulerSafety:

    def test_news_job_uses_the_configured_interval_and_cannot_overlap(self):
        import main
        from config.settings import cfg
        jobs = {}

        class _Sched:
            def __init__(self, *a, **k): pass
            def add_job(self, func, trigger=None, id=None, **kw):
                jobs[id] = {"trigger": trigger, **kw}
            def start(self): pass

        with patch.object(main, "BackgroundScheduler", _Sched), \
             patch.multiple(cfg, news_cycle_seconds=10, monitor_interval_seconds=5), \
             patch.object(main, "generate_report"), \
             patch.object(main, "compute_forward_returns"), \
             patch.object(main, "build_symbol_map"):
            main.setup_scheduler()

        news = jobs["news_cycle"]
        assert news["trigger"].interval.total_seconds() == 10
        # Concurrent cycles would race the re-eval/retry dicts and could
        # double-enter a signal. Skipping the overlap is the only safe answer.
        assert news["max_instances"] == 1
        assert news["coalesce"] is True
        # Grace must not exceed the interval, or a skipped run fires late into
        # the next one.
        assert news["misfire_grace_time"] <= max(15, 10)
