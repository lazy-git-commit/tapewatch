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
Tests for analysis/exit_variants.py — the Step 1 exit-rule simulation.

These exist to catch the same class of error `triple_barrier.py`'s own tests
guard against: a "conservative by construction" claim that turns out not to
hold on the exact input it should bind on. Each variant gets one test proving
its core mechanism and at least one proving its stated conservatism.
"""

import pytest


def _bars(rows):
    pd = pytest.importorskip("pandas")
    idx = pd.date_range("2026-09-01 10:00", periods=len(rows),
                        freq="1min", tz="America/New_York")
    return pd.DataFrame(rows, index=idx)


# ── trailing ─────────────────────────────────────────────────────────────────

class TestTrailingStop:

    def test_stop_loss_still_protects_before_the_trail_arms(self):
        # Price never reaches trigger_pct — the ordinary fixed stop must still
        # fire. A "no take-profit" variant is not "no downside protection."
        from analysis.exit_variants import label_trailing
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 100, "Low": 97.5, "Close": 98.0},   # -2.5%, below -2% SL
        ])
        res = label_trailing(bars, bars.index[0], sl_pct=2, hold_minutes=60,
                             cost_pct=0.0, trigger_pct=2.0, trail_pct=1.5)
        assert res["exit_reason"] == "stop_loss"

    def test_trail_arms_and_follows_a_new_peak_up(self):
        # Every bar's LOW is kept above what its own HIGH would imply for the
        # trail stop, so this exercises pure across-bar following rather than
        # the same-bar conservative rule (that has its own dedicated test).
        from analysis.exit_variants import label_trailing
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 103, "Low": 102.0, "Close": 102.5},  # arms: peak=103, trail=101.455
            {"Open": 103, "High": 108, "Low": 107.0, "Close": 107.5},  # new peak=108, trail=106.38
            {"Open": 107, "High": 107, "Low": 106.5, "Close": 107.0},  # holds above 106.38, no new peak
            {"Open": 107, "High": 107, "Low": 106.0, "Close": 106.1},  # low 106.0 <= 106.38 -> trail hit
        ])
        res = label_trailing(bars, bars.index[0], sl_pct=2, hold_minutes=60,
                             cost_pct=0.0, trigger_pct=2.0, trail_pct=1.5)
        assert res["exit_reason"] == "trail_stop"
        assert res["exit_price"] == pytest.approx(108 * 0.985)
        # The trail followed the LATER peak (108), not the arming price (103)
        # or the level set at arming (101.455) — the entire point of a
        # continuous trail vs. the live single-shot ratchet.
        assert res["exit_price"] > 103 * 0.985 + 1

    def test_same_bar_new_peak_and_breach_is_conservative(self):
        # A bar that raises the peak AND whose low would breach the stop
        # implied by that SAME bar's own high must exit there and then, not
        # carry the looser pre-bar stop into the next bar.
        from analysis.exit_variants import label_trailing
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 103, "Low": 102.0, "Close": 102.5},  # arms cleanly: trail=101.455
            {"Open": 103, "High": 120, "Low": 110.0, "Close": 115.0},  # new peak 120 (trail->118.2); low
                                                                       # (110) is ABOVE the OLD stop
                                                                       # (101.455) but BELOW the NEW one
        ])
        res = label_trailing(bars, bars.index[0], sl_pct=2, hold_minutes=60,
                             cost_pct=0.0, trigger_pct=2.0, trail_pct=1.5)
        assert res["exit_reason"] == "trail_stop"
        assert res["exit_price"] == pytest.approx(120 * 0.985)

    def test_never_reaching_trigger_falls_through_to_time_stop(self):
        from analysis.exit_variants import label_trailing
        bars = _bars([{"Open": 100, "High": 100.5, "Low": 99.8, "Close": 100.2}
                     for _ in range(4)])
        res = label_trailing(bars, bars.index[0], sl_pct=2, hold_minutes=3,
                             cost_pct=0.0, trigger_pct=2.0, trail_pct=1.5)
        assert res["exit_reason"] == "time_stop"

    def test_same_bar_stop_before_arm_is_conservative(self):
        # A bar whose LOW breaches the fixed stop must win even if that same
        # bar's HIGH would have cleared the arm trigger — matches
        # triple_barrier's own same-bar-favours-the-stop discipline.
        from analysis.exit_variants import label_trailing
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 103, "Low": 97.5, "Close": 99.0},  # high clears +2% arm, low breaks -2% SL
        ])
        res = label_trailing(bars, bars.index[0], sl_pct=2, hold_minutes=60,
                             cost_pct=0.0, trigger_pct=2.0, trail_pct=1.5)
        assert res["exit_reason"] == "stop_loss"


# ── partial ──────────────────────────────────────────────────────────────────

class TestPartialProfitTaking:

    def test_identical_to_baseline_before_tp_is_reached(self):
        # Before the TP fires, partial must behave exactly like label_one —
        # this isolates "what does taking a partial do" from everything else.
        from analysis.exit_variants import label_partial
        from analysis.triple_barrier import label_one
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 100, "Low": 97.5, "Close": 98.0},  # stop
        ])
        baseline = label_one(bars, bars.index[0], tp_pct=5, sl_pct=2,
                            hold_minutes=60, cost_pct=0.0)
        variant = label_partial(bars, bars.index[0], tp_pct=5, sl_pct=2,
                               hold_minutes=60, cost_pct=0.0,
                               partial_fraction=0.5, trail_pct=1.5)
        assert variant["exit_reason"] == baseline["exit_reason"] == "stop_loss"
        assert variant["net_pct"] == baseline["net_pct"]

    def test_tp_reached_splits_and_remainder_trails(self):
        from analysis.exit_variants import label_partial
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 106, "Low": 100, "Close": 105.0},   # TP (+5%) hit, leg1 @ 105
            {"Open": 105, "High": 112, "Low": 105, "Close": 112.0},   # remainder peaks at 112
            {"Open": 112, "High": 112, "Low": 110.2, "Close": 111},   # trail = 112*0.985=110.32, not hit
            {"Open": 111, "High": 111, "Low": 110.0, "Close": 110.1}, # low 110.0 < 110.32 -> trail hit
        ])
        res = label_partial(bars, bars.index[0], tp_pct=5, sl_pct=2, hold_minutes=60,
                           cost_pct=0.0, partial_fraction=0.5, trail_pct=1.5)
        assert res["exit_reason"] == "partial_tp+trail_stop"
        assert res["leg1_exit_pct"] == pytest.approx(5.0)
        assert res["leg2_exit_pct"] == pytest.approx((112 * 0.985 - 100) / 100 * 100)
        # Blended return must sit strictly between the two legs, not equal
        # either one outright — proof the split is actually being applied.
        assert res["leg1_exit_pct"] < res["gross_pct"] < res["leg2_exit_pct"]
        assert res["gross_pct"] == pytest.approx(
            0.5 * res["leg1_exit_pct"] + 0.5 * res["leg2_exit_pct"])

    def test_cost_is_charged_once_against_the_blended_return(self):
        # The blended-percentage algebra means charging cost_pct once against
        # the blend is equivalent to charging each leg its own share — NOT an
        # under-charge. This pins that down so it can't silently regress into
        # double- or zero-charging.
        from analysis.exit_variants import label_partial
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 106, "Low": 100, "Close": 105.0},
            {"Open": 105, "High": 105, "Low": 105, "Close": 105.0},  # remainder flat -> time_stop @ 105
        ])
        res = label_partial(bars, bars.index[0], tp_pct=5, sl_pct=2, hold_minutes=60,
                           cost_pct=0.46, partial_fraction=0.5, trail_pct=1.5)
        # both legs exit at +5% here, so blended gross is exactly 5.0
        assert res["gross_pct"] == pytest.approx(5.0)
        assert res["net_pct"] == pytest.approx(5.0 - 0.46)

    def test_stop_loss_before_tp_never_splits(self):
        from analysis.exit_variants import label_partial
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 100, "Low": 97.5, "Close": 98.0},
        ])
        res = label_partial(bars, bars.index[0], tp_pct=5, sl_pct=2, hold_minutes=60,
                           cost_pct=0.0, partial_fraction=0.5, trail_pct=1.5)
        assert res["exit_reason"] == "stop_loss"
        assert "leg1_exit_pct" not in res


# ── momentum_extend ──────────────────────────────────────────────────────────

class TestMomentumExtend:

    def test_flat_at_deadline_exits_on_schedule_unchanged(self):
        # Highs are DECREASING into the deadline bar — unambiguously not a
        # new high against the strictly-prior lookback window.
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100.0, "Low": 99.8, "Close": 99.9},
            {"Open": 100, "High": 100.3, "Low": 99.9, "Close": 100.0},
            {"Open": 100, "High": 100.2, "Low": 99.9, "Close": 100.0},
            {"Open": 100, "High": 100.1, "Low": 99.8, "Close": 99.9},   # deadline bar: lower high than t=2
            {"Open": 100, "High": 100.1, "Low": 99.8, "Close": 99.9},
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=5, sl_pct=2,
                                    hold_minutes=3, cost_pct=0.0,
                                    extend_minutes=10, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["exit_reason"] == "time_stop"
        assert res["extensions_used"] == 0

    def test_still_climbing_at_deadline_is_granted_one_extension(self):
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 101, "Low": 100, "Close": 101.0},
            {"Open": 101, "High": 102, "Low": 101, "Close": 102.0},   # deadline bar: new high vs t=1 -> extend
            {"Open": 102, "High": 102, "Low": 101.9, "Close": 102.0}, # in extension, flat -> exits here
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=5, sl_pct=2,
                                    hold_minutes=2, cost_pct=0.0,
                                    extend_minutes=10, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["extensions_used"] == 1
        # It must have actually walked PAST the original 2-minute deadline.
        assert res["held_minutes"] >= 3

    def test_tied_high_does_not_count_as_a_new_high(self):
        # A repeated (not exceeded) high is a flat tape, not a trend. Ties
        # must not be granted an extension — the strict `>` is the point.
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 101, "Low": 100, "Close": 101.0},
            {"Open": 101, "High": 101, "Low": 100.5, "Close": 101.0},  # deadline bar: TIES t=1's high
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=5, sl_pct=2,
                                    hold_minutes=2, cost_pct=0.0,
                                    extend_minutes=10, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["exit_reason"] == "time_stop"
        assert res["extensions_used"] == 0

    def test_extensions_are_capped(self):
        # Relentless new highs must still stop after max_extensions — an
        # "extend forever" bug would silently remove the time-stop entirely.
        # lookback (2min) > bar spacing (1min) so each deadline check has a
        # strictly-prior bar available to compare against.
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 102, "Low": 100, "Close": 101.0},
            {"Open": 101, "High": 103, "Low": 101, "Close": 102.0},  # deadline #1 (t=2): new high -> extend
            {"Open": 102, "High": 104, "Low": 102, "Close": 103.0},  # deadline #2 (t=3): new high -> extend
            {"Open": 103, "High": 105, "Low": 103, "Close": 104.0},  # deadline #3 (t=4): cap reached -> exit
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=50, sl_pct=2,
                                    hold_minutes=2, cost_pct=0.0,
                                    extend_minutes=1, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["extensions_used"] == 2
        assert res["exit_reason"] == "time_stop"

    def test_stop_loss_still_fires_during_an_extension(self):
        # A variant that changes WHEN we give up must never also weaken WHAT
        # protects the trade while it waits.
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 102, "Low": 100, "Close": 102.0},   # deadline bar, new high -> extend
            {"Open": 102, "High": 102, "Low": 97.5, "Close": 98.0},   # extension bar: stop breached
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=5, sl_pct=2,
                                    hold_minutes=1, cost_pct=0.0,
                                    extend_minutes=10, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["exit_reason"] == "stop_loss"
        assert res["extensions_used"] == 1

    def test_take_profit_still_fires_during_an_extension(self):
        from analysis.exit_variants import label_momentum_extend
        bars = _bars([
            {"Open": 100, "High": 100, "Low": 100, "Close": 100.0},
            {"Open": 100, "High": 102, "Low": 100, "Close": 102.0},   # deadline bar, new high -> extend
            {"Open": 102, "High": 106, "Low": 102, "Close": 105.0},   # extension bar: TP breached
        ])
        res = label_momentum_extend(bars, bars.index[0], tp_pct=5, sl_pct=2,
                                    hold_minutes=1, cost_pct=0.0,
                                    extend_minutes=10, extend_lookback_minutes=2,
                                    max_extensions=2)
        assert res["exit_reason"] == "take_profit"


# ── Shared behaviour every variant must preserve ────────────────────────────

class TestSharedGuards:
    """Guards every variant inherits by construction from `_entry_context` /
    `_time_stop_fallback` — verified once per variant so a future refactor
    that bypasses the shared helpers is caught immediately."""

    @pytest.mark.parametrize("label_fn,kwargs", [
        ("label_trailing", dict(sl_pct=2, hold_minutes=60, cost_pct=0.0,
                                trigger_pct=2.0, trail_pct=1.5)),
        ("label_partial", dict(tp_pct=5, sl_pct=2, hold_minutes=60, cost_pct=0.0,
                               partial_fraction=0.5, trail_pct=1.5)),
        ("label_momentum_extend", dict(tp_pct=5, sl_pct=2, hold_minutes=60, cost_pct=0.0,
                                       extend_minutes=10, extend_lookback_minutes=2,
                                       max_extensions=1)),
    ])
    def test_no_tradeable_bar_returns_none(self, label_fn, kwargs):
        import analysis.exit_variants as ev
        pd = pytest.importorskip("pandas")
        bars = _bars([{"Open": 100, "High": 100, "Low": 100, "Close": 100.0}])
        # entry_ts far after every bar in `bars` -> no row at/after it
        future = bars.index[-1] + pd.Timedelta(days=1)
        fn = getattr(ev, label_fn)
        assert fn(bars, future, **kwargs) is None
