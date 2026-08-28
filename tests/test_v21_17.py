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
v21.17 — entry price ceiling, portfolio circuit breakers, and the measurement
tools that overturned v21.16's justification.

What shipped here is deliberately narrow. The 2026-08-19 triple-barrier study
found no measurable edge in any catalyst class, at any exit configuration, under
any quality filter — so every change that would have INCREASED exposure was
withheld and only cost reduction, downside protection, and measurement shipped.
The tests below are written against that intent:

  * the entry limit must refuse a bad price WITHOUT discarding the signal —
    a limit that turns a missed fill into a lost opportunity is worse than no
    limit at all;
  * the circuit breakers must fail OPEN on a data error (the 2026-07-31 lesson:
    an unavailable portfolio value silently halted 44 news cycles) while the
    daily kill switch continues to fail CLOSED;
  * the validation tools must be hostile to their own author — the deflated
    Sharpe ratio exists because this project shipped a change on the best of
    many variants without correcting for the search.
"""

import math
from unittest.mock import MagicMock, patch

import pytest


# ── Entry limit orders ────────────────────────────────────────────────────────

class TestEntryLimitOrder:
    """LAMR (2026-08-06): sized on $161.09, filled at $164.30, stopped 28s later."""

    @staticmethod
    def _exec_patched(post, **over):
        import trading.executor as ex
        from config.settings import cfg
        defaults = dict(entry_limit_enabled=True, entry_limit_slack_pct=0.4,
                        stop_loss_pct=2.0, extended_size_factor=0.5)
        defaults.update(over)
        return (patch.object(ex, "_post", post),
                patch.object(ex, "calculate_quantity", return_value=(3.0, None)),
                patch.multiple(cfg, **defaults))

    def test_regular_hours_entry_uses_a_limit_with_a_price_ceiling(self):
        import trading.executor as ex
        post = MagicMock(return_value={"id": "o1"})
        a, b, c = self._exec_patched(post)
        with a, b, c, patch.object(ex, "_fetch_fill", return_value={"fillPrice": 100.2}):
            ex.buy("ACME_US_EQ", 100.0)
        endpoint, payload = post.call_args[0]
        assert endpoint == "/equity/orders/limit"
        assert payload["limitPrice"] == pytest.approx(100.4)
        assert payload["timeValidity"] == "DAY"

    def test_extended_session_stays_on_market(self):
        # T212 rejects extendedHours on the limit endpoint, and out there we
        # want the fill more than the price.
        import trading.executor as ex
        post = MagicMock(return_value={"id": "o2"})
        a, b, c = self._exec_patched(post)
        with a, b, c, patch.object(ex, "_fetch_fill", return_value={"fillPrice": 100.2}):
            ex.buy("ACME_US_EQ", 100.0, extended=True)
        endpoint, payload = post.call_args[0]
        assert endpoint == "/equity/orders/market"
        assert "limitPrice" not in payload
        assert payload["extendedHours"] is True

    def test_disabled_falls_back_to_market(self):
        import trading.executor as ex
        post = MagicMock(return_value={"id": "o3"})
        a, b, c = self._exec_patched(post, entry_limit_enabled=False)
        with a, b, c, patch.object(ex, "_fetch_fill", return_value={"fillPrice": 100.2}):
            ex.buy("ACME_US_EQ", 100.0)
        assert post.call_args[0][0] == "/equity/orders/market"

    def test_unfilled_limit_is_cancelled_and_flagged_retriable(self):
        # The whole point: we own nothing, we lost nothing, and the caller must
        # be able to tell this apart from a real failure.
        import trading.executor as ex
        post = MagicMock(return_value={"id": "o4"})
        a, b, c = self._exec_patched(post)
        with a, b, c, \
             patch.object(ex, "_fetch_fill", return_value=None), \
             patch.object(ex, "get_order_status", return_value="NEW"), \
             patch.object(ex, "cancel_order", return_value=True) as cancel:
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is False
        assert res.unfilled is True
        cancel.assert_called_once()

    def test_unfilled_EXTENDED_order_is_not_retriable(self):
        # An extended queue means the instrument cannot trade now at ANY price;
        # a limit miss means it cannot trade at OUR price. Only the second is
        # worth re-queueing, or we would spin on an untradeable instrument.
        import trading.executor as ex
        post = MagicMock(return_value={"id": "o5"})
        a, b, c = self._exec_patched(post)
        with a, b, c, \
             patch.object(ex, "_fetch_fill", return_value=None), \
             patch.object(ex, "get_order_status", return_value="NEW"), \
             patch.object(ex, "cancel_order", return_value=True):
            res = ex.buy("ACME_US_EQ", 100.0, extended=True)
        assert res.success is False
        assert res.unfilled is False

    def test_precision_retry_still_works_on_the_limit_endpoint(self):
        # The quantity-precision retry cost production six confirmed entries
        # before it was fixed (2026-05-28→06-05). Routing entries through a
        # different endpoint must not quietly bypass it.
        import trading.executor as ex
        from trading.executor import T212HTTPError
        err = T212HTTPError(
            400,
            '{"type":"/api-errors/quantity-precision-mismatch",'
            '"detail":"invalid quantity precision 2"}')
        post = MagicMock(side_effect=[err, {"id": "o9"}])
        a, b, c = self._exec_patched(post)
        with a, b, c, patch.object(ex, "_fetch_fill", return_value={"fillPrice": 100.2}):
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is True
        assert post.call_count == 2
        # BOTH attempts must go to the limit endpoint, and the retry must keep
        # the price ceiling — a retry that fell back to market would reintroduce
        # exactly the unbounded fill this release exists to prevent.
        for call in post.call_args_list:
            assert call[0][0] == "/equity/orders/limit"
            assert call[0][1]["limitPrice"] == pytest.approx(100.4)

    def test_slack_wider_than_the_stop_is_refused_at_startup(self):
        # An entry allowed further above our decision price than the stop is
        # wide starts the trade inside its own stop distance — the LAMR shape.
        from config.settings import cfg
        keys = dict(trading212_demo_api_key="k", trading212_demo_api_key_id="k",
                    trading212_api_key="k", trading212_api_key_id="k",
                    benzinga_api_key="k", finnhub_api_key="k",
                    twelvedata_api_key="k", anthropic_api_key="k")
        with patch.multiple(cfg, entry_limit_slack_pct=2.5, stop_loss_pct=2.0, **keys):
            with pytest.raises(ValueError, match="ENTRY_LIMIT_SLACK_PCT"):
                cfg.validate()
        with patch.multiple(cfg, entry_limit_slack_pct=0.4, stop_loss_pct=2.0, **keys):
            cfg.validate()


class TestUnfilledLimitRequeues:
    """A refused price must cost us the fill, never the idea."""

    def test_unfilled_buy_parks_the_signal_instead_of_killing_it(self):
        import main
        result = MagicMock(success=False, unfilled=True, quantity=0,
                           order_id=None, price=100.0, error="limit not reached")
        item = MagicMock(ticker="ACME", catalyst_type="guidance_raise",
                         article_id="a1")
        conf = MagicMock(session="regular", current_price=100.0,
                         recent_move_pct=0.5, reason="ok", momentum_skipped=False)
        with patch.object(main, "buy", return_value=result), \
             patch.object(main, "set_rejection_reason") as rej, \
             patch.object(main, "_queue_reeval") as requeue:
            opened = main._enter_confirmed(item, conf, signal_id=5, signal_price=99.0)
        assert opened is False
        requeue.assert_called_once()
        assert rej.call_args[0][2] == "entry_limit_unfilled"

    def test_a_genuine_buy_failure_still_terminates(self):
        import main
        result = MagicMock(success=False, unfilled=False, quantity=0,
                           order_id=None, price=100.0, error="insufficient funds")
        item = MagicMock(ticker="ACME", catalyst_type="guidance_raise", article_id="a2")
        conf = MagicMock(session="regular", current_price=100.0,
                         recent_move_pct=0.5, reason="ok", momentum_skipped=False)
        with patch.object(main, "buy", return_value=result), \
             patch.object(main, "set_rejection_reason") as rej, \
             patch.object(main, "_queue_reeval") as requeue:
            main._enter_confirmed(item, conf, signal_id=6)
        requeue.assert_not_called()
        assert rej.call_args[0][2] == "buy_failed"


# ── Portfolio circuit breakers ────────────────────────────────────────────────

class TestDrawdownBreaker:
    """MAX_DAILY_LOSS_PCT resets at midnight and cannot see a slow bleed."""

    @staticmethod
    def _gates(dd, **over):
        import main
        from config.settings import cfg
        d = dict(max_drawdown_pct=10.0, drawdown_lookback_days=30,
                 loss_streak_halt=0, max_open_positions=8, max_trades_per_day=10)
        d.update(over)
        return (patch.object(main, "get_today_realized_pnl", return_value=0.0),
                patch.object(main, "count_open_trades", return_value=0),
                patch.object(main, "count_trades_today", return_value=0),
                patch.object(main, "get_drawdown_from_peak", **dd),
                patch.multiple(cfg, **d))

    def test_breaker_trips_past_the_limit(self):
        import main
        a, b, c, d, e = self._gates({"return_value": (900.0, 1000.0, -10.0)})
        with a, b, c, d, e:
            ok, reason = main._risk_gates_pass()
        assert ok is False and "DRAWDOWN BREAKER" in reason

    def test_inside_the_limit_passes(self):
        import main
        a, b, c, d, e = self._gates({"return_value": (960.0, 1000.0, -4.0)})
        with a, b, c, d, e:
            ok, _ = main._risk_gates_pass()
        assert ok is True

    def test_fails_OPEN_when_the_snapshot_series_is_unavailable(self):
        # The 2026-07-31 lesson: an unavailable portfolio value must not
        # silently stand the system down. Only the kill switch fails closed.
        import main
        a, b, c, d, e = self._gates({"side_effect": RuntimeError("db down")})
        with a, b, c, d, e:
            ok, _ = main._risk_gates_pass()
        assert ok is True

    def test_none_result_does_not_trip_it(self):
        import main
        a, b, c, d, e = self._gates({"return_value": None})
        with a, b, c, d, e:
            ok, _ = main._risk_gates_pass()
        assert ok is True

    def test_disabled_by_zero(self):
        import main
        a, b, c, d, e = self._gates({"return_value": (100.0, 1000.0, -90.0)},
                                    max_drawdown_pct=0)
        with a, b, c, d, e:
            ok, _ = main._risk_gates_pass()
        assert ok is True


class TestLossStreakCooldown:

    def test_streak_counts_only_consecutive_losers(self):
        import storage.database as db

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self):
                return [{"profit_loss_pct": -1.0, "sell_time": "2026-08-19T14:00:00+01:00"},
                        {"profit_loss_pct": -2.0, "sell_time": "2026-08-19T13:00:00+01:00"},
                        {"profit_loss_pct": +3.0, "sell_time": "2026-08-19T12:00:00+01:00"},
                        {"profit_loss_pct": -1.0, "sell_time": "2026-08-19T11:00:00+01:00"}]

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()

        with patch.object(db, "get_conn", return_value=_Conn()):
            streak, last = db.get_loss_streak()
        assert streak == 2, "must stop counting at the winner"
        assert last == "2026-08-19T14:00:00+01:00"

    def test_unknown_pnl_stops_the_count_rather_than_guessing(self):
        import storage.database as db

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k): pass
            def fetchall(self):
                return [{"profit_loss_pct": -1.0, "sell_time": "2026-08-19T14:00:00+01:00"},
                        {"profit_loss_pct": None, "sell_time": "2026-08-19T13:00:00+01:00"},
                        {"profit_loss_pct": -5.0, "sell_time": "2026-08-19T12:00:00+01:00"}]

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()

        with patch.object(db, "get_conn", return_value=_Conn()):
            streak, _ = db.get_loss_streak()
        assert streak == 1

    def test_cooldown_blocks_then_expires(self):
        import main
        from config.settings import cfg
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        stale = (datetime.now(timezone.utc) - timedelta(minutes=200)).isoformat()
        common = dict(max_drawdown_pct=0, loss_streak_halt=4,
                      loss_streak_cooldown_minutes=90,
                      max_open_positions=8, max_trades_per_day=10)
        for sell_time, expected_ok in ((recent, False), (stale, True)):
            with patch.object(main, "get_today_realized_pnl", return_value=0.0), \
                 patch.object(main, "count_open_trades", return_value=0), \
                 patch.object(main, "count_trades_today", return_value=0), \
                 patch.object(main, "get_loss_streak", return_value=(4, sell_time)), \
                 patch.multiple(cfg, **common):
                ok, reason = main._risk_gates_pass()
            assert ok is expected_ok, reason

    def test_streak_below_the_threshold_does_not_block(self):
        import main
        from config.settings import cfg
        from datetime import datetime, timezone
        with patch.object(main, "get_today_realized_pnl", return_value=0.0), \
             patch.object(main, "count_open_trades", return_value=0), \
             patch.object(main, "count_trades_today", return_value=0), \
             patch.object(main, "get_loss_streak",
                          return_value=(3, datetime.now(timezone.utc).isoformat())), \
             patch.multiple(cfg, max_drawdown_pct=0, loss_streak_halt=4,
                            loss_streak_cooldown_minutes=90,
                            max_open_positions=8, max_trades_per_day=10):
            ok, _ = main._risk_gates_pass()
        assert ok is True


# ── Triple-barrier labelling ──────────────────────────────────────────────────

class TestTripleBarrier:
    """The labeller that overturned the v21.16 justification. It must be
    pessimistic in exactly the ways claimed, or its verdict means nothing."""

    @staticmethod
    def _bars(rows):
        pd = pytest.importorskip("pandas")
        idx = pd.date_range("2026-08-19 10:00", periods=len(rows),
                            freq="1min", tz="America/New_York")
        return pd.DataFrame(rows, index=idx)

    def test_same_bar_touching_both_barriers_is_scored_as_the_stop(self):
        # We cannot see intrabar order. Assuming the target would flatter every
        # volatile signal — precisely the signals most likely to stop out.
        from analysis.triple_barrier import label_one
        bars = self._bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 106, "Low": 97, "Close": 103.0},
        ])
        res = label_one(bars, bars.index[0], tp_pct=5, sl_pct=2,
                        hold_minutes=60, cost_pct=0.0)
        assert res["exit_reason"] == "stop_loss"
        assert res["label"] == -1

    def test_the_entry_bar_cannot_trigger_its_own_exit(self):
        # We enter at the entry bar's CLOSE, so its high and low already
        # happened. Counting them would fabricate instant exits.
        from analysis.triple_barrier import label_one
        bars = self._bars([
            {"Open": 100, "High": 120, "Low": 80, "Close": 100.0},
            {"Open": 100, "High": 100.5, "Low": 99.5, "Close": 100.0},
        ])
        res = label_one(bars, bars.index[0], tp_pct=5, sl_pct=2,
                        hold_minutes=60, cost_pct=0.0)
        assert res["exit_reason"] == "time_stop"

    def test_costs_are_charged_to_every_completed_trade(self):
        from analysis.triple_barrier import label_one
        bars = self._bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 106, "Low": 100, "Close": 105.0},
        ])
        res = label_one(bars, bars.index[0], tp_pct=5, sl_pct=2,
                        hold_minutes=60, cost_pct=0.46)
        assert res["exit_reason"] == "take_profit"
        assert res["gross_pct"] == pytest.approx(5.0)
        assert res["net_pct"] == pytest.approx(4.54)

    def test_time_stop_when_neither_barrier_is_touched(self):
        from analysis.triple_barrier import label_one
        bars = self._bars([{"Open": 100, "High": 100.2, "Low": 99.8, "Close": 100.0}
                           for _ in range(5)])
        res = label_one(bars, bars.index[0], tp_pct=5, sl_pct=2,
                        hold_minutes=3, cost_pct=0.0)
        assert res["exit_reason"] == "time_stop" and res["label"] == 0


# ── Validation guards ─────────────────────────────────────────────────────────

class TestDeflatedSharpe:
    """The guard whose absence let a search result ship as a finding."""

    def test_more_variants_raise_the_bar(self):
        from analysis.validation import expected_max_sharpe
        bars = [expected_max_sharpe(k) for k in (2, 10, 50, 200)]
        assert bars == sorted(bars), "trying more variants must raise the bar"
        assert expected_max_sharpe(1) == 0.0

    def test_a_search_winner_with_no_real_edge_is_rejected(self):
        from analysis.validation import deflated_sharpe_ratio
        rets = [0.05, -0.04, 0.06, -0.05, 0.04, -0.03, 0.05, -0.04] * 12
        d = deflated_sharpe_ratio(rets, n_trials=400)
        assert d["dsr"] < 0.5
        assert "random search" in d["verdict"]

    def test_a_strong_single_hypothesis_survives(self):
        # Per-TRADE Sharpe, so realistic magnitudes: mean +0.3% against a 1.0%
        # spread is a strong per-trade edge (ours measures -0.15). The DSR
        # formula assumes a small per-period Sharpe — an absurd value like 12
        # drives its variance term negative, which the function reports as an
        # unstable moment estimate rather than a result.
        from analysis.validation import deflated_sharpe_ratio
        rets = [1.3, -0.7] * 200                      # mean +0.3, sd 1.0
        d = deflated_sharpe_ratio(rets, n_trials=1)
        assert d["dsr"] > 0.95 and "survives" in d["verdict"], d

    def test_an_absurd_sharpe_is_reported_as_unstable_not_as_a_pass(self):
        from analysis.validation import deflated_sharpe_ratio
        d = deflated_sharpe_ratio([1.0, 1.2, 0.9, 1.1] * 40, n_trials=1)
        assert d["dsr"] == 0.0 and "unstable" in d["verdict"]

    def test_too_few_observations_refuses_to_answer(self):
        from analysis.validation import deflated_sharpe_ratio
        assert deflated_sharpe_ratio([0.1, 0.2], n_trials=5)["dsr"] == 0.0

    def test_inverse_normal_round_trips(self):
        from analysis.validation import _norm_cdf, _norm_ppf
        for p in (0.01, 0.1, 0.5, 0.9, 0.99):
            assert _norm_cdf(_norm_ppf(p)) == pytest.approx(p, abs=1e-6)


class TestWalkForward:

    def test_parameters_are_scored_only_out_of_sample(self):
        from analysis.validation import walk_forward
        obs = [{"i": i, "v": (1.0 if i % 2 == 0 else -1.0)} for i in range(200)]

        def evaluate(rows, params):
            return [r["v"] * params["mult"] for r in rows]

        res = walk_forward(obs, [{"mult": 1}, {"mult": 2}], evaluate, n_splits=3)
        assert len(res.splits) == 3
        assert res.oos_returns, "must produce out-of-sample returns"
        for s in res.splits:
            assert s["test_n"] > 0 and s["train_n"] > 0

    def test_embargo_drops_observations_after_training(self):
        from analysis.validation import walk_forward
        obs = [{"i": i} for i in range(200)]
        seen = []

        def evaluate(rows, params):
            seen.append([r["i"] for r in rows])
            return [0.1 if r["i"] % 3 else -0.05 for r in rows]

        walk_forward(obs, [{"p": 1}], evaluate, n_splits=2, embargo=10)
        train_max = max(seen[0])
        test_rows = [s for s in seen if s and min(s) > train_max]
        assert test_rows, "expected a test block strictly after the training block"
        assert min(test_rows[0]) >= train_max + 10

    def test_too_little_data_returns_empty_rather_than_a_number(self):
        from analysis.validation import walk_forward
        res = walk_forward([{"i": 1}], [{"p": 1}], lambda r, p: [0.1], n_splits=4)
        assert res.splits == [] and res.oos_returns == []
        assert res.summary()["n"] == 0
