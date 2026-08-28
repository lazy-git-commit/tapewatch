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
v21.16 — enter at the signal, stop trading the class that doesn't pay, and
prove the prompt cache is real.

Three changes ship together and each one has a way of silently reverting:

  1. cfg.skip_momentum_catalysts bypasses the `low_momentum` FLOOR for the
     classes whose edge is destroyed by waiting. The failure mode to guard is
     scope creep — a bypass that also swallows the "tape moving against the
     signal" branch, or leaks to classes/callers it was never measured on,
     turns a measured edge into "buy anything Claude liked".

  2. trades.signal_price records what the entry WOULD have cost without the
     wait. It is the evidence that decides whether change 1 was right, so a
     silently-NULL or silently-zero column is worse than no column: it would
     read as "waiting cost nothing".

  3. The prompt cache was a no-op for 1,140 calls because cache_control below
     the model's minimum prefix is accepted and ignored. Nothing errored. The
     detector is the only thing standing between a future prompt trim and
     another silent regression, so it is tested for both directions.

Every test here is written to fail if the behaviour is reverted, not merely to
restate a constant — see the mutation note in CLAUDE.md's test-classes section.
"""

import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


# ── Fixtures mirroring tests/test_core.py's confirmation seams ────────────────

_DAILY = (1_000_000, 10_000_000.0, 10.0)   # (avg_daily_volume, adv$, prev_close)
_QUOTE = {"c": 10.5, "o": 10.0, "pc": 10.28}


def _mk_sa(**overrides):
    """SessionAnalysis whose defaults confirm cleanly for a $10.50 quote."""
    from market.twelvedata_bars import SessionAnalysis
    kw = dict(
        past_price=10.2, current_bar_price=10.5, spread_proxy_pct=0.5,
        session_volume=100_000, vwap=10.45, last_price=10.5,
        session_low=None, session_high=None,
    )
    kw.update(overrides)
    return SessionAnalysis(**kw)


def _now_et(minutes_after_open=30):
    import pytz
    et = pytz.timezone("America/New_York")
    return et.localize(datetime(2026, 7, 10, 10, minutes_after_open - 30, 0))


def _confirm(sa, catalyst_type=None, quote=None, skip_set=("guidance_raise",)):
    """Run confirm_price_signal with every data dependency mocked."""
    import market.price_check as pc
    from config.settings import cfg
    fake_dt = MagicMock()
    fake_dt.now.side_effect = lambda tz=None: _now_et()
    with patch.object(pc, "datetime", fake_dt), \
         patch.object(pc, "get_trading_session", return_value="regular"), \
         patch.object(pc, "get_quote_with_fallback", return_value=quote or _QUOTE), \
         patch.object(pc, "get_session_analysis", return_value=sa), \
         patch.object(pc, "get_daily_stats", return_value=_DAILY), \
         patch.object(cfg, "skip_momentum_catalysts", set(skip_set)):
        return pc.confirm_price_signal("ACME_US_EQ", catalyst_type=catalyst_type)


# Flat tape: the catalyst published, nobody has reacted yet. This is the exact
# state the momentum floor parks in the re-eval queue, and the state the
# buy-at-signal measurement says is the best available entry.
_FLAT = dict(past_price=10.5, current_bar_price=10.5)
# Tape actively selling the "good" news — NOT what was measured, and a knife.
_AGAINST = dict(past_price=10.7, current_bar_price=10.5)


class TestMomentumFloorSkip:
    """cfg.skip_momentum_catalysts — narrow by construction."""

    def test_listed_catalyst_enters_on_flat_tape(self):
        # The whole point: no wait, no re-eval queue, entry at the signal price.
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type="guidance_raise")
        assert conf is not None
        assert conf.is_confirmed, conf.reason
        assert abs(conf.recent_move_pct) < 0.2   # genuinely below the floor

    def test_unlisted_catalyst_still_waits(self):
        # fda_approval's buy-at-signal simulation is NEGATIVE (−0.146%/trade).
        # If it is ever re-enabled in TRADEABLE_CATALYSTS it must not inherit
        # this bypass, so the skip is keyed on the catalyst, not on a global.
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type="fda_approval")
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_momentum"

    def test_no_catalyst_preserves_historical_behaviour(self):
        # Callers with no catalyst in hand (the pre-market gap-and-go eval)
        # must be bit-for-bit unaffected by this change.
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type=None)
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_momentum"

    def test_tape_moving_against_the_signal_still_rejects(self):
        # THE the load-bearing limit. The backtest sampled prices at
        # 5/15/60/120 min, so a signal that was −2% at the moment of entry
        # looked identical to a flat one. Buying active selling was never
        # measured and must not be inferred from the measurement.
        conf = _confirm(_mk_sa(**_AGAINST), catalyst_type="guidance_raise")
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_momentum"
        assert "against the signal" in conf.reason

    def test_rejection_stays_transient_so_it_still_re_evaluates(self):
        # A knife now can be a clean entry ten minutes from now; the signal
        # must keep its second chance rather than dying at first sight.
        import main
        conf = _confirm(_mk_sa(**_AGAINST), catalyst_type="guidance_raise")
        assert conf.reason_code in main._TRANSIENT_REJECT_CODES

    def test_momentum_ceiling_is_not_skipped(self):
        # A post-halt spike is still a spike for a listed class. The floor and
        # the ceiling answer different questions; only the floor was measured.
        conf = _confirm(
            _mk_sa(past_price=10.0, current_bar_price=10.5,
                   vwap=9.0, last_price=10.5),
            catalyst_type="guidance_raise",
            quote={"c": 11.9, "o": 10.0, "pc": 10.28},
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code in ("high_momentum", "extended_move", "overextended")

    def test_vwap_still_rejects_a_skipped_class(self):
        # Skipping the floor is only defensible because VWAP still runs the
        # size-neutral accumulation test. If both were off, an entry would rest
        # on Claude's opinion and nothing else.
        conf = _confirm(
            _mk_sa(past_price=10.5, current_bar_price=10.5, vwap=10.9,
                   last_price=10.5),
            catalyst_type="guidance_raise",
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "below_vwap"

    def test_liquidity_still_rejects_a_skipped_class(self):
        import market.price_check as pc
        from config.settings import cfg
        fake_dt = MagicMock()
        fake_dt.now.side_effect = lambda tz=None: _now_et()
        with patch.object(pc, "datetime", fake_dt), \
             patch.object(pc, "get_trading_session", return_value="regular"), \
             patch.object(pc, "get_quote_with_fallback", return_value=_QUOTE), \
             patch.object(pc, "get_session_analysis", return_value=_mk_sa(**_FLAT)), \
             patch.object(pc, "get_daily_stats", return_value=(1_000, 50_000.0, 10.0)), \
             patch.object(cfg, "skip_momentum_catalysts", {"guidance_raise"}):
            conf = pc.confirm_price_signal("ACME_US_EQ", catalyst_type="guidance_raise")
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "illiquid"


class TestSkipRequiresVwapConfirmation:
    """The two settings are independent knobs; only validate() couples them."""

    @staticmethod
    def _with_keys(**overrides):
        """cfg patched past the API-key check so validate() reaches the numerics."""
        from config.settings import cfg
        keys = dict(
            trading212_demo_api_key="k", trading212_demo_api_key_id="k",
            trading212_api_key="k", trading212_api_key_id="k",
            benzinga_api_key="k", finnhub_api_key="k",
            twelvedata_api_key="k", anthropic_api_key="k",
        )
        keys.update(overrides)
        return patch.multiple(cfg, **keys)

    def test_validate_rejects_skip_without_vwap(self):
        from config.settings import cfg
        with self._with_keys(skip_momentum_catalysts={"guidance_raise"},
                             require_vwap_confirmation=False):
            with pytest.raises(ValueError, match="SKIP_MOMENTUM_CATALYSTS"):
                cfg.validate()

    def test_validate_allows_no_vwap_when_nothing_is_skipped(self):
        from config.settings import cfg
        with self._with_keys(skip_momentum_catalysts=set(),
                             require_vwap_confirmation=False):
            cfg.validate()   # must not raise

    def test_validate_allows_the_shipped_combination(self):
        from config.settings import cfg
        with self._with_keys(skip_momentum_catalysts={"guidance_raise"},
                             require_vwap_confirmation=True):
            cfg.validate()   # must not raise


class TestSignalPriceRecorded:
    """trades.signal_price — the evidence the strategy change rests on."""

    def test_open_trade_persists_signal_price(self):
        import storage.database as db
        captured = {}

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params
            def fetchone(self): return {"id": 7}

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()

        with patch.object(db, "get_conn", return_value=_Conn()):
            tid = db.open_trade("ACME", 1, 10.0, 10.55, signal_price=10.31)
        assert tid == 7
        assert "signal_price" in captured["sql"]
        assert 10.31 in captured["params"], captured["params"]

    def test_queued_signal_price_survives_the_reeval_wait(self):
        # The bug this guards: passing confirmation.current_price at ENTRY time
        # instead of the price first seen. Both are floats, both are plausible,
        # and the column would then report a wait cost of exactly zero on every
        # trade — the one number the change is being judged on.
        import main
        item = MagicMock(article_id="a1", ticker="ACME", catalyst_type="guidance_raise")
        main._reeval_queue.clear()
        try:
            main._queue_reeval(item, signal_id=5, signal_price=10.31)
            entry = main._reeval_queue[("a1", "ACME")]
            assert entry["signal_price"] == 10.31
        finally:
            main._reeval_queue.clear()

    def test_synthetic_zero_price_is_stored_as_null_not_zero(self):
        # The graduated pre-market hand-off builds a PriceConfirmation with
        # current_price=0.0 (it never got a quote). Stored as 0.0 it would make
        # every downstream wait-cost query divide by zero or report −100%.
        import main
        assert main._usable_price(0.0) is None
        assert main._usable_price(None) is None
        assert main._usable_price(float("nan")) is None
        assert main._usable_price(float("inf")) is None
        assert main._usable_price(-3.0) is None
        assert main._usable_price(10.31) == 10.31

    def test_wait_cost_logging_never_raises_on_bad_input(self):
        import main
        main._record_wait_cost("ACME", None, 10.5)
        main._record_wait_cost("ACME", 10.0, float("nan"))
        main._record_wait_cost("ACME", 0.0, 10.5)


class TestEntryProvenanceRecorded:
    """
    HOW was this trade made? `signal_price` records what waiting COST but not
    which path produced the entry, and those are different questions: a trade
    that confirmed on its first look because momentum was already there and one
    that confirmed because the floor was skipped BOTH show a wait cost of ~zero.
    Without `entry_reason` the v21.16 change has no control group and is
    unfalsifiable in hindsight.
    """

    def test_confirmation_flags_a_skipped_floor(self):
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type="guidance_raise")
        assert conf.is_confirmed
        assert conf.momentum_skipped is True

    def test_confirmation_does_not_flag_a_normal_pass(self):
        # Default fixture is +2.94% — clears the floor on its own merits.
        conf = _confirm(_mk_sa(), catalyst_type="guidance_raise")
        assert conf.is_confirmed
        assert conf.momentum_skipped is False

    def test_entry_reason_distinguishes_the_two_paths(self):
        import main
        skipped = _confirm(_mk_sa(**_FLAT), catalyst_type="guidance_raise")
        normal = _confirm(_mk_sa(), catalyst_type="guidance_raise")
        assert main._entry_reason(skipped) == "momentum_skipped"
        assert main._entry_reason(normal) == "momentum_confirmed"

    def test_premarket_gap_path_is_labelled_separately(self):
        # A gap entry is a different strategy (the move already happened
        # overnight) and must not be pooled with either momentum bucket.
        import main
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type="guidance_raise")
        assert main._entry_reason(conf, entry_path="premarket_gap") == "premarket_gap"

    def test_first_look_entry_has_zero_delay(self):
        import main
        assert main._entry_delay_seconds(None) == 0

    def test_reeval_entry_records_the_wait(self):
        import main
        from datetime import datetime, timedelta, timezone
        seen = datetime.now(timezone.utc) - timedelta(minutes=7)
        secs = main._entry_delay_seconds(seen)
        assert 400 <= secs <= 440, secs

    def test_delay_never_raises_on_a_bad_timestamp(self):
        import main
        assert main._entry_delay_seconds("not-a-timestamp") is None

    def test_reeval_queue_carries_the_first_seen_timestamp(self):
        import main
        item = MagicMock(article_id="a2", ticker="ACME", catalyst_type="guidance_raise")
        main._reeval_queue.clear()
        try:
            main._queue_reeval(item, signal_id=9, signal_price=10.31)
            assert main._reeval_queue[("a2", "ACME")]["first_seen_at"] is not None
        finally:
            main._reeval_queue.clear()

    def test_execute_entry_forwards_the_entry_path(self):
        import main
        with patch.object(main, "save_signal", return_value=42), \
             patch.object(main, "_enter_confirmed", return_value=True) as enter:
            conf = _confirm(_mk_sa(), catalyst_type="guidance_raise")
            main._execute_entry(
                MagicMock(ticker="ACME", headline="h", source="s",
                          article_id="a", confidence=0.8,
                          catalyst_type="guidance_raise", catalyst_magnitude=3,
                          published_at=MagicMock(isoformat=lambda: "2026-08-18T00:00:00")),
                conf, "2026-08-18T00:00:00", entry_path="premarket_gap",
            )
        assert enter.call_args.kwargs["entry_path"] == "premarket_gap"

    def test_premarket_call_site_labels_its_entries(self):
        # N6: the helper being correct is worthless if the caller stops passing
        # the label — premarket rows would land in a momentum bucket instead,
        # which looks like valid data and silently corrupts the comparison.
        import main
        cand = {"id": 1, "ticker": "ACME"}
        conf = _confirm(_mk_sa(), catalyst_type="guidance_raise")
        with patch.object(main, "was_recently_traded", return_value=False), \
             patch.object(main, "update_premarket_candidate"), \
             patch.object(main, "_candidate_to_news_item", return_value=MagicMock()), \
             patch.object(main, "_risk_gates_pass", return_value=(True, "")), \
             patch.object(main, "_execute_entry", return_value=True) as ex:
            assert main._enter_premarket_approved([(cand, conf)], "ts") is True
        assert ex.call_args.kwargs["entry_path"] == "premarket_gap"

    def test_premarket_loop_aborts_the_cycle_when_a_risk_gate_trips(self):
        # This safety path had no test at all before the extraction: a tripped
        # kill switch must stop the cycle, not just skip one candidate.
        import main
        cands = [({"id": i, "ticker": f"T{i}"}, _confirm(_mk_sa(),
                 catalyst_type="guidance_raise")) for i in range(3)]
        with patch.object(main, "was_recently_traded", return_value=False), \
             patch.object(main, "update_premarket_candidate"), \
             patch.object(main, "_candidate_to_news_item", return_value=MagicMock()), \
             patch.object(main, "_risk_gates_pass", return_value=(False, "kill switch")), \
             patch.object(main, "_execute_entry", return_value=True) as ex:
            assert main._enter_premarket_approved(cands, "ts") is False
        assert ex.call_count == 1, "must stop after the first entry, not continue"

    def test_one_bad_candidate_does_not_drop_the_rest(self):
        # The 2026-06-11→07-06 drought shape: an exception on one candidate
        # used to abort the whole batch silently.
        import main
        cands = [({"id": i, "ticker": f"T{i}"}, _confirm(_mk_sa(),
                 catalyst_type="guidance_raise")) for i in range(3)]
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("hostile field shape")
            return True

        with patch.object(main, "was_recently_traded", return_value=False), \
             patch.object(main, "update_premarket_candidate"), \
             patch.object(main, "_candidate_to_news_item", return_value=MagicMock()), \
             patch.object(main, "_risk_gates_pass", return_value=(True, "")), \
             patch.object(main, "_execute_entry", side_effect=flaky):
            assert main._enter_premarket_approved(cands, "ts") is True
        assert len(calls) == 3, "a bad candidate must not drop those behind it"

    def test_reeval_entry_reports_the_wait_it_actually_had(self):
        # N12 — the worst of these to get wrong. If the re-eval path stops
        # handing first_seen_at to the entry, every trade that DID wait records
        # entry_delay_seconds = 0, i.e. the rows that prove the momentum gate's
        # cost would be the ones claiming it cost nothing.
        import main
        from datetime import datetime, timedelta, timezone
        conf = _confirm(_mk_sa(), catalyst_type="guidance_raise")
        assert conf.is_confirmed
        item = MagicMock(article_id="a9", ticker="ACME",
                         catalyst_type="guidance_raise")
        seen = datetime.now(timezone.utc) - timedelta(minutes=6)
        main._reeval_queue.clear()
        main._reeval_queue[("a9", "ACME")] = {
            "item": item, "signal_id": 3, "signal_price": 10.10,
            "first_seen_at": seen,
            "expires_at": datetime.now(timezone.utc) + timedelta(minutes=9),
        }
        try:
            with patch.object(main, "_risk_gates_pass", return_value=(True, "")), \
                 patch.object(main, "was_recently_traded", return_value=False), \
                 patch.object(main, "confirm_price_signal", return_value=conf), \
                 patch.object(main, "clear_rejection"), \
                 patch.object(main, "_enter_confirmed", return_value=True) as enter:
                main._process_reeval_queue()
        finally:
            main._reeval_queue.clear()
        kw = enter.call_args.kwargs
        assert kw["signal_price"] == 10.10
        assert kw["first_seen_at"] == seen, (
            "the re-eval path must report the real wait, not zero"
        )

    def test_enter_confirmed_feeds_the_real_momentum_reading_through(self):
        # N10: open_trade accepting the column is not the same as the entry path
        # supplying it. If this regresses, every row reads NULL and the outcome
        # can only be bucketed, never regressed against the actual reading.
        import main
        conf = _confirm(_mk_sa(**_FLAT), catalyst_type="guidance_raise")
        assert conf.is_confirmed and abs(conf.recent_move_pct) < 0.2

        buy_result = MagicMock(success=True, price=10.55, quantity=3.0,
                               order_id="o1", net_gbp=30.0, fx_rate=1.27,
                               fees_gbp=0.05, error=None)
        with patch.object(main, "buy", return_value=buy_result), \
             patch.object(main, "place_stop_loss", return_value=MagicMock(
                 success=True, order_id="s1")), \
             patch.object(main, "set_stop_order_id"), \
             patch.object(main, "open_trade", return_value=99) as ot:
            main._enter_confirmed(
                MagicMock(ticker="ACME_US_EQ", catalyst_type="guidance_raise"),
                conf, signal_id=7, signal_price=10.31,
            )
        kw = ot.call_args.kwargs
        assert kw["entry_reason"] == "momentum_skipped"
        assert kw["entry_momentum_pct"] == conf.recent_move_pct
        assert kw["signal_price"] == 10.31
        assert kw["entry_delay_seconds"] == 0

    def test_open_trade_persists_all_three_provenance_columns(self):
        import storage.database as db
        captured = {}

        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params=None):
                captured["sql"] = sql
                captured["params"] = params
            def fetchone(self): return {"id": 11}

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()

        with patch.object(db, "get_conn", return_value=_Conn()):
            db.open_trade("ACME", 1, 10.0, 10.55, signal_price=10.31,
                          entry_reason="momentum_skipped",
                          entry_momentum_pct=0.04, entry_delay_seconds=0)
        for col in ("entry_reason", "entry_momentum_pct", "entry_delay_seconds"):
            assert col in captured["sql"], col
        assert "momentum_skipped" in captured["params"]
        assert 0.04 in captured["params"]
        # Placeholder count must match the value count, or psycopg raises at
        # runtime on a path that has no test coverage in production.
        assert captured["sql"].count("%s") == len(captured["params"])


class TestPromptCacheIsVerified:
    """
    The cache was dead for 1,140 calls and nothing said so. cache_control below
    the model's minimum prefix is accepted, ignored, and reported as success.
    """

    def setup_method(self):
        import news.fetcher as f
        f._consecutive_uncached_calls = 0
        f._cache_alert_raised = False

    teardown_method = setup_method

    def test_cached_prefix_clears_the_model_minimum(self):
        """
        Claude Haiku 4.5 will not cache a prefix below 4096 tokens, and says
        nothing when it declines. The cached prefix is tools + system.

        The chars/token ratio is DERIVED from production rather than assumed:
        `classifier_calls` records min(tokens_in)=3641 for a 1-article batch,
        which is prefix + user-message wrapper + one article, and the previous
        prefix was 10,511 characters. Solving across a wide range of plausible
        wrapper sizes (60–400 tokens) gives 2.94–3.24 chars/token — this text
        is denser than plain English because of the JSON fragments, tickers and
        punctuation. 3.5 is used below as a pessimistic bound: comfortably
        above every derived value, so the assertion cannot pass on a lucky
        tokenizer, while still failing if the prompt is trimmed ~9%.

        This is the fast feedback, not the guarantee — _note_cache_usage()
        verifies the real thing from live usage after deploy.
        """
        import json
        from news.fetcher import _SYSTEM_PROMPT, _CLASSIFY_TOOL
        chars = len(_SYSTEM_PROMPT) + len(json.dumps(_CLASSIFY_TOOL))
        min_chars = 4096 * 3.5
        assert chars > min_chars, (
            f"cached prefix is {chars} chars (~{chars / 3.5:.0f} tokens at the "
            f"pessimistic ratio), below the {min_chars:.0f} chars needed to "
            f"clear Haiku 4.5's 4096-token minimum. cache_control will be "
            f"silently ignored and every call will pay full input price."
        )

    def test_zero_cached_tokens_eventually_alerts_once(self):
        import news.fetcher as f
        with patch.object(f, "_record_claude_event") as ev:
            for _ in range(f._CACHE_MISS_ALERT_AFTER - 1):
                f._note_cache_usage(0)
            assert ev.call_count == 0, "alerted before the threshold"
            f._note_cache_usage(0)
            assert ev.call_count == 1
            for _ in range(50):
                f._note_cache_usage(0)
            assert ev.call_count == 1, "alert must not repeat while still broken"
        assert ev.call_args[0][0] == "claude_cache_ineffective"

    def test_a_cache_hit_clears_the_alert_state(self):
        import news.fetcher as f
        with patch.object(f, "_record_claude_event") as ev:
            for _ in range(f._CACHE_MISS_ALERT_AFTER):
                f._note_cache_usage(0)
            assert ev.call_count == 1
            f._note_cache_usage(3500)          # caching started working
            assert f._consecutive_uncached_calls == 0
            for _ in range(f._CACHE_MISS_ALERT_AFTER):
                f._note_cache_usage(0)
            assert ev.call_count == 2, "a fresh outage must alert again"

    def test_cache_creation_counts_as_cache_usage(self):
        # The first call in each 5-min TTL window is a WRITE, not a read.
        # Counting reads only made a working cache look broken every window.
        import news.fetcher as f
        msg = MagicMock()
        msg.usage = MagicMock(input_tokens=120, output_tokens=400,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=4400)
        recorded = {}
        with patch("storage.database.record_classifier_call",
                   side_effect=lambda *a, **k: recorded.update(k)):
            f._record_claude_call([{"id": "x"}], 300, msg, ok=True, scored_count=1)
        assert recorded.get("tokens_cached") == 4400
        assert f._consecutive_uncached_calls == 0

    def test_replay_does_not_touch_the_cache_counter(self):
        # A backtest replay must not consume the day's alert slot, exactly as
        # for claude_truncated_batch (v21.15.1).
        import news.fetcher as f
        msg = MagicMock()
        msg.usage = MagicMock(input_tokens=120, output_tokens=400,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=0)
        for _ in range(f._CACHE_MISS_ALERT_AFTER * 2):
            f._record_claude_call([{"id": "x"}], 300, msg, ok=True,
                                  scored_count=1, live=False)
        assert f._consecutive_uncached_calls == 0
        assert not f._cache_alert_raised


class TestPromptExamplesEncodeRealFailures:
    """
    The worked examples are the deeper fix for three downstream regexes
    (_DIGEST_RE, _EXPLAINER_RE, the fda_approval carve-out). If they are
    trimmed away the regexes still catch the two headline shapes, but the
    classifier goes back to guessing on everything adjacent to them.
    """

    def test_explainer_and_digest_cases_are_taught_not_just_regexed(self):
        from news.fetcher import _SYSTEM_PROMPT
        assert "Post-Earnings Rally" in _SYSTEM_PROMPT
        assert "Market-Moving News" in _SYSTEM_PROMPT

    def test_guidance_raise_boundaries_are_spelled_out(self):
        # guidance_raise is now the ONLY tradeable class, so both error
        # directions cost money and the definition must stay explicit.
        from news.fetcher import _SYSTEM_PROMPT
        for phrase in ("Reaffirms FY25 Guidance", "Price Target",
                       "Does NOT count as guidance_raise"):
            assert phrase in _SYSTEM_PROMPT, phrase

    def test_acquirer_and_dilution_directions_are_taught(self):
        from news.fetcher import _SYSTEM_PROMPT
        assert "ma_acquirer" in _SYSTEM_PROMPT
        assert "offering_dilution" in _SYSTEM_PROMPT
