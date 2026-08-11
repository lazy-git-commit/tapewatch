"""
tests/test_review_v21_14_1.py
─────────────────────────────
Regressions for the defects the `/code-review max` pass found in v21.14.

Every one of these was SILENT: the code ran, wrote rows, and produced a report.
What was wrong was the content of that report, or a failure mode on a path
nobody exercises until the moment it matters. That is exactly the class of bug
a test suite has to hold down, because nothing else will notice it.
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import anthropic
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# The comparison tool read every column as its own name
# ─────────────────────────────────────────────────────────────────────────────
class TestRowsDecoding:

    def test_rows_returns_values_not_column_names(self):
        """
        get_conn() sets cursor_factory = RealDictCursor, so fetchall() already
        yields dict-like rows. The old `dict(zip(cols, row))` iterated each
        row's KEYS, mapping every column to its own NAME — so the liveness loop
        compared the string "provider" against "claude", matched nothing, and
        printed "(no data yet)" no matter how much data had been collected.
        The prediction panel then did float("fwd_return_5m") and crashed.
        """
        import analysis.classifier_compare as cc

        row = {"provider": "claude", "latency_ms": 1234, "ok": True}
        cur = MagicMock()
        cur.fetchall.return_value = [row]
        cur.description = [("provider",), ("latency_ms",), ("ok",)]
        cur.__enter__ = lambda s: s
        cur.__exit__ = lambda s, *a: False
        conn = MagicMock()
        conn.cursor.return_value = cur

        @contextmanager
        def _fake_conn():
            yield conn

        with patch.object(cc, "get_conn", _fake_conn):
            out = cc._rows("SELECT 1")

        assert out == [{"provider": "claude", "latency_ms": 1234, "ok": True}]
        assert out[0]["provider"] == "claude"


# ─────────────────────────────────────────────────────────────────────────────
# Claude's failures have to be IN the liveness record
# ─────────────────────────────────────────────────────────────────────────────
class TestClaudeLivenessIsRecorded:

    def _article(self):
        return {"id": "1", "headline": "h", "teaser": "t", "ticker": "T_US_EQ"}

    def test_cooldown_is_recorded_as_a_liveness_failure(self):
        """
        A suppressed cycle is a cycle in which Claude gave no answer. Writing
        no row made a multi-cycle outage render as success_rate=100% and
        worst_failure_streak=0 — the report would hide precisely what it
        exists to show, and the docstring tells the reader to weigh that
        streak above everything else.
        """
        import news.fetcher as f
        with patch.object(f, "_claude_available", return_value=False), \
             patch.object(f, "_record_claude_failure") as rec:
            assert f._batch_score_sentiment([self._article()]) == {}
        assert rec.call_count == 1
        assert rec.call_args.args[1] == "cooldown_suppressed"

    @pytest.mark.parametrize("exc_cls,status,expected", [
        (anthropic.AuthenticationError, 401, "401_auth"),
        (anthropic.RateLimitError, 429, "429_rate_limit"),
    ])
    def test_api_failures_are_recorded(self, exc_cls, status, expected):
        import news.fetcher as f
        response = MagicMock(status_code=status, headers={})
        err = exc_cls("boom", response=response, body=None)
        with patch.object(f, "_claude_available", return_value=True), \
             patch.object(f, "_claude") as claude, \
             patch.object(f, "_enter_claude_cooldown"), \
             patch.object(f, "_record_claude_event"), \
             patch.object(f, "_record_claude_failure") as rec:
            claude.messages.create.side_effect = err
            assert f._batch_score_sentiment([self._article()]) == {}
        assert rec.call_args.args[1] == expected

    def test_unknown_exception_is_recorded(self):
        import news.fetcher as f
        with patch.object(f, "_claude_available", return_value=True), \
             patch.object(f, "_claude") as claude, \
             patch.object(f, "_record_claude_failure") as rec:
            claude.messages.create.side_effect = ValueError("weird")
            assert f._batch_score_sentiment([self._article()]) == {}
        assert rec.call_args.args[1] == "ValueError"

    def test_suppressed_cycle_reports_no_latency(self):
        """
        No HTTP call was made, so there is no latency. Recording 0 would drag
        the p50/p95 that decides whether a provider fits the 60s cycle.
        """
        import news.fetcher as f
        with patch.object(f, "_claude_available", return_value=False), \
             patch("storage.database.record_classifier_call") as db:
            f._batch_score_sentiment([self._article()])
        assert db.call_args.args[3] is None      # latency_ms
        assert db.call_args.kwargs["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# A replay must not write into production observability tables
# ─────────────────────────────────────────────────────────────────────────────
class TestReplayDoesNotPolluteProduction:

    def _article(self):
        return {"id": "1", "headline": "h", "teaser": "t", "ticker": "T_US_EQ"}

    def test_replay_neither_shadows_nor_records(self):
        """
        qwen_scores is UNIQUE per article with ON CONFLICT DO NOTHING, so a
        replayed row would PERMANENTLY block the real one for that article,
        and replay latency over 20-article batches would pollute the p50/p95
        and push min(calls) past the readiness threshold.
        """
        import news.fetcher as f
        with patch.object(f, "_claude_available", return_value=False), \
             patch.object(f, "shadow_score") as shadow, \
             patch("storage.database.record_classifier_call") as db:
            f._batch_score_sentiment([self._article()], live=False)
        shadow.assert_not_called()
        db.assert_not_called()

    def test_live_is_the_default(self):
        import news.fetcher as f
        with patch.object(f, "_claude_available", return_value=False), \
             patch.object(f, "shadow_score") as shadow, \
             patch("storage.database.record_classifier_call") as db:
            f._batch_score_sentiment([self._article()])
        shadow.assert_called_once()
        db.assert_called_once()

    def test_backtest_passes_live_false_and_a_ticker(self):
        """Guards the actual caller, not just the parameter."""
        import inspect
        import backtest.backtest as bt
        src = inspect.getsource(bt.run_backtest)
        assert "_batch_score_sentiment(chunk, live=False)" in src
        assert '"ticker"' in src


# ─────────────────────────────────────────────────────────────────────────────
# A NaN fill price silently disarms the stop loss
# ─────────────────────────────────────────────────────────────────────────────
class TestFillPriceIsFinite:

    @pytest.mark.parametrize("bad", ["NaN", "nan", "Infinity", "-Infinity",
                                     "0", "-1", float("nan"), float("inf")])
    def test_unusable_fill_price_falls_back_to_signal_price(self, bad):
        """
        float("NaN") raises nothing, so a NaN price passed straight through.
        It then makes stop_price NaN — the broker rejects the resting stop and
        the position has NO stop — and every downstream comparison
        (current <= stop, current >= buy*1.05, the MFE/MAE band, the executor's
        own abs(slippage) > 3.0 check) is False against NaN, so nothing else
        catches it. The position could then only exit on the time-stop or the
        EOD flatten. Returning None routes to the known-safe signal-price path.
        """
        from trading.executor import _parse_fill
        price, _, _, _ = _parse_fill({"price": bad, "walletImpact": {}})
        assert price is None

    def test_a_good_fill_price_still_parses(self):
        from trading.executor import _parse_fill
        price, net, fx, fees = _parse_fill(
            {"price": "12.34",
             "walletImpact": {"netValue": "-100.5", "fxRate": "1.27"}})
        assert price == 12.34
        assert net == -100.5
        assert fx == 1.27


# ─────────────────────────────────────────────────────────────────────────────
# would_trade must mirror the live gates EXACTLY
# ─────────────────────────────────────────────────────────────────────────────
class TestWouldTradeMatchesLiveGates:

    @contextmanager
    def _gates(self, conf=7, mag=2):
        from config.settings import cfg
        with patch.object(cfg, "min_sentiment_confidence", conf), \
             patch.object(cfg, "min_catalyst_magnitude", mag):
            yield

    def test_magnitude_floor_is_applied(self):
        """Gate 4 is a real gate; omitting it counted signals we never take."""
        import analysis.classifier_compare as cc
        cat = cc._TRADEABLE[0]
        with self._gates(mag=2):
            assert cc._would_trade("positive", 0.9, cat, False, 3) is True
            assert cc._would_trade("positive", 0.9, cat, False, 1) is False
            assert cc._would_trade("positive", 0.9, cat, False, None) is False

    def test_confidence_is_rounded_like_production(self):
        """
        Live: round(conf * 10) >= threshold. A naive conf >= threshold/10
        disagrees across the whole [0.65, 0.70) band — production rounds 0.68
        to 7 and TRADES, the old comparison silently dropped those rows.
        """
        import analysis.classifier_compare as cc
        cat = cc._TRADEABLE[0]
        with self._gates(mag=1):
            assert cc._would_trade("positive", 0.68, cat, False, 3) is True
            assert cc._would_trade("positive", 0.64, cat, False, 3) is False

    def test_sentiment_catalyst_and_already_moved(self):
        import analysis.classifier_compare as cc
        cat = cc._TRADEABLE[0]
        with self._gates(mag=1):
            assert cc._would_trade("negative", 0.9, cat, False, 3) is False
            assert cc._would_trade("positive", 0.9, cat, True, 3) is False
            assert cc._would_trade("positive", 0.9, "other", False, 3) is False
            # Live lowercases before comparing, so this must not be a miss.
            assert cc._would_trade("Positive", 0.9, cat, False, 3) is True

    def test_garbage_never_counts_as_tradeable(self):
        import analysis.classifier_compare as cc
        cat = cc._TRADEABLE[0]
        with self._gates(mag=1):
            assert cc._would_trade(None, None, cat, False, 3) is False
            assert cc._would_trade("positive", "high", cat, False, 3) is False
            assert cc._would_trade("positive", 0.9, cat, False, "big") is False

    def test_multi_ticker_articles_prefer_a_measured_leg(self):
        """
        forward_returns measures per (article, ticker) ROW and yfinance often
        has no bars for one leg. Picking alphabetically could keep an all-NULL
        leg and silence an article that DID have a measured outcome, thinning
        the panel the report calls THE DECIDING METRIC.
        """
        import inspect
        import analysis.classifier_compare as cc
        sql = inspect.getsource(cc.prediction)
        assert "(s.fwd_return_60m IS NULL)" in sql
        assert "DISTINCT ON (s.article_id)" in sql


# ─────────────────────────────────────────────────────────────────────────────
# Shadow: parity with the live validator, and honest failure rows
# ─────────────────────────────────────────────────────────────────────────────
class TestShadowParityAndFailureRecording:

    @contextmanager
    def _enabled(self):
        from config.settings import cfg
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"):
            yield

    def _resp(self, payload, finish="stop"):
        call = MagicMock()
        call.function.arguments = (payload if isinstance(payload, str)
                                   else json.dumps(payload))
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[call]),
                                  finish_reason=finish)]
        resp.usage = None
        client = MagicMock()
        client.chat.completions.create.return_value = resp
        return client

    def test_validation_matches_the_live_path(self):
        """
        Divergence removes Qwen's answers from the INNER JOIN in prediction(),
        which drops its WORST calls from the paired sample and flatters the
        challenger with pure survivorship bias. Each case below used to
        diverge from fetcher.py, and in the opposite direction.
        """
        import news.shadow_classifier as sc
        client = self._resp({"classifications": [
            # Uppercase enum: live lowercases it; shadow hard-rejected.
            {"id": "0", "sentiment": "Positive", "confidence": 0.8,
             "catalyst_type": "guidance_raise", "catalyst_magnitude": 3},
            # Missing confidence: live defaults to 0.5; shadow dropped the row.
            {"id": "1", "sentiment": "positive",
             "catalyst_type": "guidance_raise", "catalyst_magnitude": 3},
            # JSON float magnitude: live int()s it; shadow NULLed it because
            # isinstance(3.0, int) is False.
            {"id": "2", "sentiment": "positive", "confidence": 0.8,
             "catalyst_type": "guidance_raise", "catalyst_magnitude": 3.0},
        ]})
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=3) as save, \
             patch("storage.database.record_classifier_call"):
            sc._run([{"id": str(i), "ticker": f"T{i}", "headline": "h"}
                     for i in range(3)], "msg")

        rows = {r["article_id"]: r for r in save.call_args.args[0]}
        assert len(rows) == 3
        assert rows["0"]["sentiment"] == "positive"     # lowercased, not dropped
        assert rows["1"]["confidence"] == 0.5           # defaulted, not dropped
        assert rows["2"]["catalyst_magnitude"] == 3     # int(3.0), not NULL

    def test_out_of_range_still_rejected_not_clamped(self):
        """
        The rejection rule is unchanged — but a batch the model DID answer is
        not an outage. Filing it as empty_batch made a taxonomy near-miss
        indistinguishable from a dead API in the liveness column, while Claude
        emitting the identical answers records ok=true.
        """
        import news.shadow_classifier as sc
        client = self._resp({"classifications": [
            {"id": "0", "sentiment": "positive", "confidence": 8.0,
             "catalyst_type": "guidance_raise", "catalyst_magnitude": 3},
            {"id": "1", "sentiment": "positive", "confidence": 0.8,
             "catalyst_type": "not_a_catalyst", "catalyst_magnitude": 3},
        ]})
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=0) as save, \
             patch("storage.database.record_classifier_call") as rec:
            sc._run([{"id": "0", "ticker": "T0", "headline": "h"},
                     {"id": "1", "ticker": "T1", "headline": "h"}], "msg")

        assert save.call_args.args[0] == []
        assert rec.call_args.kwargs["ok"] is True
        assert rec.call_args.kwargs["scored_count"] == 2
        assert rec.call_args.kwargs["error_type"] is None

    def test_a_genuinely_empty_batch_is_still_an_empty_batch(self):
        import news.shadow_classifier as sc
        client = self._resp({"classifications": []})
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=0), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run([{"id": "0", "ticker": "T0", "headline": "h"}], "msg")
        assert rec.call_args.kwargs["ok"] is False
        assert rec.call_args.kwargs["error_type"] == "empty_batch"

    def test_truncation_is_not_blamed_on_the_provider(self):
        """
        A completion cut off by the output cap fails to parse. Recording that
        as Qwen's own bad_json attributes OUR missing parameter to the
        provider, in the very liveness dataset this module exists to produce —
        and because batch size tracks news volume, the bias would concentrate
        on the busiest, most decision-relevant cycles.
        """
        import news.shadow_classifier as sc
        client = self._resp('{"classifications": [{"id": "0"', finish="length")
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run([{"id": "0", "ticker": "T0", "headline": "h"}], "msg")
        assert rec.call_args.kwargs["error_type"] == "truncated"

    def test_a_real_parse_failure_is_still_bad_json(self):
        import news.shadow_classifier as sc
        client = self._resp("not json at all", finish="stop")
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run([{"id": "0", "ticker": "T0", "headline": "h"}], "msg")
        assert rec.call_args.kwargs["error_type"] == "bad_json"

    def test_a_max_tokens_budget_is_sent(self):
        """The live Claude call sizes its budget; the shadow must match it."""
        import news.shadow_classifier as sc
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("stop here")
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.record_classifier_call"):
            sc._run([{"id": str(i), "ticker": "T", "headline": "h"}
                     for i in range(30)], "msg")
        sent = client.chat.completions.create.call_args.kwargs["max_tokens"]
        assert sent == max(400, 30 * 60 + 64)

    def test_dropped_batches_are_recorded_off_the_caller_thread(self):
        """
        A drop means the provider is degraded, and liveness is computed over
        the rows that EXIST — so leaving a gap excluded exactly those periods
        from Qwen's own denominator, reporting a high success rate and a zero
        failure streak for an hour of saturation.

        The row still must NOT be written inline: get_conn() retries three
        times with backoff, so a DB blip would block the news cycle — the
        coupling safety property 1 exists to prevent.
        """
        import news.shadow_classifier as sc
        with self._enabled(), \
             patch("storage.database.record_classifier_call") as rec:
            sc._pending = sc._MAX_PENDING
            with patch.object(sc, "_get_pool") as pool:
                sc.shadow_score([{"id": "1", "ticker": "T", "headline": "h"}], "m")
                pool.assert_not_called()
            rec.assert_not_called()              # nothing written inline
            assert len(sc._dropped) == 1         # buffered for the worker

            sc._pending = 0
            sc._flush_drops()                    # what the next job does
        assert rec.call_count == 1
        assert rec.call_args.kwargs["error_type"] == "dropped_backlog"
        assert sc._dropped == []

    def test_the_drop_buffer_is_bounded(self):
        """An unbounded hand-off buffer is the same memory leak by another name."""
        import news.shadow_classifier as sc
        with self._enabled():
            sc._pending = sc._MAX_PENDING
            with patch.object(sc, "_get_pool"):
                for _ in range(sc._MAX_DROPS_BUFFERED + 50):
                    sc.shadow_score([{"id": "1", "ticker": "T",
                                      "headline": "h"}], "m")
        assert len(sc._dropped) == sc._MAX_DROPS_BUFFERED

    def test_an_unusable_client_is_recorded(self):
        """
        A typo'd QWEN_BASE_URL latches one warning per process. With no row, a
        permanently dead client is indistinguishable from 'never enabled'.
        """
        import news.shadow_classifier as sc
        with self._enabled(), \
             patch.object(sc, "_get_client", return_value=None), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run([{"id": "1", "ticker": "T", "headline": "h"}], "msg")
        assert rec.call_args.kwargs["error_type"] == "client_unavailable"

    def test_shadow_still_returns_none_and_never_raises(self):
        """The contract that keeps this module unable to influence a trade."""
        import news.shadow_classifier as sc
        with self._enabled(), \
             patch.object(sc, "_get_pool", side_effect=RuntimeError("boom")):
            assert sc.shadow_score([{"id": "1", "ticker": "T",
                                     "headline": "h"}], "m") is None
        assert sc._pending == 0                  # counter released on failure


# ─────────────────────────────────────────────────────────────────────────────
# v21.14.2 — a stale minute bar is not "no coverage"
# ─────────────────────────────────────────────────────────────────────────────
class TestStaleBarsIsNotMissingCoverage:
    """
    2026-08-10: SRRK (Scholar Rock, fda_approval conf 0.75) and NVO were both
    blacklisted for the whole session with "no Finnhub/Twelvedata coverage"
    because the newest Twelvedata minute bar was 14.4 minutes old — while the
    SAME response carried usable session aggregates. Two of only four
    regular-hours tradeable-catalyst candidates that day, on liquid listings.

    A blackout strike asserts "no provider carries this instrument". A stale
    bar proves the opposite: the provider answered. This is the fourth version
    of that same confusion (v21.6 extended sessions, v21.11 frozen feeds,
    v21.12 the detector itself).
    """

    @staticmethod
    def _now_et():
        from datetime import datetime as _dt
        import pytz
        return pytz.timezone("America/New_York").localize(
            _dt(2026, 8, 10, 12, 4, 0))        # well past the 15-min open window

    def test_stale_bar_defers_instead_of_reporting_no_data(self):
        """
        The key assertion is `conf is not None`: returning None is what
        main._queue_retry counts a blacklist strike against.
        """
        from tests.test_core import _confirm_with, _mk_sa
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.0},
            _mk_sa(past_price=None),
        )
        assert conf is not None, "None here is read as 'no provider coverage'"
        assert conf.is_confirmed is False
        assert conf.reason_code == "stale_bars"

    def test_stale_bars_is_transient_so_it_never_blacklists(self):
        import main
        assert "stale_bars" in main._TRANSIENT_REJECT_CODES

    def test_a_real_data_outage_still_returns_none(self):
        """
        The guard must stay narrow: when NOTHING comes back, a strike is
        still the correct response — that is the EGGF/OXAC loop the blackout
        was built for.
        """
        import market.price_check as pc
        from unittest.mock import MagicMock as _MM
        fake_dt = _MM()
        fake_dt.now.side_effect = lambda tz=None: self._now_et()
        with patch.object(pc, "datetime", fake_dt), \
             patch.object(pc, "get_trading_session", return_value="regular"), \
             patch.object(pc, "get_quote_with_fallback", return_value=None):
            assert pc.confirm_price_signal("ACME_US_EQ") is None

    def test_early_session_still_falls_back_to_the_open(self):
        """Inside the first 15 min the open price is a fair baseline — unchanged."""
        from datetime import datetime as _dt
        import pytz
        from tests.test_core import _confirm_with, _mk_sa
        early = pytz.timezone("America/New_York").localize(
            _dt(2026, 8, 10, 9, 40, 0))
        conf, _ = _confirm_with(
            early, {"c": 10.5, "o": 10.0, "pc": 10.0},
            _mk_sa(past_price=None),
        )
        assert conf is not None
        assert conf.reason_code != "stale_bars"

    def test_a_failed_session_pull_still_returns_none(self):
        """
        The narrow half of the fix. `sa is None` means Twelvedata returned
        nothing — a genuine data failure, and the strike toward the no-quote
        blackout is correct. Only a pull that SUCCEEDED without a recent bar
        gets the transient treatment.
        """
        from tests.test_core import _confirm_with
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.28}, None)
        assert conf is None
