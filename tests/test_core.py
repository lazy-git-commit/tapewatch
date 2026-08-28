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
tests/test_core.py
───────────────────
Unit tests for the most critical logic:
  - Exit condition evaluation (no external calls)
  - Sentiment scoring via forced tool use (news/fetcher.py)
  - Position sizing incl. risk/liquidity caps (trading/executor.py)
  - T212 precision-mismatch auto-retry (trading/executor.py)
  - RVOL time-of-day normalization (market/price_check.py)
  - Backtest cost model (backtest/backtest_db.py)

Run with: pytest tests/
"""

import pytest
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock


# ── Exit condition tests ──────────────────────────────────────────────────────

class TestExitConditions:
    """Tests for monitor/position_monitor.py::check_exit_conditions"""

    def _trade(self, buy_price=100.0, minutes_ago=10):
        buy_time = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        return {
            "id": 1,
            "ticker": "AAPL_US_EQ",
            "buy_price": buy_price,
            "quantity": 1.0,
            "buy_time": buy_time,
        }

    @patch("monitor.position_monitor.get_current_price", return_value=106.0)
    def test_take_profit_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, price = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is True
        assert reason == "take_profit"
        assert price == 106.0

    @patch("monitor.position_monitor.get_current_price", return_value=106.0)
    def test_take_profit_skipped_when_resting_tp(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # A resting limit order owns the profit side — the polled TP branch
        # must not fire, otherwise the same shares get sold twice.
        should_exit, reason, price = check_exit_conditions(
            self._trade(buy_price=100.0), has_resting_tp=True
        )
        assert should_exit is False
        assert price == 106.0

    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_stop_loss_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, price = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is True
        assert reason == "stop_loss"
        assert price == 97.0

    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_stop_loss_fires_even_with_resting_tp(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # has_resting_tp only suppresses the TP branch — never the stop.
        should_exit, reason, price = check_exit_conditions(
            self._trade(buy_price=100.0), has_resting_tp=True
        )
        assert should_exit is True
        assert reason == "stop_loss"

    @patch("monitor.position_monitor.get_current_price", return_value=101.0)
    def test_time_stop_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # Trade opened 65 minutes ago — past the 60-minute time stop
        should_exit, reason, price = check_exit_conditions(self._trade(minutes_ago=65))
        assert should_exit is True
        assert reason == "time_stop"
        assert price == 101.0

    @patch("monitor.position_monitor.get_current_price", return_value=101.5)
    def test_no_exit_when_in_range(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # Price is +1.5% — not yet at take profit (+5%) or stop loss (-2%)
        should_exit, reason, price = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is False
        assert price == 101.5

    @patch("monitor.position_monitor.get_current_price", return_value=None)
    def test_no_exit_when_price_unavailable(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # When price feed is down, skip TP/SL — should_exit=False, price=None
        should_exit, reason, price = check_exit_conditions(self._trade())
        assert should_exit is False
        assert price is None


class TestExitInversion:
    """v20: the resting side is the STOP; the TP is polled; breakeven ratchet.

    Realized evidence for the inversion: 1 resting-TP fill in 11 trades vs
    stop-side slippage of −3.4%/−3.97%/−18.99% on −2% triggers — the polled
    stop gave every fast reversal a 20-second head start.
    """

    def setup_method(self):
        import monitor.position_monitor as pm
        pm._sell_fail_counts.clear()
        pm._last_status_check.clear()
        pm._last_price_log.clear()

    teardown_method = setup_method

    def _trade(self, buy_price=100.0, minutes_ago=10, stop_order_id="stop-1",
               ratchet_armed=0, seconds_ago=None):
        age = timedelta(seconds=seconds_ago) if seconds_ago is not None else timedelta(minutes=minutes_ago)
        buy_time = (datetime.now(timezone.utc) - age).isoformat()
        return {
            "id": 7,
            "ticker": "AAPL_US_EQ",
            "buy_price": buy_price,
            "quantity": 5.0,
            "buy_time": buy_time,
            "stop_order_id": stop_order_id,
            "tp_order_id": None,
            "ratchet_armed": ratchet_armed,
            "mode": "demo",
        }

    # ── check_exit_conditions with a live resting stop ───────────────────────
    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_polled_stop_suppressed_when_resting_stop_live(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, _ = check_exit_conditions(
            self._trade(), has_resting_stop=True
        )
        assert should_exit is False  # broker owns the stop — no double-sell

    @patch("monitor.position_monitor.get_current_price", return_value=106.0)
    def test_polled_tp_fires_with_resting_stop_live(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, _ = check_exit_conditions(
            self._trade(), has_resting_stop=True
        )
        assert should_exit is True and reason == "take_profit"

    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_polled_stop_active_when_no_resting_stop(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, _ = check_exit_conditions(
            self._trade(stop_order_id=None), has_resting_stop=False
        )
        assert should_exit is True and reason == "stop_loss"

    # ── Breakeven ratchet (armed flag persisted on the trade row — v20.1) ────
    @patch("monitor.position_monitor.get_current_price", return_value=100.05)
    def test_polled_stop_uses_breakeven_after_arm(self, _mock):
        import monitor.position_monitor as pm
        trade = self._trade(stop_order_id=None, ratchet_armed=1)
        # 100.05 < breakeven 100.10 → armed polled stop fires at a tiny gain,
        # not at the original 98.0. The flag comes from the DB row, so this
        # holds across service restarts.
        should_exit, reason, _ = pm.check_exit_conditions(trade)
        assert should_exit is True and reason == "stop_loss"

    @patch("monitor.position_monitor.set_ratchet_armed")
    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.place_stop_loss", return_value="stop-2")
    @patch("monitor.position_monitor.cancel_order", return_value=True)
    def test_ratchet_replaces_stop_once(self, mock_cancel, mock_place, mock_set, mock_arm):
        import monitor.position_monitor as pm
        trade = self._trade()
        pm._maybe_ratchet_stop(trade, current_price=102.5)  # ≥ +2% trigger
        mock_cancel.assert_called_once_with("stop-1")
        mock_place.assert_called_once()
        # New stop at breakeven: buy × (1 + 0.1%) = 100.10
        assert mock_place.call_args.args[2] == pytest.approx(100.10)
        mock_arm.assert_called_once_with(7)   # durable, not in-memory
        assert trade["ratchet_armed"] == 1
        assert trade["stop_order_id"] == "stop-2"
        # Second call is a no-op (once per trade).
        pm._maybe_ratchet_stop(trade, current_price=103.0)
        mock_place.assert_called_once()

    # ── Settle period (v21.5) — HOG, 2026-07-23 ───────────────────────────────
    @patch("monitor.position_monitor.set_ratchet_armed")
    @patch("monitor.position_monitor.place_stop_loss", return_value="stop-2")
    @patch("monitor.position_monitor.cancel_order", return_value=True)
    def test_ratchet_suppressed_within_settle_period(
        self, mock_cancel, mock_place, mock_arm
    ):
        # A fill's own quote can look like an instant 2%+ move (HOG filled
        # 3.6% off its signal price, and the ratchet fired 1s later off that
        # same noisy print). Within the settle window the ratchet must not
        # touch the resting stop at all, however far above trigger the price
        # looks.
        import monitor.position_monitor as pm
        trade = self._trade(seconds_ago=1)
        pm._maybe_ratchet_stop(trade, current_price=103.75)  # +3.75% — HOG's own number
        mock_cancel.assert_not_called()
        mock_place.assert_not_called()
        mock_arm.assert_not_called()
        assert not trade.get("ratchet_armed")
        assert trade["stop_order_id"] == "stop-1"  # original stop untouched

    @patch("monitor.position_monitor.set_ratchet_armed")
    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.place_stop_loss", return_value="stop-2")
    @patch("monitor.position_monitor.cancel_order", return_value=True)
    def test_ratchet_fires_once_settle_period_elapses(
        self, mock_cancel, mock_place, mock_set, mock_arm
    ):
        # Same price, same trade — just old enough now. Confirms the
        # settle period is a delay, not a permanent block.
        import monitor.position_monitor as pm
        trade = self._trade(seconds_ago=pm._RATCHET_MIN_AGE_SECONDS + 1)
        pm._maybe_ratchet_stop(trade, current_price=103.75)
        mock_cancel.assert_called_once_with("stop-1")
        mock_place.assert_called_once()
        mock_arm.assert_called_once_with(7)
        assert trade["ratchet_armed"] == 1

    @patch("monitor.position_monitor.place_stop_loss", return_value="x")
    def test_ratchet_not_triggered_below_threshold(self, mock_place):
        import monitor.position_monitor as pm
        pm._maybe_ratchet_stop(self._trade(), current_price=101.0)  # +1% < 2%
        mock_place.assert_not_called()

    @patch("monitor.position_monitor.place_stop_loss", return_value="x")
    def test_ratchet_never_touches_legacy_tp_trades(self, mock_place):
        import monitor.position_monitor as pm
        trade = self._trade(stop_order_id=None)
        trade["tp_order_id"] = "tp-9"   # pre-v20 regime
        pm._maybe_ratchet_stop(trade, current_price=105.0)
        mock_place.assert_not_called()

    @patch("monitor.position_monitor.set_ratchet_armed")
    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.place_stop_loss", return_value=None)
    @patch("monitor.position_monitor.cancel_order", return_value=True)
    def test_ratchet_placement_failure_falls_back_to_polled_breakeven(
        self, _cancel, _place, _set, mock_arm
    ):
        # Cancel succeeded but the breakeven stop wouldn't place: the trade
        # must still be protected — the DURABLE armed flag drives the POLLED
        # breakeven, and survives a restart.
        import monitor.position_monitor as pm
        trade = self._trade()
        pm._maybe_ratchet_stop(trade, current_price=102.5)
        mock_arm.assert_called_once_with(7)
        assert trade["ratchet_armed"] == 1
        assert trade.get("stop_order_id") is None

    @patch("monitor.position_monitor.set_ratchet_armed")
    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.place_stop_loss", return_value="stop-9")
    def test_ratchet_crash_recovery_places_fresh_stop(self, mock_place, _set, mock_arm):
        # Crash landed between cancel and re-place: restart sees an un-armed
        # trade with NO resting stop. The ratchet must repair it by placing a
        # breakeven stop directly (nothing to cancel first).
        import monitor.position_monitor as pm
        trade = self._trade(stop_order_id=None)
        pm._maybe_ratchet_stop(trade, current_price=102.5)
        mock_place.assert_called_once()
        assert mock_place.call_args.args[2] == pytest.approx(100.10)
        mock_arm.assert_called_once_with(7)
        assert trade["stop_order_id"] == "stop-9"

    @patch("monitor.position_monitor._close_as_resting_fill")
    @patch("monitor.position_monitor.get_order_status", return_value="FILLED")
    @patch("monitor.position_monitor.cancel_order", return_value=False)
    def test_ratchet_cancel_race_resolves_as_stop_fill(
        self, _cancel, _status, mock_close
    ):
        # Whipsaw: stop filled while we were cancelling it for the ratchet.
        import monitor.position_monitor as pm
        trade = self._trade()
        pm._maybe_ratchet_stop(trade, current_price=102.5)
        mock_close.assert_called_once()
        assert not trade.get("ratchet_armed")  # trade closed, never armed

    # ── Cancel-before-sell handles the stop kind ─────────────────────────────
    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.cancel_order", return_value=True)
    def test_cancel_resting_stop_before_sell(self, mock_cancel, _set):
        from monitor.position_monitor import _cancel_resting_before_sell
        trade = self._trade()
        assert _cancel_resting_before_sell(trade) is True
        mock_cancel.assert_called_once_with("stop-1")
        assert trade["stop_order_id"] is None

    @patch("monitor.position_monitor._close_as_resting_fill")
    @patch("monitor.position_monitor.get_order_status", return_value="FILLED")
    @patch("monitor.position_monitor.cancel_order", return_value=False)
    def test_cancel_race_stop_filled_closes_as_stop(self, _c, _s, mock_close):
        from monitor.position_monitor import _cancel_resting_before_sell
        trade = self._trade()
        assert _cancel_resting_before_sell(trade) is False  # nothing left to sell
        mock_close.assert_called_once()
        assert mock_close.call_args.args[2] == "stop"


class TestPolledPriceObservability:
    """v21.5: the HOG (2026-07-23) post-mortem found no record anywhere of
    what price the monitor was comparing against while a position sat past
    its stop threshold unprotected for 32 minutes — check_exit_conditions'
    holding state logged only at DEBUG, invisible at the service's INFO
    level. One holding-log per trade per _PRICE_LOG_EVERY_SECONDS is now
    promoted to INFO so this is provable from the logs next time.
    """

    def setup_method(self):
        import monitor.position_monitor as pm
        pm._last_price_log.clear()

    teardown_method = setup_method

    def _trade(self):
        buy_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        return {
            "id": 42, "ticker": "AAPL_US_EQ", "buy_price": 100.0,
            "quantity": 5.0, "buy_time": buy_time,
            "stop_order_id": None, "tp_order_id": None, "ratchet_armed": 0,
        }

    @patch("monitor.position_monitor.get_current_price", return_value=99.0)
    def test_first_holding_check_logs_at_info(self, _mock, caplog):
        import logging
        from monitor.position_monitor import check_exit_conditions
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            should_exit, _, _ = check_exit_conditions(self._trade())
        assert should_exit is False
        assert any(
            r.levelno == logging.INFO and "holding" in r.message
            for r in caplog.records
        )

    @patch("monitor.position_monitor.get_current_price", return_value=99.0)
    def test_second_holding_check_within_window_is_debug_only(self, _mock, caplog):
        import logging
        from monitor.position_monitor import check_exit_conditions
        trade = self._trade()
        with caplog.at_level(logging.DEBUG, logger="monitor.position_monitor"):
            check_exit_conditions(trade)  # primes the throttle at INFO
            caplog.clear()
            check_exit_conditions(trade)  # same cycle window — DEBUG only
        holding = [r for r in caplog.records if "holding" in r.message]
        assert len(holding) == 1
        assert holding[0].levelno == logging.DEBUG

    @patch("monitor.position_monitor.get_current_price", return_value=99.0)
    def test_holding_log_promoted_again_after_window_elapses(self, _mock, caplog):
        import logging
        import monitor.position_monitor as pm
        trade = self._trade()
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            pm.check_exit_conditions(trade)
            # Force the throttle to look expired without a real sleep.
            pm._last_price_log[trade["id"]] -= pm._PRICE_LOG_EVERY_SECONDS + 1
            caplog.clear()
            pm.check_exit_conditions(trade)
        assert any(
            r.levelno == logging.INFO and "holding" in r.message
            for r in caplog.records
        )


class TestGetCurrentPriceDegraded:
    """v20.1: the monitor's price path must not melt down in a quote outage.

    At the 5s cadence, in-call retry backoff would overrun the cycle and an
    unthrottled 390-bar fallback would saturate the Twelvedata token bucket
    (~12 pulls/min/position) and starve signal confirmation.
    """

    def setup_method(self):
        import market.price_check as pc
        pc._last_bars_fallback.clear()

    teardown_method = setup_method

    @patch("market.price_check.get_quote_with_fallback")
    def test_quote_path_is_fast_mode(self, mock_quote):
        import market.price_check as pc
        mock_quote.return_value = {"c": 10.5}
        assert pc.get_current_price("AAPL_US_EQ") == 10.5
        assert mock_quote.call_args.kwargs.get("fast") is True

    @patch("market.price_check.time.monotonic")
    @patch("market.price_check.get_session_analysis")
    @patch("market.price_check.get_quote_with_fallback", return_value=None)
    def test_bars_fallback_throttled_per_symbol(self, _quote, mock_sa, mock_mono):
        # Deterministic clock: don't depend on the host's real monotonic()
        # baseline (undefined by spec — can start near 0 on a fresh CI
        # container, which would make an empty _last_bars_fallback dict's
        # `.get(symbol, 0.0)` default look "recent" and falsely throttle the
        # very first call).
        import market.price_check as pc
        mock_sa.return_value = _mk_sa()
        mock_mono.side_effect = [1000.0, 1005.0]  # 5s apart, well under 30s
        assert pc.get_current_price("AAPL_US_EQ") == 10.5  # first: fallback runs
        assert pc.get_current_price("AAPL_US_EQ") is None  # 5s later: throttled
        mock_sa.assert_called_once()


class TestPlaceStopLoss:
    """executor.place_stop_loss — the v20 resting order."""

    @patch("trading.executor._post")
    def test_payload_shape(self, mock_post):
        from trading.executor import place_stop_loss
        mock_post.return_value = {"id": 12345}
        order_id = place_stop_loss("AAPL_US_EQ", 5.0, 98.0)
        assert order_id == "12345"
        path, payload = mock_post.call_args.args
        assert path == "/equity/orders/stop"
        assert payload["quantity"] == -5.0          # negative = sell
        assert payload["stopPrice"] == 98.0
        assert payload["timeValidity"] == "DAY"

    @patch("trading.executor._post", side_effect=Exception("HTTP 400"))
    def test_failure_returns_none(self, _mock):
        from trading.executor import place_stop_loss
        assert place_stop_loss("AAPL_US_EQ", 5.0, 98.0) is None


class TestDigestPrefilter:
    """v20: digest/preview/listicle headlines never reach Claude.

    CRCL 2026-07-10: 'Market-Moving News for July 10th' (3 tickers, slid
    under the >3 roundup filter) was classified 'earnings_beat conf=0.8' for
    three unrelated companies and bought the top of a 13% parabolic spike.
    """

    def setup_method(self):
        import news.fetcher as f
        f._scored_articles["date"] = None
        f._scored_articles["ids"] = set()
        f._ticker_history["date"] = None
        f._ticker_history["tickers"] = {}

    def _fetch_with(self, title):
        import news.fetcher as f
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        article = {"tickers": ["CRCL"], "title": title, "teaser": "t",
                   "benzinga_id": "d1", "published": now_iso}
        with patch("news.fetcher._fetch", return_value=[article]), \
             patch("news.fetcher._batch_score_sentiment", return_value={}) as mock_score:
            f.fetch_all_news(seen_checker=lambda a, t: False)
        return mock_score

    def test_market_moving_news_blocked(self):
        mock_score = self._fetch_with("Market-Moving News for July 10th")
        mock_score.assert_not_called()

    def test_stocks_to_watch_blocked(self):
        mock_score = self._fetch_with("3 Stocks To Watch Before The Open")
        mock_score.assert_not_called()

    def test_premarket_movers_blocked(self):
        mock_score = self._fetch_with("Friday's Premarket Movers: Winners and Losers")
        mock_score.assert_not_called()

    def test_earnings_preview_blocked(self):
        mock_score = self._fetch_with("Earnings Scheduled For July 10, 2026")
        mock_score.assert_not_called()

    def test_real_catalyst_headline_passes(self):
        mock_score = self._fetch_with(
            "Circle Internet Receives Federal Trust Bank Charter Approval"
        )
        mock_score.assert_called_once()

    def test_company_market_update_pr_passes(self):
        # v20.1 review finding: "market update" is a real PR template for
        # single-stock news — it must NOT be treated as a digest.
        mock_score = self._fetch_with(
            "Acme Therapeutics Provides Market Update On Phase 3 Results"
        )
        mock_score.assert_called_once()

    def test_investor_day_ahead_passes(self):
        # "day ahead" appears in genuine headlines; only "week ahead" digests.
        mock_score = self._fetch_with(
            "Acme Soars With Investor Day Ahead Of Product Launch"
        )
        mock_score.assert_called_once()

    def test_week_ahead_digest_still_blocked(self):
        mock_score = self._fetch_with("Wall Street's Week Ahead: Earnings, CPI")
        mock_score.assert_not_called()


class TestCatalystPrune:
    """v20: TRADEABLE_CATALYSTS default pruned to measured-positive classes."""

    def test_default_excludes_every_measured_negative_class(self):
        # Defaults matter: a missing env var must not silently re-enable the
        # classes the forward-return data measured as negative. Asserted as an
        # invariant over the excluded set rather than an equality against the
        # current list — the point of the test is that these classes cannot
        # come back by accident, not that the default has one particular value.
        import os
        from config.settings import Settings
        old = os.environ.pop("TRADEABLE_CATALYSTS", None)
        try:
            enabled = set(Settings().tradeable_catalysts)
        finally:
            if old is not None:
                os.environ["TRADEABLE_CATALYSTS"] = old
        measured_negative = {
            "contract_win", "ma_target", "earnings_beat", "product_launch",
            "short_squeeze", "analyst_action", "recap_explainer",
            "offering_dilution", "ma_acquirer", "halt_or_resume",
        }
        assert not (enabled & measured_negative), (
            "a class with measured-negative forward returns is tradeable by default"
        )
        assert enabled, "TRADEABLE_CATALYSTS default must not be empty"

    def test_default_is_the_simulated_profitable_class(self):
        # v21.16: fda_approval dropped. Its +1.42%/60m raw drift is real but
        # does not survive a 2% stop plus 0.46pp round-trip costs — simulated
        # net −0.146%/trade at a 32% win rate against a 33% break-even, vs
        # guidance_raise at +0.667% and 50%. Live P&L cannot arbitrate this
        # (fda_approval has exactly one closed trade), so the default encodes
        # the simulation and this test pins it against a silent revert.
        import os
        from config.settings import Settings
        old = os.environ.pop("TRADEABLE_CATALYSTS", None)
        try:
            assert Settings().tradeable_catalysts == ["guidance_raise"]
        finally:
            if old is not None:
                os.environ["TRADEABLE_CATALYSTS"] = old

    def test_fda_approval_prompt_requires_us_regulator(self):
        # 2026-07-13: NVS (a Health Canada approval) was tagged fda_approval,
        # diluting the measured edge that TRADEABLE_CATALYSTS pruning rests
        # on. The prompt must keep an explicit non-US-regulator carve-out.
        from news.fetcher import _SYSTEM_PROMPT
        assert "Health Canada" in _SYSTEM_PROMPT
        assert "US FDA specifically" in _SYSTEM_PROMPT


# ── Sentiment scoring tests (forced tool use) ─────────────────────────────────

class TestSentimentScoring:
    """Tests for news/fetcher.py::_batch_score_sentiment"""

    def _mock_tool_response(self, classifications: list[dict]) -> MagicMock:
        """Build a mock Claude message containing a tool_use content block."""
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    def _article(self, id="1", headline="Earnings beat", teaser="Revenue up"):
        return {"id": id, "headline": headline, "teaser": teaser}

    @patch("news.fetcher._claude")
    def test_positive_sentiment_parsed(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([
            {"id": "1", "sentiment": "positive", "confidence": 0.9,
             "catalyst_type": "earnings_beat", "already_moved": False},
        ])
        scores = _batch_score_sentiment([self._article()])
        assert scores["1"]["sentiment"] == "positive"
        assert scores["1"]["confidence"] == pytest.approx(0.9)
        assert scores["1"]["catalyst_type"] == "earnings_beat"
        assert scores["1"]["already_moved"] is False

    @patch("news.fetcher._claude")
    def test_halt_article_classified(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([
            {"id": "1", "sentiment": "neutral", "confidence": 0.2,
             "catalyst_type": "halt_or_resume", "already_moved": True},
        ])
        scores = _batch_score_sentiment([self._article(headline="X Shares Halted On Circuit Breaker")])
        assert scores["1"]["sentiment"] == "neutral"
        assert scores["1"]["already_moved"] is True

    @patch("news.fetcher._claude")
    def test_missing_id_skipped(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([
            {"sentiment": "positive", "confidence": 0.9,
             "catalyst_type": "other", "already_moved": False},  # no id
            {"id": "2", "sentiment": "neutral", "confidence": 0.3,
             "catalyst_type": "analyst_action", "already_moved": False},
        ])
        scores = _batch_score_sentiment([self._article("1"), self._article("2")])
        assert "1" not in scores
        assert scores["2"]["sentiment"] == "neutral"

    @patch("news.fetcher._claude")
    def test_no_tool_block_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        text_block = MagicMock()
        text_block.type = "text"
        msg = MagicMock()
        msg.content = [text_block]
        mock_claude.messages.create.return_value = msg
        scores = _batch_score_sentiment([self._article()])
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_api_exception_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.side_effect = Exception("API timeout")
        scores = _batch_score_sentiment([self._article()])
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_empty_articles_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        scores = _batch_score_sentiment([])
        mock_claude.messages.create.assert_not_called()
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_temperature_zero_and_forced_tool(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([])
        _batch_score_sentiment([self._article()])
        kwargs = mock_claude.messages.create.call_args.kwargs
        assert kwargs["temperature"] == 0
        assert kwargs["tool_choice"] == {"type": "tool", "name": "classify_articles"}
        # Rubric must be in the (cached) system block, not the user message
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


# ── Empty-batch retry tests (v21.7 — session-boundary blackout post-mortem) ──

class TestEmptyClassificationsRetry:
    """
    Tests for news/fetcher.py::_batch_score_sentiment — retries when Claude
    returns a well-formed but EMPTY classifications list for a non-empty batch.

    Observed at the first news_cycle tick after a session boundary (2026-07-27
    16:06-16:12 ET regular→afterhours, 2026-07-28 07:00-07:07 ET premarket scan
    start): 6-8 consecutive 200-OK forced-tool-use calls each returned [].
    Because _mark_scored only fires on a successful score, the backlog of
    unscored articles grew every cycle with no bound but max_age_minutes. A real
    catalyst published in that window risks going unscored and untraded with
    nothing but a WARNING in the logs.

    v21.12 raised the budget from 2 attempts to _EMPTY_BATCH_ATTEMPTS after
    2026-08-04, when 25 consecutive cycles saw BOTH the call and its single
    retry come back empty. See TestEmptyBatchRetryAndAlert for the alerting.
    """

    def _mock_tool_response(self, classifications: list[dict]) -> MagicMock:
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    def _article(self, id="1", headline="Earnings beat", teaser="Revenue up"):
        return {"id": id, "headline": headline, "teaser": teaser}

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._claude")
    def test_empty_then_populated_recovers_on_retry(self, mock_claude, _sleep):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.side_effect = [
            self._mock_tool_response([]),
            self._mock_tool_response([
                {"id": "1", "sentiment": "positive", "confidence": 0.9,
                 "catalyst_type": "guidance_raise", "already_moved": False},
            ]),
        ]
        scores = _batch_score_sentiment([self._article()])
        assert mock_claude.messages.create.call_count == 2
        assert scores["1"]["sentiment"] == "positive"

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_empty_on_every_attempt_gives_up_and_returns_empty(
        self, mock_claude, _evt, _sleep
    ):
        from news.fetcher import _batch_score_sentiment, _EMPTY_BATCH_ATTEMPTS
        mock_claude.messages.create.return_value = self._mock_tool_response([])
        scores = _batch_score_sentiment([self._article()])
        assert mock_claude.messages.create.call_count == _EMPTY_BATCH_ATTEMPTS
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_genuinely_empty_batch_result_not_retried_when_articles_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        scores = _batch_score_sentiment([])
        mock_claude.messages.create.assert_not_called()
        assert scores == {}


# ── Same-day same-ticker cross-reference tests (v19.5) ────────────────────────

class TestSameDayTickerCrossReference:
    """
    A second article about a ticker already scored today carries the earlier
    verdict as context (2026-07-09 LEVI post-mortem: a negative "tumbles
    despite beat" article at 09:39 ET had no bearing on how Claude read a
    positive "more upside" respin of the SAME earnings at 11:30 ET — the
    system bought at the top of the recovery bounce the first article's
    "tumble" had already produced).
    """

    def setup_method(self):
        import news.fetcher as f
        f._scored_articles["date"] = None
        f._scored_articles["ids"] = set()
        f._ticker_history["date"] = None
        f._ticker_history["tickers"] = {}

    def _mock_tool_response(self, classifications):
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    def test_no_prior_history_returns_none(self):
        from news.fetcher import _prior_ticker_context
        assert _prior_ticker_context("AAPL_US_EQ") is None

    def test_recording_then_lookup_returns_context(self):
        import news.fetcher as f
        f._record_ticker_history("AAPL_US_EQ", "X Tumbles Despite Beat", "negative",
                                  datetime.now(timezone.utc))
        ctx = f._prior_ticker_context("AAPL_US_EQ")
        assert ctx is not None and "negative" in ctx and "Tumbles" in ctx

    def test_context_caps_at_three_most_recent(self):
        import news.fetcher as f
        now = datetime.now(timezone.utc)
        for i in range(5):
            f._record_ticker_history("AAPL_US_EQ", f"Headline {i}", "neutral", now)
        ctx = f._prior_ticker_context("AAPL_US_EQ")
        assert "Headline 0" not in ctx and "Headline 1" not in ctx
        assert "Headline 4" in ctx

    def test_history_resets_on_new_day(self):
        import news.fetcher as f
        from datetime import timedelta
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        f._record_ticker_history("AAPL_US_EQ", "Old news", "positive", yesterday)
        f._ticker_history["date"] = yesterday.date()  # simulate a stale day marker
        assert f._prior_ticker_context("AAPL_US_EQ") is None

    @patch("news.fetcher._claude")
    def test_prior_context_reaches_claude_prompt(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([])
        _batch_score_sentiment([
            {"id": "1", "headline": "H", "teaser": "T",
             "prior_context": 'AAPL_US_EQ: 09:39 UTC negative ("X Tumbles")'},
        ])
        user_msg = mock_claude.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "PRIOR ARTICLE(S) TODAY" in user_msg and "X Tumbles" in user_msg

    @patch("news.fetcher._claude")
    def test_no_prior_context_key_omits_prompt_line(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([])
        _batch_score_sentiment([{"id": "1", "headline": "H", "teaser": "T"}])
        user_msg = mock_claude.messages.create.call_args.kwargs["messages"][0]["content"]
        assert "PRIOR ARTICLE(S) TODAY" not in user_msg

    @patch("news.fetcher.save_sentiment_scores")
    @patch("news.fetcher._batch_score_sentiment")
    @patch("news.fetcher._fetch")
    def test_second_article_same_ticker_carries_first_as_context(
        self, mock_fetch, mock_score, _mock_save
    ):
        import news.fetcher as f
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        mock_fetch.return_value = [{
            "tickers": ["AAPL"], "title": "AAPL Tumbles Despite Beat",
            "teaser": "t1", "benzinga_id": "a1", "published": now_iso,
        }]
        mock_score.return_value = {"a1": {
            "sentiment": "negative", "confidence": 0.75,
            "catalyst_type": "earnings_beat", "already_moved": False,
            "catalyst_magnitude": 2,
        }}
        f.fetch_all_news(seen_checker=lambda a, t: False)
        first_payload = mock_score.call_args.args[0]
        assert "prior_context" not in first_payload[0]

        mock_fetch.return_value = [{
            "tickers": ["AAPL"], "title": "AAPL More Upside In 2H",
            "teaser": "t2", "benzinga_id": "a2", "published": now_iso,
        }]
        mock_score.return_value = {"a2": {
            "sentiment": "positive", "confidence": 0.85,
            "catalyst_type": "earnings_beat", "already_moved": False,
            "catalyst_magnitude": 2,
        }}
        f.fetch_all_news(seen_checker=lambda a, t: False)
        second_payload = mock_score.call_args.args[0]
        assert "prior_context" in second_payload[0]
        assert "negative" in second_payload[0]["prior_context"]
        assert "Tumbles" in second_payload[0]["prior_context"]


# ── Catalyst magnitude tests ─────────────────────────────────────────────────

class TestCatalystMagnitude:
    """Tests for catalyst_magnitude field in news/fetcher.py"""

    def _mock_tool_response(self, classifications):
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    @patch("news.fetcher._claude")
    def test_magnitude_parsed_from_response(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([
            {"id": "1", "sentiment": "positive", "confidence": 0.95,
             "catalyst_type": "fda_approval", "already_moved": False,
             "catalyst_magnitude": 5},
        ])
        scores = _batch_score_sentiment([{"id": "1", "headline": "FDA approval", "teaser": ""}])
        assert scores["1"]["catalyst_magnitude"] == 5

    @patch("news.fetcher._claude")
    def test_missing_magnitude_defaults_to_one(self, mock_claude):
        """Old Claude responses without catalyst_magnitude default to 1 (noise)."""
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_tool_response([
            {"id": "1", "sentiment": "positive", "confidence": 0.9,
             "catalyst_type": "earnings_beat", "already_moved": False},
            # no catalyst_magnitude key
        ])
        scores = _batch_score_sentiment([{"id": "1", "headline": "Beat", "teaser": ""}])
        assert scores["1"]["catalyst_magnitude"] == 1


# ── Broker reconciliation tests ───────────────────────────────────────────────

class TestBrokerReconciliation:
    """Tests for monitor/position_monitor.py::_reconcile_positions"""

    def setup_method(self):
        # v20 throttles reconciliation to once/60s. time.monotonic()'s epoch
        # is undefined by spec — on some hosts (fresh CI containers) it can
        # start near 0, so resetting the "last checked" marker to a literal
        # 0.0 does NOT reliably read as "long enough ago": now - 0.0 can
        # itself be under 60s, silently re-throttling every test in this
        # class (observed in CI while passing locally, where uptime keeps
        # monotonic() large). -inf is "long ago" regardless of the host's
        # monotonic baseline.
        import monitor.position_monitor as pm
        pm._last_reconcile_ts = float("-inf")
        # v21.2 two-pass confirmation state: clear so a suspect recorded by one
        # test can't promote a first sighting to CRITICAL in another.
        pm._suspect_orphans = set()
        pm._suspect_phantoms = set()

    teardown_method = setup_method

    def _trade(self, trade_id, ticker, qty=10.0):
        return {"id": trade_id, "ticker": ticker, "quantity": qty,
                "buy_price": 100.0, "buy_time": "2026-06-17T13:00:00+00:00",
                "tp_order_id": None, "mode": "demo"}

    @staticmethod
    def _run_pass(db_trades):
        """One reconcile pass, defeating the 60s throttle between calls."""
        import monitor.position_monitor as pm
        pm._last_reconcile_ts = float("-inf")
        pm._reconcile_positions(db_trades)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_phantom_two_passes_logged(self, mock_broker, caplog):
        """DB-open trade absent from broker on two consecutive passes →
        CRITICAL; the first sighting alone is only INFO (v21.2)."""
        import logging
        mock_broker.return_value = {}  # broker has nothing
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            self._run_pass([self._trade(42, "AAPL_US_EQ")])
            assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)
            assert any("confirming next pass" in r.message for r in caplog.records)
            self._run_pass([self._trade(42, "AAPL_US_EQ")])
        assert any("RECONCILIATION" in r.message and "AAPL_US_EQ" in r.message
                   and "OPEN in DB but NOT in broker" in r.message
                   for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_orphan_two_passes_logged(self, mock_broker, caplog):
        """Broker position with no DB row on two consecutive passes →
        CRITICAL; the first sighting alone is only INFO (v21.2)."""
        import logging
        mock_broker.return_value = {"TSLA_US_EQ": 5.0}
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            self._run_pass([])  # no DB trades — reconcile must still run
            assert not any(r.levelno >= logging.CRITICAL for r in caplog.records)
            assert any("confirming next pass" in r.message for r in caplog.records)
            self._run_pass([])
        assert any("RECONCILIATION" in r.message and "TSLA_US_EQ" in r.message
                   and "broker holds" in r.message
                   for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_orphan_resolved_between_passes_not_logged(self, mock_broker, caplog):
        """The benign entry race: buy filled, open_trade() committed between
        passes. The DB row is visible on the second pass → never CRITICAL."""
        import logging
        mock_broker.return_value = {"NVDA_US_EQ": 3.0}
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            self._run_pass([])  # snapshot race: row not committed yet
            self._run_pass([self._trade(7, "NVDA_US_EQ", qty=3.0)])  # committed
        assert not any("RECONCILIATION" in r.message for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_phantom_resolved_between_passes_not_logged(self, mock_broker, caplog):
        """The benign exit race: sell filled, close_trade() committed between
        passes. The trade is gone from the DB on the second pass → never
        CRITICAL (the v21.1 grace window never covered this side)."""
        import logging
        mock_broker.return_value = {}
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            self._run_pass([self._trade(9, "AMD_US_EQ")])  # close committing
            self._run_pass([])  # closed
        assert not any("RECONCILIATION" in r.message for r in caplog.records)

    @patch("monitor.position_monitor.touch_heartbeat")
    @patch("monitor.position_monitor.get_open_trades")
    @patch("monitor.position_monitor.get_broker_positions")
    def test_reconcile_runs_when_db_flat(self, mock_broker, mock_trades,
                                         _mock_hb, caplog):
        """monitor_positions must reconcile even with zero open trades — a
        failed open_trade() on the only position leaves the DB empty, and
        pre-v21.2 the flat early-out made that orphan invisible forever."""
        import logging
        import monitor.position_monitor as pm
        mock_broker.return_value = {"ORCL_US_EQ": 4.0}
        mock_trades.return_value = []
        with caplog.at_level(logging.INFO, logger="monitor.position_monitor"):
            pm._last_reconcile_ts = float("-inf")
            pm.monitor_positions()
            pm._last_reconcile_ts = float("-inf")
            pm.monitor_positions()
        assert any("RECONCILIATION" in r.message and "ORCL_US_EQ" in r.message
                   for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_api_failure_skips_silently(self, mock_broker, caplog):
        """get_broker_positions() returns None (API failure) → no alerts."""
        import logging
        mock_broker.return_value = None
        from monitor.position_monitor import _reconcile_positions
        with caplog.at_level(logging.CRITICAL, logger="monitor.position_monitor"):
            _reconcile_positions([self._trade(1, "AAPL_US_EQ")])
        assert not any("RECONCILIATION" in r.message for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_matching_positions_no_alert(self, mock_broker, caplog):
        """DB and broker agree → no CRITICAL logs."""
        import logging
        mock_broker.return_value = {"AAPL_US_EQ": 10.0}
        from monitor.position_monitor import _reconcile_positions
        with caplog.at_level(logging.CRITICAL, logger="monitor.position_monitor"):
            _reconcile_positions([self._trade(1, "AAPL_US_EQ")])
        assert not any("RECONCILIATION" in r.message for r in caplog.records)


# ── Position sizing tests ─────────────────────────────────────────────────────

class TestPositionSizing:
    """Tests for trading/executor.py::calculate_quantity"""

    def _mock_cash(self, total, free):
        return {"total": total, "free": free, "invested": total - free}

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.0)
    @patch("trading.executor._get")
    def test_quantity_respects_max_position_pct(self, mock_get, _mock_fx):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=10000.0)
        # Hard cap binds: 5% of £10,000 = £500 (risk cap is 0.25%/2% = £1,250).
        # FX mocked to 1.0 so GBP budget == USD spend; at $100/share = 5 shares.
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert err is None
        assert quantity == pytest.approx(5.0, rel=1e-4)

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.0)
    @patch("trading.executor._get")
    def test_quantity_capped_by_available_cash(self, mock_get, _mock_fx):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=200.0)
        # 5% of £10,000 = £500, but only £200 cash available → use £200.
        # FX mocked to 1.0 so £200 budget == $200; at $100/share = 2 shares.
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert err is None
        assert quantity == pytest.approx(2.0, rel=1e-4)

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.0)
    @patch("trading.executor._get")
    def test_quantity_capped_by_adv_participation(self, mock_get, _mock_fx):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=10000.0)
        # ADV participation cap: 0.5% of $20,000 ADV = $100 USD cap.
        # Divided by fx=1.0 → £100 GBP cap, which binds below the £500 hard cap.
        # At $100/share = 1 share. This is what keeps exits from moving thin books.
        quantity, err = calculate_quantity("THIN_US_EQ", price=100.0, avg_dollar_volume=20_000)
        assert err is None
        assert quantity == pytest.approx(1.0, rel=1e-4)

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.0)
    @patch("trading.executor._get")
    def test_zero_cash_returns_none(self, mock_get, _mock_fx):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=1000.0, free=0.0)
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None
        assert err is not None

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._get")
    def test_api_failure_returns_none(self, mock_get, _mock_sleep):
        from trading.executor import calculate_quantity
        mock_get.side_effect = Exception("HTTP 401")
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None
        assert err is not None
        # Retried once (see TestCashLookupRetry) before giving up.
        assert mock_get.call_count == 2


# ── Cash-lookup retry tests (v21.7 — ITW post-mortem) ─────────────────────────

class TestCashLookupRetry:
    """
    Tests for trading/executor.py::calculate_quantity — one retry on a
    transient cash-API failure.

    2026-07-28: ITW cleared every gate (catalyst, confidence, price/momentum/
    VWAP/liquidity), was logged APPROVED, then died outright on a single
    un-retried HTTP 429 from this exact call — on a day with only 2 total
    429s all day. The signal was never retried (buy_failed was not a
    transient rejection code), so a fully-qualified entry was lost to one
    rate-limit blip.
    """

    def _mock_cash(self, total=5000.0, free=5000.0):
        return {"total": total, "free": free, "invested": 0.0}

    @patch("trading.executor.time.sleep")
    @patch("trading.executor.get_gbp_usd_rate", return_value=1.0)
    @patch("trading.executor._get")
    def test_transient_429_recovers_on_retry(self, mock_get, _mock_fx, mock_sleep):
        from trading.executor import calculate_quantity
        mock_get.side_effect = [Exception("HTTP 429 - TooManyRequests"), self._mock_cash()]
        quantity, err = calculate_quantity("ITW_US_EQ", price=100.0)
        assert err is None
        assert quantity is not None
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._get")
    def test_persistent_failure_still_fails_after_retry(self, mock_get, _mock_sleep):
        from trading.executor import calculate_quantity
        mock_get.side_effect = Exception("HTTP 429 - TooManyRequests")
        quantity, err = calculate_quantity("ITW_US_EQ", price=100.0)
        assert quantity is None
        assert "429" in err
        assert mock_get.call_count == 2


# ── Precision retry tests ─────────────────────────────────────────────────────

class TestBuyPrecisionRetry:
    """Tests for trading/executor.py::buy — precision mismatch auto-retry"""

    def _mock_cash(self, total=5000.0, free=5000.0):
        return {"total": total, "free": free, "invested": 0.0}

    def _precision_error(self, allowed: int) -> Exception:
        from trading.executor import T212HTTPError
        body = (
            f'{{"type":"/api-errors/quantity-precision-mismatch",'
            f'"title":"Error while placing the order",'
            f'"status":400,'
            f'"detail":"invalid quantity precision {allowed}",'
            f'"traceId":"abc"}}'
        )
        return T212HTTPError(400, body)

    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_precision_retry_succeeds(self, mock_get, mock_post, mock_fill):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        # First call raises precision error (precision 2 allowed); second call succeeds
        mock_post.side_effect = [self._precision_error(2), {"id": "99"}]
        result = buy("BCDA_US_EQ", price=1.51)
        assert result.success is True
        assert mock_post.call_count == 2
        second_call_payload = mock_post.call_args[0][1]
        second_call_qty = second_call_payload["quantity"]
        qty_str = str(second_call_qty)
        if "." in qty_str:
            decimal_places = len(qty_str.rstrip("0").split(".")[-1])
        else:
            decimal_places = 0
        assert decimal_places <= 2

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_non_precision_business_rejection_does_not_retry(self, mock_get, mock_post, _sleep):
        """A non-retryable 4xx (not precision-mismatch, not 429) fails on the first attempt."""
        from trading.executor import buy, T212HTTPError
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = T212HTTPError(400, '{"detail":"insufficient funds"}')
        result = buy("AAPL_US_EQ", price=100.0)
        assert result.success is False
        assert mock_post.call_count == 1

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_transient_5xx_on_order_placement_retries_once(self, mock_get, mock_post, _fill, _sleep):
        """A 500/429 placing the actual order is retried once (v21.9) — this call
        is the live order, not a pre-check, so losing it to one rate-limit blip
        is strictly worse than the already-fixed cash-lookup case."""
        from trading.executor import buy, T212HTTPError
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = [T212HTTPError(500, "Internal server error"), {"id": "99"}]
        result = buy("AAPL_US_EQ", price=100.0)
        assert result.success is True
        assert mock_post.call_count == 2

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_persistent_5xx_on_order_placement_fails_after_one_retry(self, mock_get, mock_post, _sleep):
        from trading.executor import buy, T212HTTPError
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = T212HTTPError(500, "Internal server error")
        result = buy("AAPL_US_EQ", price=100.0)
        assert result.success is False
        assert mock_post.call_count == 2

    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_precision_retry_still_fails(self, mock_get, mock_post):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        # v21.19: the precision retry now gets the SAME one-shot retry the
        # initial placement always had, so a TRANSIENT failure there is retried
        # rather than being instantly terminal. That asymmetry is what killed
        # three LB_US_EQ signals on 2026-08-26 when a rate limit landed on the
        # precision retry. A bare Exception counts as a network error, hence
        # three POSTs: placement, precision retry, retry-of-the-retry.
        mock_post.side_effect = [
            self._precision_error(2), Exception("HTTP 500"), Exception("HTTP 500"),
        ]
        result = buy("BCDA_US_EQ", price=1.51)
        assert result.success is False
        assert mock_post.call_count == 3

    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_precision_retry_does_not_retry_a_permanent_error(
        self, mock_get, mock_post
    ):
        # The counterpart: 401/403/404 will fail identically on retry, so
        # spending a second order request on them is exactly how an account
        # reaches a rate limit. One placement, one precision retry, stop.
        from trading.executor import buy, T212HTTPError
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = [
            self._precision_error(2), T212HTTPError(403, "forbidden"),
        ]
        result = buy("BCDA_US_EQ", price=1.51)
        assert result.success is False
        assert mock_post.call_count == 2


class TestSellExecution:
    """Tests for trading/executor.py::sell execution policy"""

    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post", return_value={"id": "eod-1"})
    def test_eod_flatten_uses_market_order(self, mock_post, _mock_fill):
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="eod_flatten")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/market"

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor.get_order_status", return_value="FILLED")
    @patch("trading.executor._post", return_value={"id": "sl-1"})
    def test_stop_loss_uses_limit_order(self, mock_post, _mock_status, _mock_fill, _mock_sleep):
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="stop_loss")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/limit"

    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post", return_value={"id": "em-1"})
    def test_emergency_flatten_uses_market_order(self, mock_post, _mock_fill):
        # An unrecorded buy must exit at market — a limit that fails to fill
        # leaves an invisible unmanaged position with no stop or EOD logic.
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="eod_flatten")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/market"


class TestExcursionTracking:
    """
    v21.10: MFE/MAE instrumentation. The monitor already computes unrealised
    P&L every 5s; persisting the running extremes is what makes the
    trailing-stop-vs-time-stop question answerable from our own data instead
    of being capped at n=8 by yfinance's 30-day 1-min retention.

    Contract: widen-only, writes only on a NEW extreme, never breaks the
    monitor loop, and a failed write is retried rather than assumed landed.
    """

    def setup_method(self):
        import monitor.position_monitor as pm
        pm._excursion_seen.clear()

    teardown_method = setup_method

    @patch("monitor.position_monitor.update_trade_excursion")
    def test_first_reading_always_writes(self, mock_upd):
        from monitor.position_monitor import _record_excursion
        _record_excursion(1, 1.5)
        mock_upd.assert_called_once_with(1, 1.5)

    @patch("monitor.position_monitor.update_trade_excursion")
    def test_reading_inside_known_band_does_not_write(self, mock_upd):
        """The 5s poll must not hammer the DB when nothing new happened."""
        from monitor.position_monitor import _record_excursion
        _record_excursion(1, 3.0)     # establishes (3.0, 3.0)
        _record_excursion(1, -1.0)    # widens low  -> (3.0, -1.0)
        mock_upd.reset_mock()
        _record_excursion(1, 0.5)     # inside band — no write
        _record_excursion(1, 2.9)     # inside band — no write
        mock_upd.assert_not_called()

    @patch("monitor.position_monitor.update_trade_excursion")
    def test_new_high_and_new_low_both_write(self, mock_upd):
        from monitor.position_monitor import _record_excursion
        import monitor.position_monitor as pm
        _record_excursion(1, 1.0)
        _record_excursion(1, 4.2)     # new high
        _record_excursion(1, -2.5)    # new low
        assert mock_upd.call_count == 3
        assert pm._excursion_seen[1] == (4.2, -2.5)

    @patch("monitor.position_monitor.update_trade_excursion", side_effect=Exception("db down"))
    def test_write_failure_is_logged_and_not_cached(self, _mock_upd, caplog):
        """A failed write must NOT poison the cache — otherwise that extreme
        is lost forever because later cycles think it was already recorded."""
        import logging
        import monitor.position_monitor as pm
        from monitor.position_monitor import _record_excursion
        with caplog.at_level(logging.WARNING, logger="monitor.position_monitor"):
            _record_excursion(1, 3.0)
        assert any("db down" in rec.message for rec in caplog.records)
        assert 1 not in pm._excursion_seen  # retried next cycle

    @patch("monitor.position_monitor.update_trade_excursion")
    def test_trades_are_tracked_independently(self, mock_upd):
        from monitor.position_monitor import _record_excursion
        import monitor.position_monitor as pm
        _record_excursion(1, 5.0)
        _record_excursion(2, -3.0)
        assert pm._excursion_seen[1] == (5.0, 5.0)
        assert pm._excursion_seen[2] == (-3.0, -3.0)


class TestClearRestingLogsOnFailure:
    """
    v21.9: _clear_resting's DB write must not fail silently. A failed write
    here leaves the DB row pointing at an order id that was just
    cancelled/resolved at the broker while the in-memory trade object says
    otherwise — a real state divergence, previously swallowed with a bare
    `except Exception: pass` and zero log trace.
    """

    def _trade(self):
        return {"id": 7, "stop_order_id": "old-stop-1", "tp_order_id": None}

    @patch("monitor.position_monitor.set_stop_order_id", side_effect=Exception("db down"))
    def test_db_failure_is_logged_not_swallowed(self, _mock_set, caplog):
        import logging
        from monitor.position_monitor import _clear_resting
        with caplog.at_level(logging.ERROR, logger="monitor.position_monitor"):
            _clear_resting(self._trade(), "stop")
        assert any("db down" in rec.message for rec in caplog.records)

    @patch("monitor.position_monitor.set_stop_order_id", side_effect=Exception("db down"))
    def test_in_memory_state_still_cleared_despite_db_failure(self, _mock_set):
        from monitor.position_monitor import _clear_resting
        trade = self._trade()
        _clear_resting(trade, "stop")
        assert trade["stop_order_id"] is None


class TestGoneRestingOrderResolution:
    """Tests for monitor/position_monitor.py::_handle_gone_resting_order

    A resting order that 404s on the pending endpoint is NOT automatically a
    fill. DAY orders expire at close; treating expiry as an exit corrupts P&L
    and leaves the real position unmanaged.
    """

    def _trade(self, buy_price=100.0, kind="tp"):
        buy_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        trade = {
            "id": 42,
            "ticker": "TEST_US_EQ",
            "buy_price": buy_price,
            "quantity": 10.0,
            "buy_time": buy_time,
            "tp_order_id": None,
            "stop_order_id": None,
            "mode": "demo",
        }
        trade["tp_order_id" if kind == "tp" else "stop_order_id"] = "order-99"
        return trade

    @patch("monitor.position_monitor.set_tp_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill")
    def test_gone_with_fill_closes_trade_as_tp(self, mock_fetch, mock_close, mock_set_tp):
        """GONE + fill detail (legacy TP) → trade closed as take_profit, returns True."""
        mock_fetch.return_value = {
            "price": "105.00",
            "walletImpact": {"netValue": "52.30", "fxRate": "1.25", "taxes": []},
        }
        from monitor.position_monitor import _handle_gone_resting_order
        trade = self._trade(kind="tp")
        result = _handle_gone_resting_order(trade, "order-99", "tp")
        assert result is True
        mock_close.assert_called_once()
        assert mock_close.call_args.args[2] == "take_profit"
        mock_set_tp.assert_not_called()

    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill")
    def test_gone_with_fill_closes_trade_as_stop(self, mock_fetch, mock_close, mock_set_stop):
        """GONE + fill detail (v20 stop) → trade closed as stop_loss, returns True."""
        mock_fetch.return_value = {
            "price": "97.90",
            "walletImpact": {"netValue": "48.10", "fxRate": "1.25", "taxes": []},
        }
        from monitor.position_monitor import _handle_gone_resting_order
        trade = self._trade(kind="stop")
        result = _handle_gone_resting_order(trade, "order-99", "stop")
        assert result is True
        mock_close.assert_called_once()
        assert mock_close.call_args.args[2] == "stop_loss"
        mock_set_stop.assert_not_called()

    @patch("monitor.position_monitor.set_tp_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill", return_value=None)
    def test_gone_without_fill_reverts_to_polled_exits(self, _mock_fetch, mock_close, mock_set_tp):
        """GONE + no fill detail → stale order id cleared, trade stays open, returns False."""
        from monitor.position_monitor import _handle_gone_resting_order
        trade = self._trade(kind="tp")
        result = _handle_gone_resting_order(trade, "order-99", "tp")
        assert result is False
        mock_close.assert_not_called()
        mock_set_tp.assert_called_once_with(42, None)
        assert trade["tp_order_id"] is None

    @patch("monitor.position_monitor.set_stop_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill", return_value=None)
    def test_gone_stop_without_fill_reverts_to_polled_stop(self, _mock_fetch, mock_close, mock_set_stop):
        from monitor.position_monitor import _handle_gone_resting_order
        trade = self._trade(kind="stop")
        result = _handle_gone_resting_order(trade, "order-99", "stop")
        assert result is False
        mock_close.assert_not_called()
        mock_set_stop.assert_called_once_with(42, None)
        assert trade["stop_order_id"] is None


class TestCashflowPnl:
    """Tests for storage/database.py cashflow sign normalization"""

    def test_positive_buy_cost_positive_sell_proceeds(self):
        from storage.database import _pnl_from_cashflows
        pnl, pct = _pnl_from_cashflows(100.0, 110.0)
        assert pnl == pytest.approx(10.0)
        assert pct == pytest.approx(10.0)

    def test_negative_buy_wallet_impact_is_handled(self):
        from storage.database import _pnl_from_cashflows
        pnl, pct = _pnl_from_cashflows(-100.0, 110.0)
        assert pnl == pytest.approx(10.0)
        assert pct == pytest.approx(10.0)


class TestIsMarketOpen:
    """Tests for market/price_check.py::is_market_open

    The 2026-06-17 incident: a long-running pmc calendar object had stale DST
    state (treated EDT open as EST open), delaying market detection by 60 min
    and expiring all 19 pre-market candidates before evaluation. Fixed by using
    open_at_time() instead of a manual row comparison.
    """

    @patch("market.price_check._NYSE")
    def test_open_during_regular_hours(self, mock_nyse):
        import pandas as pd
        from market.price_check import is_market_open
        sched = MagicMock()
        sched.empty = False
        mock_nyse.schedule.return_value = sched
        mock_nyse.open_at_time.return_value = True
        assert is_market_open() is True

    @patch("market.price_check._NYSE")
    def test_closed_before_open(self, mock_nyse):
        import pandas as pd
        from market.price_check import is_market_open
        sched = MagicMock()
        sched.empty = False
        mock_nyse.schedule.return_value = sched
        mock_nyse.open_at_time.return_value = False
        assert is_market_open() is False

    @patch("market.price_check._NYSE")
    def test_holiday_returns_false(self, mock_nyse):
        from market.price_check import is_market_open
        sched = MagicMock()
        sched.empty = True
        mock_nyse.schedule.return_value = sched
        assert is_market_open() is False

    @patch("market.price_check._NYSE")
    def test_outside_session_window_returns_false(self, mock_nyse):
        """open_at_time() raises ValueError('not covered by the schedule') before/after hours → False, no Finnhub call."""
        from market.price_check import is_market_open
        sched = MagicMock()
        sched.empty = False
        mock_nyse.schedule.return_value = sched
        mock_nyse.open_at_time.side_effect = ValueError("The provided timestamp is not covered by the schedule")
        assert is_market_open() is False

    @patch("market.price_check.requests")
    @patch("market.price_check._NYSE")
    def test_schema_valueerror_falls_back_to_finnhub(self, mock_nyse, mock_requests):
        """open_at_time() raises a schema/column ValueError → falls through to Finnhub fallback, not silent False."""
        from market.price_check import is_market_open
        sched = MagicMock()
        sched.empty = False
        mock_nyse.schedule.return_value = sched
        mock_nyse.open_at_time.side_effect = ValueError("You seem to be using a schedule that isn't based on market_times")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"isOpen": True}
        mock_requests.get.return_value = mock_resp
        assert is_market_open() is True
        mock_requests.get.assert_called_once()


# ── RVOL normalization tests ──────────────────────────────────────────────────

class TestRvol:
    """Tests for market/price_check.py::compute_rvol / _expected_volume_fraction"""

    def test_volume_fraction_monotonic(self):
        from market.price_check import _expected_volume_fraction
        fractions = [_expected_volume_fraction(m) for m in range(0, 391, 15)]
        assert all(b >= a for a, b in zip(fractions, fractions[1:]))
        assert fractions[-1] == 1.0

    def test_volume_fraction_floor_at_open(self):
        from market.price_check import _expected_volume_fraction
        # Floored so RVOL can't divide by ~0 right at the open
        assert _expected_volume_fraction(0) >= 0.04

    def test_rvol_normal_day_is_one(self):
        from market.price_check import compute_rvol, _expected_volume_fraction
        # A stock trading exactly its typical pace shows RVOL ≈ 1.0 at any time
        avg = 1_000_000
        for minutes in (30, 120, 300):
            cum = int(avg * _expected_volume_fraction(minutes))
            assert compute_rvol(cum, avg, minutes) == pytest.approx(1.0, rel=1e-3)

    def test_rvol_zero_avg_volume(self):
        from market.price_check import compute_rvol
        assert compute_rvol(100_000, 0, 60) == 0.0

    def test_morning_volume_not_penalized(self):
        from market.price_check import compute_rvol, _expected_volume_fraction
        # Trading exactly the curve's expected fraction by 10:30 is a NORMAL
        # day (RVOL ≈ 1), not "low volume" — whatever that fraction is
        # calibrated to (2026-07-08: recalibrated from 0.25 to 0.11 at minute
        # 60 against measured real volume; this test must track the curve,
        # not a hardcoded snapshot of it).
        avg = 1_000_000
        cum = int(avg * _expected_volume_fraction(60))
        rvol = compute_rvol(cum, avg, 60)
        assert rvol == pytest.approx(1.0, rel=1e-3)


# ── Symbol hygiene tests (v15) ────────────────────────────────────────────────

class TestSymbolCleaning:
    """Tests for trading/executor.py::clean_benzinga_symbol / resolve_t212_ticker"""

    def test_plain_us_symbol_unchanged(self):
        from trading.executor import clean_benzinga_symbol
        assert clean_benzinga_symbol("AAPL") == "AAPL"
        assert clean_benzinga_symbol("aapl") == "AAPL"  # uppercased

    def test_foreign_exchange_prefix_dropped(self):
        from trading.executor import clean_benzinga_symbol
        # The 2026-06-15 leak: TSX:MDA reached the price check and burned an
        # eval window. Foreign listings are not US-tradeable → drop entirely.
        assert clean_benzinga_symbol("TSX:MDA") is None
        assert clean_benzinga_symbol("LON:VOD") is None
        assert clean_benzinga_symbol("ASX:BHP") is None

    def test_unknown_exchange_prefix_dropped(self):
        from trading.executor import clean_benzinga_symbol
        # Any colon = exchange routing we don't recognise → not a clean US symbol
        assert clean_benzinga_symbol("XYZ:ABC") is None

    def test_disambiguation_digit_stripped(self):
        from trading.executor import clean_benzinga_symbol
        # Benzinga collision suffix: INBX1 → INBX, SAIL1 → SAIL
        assert clean_benzinga_symbol("INBX1") == "INBX"
        assert clean_benzinga_symbol("SAIL1") == "SAIL"

    def test_short_symbols_not_mangled(self):
        from trading.executor import clean_benzinga_symbol
        # Guard: only strip the digit when stem stays a plausible 2+ char symbol
        # and length >= 4. Short tickers with digits are left alone.
        assert clean_benzinga_symbol("BV1") == "BV1"   # too short to strip
        assert clean_benzinga_symbol("F") == "F"       # single-letter US ticker

    def test_class_share_dot_preserved(self):
        from trading.executor import clean_benzinga_symbol
        assert clean_benzinga_symbol("BRK.A") == "BRK.A"

    def test_empty_returns_none(self):
        from trading.executor import clean_benzinga_symbol
        assert clean_benzinga_symbol("") is None

    def test_resolve_returns_none_for_foreign(self):
        from trading.executor import resolve_t212_ticker
        assert resolve_t212_ticker("TSX:MDA") is None

    def test_resolve_appends_us_eq_fallback(self):
        from trading.executor import resolve_t212_ticker
        # Not in the (empty in tests) symbol map → fallback format
        assert resolve_t212_ticker("AAPL") == "AAPL_US_EQ"
        # With cleaning applied first
        assert resolve_t212_ticker("INBX1") == "INBX_US_EQ"


# ── Quote fallback tests (v15) ────────────────────────────────────────────────

class TestQuoteFallback:
    """Tests for market/price_check.py::get_quote_with_fallback"""

    @patch("market.price_check.get_twelvedata_quote")
    @patch("market.price_check.get_finnhub_quote")
    def test_finnhub_used_when_available(self, mock_finnhub, mock_td):
        from market.price_check import get_quote_with_fallback
        mock_finnhub.return_value = {"c": 100.0, "o": 99.0, "pc": 98.0}
        q = get_quote_with_fallback("AAPL")
        assert q["c"] == 100.0
        mock_td.assert_not_called()  # no fallback when primary answers

    @patch("market.price_check.get_twelvedata_quote")
    @patch("market.price_check.get_finnhub_quote")
    def test_falls_back_to_twelvedata(self, mock_finnhub, mock_td):
        from market.price_check import get_quote_with_fallback
        mock_finnhub.return_value = None  # Finnhub has no coverage
        mock_td.return_value = {"c": 8.15, "o": 8.0, "pc": 3.97}
        q = get_quote_with_fallback("CUPR")
        assert q["c"] == 8.15
        assert q["pc"] == 3.97
        mock_td.assert_called_once()

    @patch("market.price_check.get_twelvedata_quote")
    @patch("market.price_check.get_finnhub_quote")
    def test_returns_none_when_both_fail(self, mock_finnhub, mock_td):
        from market.price_check import get_quote_with_fallback
        mock_finnhub.return_value = None
        mock_td.return_value = None
        assert get_quote_with_fallback("NOPE") is None

    @patch("market.price_check.get_twelvedata_quote")
    @patch("market.price_check.get_finnhub_quote")
    def test_pc_backfilled_when_finnhub_pc_zero(self, mock_finnhub, mock_td):
        # Regression (2026-06-16): Finnhub returns a valid price but pc=0 in the
        # first minutes after open. Must backfill prev close from Twelvedata
        # rather than returning the unusable pc — otherwise every premarket
        # candidate (OTLK +27%, SPCB +18%) is terminally rejected "no prev close".
        from market.price_check import get_quote_with_fallback
        mock_finnhub.return_value = {"c": 1.53, "o": 1.19, "pc": 0}
        mock_td.return_value = {"c": 1.52, "o": 1.19, "pc": 1.16}
        q = get_quote_with_fallback("OTLK")
        assert q["c"] == 1.53           # keeps Finnhub's real-time price
        assert q["pc"] == 1.16          # backfilled from Twelvedata
        mock_td.assert_called_once()

    @patch("market.price_check.get_twelvedata_quote")
    @patch("market.price_check.get_finnhub_quote")
    def test_no_backfill_when_finnhub_pc_valid(self, mock_finnhub, mock_td):
        # When Finnhub's pc is good, don't burn a Twelvedata credit.
        from market.price_check import get_quote_with_fallback
        mock_finnhub.return_value = {"c": 100.0, "o": 99.0, "pc": 98.0}
        q = get_quote_with_fallback("AAPL")
        assert q["pc"] == 98.0
        mock_td.assert_not_called()


# ── Session analysis tests (v20: one pull feeds every gate) ──────────────────

class TestVwap:
    """VWAP math inside market/twelvedata_bars.py::get_session_analysis"""

    def _bar(self, dt, h, l, c, v):
        return {"datetime": dt, "high": str(h), "low": str(l), "close": str(c), "volume": str(v)}

    def _today_bars(self):
        """Two same-day ET bars (newest first), so the session filter keeps both."""
        from datetime import datetime
        import pytz
        now_et = datetime.now(pytz.timezone("America/New_York"))
        d = now_et.strftime("%Y-%m-%d")
        # Heavy volume (900) at price 10, light (100) at 20. Times recent
        # enough to pass the 10-min staleness guard.
        t1 = (now_et - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
        t2 = (now_et - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:00")
        return [
            self._bar(t1, 20, 20, 20, 100),  # newest first
            self._bar(t2, 10, 10, 10, 900),
        ]

    @patch("market.twelvedata_bars._get_time_series")
    def test_vwap_weighted_by_volume(self, mock_ts):
        import market.twelvedata_bars as td
        mock_ts.return_value = self._today_bars()
        sa = td.get_session_analysis("AAPL")
        # typical prices 20 and 10; volume-weighted (20*100 + 10*900)/1000 = 11.0
        assert sa is not None
        assert sa.vwap == pytest.approx(11.0, rel=1e-6)
        assert sa.last_price == 20.0  # most recent bar's close

    @patch("market.twelvedata_bars._get_time_series")
    def test_none_when_no_data(self, mock_ts):
        import market.twelvedata_bars as td
        mock_ts.return_value = None
        assert td.get_session_analysis("AAPL") is None

    @patch("market.twelvedata_bars._get_time_series")
    def test_stale_bars_keep_aggregates_but_drop_momentum(self, mock_ts):
        # VECO guard: newest today-bar >10 min old → momentum fields None;
        # the cumulative session aggregates are still valid as-of-last-print.
        from datetime import datetime
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        stale = (now_et - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:00")
        mock_ts.return_value = [self._bar(stale, 10.2, 10.0, 10.1, 5000)]
        sa = td.get_session_analysis("AAPL")
        assert sa is not None
        assert sa.past_price is None and sa.current_bar_price is None
        assert sa.session_volume == 5000 and sa.vwap is not None

    @patch("market.twelvedata_bars._get_time_series")
    def test_baseline_never_comes_from_yesterday(self, mock_ts):
        # Pre-existing bug fixed in v20: right after the open, "the newest bar
        # ≥ lookback old" used to match YESTERDAY'S 15:59 bar, silently
        # treating the overnight gap as 5-minute momentum. Today-only now.
        from datetime import datetime
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        d = now_et.strftime("%Y-%m-%d")
        y = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        fresh = (now_et - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
        mock_ts.return_value = [
            self._bar(fresh, 15.1, 14.9, 15.0, 1000),      # today, fresh
            self._bar(f"{y} 15:59:00", 10.1, 9.9, 10.0, 8000),  # yesterday
        ]
        sa = td.get_session_analysis("AAPL")
        assert sa is not None
        assert sa.past_price is None  # no today-bar old enough — NOT 10.0
        assert sa.session_volume == 1000  # yesterday's bar excluded


class TestDailyStats:
    """market/twelvedata_bars.py::get_daily_stats — ADV/prev-close, day-cached."""

    def setup_method(self):
        import market.twelvedata_bars as td
        td._daily_stats_cache.clear()

    teardown_method = setup_method

    def _daily_bar(self, dt, close=10, volume=1000):
        return {"datetime": dt, "close": str(close), "volume": str(volume)}

    @patch("market.twelvedata_bars._get_time_series")
    def test_prior_sessions_only_in_adv(self, mock_ts):
        # Today's partial daily bar (when rolled) must be EXCLUDED from ADV.
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today = now_et.strftime("%Y-%m-%d")
        yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        mock_ts.return_value = [
            self._daily_bar(today, close=11, volume=500),
            self._daily_bar(yesterday, close=10, volume=1000),
        ]
        stats = td.get_daily_stats("AAPL")
        assert stats is not None
        avg_vol, adv_dollars, prev_close = stats
        assert avg_vol == 1000
        assert adv_dollars == pytest.approx(10_000)
        assert prev_close == pytest.approx(10)

    @patch("market.twelvedata_bars._get_time_series")
    def test_unrolled_daily_bar_still_gives_adv_and_prev_close(self, mock_ts):
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        before = (now_et - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_ts.return_value = [
            self._daily_bar(yesterday, close=10, volume=5000),
            self._daily_bar(before, close=9, volume=1000),
        ]
        stats = td.get_daily_stats("AAPL")
        assert stats is not None
        avg_vol, adv_dollars, prev_close = stats
        assert avg_vol == pytest.approx(3000)
        assert adv_dollars == pytest.approx(30_000)
        assert prev_close == pytest.approx(10)

    @patch("market.twelvedata_bars._get_time_series")
    def test_second_call_same_day_hits_cache(self, mock_ts):
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        before = (now_et - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_ts.return_value = [
            self._daily_bar(yesterday, close=10, volume=5000),
            self._daily_bar(before, close=9, volume=1000),
        ]
        first = td.get_daily_stats("AAPL")
        second = td.get_daily_stats("AAPL")
        assert first == second
        mock_ts.assert_called_once()  # the whole point: one HTTP per symbol per day

    @patch("market.twelvedata_bars._get_time_series", return_value=None)
    def test_failures_are_not_cached(self, mock_ts):
        import market.twelvedata_bars as td
        assert td.get_daily_stats("AAPL") is None
        assert td.get_daily_stats("AAPL") is None
        assert mock_ts.call_count == 2  # each attempt retries the fetch


# ── Backtest ↔ production parity tests (v15 audit) ─────────────────────────────

class TestBacktestParity:
    """The backtest must test the SAME logic production runs."""

    def test_backtest_uses_v15_thresholds(self):
        # Constants are sourced from cfg, so they can't silently diverge from
        # production. Guards against a future edit hardcoding a stale value.
        import backtest.backtest_db as b
        from config.settings import cfg
        assert b.MIN_PRICE_MOVE_PCT == cfg.min_price_move_pct
        assert b.MAX_PRICE_MOVE_PCT == cfg.max_price_move_pct
        assert b.MIN_RVOL == cfg.min_rvol
        assert b.MAX_RVOL == cfg.max_rvol
        assert b.MAX_DAY_MOVE_PCT == cfg.max_day_move_pct
        assert b.MAX_SPREAD_PCT == cfg.max_spread_pct
        assert b.REQUIRE_VWAP_CONFIRM == cfg.require_vwap_confirmation

    def test_backtest_has_vwap_gate(self):
        import backtest.backtest_db as b
        # The VWAP confirmation helper must exist (v15 parity) and old alias kept
        assert hasattr(b, "_session_vwap_at")
        assert b.run_v12_check is b.run_v15_check

    def test_backtest_vwap_weighting(self):
        import backtest.backtest_db as b
        import pandas as pd
        from datetime import datetime, timezone
        # Two bars: heavy vol at price 10, light at 20 → VWAP weighted to 10
        idx = pd.to_datetime(
            ["2026-06-15 14:30", "2026-06-15 14:31"], utc=True
        )
        df = pd.DataFrame(
            {"High": [10, 20], "Low": [10, 20], "Close": [10, 20], "Volume": [900, 100]},
            index=idx,
        )
        vwap = b._session_vwap_at(df, datetime(2026, 6, 15, 14, 31, tzinfo=timezone.utc))
        assert vwap == pytest.approx(11.0, rel=1e-6)  # (10*900 + 20*100)/1000


# ── Backtest cost model tests ─────────────────────────────────────────────────

class TestBacktestCosts:
    """Tests for backtest/backtest_db.py::_slippage_pct / _apply_costs"""

    def test_slippage_tiers_decrease_with_liquidity(self):
        from backtest.backtest_db import _slippage_pct
        assert _slippage_pct(100_000_000) < _slippage_pct(10_000_000)
        assert _slippage_pct(10_000_000) < _slippage_pct(2_000_000)
        assert _slippage_pct(2_000_000) < _slippage_pct(500_000)

    def test_unknown_liquidity_assumed_thin(self):
        from backtest.backtest_db import _slippage_pct
        assert _slippage_pct(None) >= 0.5

    def test_costs_reduce_pnl(self):
        from backtest.backtest_db import _apply_costs, FX_COST_RT_PCT
        gross = 5.0
        net = _apply_costs(gross, 100_000_000)
        # Net = gross − FX RT − 2× slippage; always strictly below gross
        assert net < gross
        assert net == pytest.approx(gross - FX_COST_RT_PCT - 2 * 0.05)


# ── Pre-market eval hardening (2026-06-18 zero-trades incident) ────────────────

def _mk_conf(reason_code="approved", day_change_pct=5.0, is_confirmed=True):
    """Build a minimal PriceConfirmation for verdict-logic tests."""
    from market.price_check import PriceConfirmation
    return PriceConfirmation(
        ticker="X_US_EQ", symbol="X", current_price=10.0, open_price=9.5,
        prev_close=9.5, day_move_pct=5.0, day_change_pct=day_change_pct,
        recent_move_pct=1.0, current_volume=1000, avg_volume=500, rvol=2.0,
        avg_dollar_volume=1e7, spread_proxy_pct=0.5,
        is_confirmed=is_confirmed, reason="test", reason_code=reason_code,
    )


def _resp(status_code, json_body=None):
    """Mock a requests.Response with a given status code / JSON body."""
    import requests as _rq
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = json_body or {}
    if status_code >= 400:
        m.raise_for_status.side_effect = _rq.exceptions.HTTPError(
            f"{status_code} Client Error"
        )
    else:
        m.raise_for_status.return_value = None
    return m


class TestTwelvedataFastQuote:
    """
    get_twelvedata_quote(fast=True) must NOT block on retry backoff — the
    pre-market window is time-boxed and every retry second decays the edge.
    A 404 must be terminal even in normal mode (the symbol doesn't exist).
    """

    @patch("market.twelvedata_bars.time.sleep")
    @patch("market.twelvedata_bars.requests.get")
    def test_fast_429_no_retry_no_sleep(self, mock_get, mock_sleep):
        from market.twelvedata_bars import get_twelvedata_quote
        mock_get.return_value = _resp(429)
        assert get_twelvedata_quote("SLOW", fast=True) is None
        assert mock_get.call_count == 1          # no retry
        mock_sleep.assert_not_called()           # no backoff burned

    @patch("market.twelvedata_bars.time.sleep")
    @patch("market.twelvedata_bars.requests.get")
    def test_fast_timeout_no_retry_no_sleep(self, mock_get, mock_sleep):
        import requests
        from market.twelvedata_bars import get_twelvedata_quote
        mock_get.side_effect = requests.exceptions.Timeout("boom")
        assert get_twelvedata_quote("SLOW", fast=True) is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("market.twelvedata_bars.time.sleep")
    @patch("market.twelvedata_bars.requests.get")
    def test_404_terminal_even_in_normal_mode(self, mock_get, mock_sleep):
        from market.twelvedata_bars import get_twelvedata_quote
        mock_get.return_value = _resp(404)
        assert get_twelvedata_quote("NOPE", fast=False) is None
        assert mock_get.call_count == 1          # 404 not retried 3×
        mock_sleep.assert_not_called()

    @patch("market.twelvedata_bars.time.sleep")
    @patch("market.twelvedata_bars.requests.get")
    def test_normal_429_does_retry(self, mock_get, mock_sleep):
        """Non-fast callers keep the full retry behaviour (regression guard)."""
        from market.twelvedata_bars import get_twelvedata_quote
        mock_get.return_value = _resp(429)
        assert get_twelvedata_quote("BUSY", fast=False) is None
        assert mock_get.call_count == 3          # 3 attempts
        assert mock_sleep.call_count == 3        # backoff between each

    @patch("market.twelvedata_bars.requests.get")
    def test_fast_success_returns_quote(self, mock_get):
        from market.twelvedata_bars import get_twelvedata_quote
        mock_get.return_value = _resp(200, {
            "close": "12.5", "open": "12.0", "previous_close": "11.0",
        })
        q = get_twelvedata_quote("OK", fast=True)
        assert q is not None and q["c"] == 12.5 and q["pc"] == 11.0


class TestPremarketEvalConcurrency:
    """
    evaluate_premarket_candidates must price-confirm candidates concurrently
    and never let one slow/dead ticker starve the rest — the root cause of the
    2026-06-18 zero-trades day (serial eval + retry backoff blew the cycle).
    """

    def _candidates(self, n):
        from datetime import datetime as _dt
        import pytz
        now = _dt.now(pytz.timezone("Europe/London")).isoformat()
        return [
            {"id": i, "ticker": f"T{i}_US_EQ", "headline": f"news {i}",
             "created_at": now}
            for i in range(n)
        ]

    @patch("premarket.scanner.update_premarket_candidate")
    @patch("premarket.scanner.get_pending_premarket_candidates")
    @patch("premarket.scanner._minutes_since_open", return_value=6.0)
    @patch("premarket.scanner.confirm_price_signal")
    def test_one_slow_ticker_does_not_block_others(
        self, mock_confirm, _mo, mock_pending, _upd
    ):
        import time as _t
        from premarket.scanner import evaluate_premarket_candidates
        mock_pending.return_value = self._candidates(6)

        def side_effect(ticker, fast=False):
            assert fast is True  # window must use the fast path
            _t.sleep(0.3)        # every confirm has the SAME latency
            return _mk_conf(reason_code="approved", day_change_pct=5.0)

        mock_confirm.side_effect = side_effect
        t0 = _t.monotonic()
        approved, graduated = evaluate_premarket_candidates()
        elapsed = _t.monotonic() - t0

        # 6 confirms at 0.3s each = 1.8s serial; in parallel (pool of 8) the wall
        # time is ~one call. <1.0s proves they ran concurrently, not summed.
        assert elapsed < 1.0
        assert len(approved) == 6  # all confirmed
        assert graduated == []  # well inside the 30-min eval window

    @patch("premarket.scanner._EVAL_CYCLE_BUDGET_SECONDS", 0.2)
    @patch("premarket.scanner.update_premarket_candidate")
    @patch("premarket.scanner.get_pending_premarket_candidates")
    @patch("premarket.scanner._minutes_since_open", return_value=6.0)
    @patch("premarket.scanner.confirm_price_signal")
    def test_budget_exceeded_leaves_candidate_pending(
        self, mock_confirm, _mo, mock_pending, mock_upd
    ):
        import time as _t
        from premarket.scanner import evaluate_premarket_candidates
        mock_pending.return_value = self._candidates(2)

        def side_effect(ticker, fast=False):
            if ticker == "T1_US_EQ":
                _t.sleep(0.6)    # exceeds the 0.2s budget
            return _mk_conf(reason_code="approved", day_change_pct=5.0)

        mock_confirm.side_effect = side_effect
        approved, _graduated = evaluate_premarket_candidates()
        # T0 resolves and is approved; T1 blows the budget → NOT given a verdict
        # (no status write) so it stays pending for the next cycle.
        approved_ids = {c["id"] for c, _ in approved}
        assert 0 in approved_ids
        assert 1 not in approved_ids
        # T1 must NOT have been written to any terminal status this cycle.
        written_ids = {call.args[0] for call in mock_upd.call_args_list}
        assert 1 not in written_ids


class TestOpenGraceNoQuote:
    """
    v19.4: a no-quote miss in the first _OPEN_GRACE_MINUTES doesn't burn a
    strike (2026-07-08: Twelvedata served a 24h-stale quote for ~19 tickers
    simultaneously in the first ~90s after the open, a systemic provider-cache
    glitch unrelated to any single ticker's real coverage).
    """

    def setup_method(self):
        import premarket.scanner as sc
        sc._no_quote_strikes.clear()

    teardown_method = setup_method

    @patch("premarket.scanner.update_premarket_candidate")
    def test_no_quote_in_grace_window_does_not_strike(self, mock_upd):
        import premarket.scanner as sc
        assert sc._apply_confirmation({"id": 1, "ticker": "A"}, None, minutes_open=0.5) is None
        assert 1 not in sc._no_quote_strikes
        mock_upd.assert_not_called()

    @patch("premarket.scanner.update_premarket_candidate")
    def test_no_quote_after_grace_window_strikes_normally(self, mock_upd):
        import premarket.scanner as sc
        assert sc._apply_confirmation({"id": 1, "ticker": "A"}, None, minutes_open=5.0) is None
        assert sc._no_quote_strikes[1] == 1

    @patch("premarket.scanner.update_premarket_candidate")
    def test_default_minutes_open_behaves_like_post_grace(self, mock_upd):
        # Callers that don't pass minutes_open (all pre-existing test call
        # sites) must keep striking normally, not silently get free retries.
        import premarket.scanner as sc
        assert sc._apply_confirmation({"id": 1, "ticker": "A"}, None) is None
        assert sc._no_quote_strikes[1] == 1


class TestPremarketGraduation:
    """
    v19.4: a premarket candidate that's still PENDING (never confirmed, never
    terminally rejected) when the 30-min gap-and-go window closes is handed
    off to the standard re-evaluation queue instead of being discarded outright
    (2026-07-08: KGS/ARQT/AYA/URGN drifted 1-3% higher over the rest of the
    session after their premarket window expired with no path back in).
    """

    @patch("premarket.scanner.update_premarket_candidate")
    def test_pending_candidate_past_window_is_graduated_not_dropped(self, mock_upd):
        import premarket.scanner as sc
        cand = {"id": 1, "ticker": "A_US_EQ",
                "created_at": datetime.now(timezone.utc).isoformat()}
        live, graduated = sc._live_candidates([cand], minutes_open=sc._EVAL_WINDOW_MINUTES + 1)
        assert live == []
        assert graduated == [cand]
        assert mock_upd.call_args.args[1] == "expired"

    @patch("premarket.scanner.update_premarket_candidate")
    def test_stale_prior_day_candidate_is_not_graduated(self, mock_upd):
        import premarket.scanner as sc
        from datetime import timedelta
        cand = {"id": 2, "ticker": "B_US_EQ",
                "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
        live, graduated = sc._live_candidates([cand], minutes_open=sc._EVAL_WINDOW_MINUTES + 1)
        assert live == []
        assert graduated == []  # stale — just dead, not handed off

    @patch("premarket.scanner.update_premarket_candidate")
    def test_still_inside_window_is_neither_live_nor_graduated_wrongly(self, mock_upd):
        import premarket.scanner as sc
        cand = {"id": 3, "ticker": "C_US_EQ",
                "created_at": datetime.now(timezone.utc).isoformat()}
        live, graduated = sc._live_candidates([cand], minutes_open=5.0)
        assert live == [cand]
        assert graduated == []

    @patch("main.was_recently_traded", return_value=False)
    @patch("main._execute_entry")
    @patch("main.evaluate_premarket_candidates")
    @patch("main._risk_gates_pass", return_value=(True, ""))
    @patch("main.is_too_late_to_buy", return_value=False)
    @patch("main.get_trading_session", return_value="regular")
    @patch("main.touch_heartbeat")
    def test_news_cycle_hands_off_graduated_candidates(
        self, _hb, _session, _late, _gates, mock_eval, mock_exec, _traded
    ):
        # news_cycle must route graduated candidates through _execute_entry
        # with an unconfirmed, transient-coded PriceConfirmation so they land
        # in the standard re-eval queue (same mechanism regular-hours signals
        # use), not just get logged and forgotten.
        import main
        cand = {"id": 5, "ticker": "D_US_EQ", "headline": "h",
                "article_id": "art-5", "confidence": 0.8,
                "catalyst_type": "fda_approval", "catalyst_magnitude": 3,
                "published_at": datetime.now(timezone.utc).isoformat()}
        mock_eval.return_value = ([], [cand])
        mock_exec.return_value = False

        with patch("main.fetch_all_news", return_value=[]):
            main.news_cycle()

        assert mock_exec.called
        item_arg, conf_arg, _fetched_at = mock_exec.call_args.args
        assert item_arg.ticker == "D_US_EQ"
        assert conf_arg.is_confirmed is False
        assert conf_arg.reason_code == "low_momentum"  # transient → re-eval queue


class TestPremarketBatchIsolation:
    """
    v21.9: a bug that only one candidate's data triggers must not silently
    drop every candidate queued behind it. Regression for the same SHAPE of
    bug as TestPremarketCandidateToNewsItem's 2026-06-11 drought (a single
    unhandled exception used to abort the whole premarket exec loop with one
    generic log line and no per-candidate DB trace) — this time verifying the
    loop itself isolates failures per-candidate rather than relying on every
    possible exception source inside it being pre-emptively fixed.
    """

    @patch("main.update_premarket_candidate")
    @patch("main._execute_entry")
    @patch("main._candidate_to_news_item")
    @patch("main.evaluate_premarket_candidates")
    @patch("main.was_recently_traded", return_value=False)
    @patch("main._risk_gates_pass", return_value=(True, ""))
    @patch("main.is_too_late_to_buy", return_value=False)
    @patch("main.get_trading_session", return_value="regular")
    @patch("main.touch_heartbeat")
    def test_one_bad_candidate_does_not_block_the_rest(
        self, _hb, _session, _late, _gates, _traded, mock_eval,
        mock_to_item, mock_exec, mock_upd,
    ):
        import main
        cand_bad = {"id": 10, "ticker": "BAD_US_EQ"}
        cand_good = {"id": 11, "ticker": "GOOD_US_EQ"}
        conf = _mk_conf(reason_code="approved", day_change_pct=5.0)
        mock_eval.return_value = ([(cand_bad, conf), (cand_good, conf)], [])
        # The first candidate's conversion raises — simulates a data-shape bug
        # specific to that one row (e.g. a missing/malformed field).
        mock_to_item.side_effect = [TypeError("boom"), MagicMock(ticker="GOOD_US_EQ")]
        mock_exec.return_value = True

        with patch("main.fetch_all_news", return_value=[]):
            main.news_cycle()

        # The good candidate must still have been processed despite the bad
        # one raising first.
        assert mock_exec.called
        # The bad candidate must be recorded as rejected (not left "pending"
        # with zero trace of what happened), and the good one as traded.
        statuses = {call.args[0]: call.args[1] for call in mock_upd.call_args_list}
        assert statuses.get(10) == "rejected"
        assert statuses.get(11) == "traded"


class TestApplyConfirmation:
    """_apply_confirmation preserves the exact gate verdicts of the old loop."""

    @patch("premarket.scanner.update_premarket_candidate")
    def test_none_conf_stays_pending(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        assert _apply_confirmation({"id": 1, "ticker": "A"}, None) is None
        mock_upd.assert_not_called()  # no status write → pending

    @patch("premarket.scanner.update_premarket_candidate")
    def test_missing_prev_close_stays_pending(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=None, is_confirmed=False,
                        reason_code="opening_block")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        mock_upd.assert_not_called()

    @patch("premarket.scanner.update_premarket_candidate")
    def test_opening_block_stays_pending(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=False,
                        reason_code="opening_block")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        mock_upd.assert_not_called()

    @patch("premarket.scanner.update_premarket_candidate")
    def test_gap_too_small_rejected(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=0.2)  # below MIN_GAP_PCT (1.0)
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        assert mock_upd.call_args.args[1] == "rejected"

    @patch("premarket.scanner.update_premarket_candidate")
    def test_low_momentum_stays_pending_for_retry(self, mock_upd):
        # v19.2: low_momentum/low_volume are TRANSIENT tape states — the
        # candidate stays pending and re-evaluates until the window closes
        # (AGIO 2026-07-07: killed at minute 5 on lagged RVOL, never re-checked).
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=False,
                        reason_code="low_momentum")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        mock_upd.assert_not_called()  # no status write → pending

    @patch("premarket.scanner.update_premarket_candidate")
    def test_low_volume_stays_pending_for_retry(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=False,
                        reason_code="low_volume")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        mock_upd.assert_not_called()

    @patch("premarket.scanner.update_premarket_candidate")
    def test_penny_stock_rejected_with_real_reason(self, mock_upd):
        # v19.2: terminal rejections that fire before prev_close is computed
        # (penny_stock, wide_spread) must record their REAL reason immediately —
        # not strike out as "prev close unavailable" (PLUG 2026-07-07: five
        # wasted eval cycles, then a data-problem epitaph for a $2.65 stock).
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=None, is_confirmed=False,
                        reason_code="penny_stock")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        assert mock_upd.call_args.args[1] == "rejected"
        assert "penny_stock" in mock_upd.call_args.args[2]

    @patch("premarket.scanner.update_premarket_candidate")
    def test_extended_move_rejected_terminally(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=False,
                        reason_code="extended_move")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        assert mock_upd.call_args.args[1] == "rejected"
        assert "extended_move" in mock_upd.call_args.args[2]

    @patch("premarket.scanner.update_premarket_candidate")
    def test_confirmed_in_band_approved(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=True)
        cand = {"id": 1, "ticker": "A", "headline": "h"}
        result = _apply_confirmation(cand, conf)
        assert result is not None and result[0] is cand
        mock_upd.assert_not_called()  # approval status is written by caller


# ── Claude resilience tests (v17: outage / out-of-credits handling) ────────────

def _httpx_response(status: int):
    """Minimal httpx.Response for constructing typed anthropic errors in tests."""
    import httpx
    return httpx.Response(status_code=status, request=httpx.Request("POST", "https://x"))


class TestClaudeResilience:
    """news/fetcher.py: typed Claude failures → fail-closed + correct cooldown."""

    def setup_method(self):
        # Clear any cooldown left by a prior test so each starts with Claude "up".
        import news.fetcher as f
        f._claude_cooldown = None

    def teardown_method(self):
        # Don't leak a cooldown into later test classes (would silently no-op any
        # later _batch_score_sentiment call).
        import news.fetcher as f
        f._claude_cooldown = None

    def _article(self):
        return {"id": "1", "headline": "Earnings beat", "teaser": "Revenue up"}

    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_overload_529_enters_short_cooldown(self, mock_claude, _ev):
        import anthropic, news.fetcher as f
        mock_claude.messages.create.side_effect = anthropic.APIStatusError(
            "overloaded", response=_httpx_response(529), body=None
        )
        assert f._batch_score_sentiment([self._article()]) == {}      # fail closed
        assert f._claude_cooldown is not None
        # Short (transient) cooldown, not the long billing one.
        assert f._claude_cooldown["until"] - time.monotonic() <= f._CLAUDE_OUTAGE_COOLDOWN_SECONDS + 1

    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_billing_403_enters_long_cooldown(self, mock_claude, record_ev):
        import anthropic, news.fetcher as f
        # Build the error EXACTLY as the SDK does for a real out-of-credits 403:
        # the type lives at body["error"]["type"], NOT exc.type (which is the
        # outer "error" wrapper). This is the prod shape the unit test must mirror
        # so we don't re-introduce the dead-branch bug.
        body = {"type": "error", "error": {"type": "billing_error", "message": "credit balance too low"}}
        err = anthropic.PermissionDeniedError(
            "credit balance too low", response=_httpx_response(403), body=body["error"]
        )
        mock_claude.messages.create.side_effect = err
        assert f._batch_score_sentiment([self._article()]) == {}
        # Billing cooldown is much longer than the transient-outage one.
        remaining = f._claude_cooldown["until"] - time.monotonic()
        assert remaining > f._CLAUDE_OUTAGE_COOLDOWN_SECONDS + 1
        # The billing_error must have been correctly disambiguated (not the
        # generic permission fallback) and recorded as such.
        recorded_types = [c.args[0] for c in record_ev.call_args_list]
        assert "claude_billing_error" in recorded_types
        detail = next(c.args[1] for c in record_ev.call_args_list if c.args[0] == "claude_billing_error")
        assert "billing_error" in detail  # proves _api_error_type read the body

    def test_api_error_type_reads_nested_body(self):
        # Direct unit test of the extraction: SDK .type is the "error" wrapper;
        # the real type is at body["error"]["type"].
        import anthropic, news.fetcher as f
        body = {"type": "error", "error": {"type": "billing_error", "message": "x"}}
        err = anthropic.PermissionDeniedError(
            "x", response=_httpx_response(403), body=body["error"]
        )
        assert f._api_error_type(err) == "billing_error"
        # A plain permission error (no billing type) → None → caller defaults.
        err2 = anthropic.PermissionDeniedError(
            "x", response=_httpx_response(403), body={"type": "permission_error", "message": "x"}
        )
        assert f._api_error_type(err2) == "permission_error"

    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_cooldown_suppresses_subsequent_calls(self, mock_claude, _ev):
        import anthropic, news.fetcher as f
        mock_claude.messages.create.side_effect = anthropic.APIStatusError(
            "overloaded", response=_httpx_response(529), body=None
        )
        f._batch_score_sentiment([self._article()])
        assert mock_claude.messages.create.call_count == 1
        # Second call within the cooldown window must NOT hit the API again.
        assert f._batch_score_sentiment([self._article()]) == {}
        assert mock_claude.messages.create.call_count == 1

    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_cooldown_lifts_after_window(self, mock_claude, _ev):
        import anthropic, news.fetcher as f
        mock_claude.messages.create.side_effect = anthropic.APIStatusError(
            "overloaded", response=_httpx_response(529), body=None
        )
        f._batch_score_sentiment([self._article()])
        # Force the cooldown into the past — the next call should try Claude again.
        f._claude_cooldown["until"] = time.monotonic() - 1
        block = MagicMock(); block.type = "tool_use"
        block.input = {"classifications": [
            {"id": "1", "sentiment": "neutral", "confidence": 0.3,
             "catalyst_type": "other", "already_moved": False, "catalyst_magnitude": 1},
        ]}
        msg = MagicMock(); msg.content = [block]
        mock_claude.messages.create.side_effect = None
        mock_claude.messages.create.return_value = msg
        scores = f._batch_score_sentiment([self._article()])
        assert scores["1"]["sentiment"] == "neutral"
        assert f._claude_cooldown is None  # cleared on successful resume


# ── Twelvedata credit-budget guard tests (v17) ─────────────────────────────────

class TestTwelvedataCreditGuard:
    """market/twelvedata_bars.py: exhausted budget short-circuits before any HTTP."""

    def setup_method(self):
        import market.twelvedata_bars as td
        td._credit_meter = {"date": None, "used": 0}
        td._meter_latches = {"date": None, "warned": False,
                             "exhausted_logged": False, "exhausted_emitted": False}
        # Refill the per-minute bucket so tests start with a clean slate.
        td._bucket_tokens = float(td._PER_MINUTE_LIMIT)

    def teardown_method(self):
        # Reset so a frozen/exhausted meter doesn't block a later test's TD path.
        import market.twelvedata_bars as td
        td._credit_meter = {"date": None, "used": 0}
        td._meter_latches = {"date": None, "warned": False,
                             "exhausted_logged": False, "exhausted_emitted": False}
        td._bucket_tokens = float(td._PER_MINUTE_LIMIT)

    def test_not_exhausted_under_soft_cap(self):
        import market.twelvedata_bars as td
        td._credit_meter = {"date": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).date(), "used": 10}
        assert td.credits_exhausted() is False

    @patch("market.twelvedata_bars._emit_credit_exhausted_event")
    def test_exhausted_at_soft_cap(self, _emit):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(),
                            "used": td._DAILY_CREDIT_SOFT_CAP}
        assert td.credits_exhausted() is True

    @patch("market.twelvedata_bars.requests.get")
    @patch("market.twelvedata_bars._emit_credit_exhausted_event")
    def test_time_series_skips_http_when_exhausted(self, _emit, mock_get):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(),
                            "used": td._DAILY_CREDIT_SOFT_CAP}
        assert td._get_time_series("AAPL", "1min", 10) is None
        mock_get.assert_not_called()  # the whole point: no call, no 18s 429 storm

    @patch("market.twelvedata_bars.requests.get")
    @patch("market.twelvedata_bars._emit_credit_exhausted_event")
    def test_quote_skips_http_when_exhausted(self, _emit, mock_get):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(),
                            "used": td._DAILY_CREDIT_SOFT_CAP}
        assert td.get_twelvedata_quote("AAPL") is None
        mock_get.assert_not_called()

    @patch("market.twelvedata_bars._record_credit_use")
    @patch("market.twelvedata_bars.requests.get")
    def test_fast_mode_no_retry_on_429(self, mock_get, _rec):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(), "used": 0}
        resp = MagicMock(); resp.status_code = 429
        mock_get.return_value = resp
        # fast=True must make exactly ONE attempt (no 3+6+9s backoff loop).
        assert td._get_time_series("AAPL", "1min", 10, fast=True) is None
        assert mock_get.call_count == 1

    @patch("market.twelvedata_bars.requests.get")
    def test_per_minute_bucket_blocks_when_empty(self, mock_get):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(), "used": 0}
        # Drain the bucket completely.
        td._bucket_tokens = 0.0
        # Next call should be blocked before HTTP.
        assert td._get_time_series("AAPL", "1min", 10, fast=True) is None
        mock_get.assert_not_called()

    @patch("market.twelvedata_bars.requests.get")
    def test_per_minute_bucket_allows_when_full(self, mock_get):
        import market.twelvedata_bars as td
        from datetime import datetime, timezone
        td._credit_meter = {"date": datetime.now(timezone.utc).date(), "used": 0}
        td._bucket_tokens = float(td._PER_MINUTE_LIMIT)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok", "values": [{"datetime": "2026-01-01 10:00:00", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}]}
        mock_get.return_value = resp
        result = td._get_time_series("AAPL", "1min", 1, fast=True)
        assert result is not None
        mock_get.assert_called_once()


# ── Forward-return anchoring (analysis/forward_returns.py) ────────────────────

class TestForwardReturnAnchoring:
    """
    The eval-loop fix: forward returns for out-of-session articles must be
    measured from the session OPEN, not from publish time. Pre-fix, a
    pre-market article had both window endpoints resolve to the same first
    RTH bar → exact 0.0 recorded for 39% of the table.
    """

    def _session_bars(self, day: str = "2026-07-01"):
        import pandas as pd
        # 1-min RTH session 13:30–20:00 UTC, price ramps 100 → 139 linearly
        idx = pd.date_range(f"{day} 13:30", f"{day} 20:00", freq="1min", tz="UTC")
        closes = [100 + i * 0.1 for i in range(len(idx))]
        return pd.DataFrame({"Close": closes}, index=idx)

    def test_premarket_article_anchors_at_open(self):
        from analysis.forward_returns import _bars_and_anchor, _forward_return
        bars = self._session_bars()
        published = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)  # pre-market
        with patch("analysis.forward_returns._get_intraday_bars", return_value=bars):
            got_bars, anchor = _bars_and_anchor("ACME", published)
        assert anchor == bars.index[0]  # clamped to the open
        r60 = _forward_return(got_bars, anchor, 60)
        assert r60 == pytest.approx(6.0, abs=0.1)  # 100 → 106 over 60 bars

    def test_rth_article_anchors_at_publish(self):
        from analysis.forward_returns import _bars_and_anchor
        bars = self._session_bars()
        published = datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc)  # mid-session
        with patch("analysis.forward_returns._get_intraday_bars", return_value=bars):
            _, anchor = _bars_and_anchor("ACME", published)
        assert anchor == published

    def test_after_hours_article_uses_next_session(self):
        from analysis.forward_returns import _bars_and_anchor
        day1 = self._session_bars("2026-07-01")
        day2 = self._session_bars("2026-07-02")
        published = datetime(2026, 7, 1, 21, 30, tzinfo=timezone.utc)  # after close

        def fake_bars(symbol, day):
            return {1: day1, 2: day2}.get(day.day)

        with patch("analysis.forward_returns._get_intraday_bars", side_effect=fake_bars):
            got_bars, anchor = _bars_and_anchor("ACME", published)
        assert anchor == day2.index[0]  # next session's open
        assert got_bars.index[0].day == 2

    def test_no_data_returns_none(self):
        from analysis.forward_returns import _bars_and_anchor
        published = datetime(2026, 7, 1, 11, 0, tzinfo=timezone.utc)
        with patch("analysis.forward_returns._get_intraday_bars", return_value=None):
            bars, anchor = _bars_and_anchor("ACME", published)
        assert bars is None and anchor is None

    def test_120m_forward_return(self):
        # 120 bars past the open on the 100→139 ramp: 100 → 112 = +12%.
        from analysis.forward_returns import _forward_return
        bars = self._session_bars()
        r120 = _forward_return(bars, bars.index[0], 120)
        assert r120 == pytest.approx(12.0, abs=0.1)

    def test_eod_return_measures_to_close(self):
        # From the open to the session's LAST bar: 100 → 139 = +39%.
        from analysis.forward_returns import _eod_return
        bars = self._session_bars()
        assert _eod_return(bars, bars.index[0]) == pytest.approx(39.0, abs=0.1)

    def test_eod_return_none_when_anchor_past_last_bar(self):
        import pandas as pd
        from analysis.forward_returns import _eod_return
        bars = self._session_bars()
        after = bars.index[-1] + pd.Timedelta(minutes=5)
        assert _eod_return(bars, after) is None


class TestForwardReturnsTickerResolution:
    """
    Found via the 2026-07-22 SMCI investigation: _compute_batch used to derive
    the yfinance symbol with a naive `ticker.split("_")[0]`, which mangles any
    T212 code that isn't the plain SYMBOL_US_EQ shape (FLY1_US_EQ, SMCIl_EQ,
    ETF "_EQ"-without-"_US" codes, ...) into a ticker that doesn't exist —
    yfinance then fails and the row is permanently marked computed with
    all-NULL returns (61 malformed codes / 400+ poisoned rows / 5,000+ log
    errors over the prior 30 days). Must reuse trading.executor.t212_to_symbol
    — already correct, already used by the live quote path — instead of
    reimplementing the split here.
    """

    def _row(self, ticker: str) -> dict:
        return {"id": 1, "ticker": ticker, "published_at": "2026-07-01T15:00:00+00:00"}

    def test_compute_batch_resolves_malformed_ticker_via_t212_map(self):
        import trading.executor as ex
        from analysis import forward_returns as fr

        saved = ex._t212_to_symbol
        ex._t212_to_symbol = {"SMCIl_EQ": "SMCI"}
        captured = {}

        def fake_bars(symbol, day):
            captured["symbol"] = symbol
            return None

        try:
            with patch.object(fr, "_get_intraday_bars", side_effect=fake_bars), \
                 patch.object(fr, "update_forward_returns"):
                fr._compute_batch([self._row("SMCIl_EQ")])
        finally:
            ex._t212_to_symbol = saved
        assert captured["symbol"] == "SMCI"

    def test_compute_batch_falls_back_to_split_when_unmapped(self):
        import trading.executor as ex
        from analysis import forward_returns as fr

        saved = ex._t212_to_symbol
        ex._t212_to_symbol = {}
        captured = {}

        def fake_bars(symbol, day):
            captured["symbol"] = symbol
            return None

        try:
            with patch.object(fr, "_get_intraday_bars", side_effect=fake_bars), \
                 patch.object(fr, "update_forward_returns"):
                fr._compute_batch([self._row("AAPL_US_EQ")])
        finally:
            ex._t212_to_symbol = saved
        assert captured["symbol"] == "AAPL"


class TestForwardReturnsTickerMappingCrashIsolation:
    """
    v21.9: t212_to_symbol() is exactly the function that has already had one
    ticker-mapping bug in this module's history (see the class above and
    CHANGELOG v21.4) — an unguarded call to it must not crash the WHOLE batch
    (and every batch after it in the run) if it ever raises again for a new/
    unexpected ticker shape. One bad row should mark itself unresolved and let
    the rest of the batch proceed.
    """

    def _row(self, ticker: str, row_id: int = 1) -> dict:
        return {"id": row_id, "ticker": ticker, "published_at": "2026-07-01T15:00:00+00:00"}

    def test_t212_to_symbol_exception_does_not_abort_batch(self):
        from analysis import forward_returns as fr

        calls = {"n": 0}

        def raising_mapper(ticker):
            calls["n"] += 1
            if ticker == "BAD_US_EQ":
                raise TypeError("unexpected ticker shape")
            return "GOOD"

        updated_ids = []

        def fake_update(score_id, *args):
            updated_ids.append(score_id)

        with patch.object(fr, "t212_to_symbol", side_effect=raising_mapper), \
             patch.object(fr, "_get_intraday_bars", return_value=None), \
             patch.object(fr, "update_forward_returns", side_effect=fake_update):
            n = fr._compute_batch([
                self._row("BAD_US_EQ", row_id=1),
                self._row("GOOD_US_EQ", row_id=2),
            ])

        # Both rows resolved (marked computed), not just the good one — the
        # bad row's exception must not have killed the second row's processing.
        assert n == 2
        assert set(updated_ids) == {1, 2}


class TestYfinanceOutageCounter:
    """
    v21.9: an exception fetching yfinance bars and a legitimately-empty result
    (weekend/holiday/delisted ticker) both return None from
    _get_intraday_bars, and that ambiguity is fine for one ticker-day — but a
    real yfinance outage (Yahoo-side rate limit/block, API shape change) used
    to be logged at DEBUG with no counter anywhere, so it would silently mark
    every row in a run NULL with zero visibility above DEBUG. Mirrors
    news/fetcher.py's Benzinga outage tripwire.
    """

    def setup_method(self):
        from analysis import forward_returns as fr
        fr._yfinance_consecutive_failures = 0

    teardown_method = setup_method

    @patch("storage.database.record_system_event")
    def test_event_fires_once_at_threshold(self, mock_evt):
        from analysis import forward_returns as fr
        for _ in range(fr._YFINANCE_OUTAGE_THRESHOLD + 3):
            fr._note_yfinance_failure()
        assert mock_evt.call_count == 1
        assert mock_evt.call_args.args[0] == "yfinance_outage"

    @patch("storage.database.record_system_event")
    def test_success_resets_counter(self, mock_evt):
        from analysis import forward_returns as fr
        for _ in range(fr._YFINANCE_OUTAGE_THRESHOLD - 1):
            fr._note_yfinance_failure()
        fr._note_yfinance_ok()
        fr._note_yfinance_failure()
        mock_evt.assert_not_called()

    def test_empty_dataframe_is_not_counted_as_a_failure(self):
        """A legitimately empty result (weekend/holiday) must not burn toward
        the outage tripwire meant for yfinance itself being down."""
        import pandas as pd
        from analysis import forward_returns as fr
        fr._bars_cache.clear()
        with patch("analysis.forward_returns.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame()
            fr._get_intraday_bars("AAPL", datetime(2026, 7, 25, tzinfo=timezone.utc))
        assert fr._yfinance_consecutive_failures == 0

    def test_exception_is_counted_as_a_failure(self):
        from analysis import forward_returns as fr
        fr._bars_cache.clear()
        with patch("analysis.forward_returns.yf.Ticker") as mock_ticker:
            mock_ticker.return_value.history.side_effect = Exception("rate limited")
            fr._get_intraday_bars("AAPL", datetime(2026, 7, 25, tzinfo=timezone.utc))
        assert fr._yfinance_consecutive_failures == 1


# ── Entry-cutoff / hold-horizon decoupling (v21.3) ───────────────────────────

class TestEntryCutoffDecoupling:
    """
    ENTRY_CUTOFF_MINUTES governs how late a new position may open; it is
    decoupled from TIME_STOP_MINUTES (the hold) so lengthening the hold does
    not collapse the entry window. Unset, it falls back to the time-stop value
    (behavior unchanged).
    """

    def _settings(self, env: dict):
        import os
        from config.settings import Settings
        # Start from a clean slate for both knobs, then apply the case's env.
        base = {k: v for k, v in os.environ.items()
                if k not in ("ENTRY_CUTOFF_MINUTES", "TIME_STOP_MINUTES")}
        base.update(env)
        with patch.dict(os.environ, base, clear=True):
            return Settings()

    def test_defaults_to_time_stop_when_unset(self):
        s = self._settings({"TIME_STOP_MINUTES": "45"})
        assert s.entry_cutoff_minutes == 45  # falls back to the hold value

    def test_env_override_decouples_from_hold(self):
        s = self._settings({"TIME_STOP_MINUTES": "120", "ENTRY_CUTOFF_MINUTES": "60"})
        assert s.time_stop_minutes == 120
        assert s.entry_cutoff_minutes == 60  # independent of the longer hold

    def test_is_too_late_uses_entry_cutoff_not_time_stop(self):
        import market.price_check as pc
        # tradeable_left = 100 - 15 = 85 min before the afterhours flatten.
        with patch.object(pc, "minutes_until_session_end", return_value=100), \
             patch.object(pc.cfg, "extended_flatten_buffer_minutes", 15), \
             patch.object(pc.cfg, "time_stop_minutes", 120):
            with patch.object(pc.cfg, "entry_cutoff_minutes", 60):
                assert pc.is_too_late_to_buy(pc.AFTERHOURS) is False   # 85 > 60
            with patch.object(pc.cfg, "entry_cutoff_minutes", 90):
                assert pc.is_too_late_to_buy(pc.AFTERHOURS) is True    # 85 <= 90
        # time_stop_minutes=120 throughout: the decision tracks the cutoff, not the hold.


# ── Precision-retry robustness (trading/executor.py) ─────────────────────────

class TestPrecisionRetryRobustness:
    """The retry must survive T212's varying detail wording and never round UP."""

    def _mock_cash(self):
        return {"total": 5000.0, "free": 5000.0, "invested": 0.0}

    def _precision_error(self, detail: str) -> Exception:
        from trading.executor import T212HTTPError
        body = (
            f'{{"type":"/api-errors/quantity-precision-mismatch",'
            f'"title":"Error while placing the order","status":400,'
            f'"detail":"{detail}","traceId":"abc"}}'
        )
        return T212HTTPError(400, body)

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.25)
    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_verbose_detail_wording_parsed(self, mock_get, mock_post, _fill, _fx):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = [
            self._precision_error("Quantity precision mismatch. Max allowed precision: 1."),
            {"id": "77"},
        ]
        result = buy("ACME_US_EQ", price=3.17)
        assert result.success is True
        qty = mock_post.call_args[0][1]["quantity"]
        assert qty == round(qty, 1)

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.25)
    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_unparseable_detail_falls_back_to_whole_shares(self, mock_get, mock_post, _fill, _fx):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = [
            self._precision_error("quantity precision mismatch"),  # no number at all
            {"id": "78"},
        ]
        result = buy("ACME_US_EQ", price=3.17)
        assert result.success is True
        qty = mock_post.call_args[0][1]["quantity"]
        assert qty == int(qty)  # whole shares

    @patch("trading.executor.get_gbp_usd_rate", return_value=1.25)
    @patch("trading.executor._fetch_fill", return_value={"fillPrice": 1.51})
    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_quantity_floored_not_rounded(self, mock_get, mock_post, _fill, _fx):
        from trading.executor import buy, calculate_quantity
        mock_get.return_value = self._mock_cash()
        original_qty, _ = calculate_quantity("ACME_US_EQ", 3.17)
        mock_post.side_effect = [
            self._precision_error("invalid quantity precision 0"),
            {"id": "79"},
        ]
        result = buy("ACME_US_EQ", price=3.17)
        assert result.success is True
        qty = mock_post.call_args[0][1]["quantity"]
        assert qty <= original_qty  # floored — never exceeds the sized budget
        assert qty == int(qty)


# ── Scored-article session dedup (news/fetcher.py) ────────────────────────────

class TestScoredArticleDedup:
    """The wider freshness window must not re-score articles through Claude."""

    def test_marked_article_is_skipped(self):
        import news.fetcher as f
        f._scored_articles = {"date": None, "ids": set()}
        assert f._already_scored("a1") is False
        f._mark_scored(["a1", "a2"])
        assert f._already_scored("a1") is True
        assert f._already_scored("a3") is False

    def test_resets_on_new_day(self):
        import news.fetcher as f
        from datetime import date
        f._scored_articles = {"date": date(2020, 1, 1), "ids": {"a1"}}
        assert f._already_scored("a1") is False  # stale day → set was reset


class TestPremarketCandidateToNewsItem:
    """
    Regression for the 2026-06-11→07-06 zero-trade drought. catalyst_magnitude
    became a REQUIRED NewsItem field in v15.8, but main._candidate_to_news_item
    never supplied it, so converting an APPROVED premarket candidate raised
    TypeError — caught in news_cycle, aborting the whole premarket exec loop.
    Every gap-and-go entry silently died at the execution boundary for ~4 weeks.
    """

    def _row(self, **overrides):
        row = {
            "id": 353,
            "article_id": "bz-123",
            "ticker": "PIRS_US_EQ",
            "headline": "Palvella Submits First Module Of Rolling NDA",
            "catalyst_type": "fda_approval",
            "confidence": 0.75,
            "published_at": "2026-06-29T12:30:00+01:00",
            "created_at": "2026-06-29T12:30:00+01:00",
            "status": "pending",
            "catalyst_magnitude": 4,
        }
        row.update(overrides)
        return row

    def test_converts_without_raising_and_preserves_magnitude(self):
        import main
        item = main._candidate_to_news_item(self._row())
        assert item.ticker == "PIRS_US_EQ"
        assert item.sentiment == "positive"
        assert item.catalyst_magnitude == 4  # the field that used to be missing

    def test_legacy_row_without_magnitude_defaults_to_noise(self):
        # Rows written before v15.8 have NULL catalyst_magnitude. Conversion must
        # still succeed (never crash the exec loop) — default to 1 (noise).
        import main
        item = main._candidate_to_news_item(self._row(catalyst_magnitude=None))
        assert item.catalyst_magnitude == 1


class TestFinnhubFastMode:
    """market/finnhub_bars.py: fast=True makes exactly ONE attempt, no sleeps.

    The premarket eval pool's "no retry backoff" contract covered every
    Twelvedata call but not the PRIMARY quote source — a slow Finnhub could
    hold a pool thread ~17s inside the 30s eval budget (same starvation class
    as the 2026-06-23 Twelvedata incident).
    """

    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_fast_single_attempt_on_timeout(self, mock_get, mock_sleep):
        import requests as _rq
        from market.finnhub_bars import get_finnhub_quote
        mock_get.side_effect = _rq.exceptions.Timeout()
        assert get_finnhub_quote("AAPL", fast=True) is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_default_retries_three_times(self, mock_get, mock_sleep):
        import requests as _rq
        from market.finnhub_bars import get_finnhub_quote
        mock_get.side_effect = _rq.exceptions.Timeout()
        assert get_finnhub_quote("AAPL") is None
        assert mock_get.call_count == 3

    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_fast_no_retry_on_5xx(self, mock_get, mock_sleep):
        from market.finnhub_bars import get_finnhub_quote
        resp = MagicMock(); resp.status_code = 503
        mock_get.return_value = resp
        assert get_finnhub_quote("AAPL", fast=True) is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()


class TestFinnhubAuthFailureLatch:
    """
    v21.9: a 401/403 (revoked/invalid API key) is a SYSTEMIC failure affecting
    every symbol, not "this ticker has no coverage" — must be latched and
    logged distinctly from a per-symbol 404/422, mirroring
    twelvedata_bars.py's prepost-denial latch. Without this, a dead key looks
    identical to N independent "no Finnhub data" misses.
    """

    def setup_method(self):
        import market.finnhub_bars as fh
        fh._auth_ok = None

    teardown_method = setup_method

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.requests.get")
    def test_403_latches_and_emits_system_event(self, mock_get, mock_evt):
        from market.finnhub_bars import get_finnhub_quote, finnhub_auth_ok
        resp = MagicMock(); resp.status_code = 403; resp.text = "invalid api key"
        mock_get.return_value = resp
        assert get_finnhub_quote("AAPL") is None
        assert finnhub_auth_ok() is False
        mock_evt.assert_called_once()
        assert mock_evt.call_args.args[0] == "finnhub_auth_failure"

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.requests.get")
    def test_401_also_latches(self, mock_get, mock_evt):
        from market.finnhub_bars import get_finnhub_quote, finnhub_auth_ok
        resp = MagicMock(); resp.status_code = 401; resp.text = "unauthorized"
        mock_get.return_value = resp
        assert get_finnhub_quote("AAPL") is None
        assert finnhub_auth_ok() is False

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.requests.get")
    def test_404_does_not_latch(self, mock_get, mock_evt):
        """A per-symbol 404 is genuinely 'no coverage' — must not be confused
        with an account-wide auth failure."""
        from market.finnhub_bars import get_finnhub_quote, finnhub_auth_ok
        resp = MagicMock(); resp.status_code = 404; resp.text = "symbol not found"
        mock_get.return_value = resp
        assert get_finnhub_quote("AAPL") is None
        assert finnhub_auth_ok() is True
        mock_evt.assert_not_called()

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.requests.get")
    def test_latch_fires_system_event_only_once(self, mock_get, mock_evt):
        from market.finnhub_bars import get_finnhub_quote
        resp = MagicMock(); resp.status_code = 403; resp.text = "invalid api key"
        mock_get.return_value = resp
        get_finnhub_quote("AAPL")
        get_finnhub_quote("MSFT")
        assert mock_evt.call_count == 1


class TestFinnhubOutageTripwire:
    """
    v21.10: the v21.9 auth latch only catches a definitive 401/403. On
    2026-07-30 Finnhub instead TIMED OUT on every poll for ~5 min while a
    position was open — each failure an isolated per-symbol WARNING, nothing
    counting them, so a dead primary price source produced no operator signal.
    """

    def setup_method(self):
        import market.finnhub_bars as fh
        fh._finnhub_consecutive_failures = 0
        fh._auth_ok = None

    teardown_method = setup_method

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_sustained_timeouts_fire_one_event(self, mock_get, _sleep, mock_evt):
        import requests as _rq
        import market.finnhub_bars as fh
        mock_get.side_effect = _rq.exceptions.Timeout()
        for _ in range(fh._FINNHUB_OUTAGE_THRESHOLD + 2):
            fh.get_finnhub_quote("AAPL", fast=True)
        assert mock_evt.call_count == 1  # only at the threshold crossing
        assert mock_evt.call_args.args[0] == "finnhub_outage"

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_alert_latches_for_the_process(self, mock_get, _sleep, mock_evt):
        """The event must fire at most once per process even across separate
        streaks: system_events de-dupes daily anyway, and this sits on the
        monitor's 5s price path where a degraded DB could otherwise make
        record_system_event retry-with-backoff repeatedly."""
        import requests as _rq
        import market.finnhub_bars as fh
        good = MagicMock(status_code=200)
        good.json.return_value = {"c": 10.0, "o": 9.5, "pc": 9.4, "t": 1}
        for _cycle in range(2):
            mock_get.side_effect = _rq.exceptions.Timeout()
            for _ in range(fh._FINNHUB_OUTAGE_THRESHOLD + 1):
                fh.get_finnhub_quote("AAPL", fast=True)
            mock_get.side_effect = None          # recovery resets the streak
            mock_get.return_value = good
            fh.get_finnhub_quote("AAPL", fast=True)
        assert mock_evt.call_count == 1

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.time.sleep")
    @patch("market.finnhub_bars.requests.get")
    def test_a_success_resets_the_streak(self, mock_get, _sleep, mock_evt):
        """One flaky ticker between good quotes must never trip the wire."""
        import requests as _rq
        import market.finnhub_bars as fh
        good = MagicMock(status_code=200)
        good.json.return_value = {"c": 10.0, "o": 9.5, "pc": 9.4, "t": 1}
        for _ in range(fh._FINNHUB_OUTAGE_THRESHOLD - 1):
            mock_get.side_effect = _rq.exceptions.Timeout()
            fh.get_finnhub_quote("AAPL", fast=True)
        mock_get.side_effect = None
        mock_get.return_value = good
        fh.get_finnhub_quote("MSFT", fast=True)
        assert fh._finnhub_consecutive_failures == 0
        mock_evt.assert_not_called()

    @patch("storage.database.record_system_event")
    @patch("market.finnhub_bars.requests.get")
    def test_404_counts_as_healthy_provider(self, mock_get, mock_evt):
        """A bad SYMBOL is a healthy provider answering correctly — it must
        clear the outage streak, not contribute to it."""
        import market.finnhub_bars as fh
        fh._finnhub_consecutive_failures = 5
        resp = MagicMock(status_code=404); resp.text = "not found"
        mock_get.return_value = resp
        fh.get_finnhub_quote("NOPE", fast=True)
        assert fh._finnhub_consecutive_failures == 0


class TestStaleQuoteFeedTripwire:
    """
    v21.10: a frozen provider feed. On 2026-07-30 Twelvedata served a quote
    stuck at 14:30 ET for 71+ min — 23 refusals, each an isolated WARNING,
    none counted, while a position's take-profit was being polled.
    """

    def setup_method(self):
        import market.price_check as pc
        pc._stale_quote_streak.clear()
        pc._stale_quote_symbols.clear()

    teardown_method = setup_method

    @staticmethod
    def _symbols(n):
        """Distinct symbols — a real provider freeze hits every name asked."""
        return [f"SYM{i}" for i in range(n)]

    @patch("storage.database.record_system_event")
    def test_streak_of_stale_quotes_fires_once(self, mock_evt):
        import time as _t
        import market.price_check as pc
        old = {"t": _t.time() - 3600}  # 60 min old
        for sym in self._symbols(pc._STALE_QUOTE_ALERT_THRESHOLD + 3):
            assert pc._quote_is_stale(sym, old, "Twelvedata") is True
        assert mock_evt.call_count == 1
        assert mock_evt.call_args.args[0] == "stale_quote_feed"

    @patch("storage.database.record_system_event")
    def test_alert_latches_per_source(self, mock_evt):
        """Once reported for a source, a later streak on that same source must
        not re-fire — this runs on the 5s price path and a degraded DB would
        make record_system_event retry-with-backoff each time."""
        import time as _t
        import market.price_check as pc
        old = {"t": _t.time() - 3600}
        fresh = {"t": _t.time()}
        for _cycle in range(2):
            for sym in self._symbols(pc._STALE_QUOTE_ALERT_THRESHOLD + 1):
                pc._quote_is_stale(sym, old, "Twelvedata")
            pc._quote_is_stale("FSS", fresh, "Twelvedata")  # recovery
        assert mock_evt.call_count == 1

    @patch("storage.database.record_system_event")
    def test_fresh_quote_resets_streak(self, mock_evt):
        import time as _t
        import market.price_check as pc
        old = {"t": _t.time() - 3600}
        fresh = {"t": _t.time()}
        for sym in self._symbols(pc._STALE_QUOTE_ALERT_THRESHOLD - 1):
            pc._quote_is_stale(sym, old, "Twelvedata")
        assert pc._quote_is_stale("FSS", fresh, "Twelvedata") is False
        assert pc._stale_quote_streak["Twelvedata"] == 0
        # The distinct-symbol set must clear WITH the streak, or a feed that
        # alternates fresh/stale would accumulate symbols until it eventually
        # cleared the bar without ever having been frozen (v21.12).
        assert not pc._stale_quote_symbols["Twelvedata"]
        mock_evt.assert_not_called()

    @patch("storage.database.record_system_event")
    def test_sources_counted_separately(self, mock_evt):
        """Finnhub being frozen says nothing about Twelvedata's feed."""
        import time as _t
        import market.price_check as pc
        old = {"t": _t.time() - 3600}
        for _ in range(pc._STALE_QUOTE_ALERT_THRESHOLD - 1):
            pc._quote_is_stale("A", old, "Finnhub")
        pc._quote_is_stale("B", old, "Twelvedata")
        assert pc._stale_quote_streak["Twelvedata"] == 1
        mock_evt.assert_not_called()

    def test_quote_without_timestamp_is_not_counted(self):
        """Fail-open on missing metadata — must not inflate the streak."""
        import market.price_check as pc
        assert pc._quote_is_stale("A", {"c": 5.0}, "Finnhub") is False
        assert pc._stale_quote_streak.get("Finnhub", 0) == 0


class TestFxRateCreditGuard:
    """get_gbp_usd_rate runs behind the SAME credit/rate gates as bar calls.

    It used to bypass both: every /price call was invisible to the daily
    credit meter AND uncounted against the 55/min token bucket.
    """

    def setup_method(self):
        import time as _time
        import market.twelvedata_bars as td
        td._credit_meter = {"date": None, "used": 0}
        td._meter_latches = {"date": None, "warned": False,
                             "exhausted_logged": False, "exhausted_emitted": False}
        td._bucket_tokens = float(td._PER_MINUTE_LIMIT)
        td._bucket_last_refill = _time.monotonic()
        td._FX_CACHE["rate"] = None
        td._FX_CACHE["ts"] = 0.0

    teardown_method = setup_method

    @patch("market.twelvedata_bars.requests.get")
    def test_no_token_serves_fallback_without_http(self, mock_get):
        import time as _time
        import market.twelvedata_bars as td
        td._bucket_tokens = 0.0
        td._bucket_last_refill = _time.monotonic()  # no elapsed-time refill
        assert td.get_gbp_usd_rate() == td._FX_FALLBACK
        mock_get.assert_not_called()

    @patch("market.twelvedata_bars._emit_credit_exhausted_event")
    @patch("market.twelvedata_bars.requests.get")
    def test_exhausted_serves_fallback_without_http(self, mock_get, _emit):
        from datetime import datetime, timezone
        import market.twelvedata_bars as td
        td._credit_meter = {"date": datetime.now(timezone.utc).date(),
                            "used": td._DAILY_CREDIT_SOFT_CAP}
        assert td.get_gbp_usd_rate() == td._FX_FALLBACK
        mock_get.assert_not_called()

    @patch("market.twelvedata_bars.requests.get")
    def test_successful_fetch_is_metered(self, mock_get):
        import market.twelvedata_bars as td
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"price": "1.3050"}
        mock_get.return_value = resp
        assert td.get_gbp_usd_rate() == 1.3050
        assert td.get_credits_used_today() == 1  # the call now counts


class TestNoQuoteStrikeReset:
    """main.py session blackout: strikes must be CONSECUTIVE, not cumulative.

    Before the fix, two unrelated transient data misses hours apart would
    permanently blacklist a ticker with perfectly good coverage.
    """

    def setup_method(self):
        import main
        main._retry_queue.clear()
        main._no_quote_ticker_strikes.clear()
        main._no_quote_blackout.clear()
        main._no_quote_blackout_day = None

    teardown_method = setup_method

    def _item(self, ticker="ABC_US_EQ"):
        import pytz as _pytz
        from news.fetcher import NewsItem
        return NewsItem(
            article_id="a1", ticker=ticker, headline="h", body="", source="s",
            published_at=datetime.now(_pytz.utc), sentiment="positive",
            confidence=0.8, catalyst_type="fda_approval", already_moved=False,
            catalyst_magnitude=3,
        )

    def test_two_consecutive_misses_blacklist(self):
        import main
        main._queue_retry(self._item())
        main._queue_retry(self._item())
        assert "ABC_US_EQ" in main._no_quote_blackout

    def test_success_between_misses_prevents_blacklist(self):
        import main
        main._queue_retry(self._item())
        main._note_price_data_ok("ABC_US_EQ")  # a quote answered in between
        main._queue_retry(self._item())
        assert "ABC_US_EQ" not in main._no_quote_blackout

    # ── v21.6: extended-session misses must not blacklist ────────────────────
    def test_extended_session_misses_never_blacklist(self):
        # After-hours/premarket bars aren't on our Twelvedata plan and
        # Finnhub's quote freezes at the close, so a miss out there says
        # nothing about the ticker. On 2026-07-27 this blacklisted CDNS,
        # SANM, CLS and six more liquid names for reporting after the bell.
        import main
        for _ in range(5):
            main._queue_retry(self._item(), count_strike=False)
        assert "ABC_US_EQ" not in main._no_quote_blackout
        assert "ABC_US_EQ" not in main._no_quote_ticker_strikes

    def test_extended_miss_does_not_advance_a_pending_strike(self):
        import main
        main._queue_retry(self._item())                       # RTH miss: strike 1
        main._queue_retry(self._item(), count_strike=False)   # extended: no-op
        assert "ABC_US_EQ" not in main._no_quote_blackout
        assert main._no_quote_ticker_strikes["ABC_US_EQ"] == 1

    def test_signal_is_still_parked_for_retry_without_a_strike(self):
        # Not counting a strike must not mean dropping the signal.
        import main
        main._queue_retry(self._item(), count_strike=False)
        assert ("a1", "ABC_US_EQ") in main._retry_queue

    # ── v21.6: the blackout is a one-DAY judgement ───────────────────────────
    def test_blackout_clears_on_new_trading_day(self):
        # The original code only cleared on process restart; the live service
        # ran six days and 16 tickers without one.
        import main
        main._queue_retry(self._item())
        main._queue_retry(self._item())
        assert "ABC_US_EQ" in main._no_quote_blackout
        main._no_quote_blackout_day = "1999-01-01"   # pretend the day rolled
        main._reset_no_quote_blackout_if_new_day()
        assert "ABC_US_EQ" not in main._no_quote_blackout
        assert not main._no_quote_ticker_strikes

    def test_blackout_survives_within_the_same_day(self):
        import main
        main._reset_no_quote_blackout_if_new_day()   # stamps today
        main._queue_retry(self._item())
        main._queue_retry(self._item())
        main._reset_no_quote_blackout_if_new_day()   # same day — no-op
        assert "ABC_US_EQ" in main._no_quote_blackout


# ── Pre-broker buy retry tests (v21.7 — ITW post-mortem) ──────────────────────

class TestEnterConfirmedBuyRetry:
    """
    Tests for main.py::_enter_confirmed — one retry when buy() fails BEFORE
    reaching the broker (calculate_quantity error: order_id is None and
    quantity == 0). A failure that already reached the broker (non-empty
    order_id/quantity) must never be retried here — that would risk placing
    a second live order for the same signal.
    """

    def _item(self, ticker="ITW_US_EQ"):
        import pytz as _pytz
        from news.fetcher import NewsItem
        return NewsItem(
            article_id="a1", ticker=ticker, headline="h", body="", source="s",
            published_at=datetime.now(_pytz.utc), sentiment="positive",
            confidence=0.8, catalyst_type="guidance_raise", already_moved=False,
            catalyst_magnitude=3,
        )

    def _confirmation(self, ticker="ITW_US_EQ"):
        from market.price_check import PriceConfirmation
        from market.sessions import REGULAR
        return PriceConfirmation(
            ticker=ticker, symbol="ITW", current_price=297.08, open_price=290.0,
            prev_close=284.9, day_move_pct=2.4, day_change_pct=4.3,
            recent_move_pct=0.42, current_volume=100000, avg_volume=95000,
            rvol=1.0, avg_dollar_volume=397_645_476, spread_proxy_pct=0.1,
            is_confirmed=True, reason="approved", reason_code="approved",
            session=REGULAR,
        )

    def _order_result(self, success, order_id=None, quantity=0, error=None):
        from trading.executor import OrderResult
        return OrderResult(
            success=success, ticker="ITW_US_EQ", quantity=quantity,
            price=297.08, order_id=order_id, error=error,
        )

    @patch("main.set_rejection_reason")
    @patch("main.mark_signal_acted_on")
    @patch("main.place_stop_loss", return_value=None)
    @patch("main.open_trade", return_value=1)
    @patch("main.buy")
    def test_pre_broker_failure_retried_and_succeeds(
        self, mock_buy, mock_open_trade, _mock_stop, _mock_acted, _mock_reject,
    ):
        import main
        mock_buy.side_effect = [
            self._order_result(False, order_id=None, quantity=0,
                                error="T212 cash API failed: HTTP 429"),
            self._order_result(True, order_id="o1", quantity=1.5),
        ]
        result = main._enter_confirmed(self._item(), self._confirmation(), signal_id=42)
        assert result is True
        assert mock_buy.call_count == 2
        mock_open_trade.assert_called_once()

    @patch("main.set_rejection_reason")
    @patch("main.buy")
    def test_pre_broker_failure_gives_up_after_one_retry(
        self, mock_buy, mock_reject,
    ):
        import main
        mock_buy.return_value = self._order_result(
            False, order_id=None, quantity=0, error="T212 cash API failed: HTTP 429",
        )
        result = main._enter_confirmed(self._item(), self._confirmation(), signal_id=42)
        assert result is False
        assert mock_buy.call_count == 2
        mock_reject.assert_called_once()
        assert mock_reject.call_args.args[2] == "buy_failed"

    @patch("main.set_rejection_reason")
    @patch("main.buy")
    def test_post_broker_failure_is_never_retried(self, mock_buy, mock_reject):
        # An order_id or nonzero quantity means the broker was already
        # contacted (e.g. the buy raced a precision-retry then failed on
        # post-order processing) — retrying here risks a second live order.
        import main
        mock_buy.return_value = self._order_result(
            False, order_id="o1", quantity=1.5, error="BUY post-order processing failed",
        )
        result = main._enter_confirmed(self._item(), self._confirmation(), signal_id=42)
        assert result is False
        assert mock_buy.call_count == 1
        mock_reject.assert_called_once()


class TestPrepostCapabilityLatch:
    """v21.6: prepost=true is a PLAN entitlement on Twelvedata, not per-symbol
    coverage. Below the Pro tier every extended-hours request 403s, for every
    symbol, forever — and the generic retry path made that look like "this
    ticker has no data", which blacklisted nine liquid names on 2026-07-27.
    Feature-detect once, then stop paying for it.
    """

    def setup_method(self):
        import time as _time
        import market.twelvedata_bars as td
        td._prepost_supported = None
        td._credit_meter = {"date": None, "used": 0}
        td._meter_latches = {"date": None, "warned": False,
                             "exhausted_logged": False, "exhausted_emitted": False}
        td._bucket_tokens = float(td._PER_MINUTE_LIMIT)
        td._bucket_last_refill = _time.monotonic()

    teardown_method = setup_method

    _DENIED = {
        "code": 403,
        "message": ("Pre-market and post-market data are available on the Pro "
                    "plan (individual) and the Venture plan (business) and above."),
        "status": "error",
    }

    def _denied_response(self):
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = self._DENIED
        return resp

    @patch("market.twelvedata_bars.record_system_event", create=True)
    @patch("market.twelvedata_bars.requests.get")
    def test_403_latches_and_returns_none(self, mock_get, _ev):
        import market.twelvedata_bars as td
        mock_get.return_value = self._denied_response()
        assert td._get_time_series("CDNS", "1min", 960, prepost=True) is None
        assert td._prepost_supported is False
        assert td.extended_bars_available() is False

    @patch("market.twelvedata_bars.record_system_event", create=True)
    @patch("market.twelvedata_bars.requests.get")
    def test_403_is_not_retried(self, mock_get, _ev):
        # The old path let raise_for_status() throw into the generic handler,
        # which burned the full 3-attempt backoff on a permanent condition.
        import market.twelvedata_bars as td
        mock_get.return_value = self._denied_response()
        td._get_time_series("CDNS", "1min", 960, prepost=True)
        assert mock_get.call_count == 1

    @patch("market.twelvedata_bars.record_system_event", create=True)
    @patch("market.twelvedata_bars.requests.get")
    def test_subsequent_prepost_calls_skip_http_entirely(self, mock_get, _ev):
        import market.twelvedata_bars as td
        mock_get.return_value = self._denied_response()
        td._get_time_series("CDNS", "1min", 960, prepost=True)
        mock_get.reset_mock()
        for sym in ("SANM", "CLS", "KFRC"):
            assert td._get_time_series(sym, "1min", 960, prepost=True) is None
        mock_get.assert_not_called()   # no credits, no 403 storm

    @patch("market.twelvedata_bars.record_system_event", create=True)
    @patch("market.twelvedata_bars.requests.get")
    def test_rth_requests_unaffected_by_the_latch(self, mock_get, _ev):
        # The whole point: an extended-hours entitlement wall must not take
        # regular-hours data down with it.
        import market.twelvedata_bars as td
        mock_get.return_value = self._denied_response()
        td._get_time_series("CDNS", "1min", 960, prepost=True)   # trips latch
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status = MagicMock()
        ok.json.return_value = {"values": [{"datetime": "2026-07-27 15:59:00",
                                            "open": "1", "high": "1",
                                            "low": "1", "close": "1",
                                            "volume": "10"}]}
        mock_get.return_value = ok
        assert td._get_time_series("CDNS", "1min", 390, prepost=False) is not None

    @patch("market.twelvedata_bars.record_system_event", create=True)
    @patch("market.twelvedata_bars.requests.get")
    def test_unrelated_403_does_not_latch(self, mock_get, _ev):
        # A different 403 (bad key, symbol not entitled) is not the prepost
        # wall and must not disable extended bars process-wide.
        import market.twelvedata_bars as td
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {"code": 403, "message": "Invalid API key",
                                  "status": "error"}
        mock_get.return_value = resp
        assert td._get_time_series("CDNS", "1min", 960, prepost=True) is None
        assert td._prepost_supported is None   # not latched
        assert td.extended_bars_available() is True


class TestShippedDefaultsAreSafe:
    """The code defaults ARE the shipped configuration.

    These were previously asserted against a deployment workflow that pinned
    every value, so the Python defaults could drift unnoticed — and they did.
    With no deployment pipeline in this repository, whatever
    `config/settings.py` says is what a fresh clone runs, so the safety
    properties are asserted directly against it.
    """

    def _fresh(self, monkeypatch):
        """A Settings built as if no environment variables were set at all."""
        for key in (
            "AFTERHOURS_TRADING_ENABLED",
            "EXTENDED_HOURS_ENABLED",
            "PREMARKET_TRADING_ENABLED",
        ):
            monkeypatch.delenv(key, raising=False)
        from config.settings import Settings
        return Settings()

    def test_afterhours_trading_ships_disabled(self, monkeypatch):
        # Extended-hours bars are a paid entitlement on most plans; a provider
        # without them cannot confirm an after-hours signal at all. Worse, the
        # resulting no-quote misses can be mistaken for "this ticker has no
        # coverage" and blacklist it for the session — nine liquid large/mid
        # caps were suppressed that way on 2026-07-27, purely for reporting
        # after the bell.
        assert self._fresh(monkeypatch).afterhours_trading_enabled is False

    def test_extended_hours_master_switch_stays_on(self, monkeypatch):
        # Management of a position that leaks into an extended session must not
        # be disabled along with entries — see is_manage_session's docstring in
        # market/sessions.py for why the master switch and the entry toggle are
        # intentionally decoupled. Turning entries off must never mean a live
        # position stops being watched.
        assert self._fresh(monkeypatch).extended_hours_enabled is True

    def test_premarket_trading_ships_disabled(self, monkeypatch):
        # Thin 5am books and wide spreads. The pre-market scanner plus the
        # at-open gap-and-go evaluation is the deliberate pre-market strategy;
        # it trades the same news with confirmation.
        assert self._fresh(monkeypatch).premarket_trading_enabled is False

    def test_trading_mode_ships_as_demo(self, monkeypatch):
        monkeypatch.delenv("TRADING_MODE", raising=False)
        from config.settings import Settings
        assert Settings().is_live is False


class TestOpeningBlockTransient:
    """v21.6: opening_block is a pure countdown — it is guaranteed to clear
    within cfg.open_block_minutes — yet it was terminal, so a catalyst that
    printed inside the window was discarded outright. Earnings and guidance
    print in the first minutes after 16:00 ET, exactly the window it covers:
    CDNS (guidance_raise, conf 0.88) died 4.0 min into a 5-min block on
    2026-07-27, with TXN and THRM before it.
    """

    def test_main_treats_opening_block_as_transient(self):
        import main
        assert "opening_block" in main._TRANSIENT_REJECT_CODES

    def test_scanner_treats_opening_block_as_transient(self):
        import premarket.scanner as sc
        assert "opening_block" in sc._TRANSIENT_REJECT_CODES

    def test_genuinely_terminal_codes_stay_terminal(self):
        # The block is transient because it self-clears on a timer; nothing
        # else in this list does, and widening the set further would keep
        # re-checking signals whose rejection is a property of the instrument.
        import main
        import premarket.scanner as sc
        for code in ("penny_stock", "illiquid", "dead_cat", "extended_move",
                     "wide_spread", "high_momentum", "high_volume",
                     "below_vwap", "exhausted_bounce", "insufficient_data"):
            assert code not in main._TRANSIENT_REJECT_CODES
            assert code not in sc._TRANSIENT_REJECT_CODES


class TestPremarketStrikeCleanup:
    """scanner strike dicts are dropped on EVERY terminal verdict (no leak)."""

    def setup_method(self):
        import premarket.scanner as sc
        sc._no_quote_strikes.clear()
        sc._gap_pct_strikes.clear()

    teardown_method = setup_method

    @patch("premarket.scanner.update_premarket_candidate")
    def test_gap_reject_clears_both_counters(self, mock_upd):
        import premarket.scanner as sc
        sc._no_quote_strikes[7] = 1
        sc._gap_pct_strikes[7] = 2
        conf = _mk_conf(day_change_pct=0.2)  # below MIN_GAP_PCT → rejected
        assert sc._apply_confirmation({"id": 7, "ticker": "A"}, conf) is None
        assert 7 not in sc._no_quote_strikes
        assert 7 not in sc._gap_pct_strikes

    @patch("premarket.scanner.update_premarket_candidate")
    def test_window_expiry_clears_counters(self, mock_upd):
        import premarket.scanner as sc
        sc._no_quote_strikes[9] = 2
        cand = {"id": 9, "ticker": "B",
                "created_at": datetime.now(timezone.utc).isoformat()}
        live, graduated = sc._live_candidates([cand], minutes_open=sc._EVAL_WINDOW_MINUTES + 1)
        assert live == []
        assert graduated == [cand]  # still-pending candidate handed off, not just dropped
        assert 9 not in sc._no_quote_strikes


class TestBenzingaOutageEvent:
    """news/fetcher.py: sustained feed failure emits ONE system_event.

    Benzinga was the last external dependency with no outage marker — a dead
    feed looks exactly like a quiet news day on every dashboard.
    """

    def setup_method(self):
        import news.fetcher as f
        f._benzinga_consecutive_failures = 0

    teardown_method = setup_method

    @patch("storage.database.record_system_event")
    def test_event_fires_once_at_threshold(self, mock_evt):
        import news.fetcher as f
        for _ in range(f._BENZINGA_OUTAGE_THRESHOLD + 5):
            f._note_benzinga_failure()
        assert mock_evt.call_count == 1  # only at the exact threshold crossing
        assert mock_evt.call_args.args[0] == "benzinga_outage"

    @patch("storage.database.record_system_event")
    def test_success_resets_counter(self, mock_evt):
        import news.fetcher as f
        for _ in range(f._BENZINGA_OUTAGE_THRESHOLD - 1):
            f._note_benzinga_failure()
        f._note_benzinga_ok()
        f._note_benzinga_failure()  # 1 of 10 again, not 10 of 10
        mock_evt.assert_not_called()


class TestBenzingaMalformedResponseShape:
    """
    v21.9: a 200 OK whose body isn't the expected {"results"|"articles": [...]}
    envelope (schema change, an error wrapped in a 200) must count as a FETCH
    FAILURE, not "fetched zero articles" — the latter resets
    _benzinga_consecutive_failures and would let the outage tripwire
    (TestBenzingaOutageEvent above) never fire while every cycle silently
    starves.
    """

    def setup_method(self):
        import news.fetcher as f
        f._benzinga_consecutive_failures = 0

    teardown_method = setup_method

    @patch("news.fetcher.requests.get")
    def test_normal_empty_results_is_still_success(self, mock_get):
        import news.fetcher as f
        mock_get.return_value = MagicMock(ok=True, json=lambda: {"results": []})
        articles = f._fetch(lookback_minutes=5)
        assert articles == []
        assert f._benzinga_consecutive_failures == 0

    @patch("news.fetcher.requests.get")
    def test_unrecognized_envelope_counts_as_failure(self, mock_get):
        import news.fetcher as f
        mock_get.return_value = MagicMock(ok=True, json=lambda: {"message": "invalid API key format"})
        articles = f._fetch(lookback_minutes=5)
        assert articles == []
        assert f._benzinga_consecutive_failures == 1

    @patch("news.fetcher.requests.get")
    def test_non_dict_body_counts_as_failure(self, mock_get):
        import news.fetcher as f
        mock_get.return_value = MagicMock(ok=True, json=lambda: ["unexpected", "list", "shape"])
        articles = f._fetch(lookback_minutes=5)
        assert articles == []
        assert f._benzinga_consecutive_failures == 1


# ── v19.2 tests: data-integrity + opportunity-capture fixes (2026-07-07) ──────


class TestT212SymbolInversion:
    """trading/executor.py::t212_to_symbol — exact inverse map with fallback."""

    def setup_method(self):
        import trading.executor as ex
        self._saved = ex._t212_to_symbol
        ex._t212_to_symbol = {"FLY1_US_EQ": "FLY", "AVAV__US_EQ": "AVAV"}

    def teardown_method(self):
        import trading.executor as ex
        ex._t212_to_symbol = self._saved

    def test_mapped_code_returns_exchange_symbol(self):
        from trading.executor import t212_to_symbol
        # T212 re-uses historic symbols with a digit suffix: exchange "FLY"
        # lives as T212 code "FLY1_US_EQ". Suffix-stripping produced "FLY1",
        # which no data API knows (2026-07-07: both FLY candidates expired).
        assert t212_to_symbol("FLY1_US_EQ") == "FLY"
        assert t212_to_symbol("AVAV__US_EQ") == "AVAV"

    def test_unmapped_code_falls_back_to_suffix_strip(self):
        from trading.executor import t212_to_symbol
        assert t212_to_symbol("AAPL_US_EQ") == "AAPL"


class TestQuoteStaleness:
    """price_check quote staleness — a frozen quote is not a live price."""

    def test_fresh_quote_not_stale(self):
        import time as _t
        from market.price_check import _quote_is_stale
        assert _quote_is_stale("X", {"t": _t.time() - 60}, "Finnhub") is False

    def test_old_quote_is_stale(self):
        import time as _t
        from market.price_check import _quote_is_stale
        assert _quote_is_stale("X", {"t": _t.time() - 25 * 60}, "Finnhub") is True

    def test_missing_timestamp_fails_open(self):
        from market.price_check import _quote_is_stale
        assert _quote_is_stale("X", {"c": 10.0}, "Finnhub") is False
        assert _quote_is_stale("X", {"t": None}, "Finnhub") is False

    @patch("market.price_check.get_twelvedata_quote", return_value=None)
    @patch("market.price_check.get_finnhub_quote")
    def test_stale_finnhub_falls_through_to_none(self, mock_fh, _td):
        # GLASF 2026-07-07: Finnhub served a $12.50 print frozen since entry.
        # A stale primary quote must be treated as NO coverage, not a price.
        import time as _t
        from market.price_check import get_quote_with_fallback
        mock_fh.return_value = {"c": 12.50, "o": 12.25, "pc": 12.32,
                                "t": _t.time() - 3600}
        assert get_quote_with_fallback("GLASF") is None

    @patch("market.price_check.get_twelvedata_quote", return_value=None)
    @patch("market.price_check.get_finnhub_quote")
    def test_fresh_finnhub_passes_through(self, mock_fh, _td):
        import time as _t
        from market.price_check import get_quote_with_fallback
        mock_fh.return_value = {"c": 12.50, "o": 12.25, "pc": 12.32,
                                "t": _t.time() - 30}
        q = get_quote_with_fallback("ACME")
        assert q is not None and q["c"] == 12.50


def _mk_sa(**overrides):
    """Build a SessionAnalysis with confirmable defaults for gate tests.

    Defaults describe a healthy setup for a $10.50 quote: +2.94% momentum vs
    the 10.2 baseline, RVOL ≈ 2 (100k session volume vs 1M ADV at 30 min into
    the session), price +0.48% above a 10.45 VWAP (inside the 1.5% extension
    ceiling), no low/high range data (exhaustion gate silent).
    """
    from market.twelvedata_bars import SessionAnalysis
    kw = dict(
        past_price=10.2, current_bar_price=10.5, spread_proxy_pct=0.5,
        session_volume=100_000, vwap=10.45, last_price=10.5,
        session_low=None, session_high=None,
    )
    kw.update(overrides)
    return SessionAnalysis(**kw)


_DAILY = (1_000_000, 10_000_000.0, 10.0)  # (avg_daily_volume, adv$, prev_close)


def _confirm_with(monkey_now_et, quote, sa, daily=_DAILY):
    """Run confirm_price_signal with all data dependencies mocked (v20 seams:
    one session-analysis pull + cached daily stats).

    The session is pinned to "regular": get_trading_session() reads the REAL
    wall clock (pd.Timestamp.now), not the mocked pc.datetime — unpinned, the
    whole suite silently switched to the extended-regime gate variant whenever
    it ran 16:00–20:00 ET and failed on fixtures built for RTH (v21.2; the
    deploy pipeline runs pytest, so this made deploys time-of-day dependent).
    """
    import market.price_check as pc
    fake_dt = MagicMock()
    fake_dt.now.side_effect = lambda tz=None: monkey_now_et
    with patch.object(pc, "datetime", fake_dt), \
         patch.object(pc, "get_trading_session", return_value="regular"), \
         patch.object(pc, "get_quote_with_fallback", return_value=quote), \
         patch.object(pc, "get_session_analysis", return_value=sa) as mock_sa, \
         patch.object(pc, "get_daily_stats", return_value=daily):
        conf = pc.confirm_price_signal("ACME_US_EQ")
    return conf, mock_sa


class TestSessionVolumeGates:
    """
    v20: session minute-bar volume is THE RVOL numerator (the lagging daily
    bar and its rescue dance are gone), and degraded data still fails closed:
      - zero measured session volume → low_volume (GLASF must not trade)
      - session bars entirely missing early in the session → insufficient_data
        (open-price momentum fallback alone must never confirm a bare quote)
    """

    # pc=10.28 → +2.14% on the day. Keeps these tests aimed at the VOLUME
    # gates: at the old pc=10.0 (+5.0%) the zero-volume case now trips the
    # v21.11 plausibility cross-check first, which is a correct verdict but a
    # different one than this class exists to pin down.
    _QUOTE = {"c": 10.5, "o": 10.0, "pc": 10.28}

    @staticmethod
    def _now_et(minutes_after_open=30):
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 10, 9, 30 + minutes_after_open % 60,
                                    0)) if minutes_after_open < 30 else \
               et.localize(datetime(2026, 7, 10, 10, minutes_after_open - 30, 0))

    def test_healthy_session_volume_confirms_with_one_pull(self):
        conf, mock_sa = _confirm_with(self._now_et(), self._QUOTE, _mk_sa())
        assert conf is not None and conf.is_confirmed, conf and conf.reason
        assert conf.rvol > 1.5
        mock_sa.assert_called_once()  # every gate fed by ONE bars pull

    def test_zero_session_volume_rejects_low_volume(self):
        # GLASF case: nothing traded per the minute bars. The zero measurement
        # counts as a measurement and fails the participation gate.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=0, vwap=None, last_price=None),
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_volume"

    def test_no_session_bars_early_rejects_insufficient_data(self):
        # First 15 min: no bars at all → open-price momentum fallback engages,
        # but with no volume measurement AND no VWAP the signal must NOT
        # confirm on a bare quote.
        conf, _ = _confirm_with(self._now_et(10), self._QUOTE, None)
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "insufficient_data"

    def test_no_session_bars_late_cannot_evaluate(self):
        # Past the open window the open-price fallback is not honest — the
        # signal is unpriceable this cycle (retry), not rejected.
        conf, _ = _confirm_with(self._now_et(30), self._QUOTE, None)
        assert conf is None


class TestOverextendedGate:
    """
    v20: never enter with the stop on the far side of value. If price is
    further above VWAP than the stop is wide, a routine reversion to VWAP
    stops the trade out by construction (LEVI +1.9% above VWAP, CRCL +2.2%,
    both with a 2% stop — the two 2026-07 losses). TRANSIENT: the re-eval
    queue converts the reject into a first-pullback entry.
    """

    _QUOTE = {"c": 10.5, "o": 10.0, "pc": 10.0}

    @staticmethod
    def _now_et():
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 10, 10, 0, 0))  # 30 min after open

    def test_extended_above_vwap_rejected(self):
        # price 10.5 vs vwap 10.0 → +5.0% above, way past the 1.5% ceiling.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE, _mk_sa(vwap=10.0),
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "overextended"

    def test_near_vwap_passes(self):
        # price 10.5 vs vwap 10.45 → +0.48% above — inside the band.
        conf, _ = _confirm_with(self._now_et(), self._QUOTE, _mk_sa())
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_overextended_is_transient_everywhere(self):
        # Both re-check mechanisms must treat it as tape-of-this-minute.
        import main as m
        import premarket.scanner as sc
        assert "overextended" in m._TRANSIENT_REJECT_CODES
        assert "overextended" in sc._TRANSIENT_REJECT_CODES

    def test_no_vwap_means_gate_silent(self):
        # Without a VWAP there is no value reference — the extension gate
        # stays out of it (insufficient_data governs the fully-blind case;
        # here session volume still provides participation evidence).
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE, _mk_sa(vwap=None),
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_fires_even_with_vwap_confirmation_disabled(self):
        # v20.1 review finding: the extension ceiling is stop-geometry, not an
        # accumulation test — turning off REQUIRE_VWAP_CONFIRMATION must not
        # silently disable the chasing protection with it.
        import market.price_check as pc
        with patch.object(pc.cfg, "require_vwap_confirmation", False):
            conf, _ = _confirm_with(
                self._now_et(), self._QUOTE, _mk_sa(vwap=10.0),  # +5% above
            )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "overextended"


class TestRvolBypass:
    """
    v20.2: a mega/large-cap (ADV$ >= cfg.rvol_bypass_min_adv_dollar) doesn't
    need anomalous RELATIVE volume to make a real move — a held VWAP is the
    size-neutral substitute for RVOL. 2026-07-13 post-mortem: BMY (ADV$
    $752M) drifted +2.1% all session, RVOL never exceeded 0.3, held VWAP
    throughout, and was rejected low_volume on all 27 re-eval cycles because
    the RVOL floor ran before the VWAP gate ever got a look.
    """

    # pc=10.28 → +2.14% on the day, matching the BMY drift this class
    # documents. (It was pc=10.0/+5.0% until v21.11, which was never the
    # incident: a +5% day move on RVOL ~0.06 is the volume feed lagging, and
    # the 5.5 plausibility gate now — correctly — defers that combination
    # before the bypass is ever consulted. See test_implausible_volume_*.)
    _QUOTE = {"c": 10.5, "o": 10.0, "pc": 10.28}
    _MEGACAP_DAILY = (5_000_000, 750_000_000.0, 10.28)  # BMY-scale ADV$

    @staticmethod
    def _now_et():
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 10, 10, 0, 0))  # 30 min after open

    def test_megacap_holding_vwap_bypasses_low_rvol(self):
        # avg_daily_volume 5M x expected_fraction(30min)=0.05 -> expected 250k;
        # session_volume 50k -> rvol 0.2, well below the 1.5 floor. Price 10.5
        # holds the default 10.45 VWAP (+0.48%), so the bypass should confirm.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=50_000),
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason
        assert conf.rvol < 1.5

    def test_bypass_does_not_apply_below_adv_floor(self):
        # Same low RVOL, but ADV$ is the default $10M — far below the $50M
        # bypass floor. Existing small/mid-cap behavior must be unchanged.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=10_000),  # 1M avg x 0.05 = 50k expected -> rvol 0.2
            daily=_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_volume"

    def test_bypass_does_not_apply_when_vwap_not_held(self):
        # Mega-cap ADV$, low RVOL, but price sits BELOW VWAP — no evidence of
        # accumulation, so the RVOL floor must still reject.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=50_000, vwap=10.55),  # price 10.5 < vwap 10.55
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_volume"

    def test_bypass_does_not_apply_without_vwap(self):
        # Mega-cap ADV$, low RVOL, but no VWAP available at all — nothing to
        # substitute for RVOL, so the floor must still reject.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=50_000, vwap=None),
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "low_volume"

    def test_rvol_ceiling_still_applies_to_megacaps(self):
        # The bypass only touches the FLOOR — a parabolic mega-cap is still
        # the halt-pattern signature regardless of ADV$.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_volume=10_000_000),  # 5M x 0.05=250k expected -> rvol 40
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "high_volume"


class TestExhaustionGate:
    """
    v19.5: reject a stock that has already recovered most of a large intraday
    round trip, even when day_change_pct (vs yesterday) and recent_move_pct
    (last ~5 min) both look clean (2026-07-09 LEVI post-mortem: gapped -7.8%
    at the open, recovered to +2.3% by entry — bought within 15 cents of the
    exact high of the day).
    """

    _QUOTE = {"c": 10.5, "o": 10.0, "pc": 10.0}

    @staticmethod
    def _now_et():
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 9, 10, 0, 0))  # 30 min after open

    def test_recovered_bounce_rejected(self):
        # range = (10.52-9.5)/9.5 = 10.7% (>=5%); recovered = (10.5-9.5)/(10.52-9.5) = 98% (>=75%)
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_low=9.5, session_high=10.52),
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "exhausted_bounce"

    def test_small_range_not_flagged(self):
        # range = (10.6-10.3)/10.3 = 2.9% < 5% floor — not a real round trip,
        # gate doesn't even evaluate recovered fraction.
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_low=10.3, session_high=10.6),
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_large_range_but_not_mostly_recovered_passes(self):
        # range = (12.0-9.0)/9.0 = 33% (big); recovered = (10.5-9.0)/(12.0-9.0) = 50% < 75%
        conf, _ = _confirm_with(
            self._now_et(), self._QUOTE,
            _mk_sa(session_low=9.0, session_high=12.0, vwap=10.5),
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_missing_range_data_does_not_gate(self):
        # No session_low/session_high available — the gate has nothing to
        # evaluate and must not reject for lack of data (that's
        # insufficient_data's job).
        conf, _ = _confirm_with(self._now_et(), self._QUOTE, _mk_sa())
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_toggle_off_disables_gate(self):
        import market.price_check as pc
        with patch.object(pc.cfg, "require_exhaustion_check", False):
            conf, _ = _confirm_with(
                self._now_et(), self._QUOTE,
                _mk_sa(session_low=9.5, session_high=10.52),  # rejected case above
            )
        assert conf is not None and conf.is_confirmed, conf and conf.reason


class TestSellEscalation:
    """monitor: consecutive unfilled limit sells escalate to a market order."""

    def setup_method(self):
        import monitor.position_monitor as pm
        pm._sell_fail_counts.clear()

    teardown_method = setup_method

    @patch("monitor.position_monitor.record_system_event")
    def test_escalates_after_threshold(self, mock_evt):
        import monitor.position_monitor as pm
        assert pm._note_sell_failed(9, "GLASF_US_EQ") is False
        assert pm._note_sell_failed(9, "GLASF_US_EQ") is False
        assert pm._note_sell_failed(9, "GLASF_US_EQ") is True   # 3rd strike
        assert pm._sell_fail_counts[9] == 3
        mock_evt.assert_called_once()          # exit_stuck emitted at threshold
        assert mock_evt.call_args.args[0] == "exit_stuck"
        assert pm._note_sell_failed(9, "GLASF_US_EQ") is True   # stays escalated
        mock_evt.assert_called_once()          # but the event fires only once

    @patch("monitor.position_monitor.record_system_event")
    def test_counters_are_per_trade(self, _evt):
        import monitor.position_monitor as pm
        pm._note_sell_failed(1, "A_US_EQ")
        pm._note_sell_failed(2, "B_US_EQ")
        assert pm._sell_fail_counts == {1: 1, 2: 1}


class TestReevalQueue:
    """main.py transient-rejection re-evaluation (VERA/CSCO class misses)."""

    def _item(self, ticker="VERA_US_EQ"):
        from news.fetcher import NewsItem
        from datetime import timezone as _tz
        return NewsItem(
            article_id="a1", ticker=ticker, headline="h", body="", source="bz",
            published_at=datetime.now(_tz.utc), sentiment="positive",
            confidence=0.95, catalyst_type="fda_approval",
            already_moved=False, catalyst_magnitude=5,
        )

    def setup_method(self):
        import main
        main._reeval_queue.clear()

    teardown_method = setup_method

    def test_transient_rejection_parks_signal(self):
        import main
        main._queue_reeval(self._item(), signal_id=42)
        assert ("a1", "VERA_US_EQ") in main._reeval_queue
        # Re-parking must not extend the expiry window.
        first_expiry = main._reeval_queue[("a1", "VERA_US_EQ")]["expires_at"]
        main._queue_reeval(self._item(), signal_id=42)
        assert main._reeval_queue[("a1", "VERA_US_EQ")]["expires_at"] == first_expiry

    @patch("main._enter_confirmed", return_value=True)
    @patch("main.clear_rejection")
    @patch("main.set_rejection_reason")
    @patch("main.confirm_price_signal")
    @patch("main.was_recently_traded", return_value=False)
    @patch("main._risk_gates_pass", return_value=(True, ""))
    def test_participation_arrives_then_trades(
        self, _gates, _cooldown, mock_confirm, mock_set_rej, mock_clear, mock_enter
    ):
        import main
        main._queue_reeval(self._item(), signal_id=42)

        # Cycle 1: still low_volume → stays parked, row updated.
        mock_confirm.return_value = _mk_conf(
            reason_code="low_volume", is_confirmed=False)
        assert main._process_reeval_queue() == 0
        assert len(main._reeval_queue) == 1
        mock_set_rej.assert_called_once()

        # Cycle 2: volume arrived → confirmed, rejection cleared, trade entered.
        mock_confirm.return_value = _mk_conf(reason_code="approved",
                                             is_confirmed=True)
        assert main._process_reeval_queue() == 1
        assert len(main._reeval_queue) == 0
        mock_clear.assert_called_once_with(42)
        mock_enter.assert_called_once()

    @patch("main._enter_confirmed", return_value=True)
    @patch("main.set_rejection_reason")
    @patch("main.confirm_price_signal")
    @patch("main.was_recently_traded", return_value=False)
    @patch("main._risk_gates_pass", return_value=(True, ""))
    def test_terminal_rejection_unparks(
        self, _gates, _cooldown, mock_confirm, mock_set_rej, mock_enter
    ):
        import main
        main._queue_reeval(self._item(), signal_id=42)
        mock_confirm.return_value = _mk_conf(
            reason_code="extended_move", is_confirmed=False)
        assert main._process_reeval_queue() == 0
        assert len(main._reeval_queue) == 0     # terminal → dropped
        mock_set_rej.assert_called_once()       # final reason recorded
        mock_enter.assert_not_called()

    @patch("main.confirm_price_signal")
    @patch("main._risk_gates_pass", return_value=(True, ""))
    def test_expired_entry_dropped_without_recheck(self, _gates, mock_confirm):
        import main
        from datetime import timezone as _tz
        main._queue_reeval(self._item(), signal_id=42)
        key = ("a1", "VERA_US_EQ")
        main._reeval_queue[key]["expires_at"] = (
            datetime.now(_tz.utc) - timedelta(minutes=1))
        assert main._process_reeval_queue() == 0
        assert len(main._reeval_queue) == 0
        mock_confirm.assert_not_called()


class TestSessionAnalysisAggregates:
    """get_session_analysis: today-only volume/low/high aggregation."""

    @patch("market.twelvedata_bars._get_time_series")
    def test_sums_only_todays_volume(self, mock_ts):
        import market.twelvedata_bars as td
        import pytz as _pytz
        et = _pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        t1 = (now_et - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
        t2 = (now_et - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:00")
        mock_ts.return_value = [
            {"datetime": t1, "high": "10.2", "low": "10.0",
             "close": "10.1", "volume": "3000"},
            {"datetime": t2, "high": "10.1", "low": "9.9",
             "close": "10.0", "volume": "2000"},
            {"datetime": "2026-06-29 15:59:00", "high": "9.0", "low": "8.8",
             "close": "8.9", "volume": "99999"},   # prior session — excluded
        ]
        sa = td.get_session_analysis("ACME")
        assert sa is not None
        assert sa.session_volume == 5000
        assert sa.vwap is not None and 9.9 < sa.vwap < 10.2
        assert sa.last_price == 10.1
        assert sa.session_low == 9.9   # prior-session bar excluded
        assert sa.session_high == 10.2


# ── v21.11: the 2026-07-31 NVT post-mortem ────────────────────────────────────

class TestEntryPriceFreshness:
    """
    v21.11 gate 0 (stale_price). 2026-07-31, NVT: both providers froze at the
    open. The quote that confirmed the entry honestly reported itself ~3 min
    old and the 20-minute COVERAGE threshold waved it through — so momentum
    (+1.74%), RVOL and VWAP were all computed from the 09:33 tape while the
    real market traded 1.1% lower and falling. Stopped out 42s after the fill.

    A lagging quote does not make the gates fail. It makes them agree.
    """

    @staticmethod
    def _now_et():
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 10, 10, 0, 0))

    def _quote(self, age_seconds):
        return {"c": 10.5, "o": 10.0, "pc": 10.28,
                "t": time.time() - age_seconds}

    def test_fresh_quote_confirms(self):
        conf, _ = _confirm_with(self._now_et(), self._quote(10), _mk_sa())
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_lagging_quote_rejected_before_any_other_gate(self):
        conf, mock_sa = _confirm_with(self._now_et(), self._quote(180), _mk_sa())
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "stale_price"
        # Runs before the bars pull: a price we won't act on costs no credit.
        mock_sa.assert_not_called()

    def test_quote_without_timestamp_is_not_rejected(self):
        # Fail-open on missing metadata — the other gates still apply. Only
        # POSITIVE evidence of lag rejects.
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.28}, _mk_sa(),
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_boundary_is_the_configured_age(self):
        import market.price_check as pc
        with patch.object(pc.cfg, "max_entry_quote_age_seconds", 90):
            inside, _ = _confirm_with(self._now_et(), self._quote(80), _mk_sa())
            outside, _ = _confirm_with(self._now_et(), self._quote(100), _mk_sa())
        assert inside is not None and inside.is_confirmed
        assert outside is not None and outside.reason_code == "stale_price"

    def test_stale_price_is_transient_everywhere(self):
        # A feed running behind catches up in minutes — re-check, don't discard.
        import main as m
        import premarket.scanner as sc
        assert "stale_price" in m._TRANSIENT_REJECT_CODES
        assert "stale_price" in sc._TRANSIENT_REJECT_CODES


class TestVolumePlausibility:
    """
    v21.11 gate 5.5 (stale_volume). NVT was +15.59% on the day with RVOL
    reported as 0.28, while the real tape printed ~10% of an average DAY in
    the first minute alone. Because that reading looked low it triggered the
    size-neutral RVOL bypass — the gate meant for genuinely quiet mega-caps —
    and confirmed the entry on participation evidence that never existed.
    """

    _MEGACAP_DAILY = (5_000_000, 750_000_000.0, 10.0)

    @staticmethod
    def _now_et():
        import pytz
        et = pytz.timezone("America/New_York")
        return et.localize(datetime(2026, 7, 10, 10, 0, 0))

    def test_big_move_on_near_zero_rvol_defers(self):
        # +5.0% on the day, RVOL ~0.06 — the shares had to trade for the
        # price to get there, so the volume feed is behind.
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.0},
            _mk_sa(session_volume=50_000, vwap=10.45),
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "stale_volume"

    def test_quiet_megacap_drift_still_uses_the_bypass(self):
        # The BMY case this must NOT break: +2.14% on the day, low RVOL,
        # VWAP held. A small move on low volume is a market state, not a
        # data state.
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.28},
            _mk_sa(session_volume=50_000, vwap=10.45),
            daily=(5_000_000, 750_000_000.0, 10.28),
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason
        assert conf.rvol < 1.5

    def test_big_move_with_real_volume_is_untouched(self):
        # +5.0% on the day AND healthy RVOL — nothing implausible here.
        conf, _ = _confirm_with(
            self._now_et(), {"c": 10.5, "o": 10.0, "pc": 10.0},
            _mk_sa(session_volume=400_000, vwap=10.45),
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and conf.is_confirmed, conf and conf.reason

    def test_day_move_ceiling_still_wins(self):
        # Precedence: a move too big to trade is a PERMANENT verdict. It must
        # stay terminal (extended_move), not be downgraded to this transient
        # code and re-queued forever.
        conf, _ = _confirm_with(
            self._now_et(), {"c": 13.0, "o": 10.0, "pc": 10.0},
            _mk_sa(session_volume=50_000, vwap=12.9, past_price=12.9,
                   current_bar_price=13.0, last_price=13.0),
            daily=self._MEGACAP_DAILY,
        )
        assert conf is not None and not conf.is_confirmed
        assert conf.reason_code == "extended_move"

    def test_stale_volume_is_transient_everywhere(self):
        import main as m
        import premarket.scanner as sc
        assert "stale_volume" in m._TRANSIENT_REJECT_CODES
        assert "stale_volume" in sc._TRANSIENT_REJECT_CODES


class TestDayMoveCeiling:
    """
    v21.11: MAX_DAY_MOVE_PCT 25% → 10%, calibrated on all 24 closed trades.
    With a 2% stop and a 5% target, entering a stock already up 15% needs
    +21% on the day to pay out while a routine pullback stops it — the
    risk:reward is inverted before the entry is even placed.
    """

    def test_default_is_ten_percent(self):
        import os
        from config.settings import Settings
        saved = os.environ.pop("MAX_DAY_MOVE_PCT", None)
        try:
            assert Settings().max_day_move_pct == 10.0
        finally:
            if saved is not None:
                os.environ["MAX_DAY_MOVE_PCT"] = saved

    def test_ceiling_must_exceed_the_take_profit_target(self):
        # A ceiling at or below the target rejects everything that could pay
        # out. cfg.validate() must refuse that configuration outright.
        from config.settings import Settings
        s = Settings()
        s.max_day_move_pct = s.take_profit_pct
        with pytest.raises(EnvironmentError):
            s.validate()


class TestCashCacheCollision:
    """
    v21.11: /equity/account/cash had three callers on independent schedules.
    APScheduler anchors every IntervalTrigger to process start and 5 min is an
    exact multiple of 1 min, so the kill-switch call and the 5-minute snapshot
    landed on the SAME INSTANT every fifth minute — 64 rejections on
    2026-07-31, 44 of which stood an entire news cycle down. The lock is the
    fix (it serializes the racers); the TTL bounds how stale the shared answer
    may be.
    """

    def _cash(self):
        return {"total": 5000.0, "free": 5000.0, "invested": 0.0}

    @patch("trading.executor._get")
    def test_second_caller_within_ttl_makes_no_request(self, mock_get):
        import trading.executor as ex
        mock_get.return_value = self._cash()
        assert ex.get_portfolio_value() == 5000.0
        assert ex.get_account_summary() == (5000.0, 5000.0)
        assert mock_get.call_count == 1   # the collision that used to 429

    @patch("trading.executor._get")
    def test_expired_ttl_refetches(self, mock_get):
        import trading.executor as ex
        mock_get.return_value = self._cash()
        ex.get_portfolio_value()
        ex._cash_cache = (time.time() - ex._CASH_CACHE_TTL_SECONDS - 1,
                          self._cash())
        ex.get_portfolio_value()
        assert mock_get.call_count == 2

    @patch("trading.executor._get")
    def test_failure_is_not_cached(self, mock_get):
        import trading.executor as ex
        mock_get.side_effect = [
            ex.T212HTTPError(429, "too many requests"),
            ex.T212HTTPError(429, "too many requests"),
            self._cash(),
        ]
        with patch("trading.executor.time.sleep"):
            assert ex.get_portfolio_value() is None    # both attempts failed
            assert ex.get_portfolio_value() == 5000.0  # next call still tries

    @patch("trading.executor._get")
    def test_concurrent_callers_are_serialized(self, mock_get):
        # The real-world shape: two scheduler threads firing on the same tick.
        import threading as _th
        import trading.executor as ex

        def slow_get(_path):
            time.sleep(0.05)
            return self._cash()

        mock_get.side_effect = slow_get
        results = []
        threads = [
            _th.Thread(target=lambda: results.append(ex.get_portfolio_value()))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [5000.0] * 4
        assert mock_get.call_count == 1


class TestPhantomTickerDropped:
    """
    v21.11: Benzinga tagged a Moog article with both "MOG.A" (real) and "MOG"
    (not a US listing — Moog trades as MOG.A/MOG.B). The <symbol>_US_EQ
    fallback manufactured MOG_US_EQ, which then spent 2026-07-31 consuming
    quote retries and API budget for an instrument that cannot exist.
    """

    def test_symbol_absent_from_a_built_map_is_dropped(self):
        import trading.executor as ex
        with patch.object(ex, "_symbol_to_t212", {"MOG.A": "MOG.A_US_EQ"}):
            assert ex.resolve_t212_ticker("MOG.A") == "MOG.A_US_EQ"
            assert ex.resolve_t212_ticker("MOG") is None

    def test_fallback_still_applies_before_the_map_is_built(self):
        # A startup before the first successful build must not drop everything.
        import trading.executor as ex
        with patch.object(ex, "_symbol_to_t212", {}):
            assert ex.resolve_t212_ticker("AAPL") == "AAPL_US_EQ"


class TestExitExcursionRecorded:
    """
    v21.11: MFE/MAE only ever saw prices the POLLING loop observed, and a
    broker-side resting stop fills without the poller involved. NVT closed at
    -2.56% carrying max_adverse_pct = +0.75% — an impossible row, because the
    last polled quote was frozen and the real fill was never fed in. That
    biases MAE toward zero on exactly the trades where the question is "how
    much heat did this take?".
    """

    def test_resting_stop_fill_widens_the_band(self):
        import monitor.position_monitor as pm
        trade = {"id": 24, "ticker": "NVT_US_EQ", "buy_price": 166.13,
                 "stop_order_id": "1", "quantity": 1.98}
        with patch.object(pm, "update_trade_excursion") as mock_upd, \
             patch.object(pm, "close_trade"), \
             patch.object(pm, "_fetch_fill", return_value=None), \
             patch.object(pm, "_parse_fill",
                          return_value=(162.32, None, None, None)):
            pm._close_as_resting_fill(trade, "1", "stop")
        recorded = [c.args[1] for c in mock_upd.call_args_list]
        assert recorded, "the realized stop fill must reach the excursion band"
        assert recorded[0] == pytest.approx(-2.293, abs=0.01)

    def test_polled_exit_price_also_recorded(self):
        import monitor.position_monitor as pm
        trade = {"id": 25, "ticker": "ACME_US_EQ", "buy_price": 100.0}
        with patch.object(pm, "update_trade_excursion") as mock_upd:
            pm._record_exit_excursion(trade, 104.0)
        assert mock_upd.call_args.args[1] == pytest.approx(4.0)

    def test_bad_fill_price_is_ignored_not_raised(self):
        import monitor.position_monitor as pm
        trade = {"id": 26, "ticker": "ACME_US_EQ", "buy_price": 100.0}
        with patch.object(pm, "update_trade_excursion") as mock_upd:
            pm._record_exit_excursion(trade, None)
            pm._record_exit_excursion({"id": 27, "buy_price": 0}, 10.0)
        mock_upd.assert_not_called()


class TestProviderOutageDoesNotBlacklistTickers:
    """
    v21.11: a no-quote strike asserts "no provider carries this instrument".
    On 2026-07-31 both feeds served the previous day's close for every symbol
    they were asked about (SONY included) for 40+ minutes, and GTES + IRMD —
    liquid, fully-covered names — were blacklisted for the session as a
    result. The frozen-feed tripwire already DETECTED this; the blacklist
    simply wasn't listening to it.
    """

    def _item(self, ticker="GTES_US_EQ"):
        import pytz as _pytz
        from news.fetcher import NewsItem
        return NewsItem(
            article_id="a1", ticker=ticker, headline="h", body="", source="s",
            published_at=datetime.now(_pytz.utc), sentiment="positive",
            confidence=0.8, catalyst_type="guidance_raise", already_moved=False,
            catalyst_magnitude=3,
        )

    def test_strikes_suppressed_while_a_feed_is_frozen(self):
        import main
        main._no_quote_ticker_strikes.clear()
        main._no_quote_blackout.clear()
        with patch.object(main, "quote_feed_degraded", return_value=True):
            for _ in range(5):
                main._queue_retry(self._item())
        assert "GTES_US_EQ" not in main._no_quote_blackout
        assert not main._no_quote_ticker_strikes

    def test_strikes_still_count_when_feeds_are_healthy(self):
        import main
        main._no_quote_ticker_strikes.clear()
        main._no_quote_blackout.clear()
        with patch.object(main, "quote_feed_degraded", return_value=False):
            for _ in range(main._NO_QUOTE_BLACKOUT_RETRIES):
                main._queue_retry(self._item())
        assert "GTES_US_EQ" in main._no_quote_blackout

    def test_degraded_reads_the_live_streak_not_the_alert_latch(self):
        # The alert fires once per process; this must go back to False as
        # soon as a usable quote arrives, or one outage would suppress
        # strikes for the rest of the day.
        import market.price_check as pc
        for i in range(pc._STALE_QUOTE_ALERT_THRESHOLD):
            pc._note_quote_stale("Finnhub", f"SYM{i}", 1051)
        assert pc.quote_feed_degraded() is True
        pc._note_quote_fresh("Finnhub")
        assert pc.quote_feed_degraded() is False


# ── v21.12 (2026-08-04 post-mortem) ──────────────────────────────────────────

class TestExplainerHeadlineFilter:
    """
    v21.12: an article ABOUT a price move that already happened is not the
    catalyst that caused it. Trade #25 (BE, 2026-08-04, −2.82%, the only trade
    of the day) came from "Bloom Energy Stock Charges Higher Tuesday: What's
    Driving the Post-Earnings Rally?" — scored guidance_raise / positive /
    conf 0.75 with already_moved=FALSE, while the headline says in its own
    words that the rally was underway and the stock was already +3.99% on the
    day when we bought it.
    """

    # Real headlines from the scored corpus (2026-07-25 → 2026-08-04).
    EXPLAINERS = [
        "Bloom Energy Stock Charges Higher Tuesday: What's Driving the Post-Earnings Rally?",
        "What's Going On With Applied Digital Stock Today?",
        "Why Tower Semiconductor Stock Is Surging Today: TSEM Beats Q2 Earnings",
        "Philips Stock Sinks To 52-Week Low - Here's Why",
        "What's Behind the Amazon Stock Bounce Ahead of Q2 Earnings?",
        "Moleculin Biotech Stock Is Sinking Friday: What's Going On?",
        "Robinhood Stock is Pulling Back: What's Happening Today?",
        "Nokia Stock Rallies Tuesday: What's Driving the Rebound?",
        "ServiceNow Stock Powers Higher Tuesday: What's Driving the Move?",
        "Ford Motor Stock Dips Friday: What's Driving the Post-Earnings Reset?",
    ]

    # Genuine single-stock catalysts — these MUST survive the filter, because a
    # false positive here is a silently-missed trade with no eval-loop trace.
    CATALYSTS = [
        "Bloom Energy Raises FY2026 Revenue Guidance To $2.0B From $1.8B",
        "Mazda Motor Affirms FY2027 GAAP EPS Guidance of $0.46 vs $0.42 Est",
        "FDA Approves Acme Pharma's Lead Candidate For Advanced Melanoma",
        "TransDigm Q3 Adj. EPS $10.87 Beats $10.30 Estimate, Sales $2.741B",
        "Voyager Boosts Forecast After Astrobotic Acquisition Success",
        "Grab Delivers Strong GMV, Raises Outlook",
        "Aehr Test Systems Receives Follow-On Production Order From Lead Customer",
        "Novo Nordisk Raises 2026 Adj Sales, Operating Profit Outlook",
        "Acme Provides Market Update On Phase 3 Results",
        "Acme Announces $500M Share Repurchase Program",
    ]

    def test_explainer_headlines_match(self):
        from news.fetcher import _EXPLAINER_RE
        for h in self.EXPLAINERS:
            assert _EXPLAINER_RE.search(h), f"should have matched: {h}"

    def test_genuine_catalysts_do_not_match(self):
        from news.fetcher import _EXPLAINER_RE
        for h in self.CATALYSTS:
            assert not _EXPLAINER_RE.search(h), f"false positive: {h}"

    def test_the_bloom_energy_article_never_reaches_claude(self):
        """End-to-end: the article that produced trade #25 must be dropped in
        the pre-Claude filter pass, not merely scored and gated later."""
        from news.fetcher import _EXPLAINER_RE, _DIGEST_RE, _ANALYST_ACTION_RE
        h = self.EXPLAINERS[0]
        # It slipped the two pre-existing filters — that is why it was traded.
        assert not _DIGEST_RE.search(h)
        assert not _ANALYST_ACTION_RE.search(h)
        assert _EXPLAINER_RE.search(h)

    def test_null_headline_does_not_crash(self):
        """The feed can send an explicit null title (see the `or ""` at the
        call site) — the regex must never be handed None."""
        from news.fetcher import _EXPLAINER_RE
        assert not _EXPLAINER_RE.search("")


class TestFrozenFeedNeedsDistinctSymbols:
    """
    v21.12: a stale STREAK is not a frozen provider unless it spans several
    DISTINCT symbols. 2026-08-04: MZDAY (Mazda's OTC ADR) sat in the re-eval
    queue and was polled 221 times; ten in a row tripped the tripwire on BOTH
    providers while the feeds were healthy (BE quoted correctly two minutes
    later). The false alarm closed a loop — quote_feed_degraded() suppresses
    the strikes that would blacklist a dead ticker, so MZDAY protected itself
    from ever being blacklisted and kept polling to re-trip the alarm.
    """

    def setup_method(self):
        import market.price_check as pc
        pc._stale_quote_streak.clear()
        pc._stale_quote_symbols.clear()
        pc._stale_quote_reported.clear()

    teardown_method = setup_method

    @patch("storage.database.record_system_event")
    def test_one_dead_ticker_polled_in_a_loop_is_not_a_frozen_feed(self, mock_evt):
        import market.price_check as pc
        # Far past the streak threshold, but always the same instrument.
        for _ in range(pc._STALE_QUOTE_ALERT_THRESHOLD * 5):
            pc._note_quote_stale("Twelvedata", "MZDAY", 1444)
        assert pc.quote_feed_degraded() is False
        mock_evt.assert_not_called()

    @patch("storage.database.record_system_event")
    def test_a_real_outage_across_symbols_still_fires(self, mock_evt):
        """2026-07-31: both feeds served the previous day's close for EVERY
        symbol asked, SONY included — that clears a distinct-symbol bar
        trivially, so the original protection must be intact."""
        import market.price_check as pc
        for i in range(pc._STALE_QUOTE_ALERT_THRESHOLD):
            pc._note_quote_stale("Finnhub", f"SYM{i}", 1051)
        assert pc.quote_feed_degraded() is True
        assert mock_evt.call_count == 1
        assert mock_evt.call_args.args[0] == "stale_quote_feed"

    @patch("storage.database.record_system_event")
    def test_distinct_symbols_alone_are_not_enough(self, mock_evt):
        """Both bars must be cleared — a handful of illiquid names is not a
        provider outage either."""
        import market.price_check as pc
        assert pc._STALE_QUOTE_MIN_DISTINCT_SYMBOLS < pc._STALE_QUOTE_ALERT_THRESHOLD
        for i in range(pc._STALE_QUOTE_MIN_DISTINCT_SYMBOLS):
            pc._note_quote_stale("Finnhub", f"SYM{i}", 900)
        assert pc.quote_feed_degraded() is False
        mock_evt.assert_not_called()

    def test_symbol_set_clears_on_recovery(self):
        """Otherwise a feed alternating fresh/stale accumulates distinct
        symbols forever and eventually clears the bar without ever freezing."""
        import market.price_check as pc
        for i in range(pc._STALE_QUOTE_MIN_DISTINCT_SYMBOLS + 1):
            pc._note_quote_stale("Finnhub", f"SYM{i}", 900)
        pc._note_quote_fresh("Finnhub")
        assert not pc._stale_quote_symbols["Finnhub"]
        assert pc._stale_quote_streak["Finnhub"] == 0

    @patch("storage.database.record_system_event")
    def test_mzday_scenario_leaves_the_ticker_blacklistable(self, mock_evt):
        """The point of the fix: with the feed correctly judged healthy, a
        genuinely un-quotable ticker can accumulate strikes again."""
        import main
        import market.price_check as pc
        main._no_quote_ticker_strikes.clear()
        main._no_quote_blackout.clear()
        for _ in range(pc._STALE_QUOTE_ALERT_THRESHOLD * 3):
            pc._note_quote_stale("Twelvedata", "MZDAY", 1444)
        assert pc.quote_feed_degraded() is False

        import pytz as _pytz
        from news.fetcher import NewsItem
        item = NewsItem(
            article_id="a1", ticker="MZDAY_US_EQ", headline="h", body="",
            source="s", published_at=datetime.now(_pytz.utc),
            sentiment="positive", confidence=0.8,
            catalyst_type="guidance_raise", already_moved=False,
            catalyst_magnitude=3,
        )
        for _ in range(main._NO_QUOTE_BLACKOUT_RETRIES):
            main._queue_retry(item)
        assert "MZDAY_US_EQ" in main._no_quote_blackout


class TestEmptyBatchRetryAndAlert:
    """
    v21.12: the v21.7 SINGLE retry is not enough. 2026-08-04: 25 news cycles
    across two windows (07:00-07:18 and 07:31-07:36 ET) where BOTH the first
    call and its retry returned an empty classifications list. The unscored
    backlog grew 10 → 36 articles and then shrank as they aged out of the
    freshness window UNSCORED — and nothing recorded a system_event, so the
    25-minute blind spot was invisible to every monitoring surface.
    """

    def _tool_msg(self, classifications):
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    def _articles(self, n=3):
        return [{"id": str(i), "headline": f"h{i}", "teaser": "t"} for i in range(n)]

    def setup_method(self):
        import news.fetcher as nf
        nf._consecutive_empty_batches = 0
        nf._claude_cooldown = None

    teardown_method = setup_method

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_all_attempts_empty_records_one_event(self, mock_claude, mock_evt, _sleep):
        from news.fetcher import _batch_score_sentiment, _EMPTY_BATCH_ATTEMPTS
        mock_claude.messages.create.return_value = self._tool_msg([])
        scores = _batch_score_sentiment(self._articles())
        assert scores == {}                                    # fail-closed
        assert mock_claude.messages.create.call_count == _EMPTY_BATCH_ATTEMPTS
        assert mock_evt.call_count == 1
        assert mock_evt.call_args.args[0] == "claude_empty_batch"

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_recovery_on_a_later_attempt_scores_and_does_not_alert(
        self, mock_claude, mock_evt, _sleep
    ):
        from news.fetcher import _batch_score_sentiment
        good = [{"id": "0", "sentiment": "positive", "confidence": 0.8,
                 "catalyst_type": "guidance_raise", "already_moved": False,
                 "catalyst_magnitude": 3}]
        # v21.13: the budget is 1 retry, so recovery must land on attempt 2.
        mock_claude.messages.create.side_effect = [
            self._tool_msg([]), self._tool_msg(good),
        ]
        scores = _batch_score_sentiment(self._articles())
        assert scores["0"]["catalyst_type"] == "guidance_raise"
        mock_evt.assert_not_called()

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_first_attempt_success_costs_exactly_one_call(
        self, mock_claude, mock_evt, _sleep
    ):
        """The retry must not add cost to the overwhelmingly common case."""
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._tool_msg([
            {"id": "0", "sentiment": "neutral", "confidence": 0.3,
             "catalyst_type": "other", "already_moved": False,
             "catalyst_magnitude": 1},
        ])
        _batch_score_sentiment(self._articles())
        assert mock_claude.messages.create.call_count == 1
        mock_evt.assert_not_called()

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._claude")
    def test_backoff_stays_inside_the_news_cycle_cadence(self, mock_claude, mock_sleep):
        from news.fetcher import (
            _batch_score_sentiment, _EMPTY_BATCH_ATTEMPTS,
            _EMPTY_BATCH_BACKOFF_SECONDS,
        )
        mock_claude.messages.create.return_value = self._tool_msg([])
        with patch("news.fetcher._record_claude_event"):
            _batch_score_sentiment(self._articles())
        # One backoff BETWEEN attempts, never after the last one.
        assert mock_sleep.call_count == _EMPTY_BATCH_ATTEMPTS - 1
        total = _EMPTY_BATCH_BACKOFF_SECONDS * (_EMPTY_BATCH_ATTEMPTS - 1)
        assert total < 30, "backoff must leave room inside the 60s news cycle"

    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_malformed_response_still_fails_fast_without_retrying(
        self, mock_claude, mock_evt
    ):
        """A missing tool_use block is a PARSING failure, not an empty batch —
        it must return immediately rather than burn the retry budget."""
        from news.fetcher import _batch_score_sentiment
        msg = MagicMock()
        msg.content = []          # no tool_use block at all
        mock_claude.messages.create.return_value = msg
        assert _batch_score_sentiment(self._articles()) == {}
        assert mock_claude.messages.create.call_count == 1
        mock_evt.assert_not_called()


class TestEmptyBatchCooldown:
    """
    v21.13: retries alone do not work on this failure. 2026-08-04 saw 25
    consecutive all-empty cycles with 1 retry; 2026-08-06 saw 58 with 3
    attempts each — 98 minutes, 174 wasted API calls, nothing scored, and it
    overlapped the premarket watchlist build by 38 minutes. Repeated failure
    must stand the classifier DOWN, not retry harder.
    """

    def setup_method(self):
        import news.fetcher as nf
        nf._consecutive_empty_batches = 0
        nf._claude_cooldown = None

    teardown_method = setup_method

    def _tool_msg(self, classifications):
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": classifications}
        msg = MagicMock()
        msg.content = [block]
        return msg

    def _articles(self, n=3):
        return [{"id": str(i), "headline": f"h{i}", "teaser": "t"} for i in range(n)]

    def _good(self):
        return [{"id": "0", "sentiment": "positive", "confidence": 0.8,
                 "catalyst_type": "guidance_raise", "already_moved": False,
                 "catalyst_magnitude": 3}]

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_repeated_empty_cycles_enter_a_cooldown(self, mock_claude, _evt, _sleep):
        import news.fetcher as nf
        mock_claude.messages.create.return_value = self._tool_msg([])
        for _ in range(nf._EMPTY_BATCH_COOLDOWN_TRIGGER):
            nf._batch_score_sentiment(self._articles())
        assert nf._consecutive_empty_batches == nf._EMPTY_BATCH_COOLDOWN_TRIGGER
        assert nf._claude_available() is False

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_cooldown_stops_further_api_calls(self, mock_claude, _evt, _sleep):
        """The whole point: 98 minutes of this cost 174 calls. Once the
        cooldown is on, the next cycle must cost ZERO."""
        import news.fetcher as nf
        mock_claude.messages.create.return_value = self._tool_msg([])
        for _ in range(nf._EMPTY_BATCH_COOLDOWN_TRIGGER):
            nf._batch_score_sentiment(self._articles())
        calls_before = mock_claude.messages.create.call_count
        assert nf._batch_score_sentiment(self._articles()) == {}   # still fail-closed
        assert mock_claude.messages.create.call_count == calls_before

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_one_isolated_empty_cycle_does_not_cool_down(self, mock_claude, _evt, _sleep):
        """A single blip must stay cheap to recover from — the retry exists
        precisely for the isolated case."""
        import news.fetcher as nf
        assert nf._EMPTY_BATCH_COOLDOWN_TRIGGER > 1
        mock_claude.messages.create.return_value = self._tool_msg([])
        nf._batch_score_sentiment(self._articles())
        assert nf._claude_available() is True

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher._claude")
    def test_a_successful_cycle_clears_the_streak(self, mock_claude, _evt, _sleep):
        """Otherwise an earlier bad patch pushes a later isolated blip
        straight into a cooldown it didn't earn."""
        import news.fetcher as nf
        mock_claude.messages.create.return_value = self._tool_msg([])
        nf._batch_score_sentiment(self._articles())
        assert nf._consecutive_empty_batches == 1
        mock_claude.messages.create.return_value = self._tool_msg(self._good())
        nf._batch_score_sentiment(self._articles())
        assert nf._consecutive_empty_batches == 0
        assert nf._claude_available() is True

    def test_retry_budget_is_back_to_one_retry(self):
        """3 attempts never once helped across 83 observed failing cycles."""
        import news.fetcher as nf
        assert nf._EMPTY_BATCH_ATTEMPTS == 2


class TestShadowClassifier:
    """
    v21.14: Qwen runs alongside every Claude batch, Claude alone decides.

    The safety properties are the whole point — a shadow that can delay, break
    or influence a trading decision is worse than no shadow at all. These tests
    exist to keep that true as the module changes.
    """

    def setup_method(self):
        import news.shadow_classifier as sc
        from config.settings import cfg
        sc._pending = 0
        sc._client = None
        sc._unavailable_logged = False

    teardown_method = setup_method

    def _articles(self, n=3):
        return [{"id": str(i), "ticker": f"T{i}", "headline": f"h{i}",
                 "teaser": "t"} for i in range(n)]

    def test_disabled_without_credentials(self):
        """A missing secret must degrade to 'no shadow data', never break."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        with patch.object(cfg, "qwen_api_key", ""), \
             patch.object(cfg, "qwen_base_url", ""):
            assert sc.shadow_enabled() is False
            sc.shadow_score(self._articles(), "msg")      # must not raise

    def test_dispatch_never_blocks_the_caller(self):
        """The news cycle must never wait on the shadow provider."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        started = threading.Event()
        release = threading.Event()

        def _slow(*_a, **_k):
            started.set()
            release.wait(5)

        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"), \
             patch.object(sc, "_run", _slow):
            t0 = time.monotonic()
            sc.shadow_score(self._articles(), "msg")
            elapsed = time.monotonic() - t0
            assert started.wait(5), "job never started"
            assert elapsed < 0.5, f"dispatch blocked for {elapsed:.2f}s"
            release.set()

    def test_backlog_is_dropped_not_queued(self):
        """An unbounded queue would turn a slow provider into a memory leak."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"):
            sc._pending = sc._MAX_PENDING
            with patch.object(sc, "_get_pool") as pool:
                sc.shadow_score(self._articles(), "msg")
                pool.assert_not_called()

    def test_provider_exception_is_recorded_not_raised(self):
        """A shadow failure is DATA (the liveness signal), never an incident."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        client = MagicMock()
        client.chat.completions.create.side_effect = RuntimeError("503")
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run(self._articles(), "msg")       # must not raise
            assert rec.call_count == 1
            assert rec.call_args.kwargs["ok"] is False
            assert rec.call_args.kwargs["error_type"] == "RuntimeError"

    def test_missing_tool_call_recorded_as_failure(self):
        """The same failure shape we hedge against on the Claude side."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[]))]
        resp.usage = None
        client = MagicMock()
        client.chat.completions.create.return_value = resp
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.record_classifier_call") as rec:
            sc._run(self._articles(), "msg")
            assert rec.call_args.kwargs["error_type"] == "no_tool_call"
            assert rec.call_args.kwargs["ok"] is False

    def test_valid_response_is_persisted(self):
        import news.shadow_classifier as sc
        from config.settings import cfg
        good = {"classifications": [
            {"id": "0", "sentiment": "positive", "confidence": 0.8,
             "catalyst_type": "guidance_raise", "already_moved": False,
             "catalyst_magnitude": 3},
        ]}
        call = MagicMock()
        call.function.arguments = json.dumps(good)
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[call]))]
        resp.usage = MagicMock(prompt_tokens=100, completion_tokens=20,
                               prompt_tokens_details=MagicMock(cached_tokens=80))
        client = MagicMock()
        client.chat.completions.create.return_value = resp
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=1) as save, \
             patch("storage.database.record_classifier_call") as rec:
            sc._run(self._articles(), "msg")
            rows = save.call_args.args[0]
            assert len(rows) == 1
            assert rows[0]["catalyst_type"] == "guidance_raise"
            assert rows[0]["ticker"] == "T0"          # joined back to the article
            assert rec.call_args.kwargs["ok"] is True
            assert rec.call_args.kwargs["tokens_cached"] == 80

    def test_invalid_records_are_rejected_not_clamped(self):
        """A model emitting nonsense must SCORE as having emitted nonsense."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        payload = {"classifications": [
            {"id": "0", "sentiment": "bullish", "confidence": 0.8,          # bad enum
             "catalyst_type": "guidance_raise", "already_moved": False},
            {"id": "1", "sentiment": "positive", "confidence": 8.0,         # out of range
             "catalyst_type": "guidance_raise", "already_moved": False},
            {"id": "2", "sentiment": "positive", "confidence": 0.8,
             "catalyst_type": "not_a_catalyst", "already_moved": False},    # bad class
        ]}
        call = MagicMock()
        call.function.arguments = json.dumps(payload)
        resp = MagicMock()
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[call]))]
        resp.usage = None
        client = MagicMock()
        client.chat.completions.create.return_value = resp
        with patch.object(cfg, "qwen_api_key", "k"), \
             patch.object(cfg, "qwen_base_url", "u"), \
             patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=0) as save, \
             patch("storage.database.record_classifier_call"):
            sc._run(self._articles(), "msg")
            assert save.call_args.args[0] == []

    def test_shadow_never_influences_claude_scores(self):
        """The contract: shadow_score returns nothing the pipeline can read."""
        import news.shadow_classifier as sc
        from config.settings import cfg
        assert sc.shadow_score(self._articles(), "msg") is None

    def test_batch_entries_carry_a_ticker_for_attribution(self):
        """
        qwen_scores.ticker is NOT NULL and the batch dicts historically carried
        only id/headline/teaser — so the shadow writer would have stored an
        empty string for every row, silently destroying the ticker dimension of
        the dataset. The prompt does not use this key; attribution does.
        """
        import news.fetcher as nf
        from datetime import datetime as _dt, timezone as _tz
        article = {
            "benzinga_id": "a1",
            "title": "ITT Raises FY2026 Adj EPS Guidance",
            "teaser": "Guidance raised.",
            "published": _dt.now(_tz.utc).isoformat(),
            "tickers": ["ITT"],
        }
        with patch.object(nf, "_batch_score_sentiment", return_value={}) as batch, \
             patch.object(nf, "_fetch", return_value=[article]), \
             patch.object(nf, "_already_scored", return_value=False):
            nf.fetch_all_news(seen_checker=lambda *_a: False)

        assert batch.call_args, "the batch was never built — test setup is stale"
        entries = batch.call_args.args[0]
        assert entries, "no eligible articles reached the classifier"
        for entry in entries:
            assert entry.get("ticker"), f"no ticker on batch entry: {entry}"

    @patch("news.fetcher.shadow_score")
    @patch("news.fetcher._claude")
    def test_shadow_still_runs_during_a_claude_cooldown(self, mock_claude, mock_shadow):
        """
        The most important data point of the whole exercise.

        A Claude cooldown means Claude is FAILING — precisely the scenario a
        fallback exists for. If the shadow were gated behind the cooldown check
        we would never collect a single observation of how Qwen behaves during a
        Claude outage, which is the question this is meant to answer.
        """
        import news.fetcher as nf
        nf._enter_claude_cooldown(120, "simulated outage")
        assert nf._claude_available() is False

        scores = nf._batch_score_sentiment(
            [{"id": "0", "headline": "h", "teaser": "t", "ticker": "AAPL"}]
        )
        assert scores == {}                          # Claude still fails closed
        mock_claude.messages.create.assert_not_called()
        mock_shadow.assert_called_once()             # ...but Qwen was asked

    @patch("news.fetcher.time.sleep")
    @patch("news.fetcher._record_claude_event")
    @patch("news.fetcher.shadow_score", side_effect=RuntimeError("shadow blew up"))
    @patch("news.fetcher._claude")
    def test_shadow_failure_cannot_break_scoring(
        self, mock_claude, _shadow, _evt, _sleep
    ):
        """Even a synchronous explosion in dispatch must not affect Claude."""
        from news.fetcher import _batch_score_sentiment
        block = MagicMock()
        block.type = "tool_use"
        block.input = {"classifications": [
            {"id": "0", "sentiment": "positive", "confidence": 0.9,
             "catalyst_type": "guidance_raise", "already_moved": False,
             "catalyst_magnitude": 3},
        ]}
        msg = MagicMock()
        msg.content = [block]
        mock_claude.messages.create.return_value = msg
        scores = _batch_score_sentiment(
            [{"id": "0", "headline": "h", "teaser": "t"}]
        )
        assert scores["0"]["catalyst_type"] == "guidance_raise"


class TestEntrySlippageInstrumentation:
    """
    v21.13: the signal→fill gap is money lost before the thesis is tested.
    LAMR (2026-08-06): approved $161.09, filled $164.30 (+1.99%) after 34s —
    above the stock's high for the entire session. The stop then sat 2% below
    a price we never chose, and a drift back to the signal price stopped us out
    28 seconds after entry.
    """

    @patch("main.record_system_event")
    def test_large_slippage_warns_and_records_an_event(self, mock_evt):
        import main
        main._record_entry_slippage("LAMR_US_EQ", 161.09, 164.30, 34.0)
        assert mock_evt.call_count == 1
        assert mock_evt.call_args.args[0] == "entry_slippage_high"
        assert "LAMR_US_EQ" in mock_evt.call_args.args[1]

    @patch("main.record_system_event")
    def test_normal_slippage_is_logged_but_not_alerted(self, mock_evt):
        import main
        # ITT the same morning: +0.12%, ordinary spread crossing.
        main._record_entry_slippage("ITT_US_EQ", 224.41, 224.69, 4.0)
        mock_evt.assert_not_called()

    @patch("main.record_system_event")
    def test_favourable_fill_never_alerts(self, mock_evt):
        import main
        main._record_entry_slippage("X_US_EQ", 100.0, 98.0, 3.0)
        mock_evt.assert_not_called()

    @patch("main.record_system_event")
    def test_threshold_is_inclusive_at_the_boundary(self, mock_evt):
        import main
        main._record_entry_slippage("X_US_EQ", 100.0, 101.0, 3.0)   # exactly +1.0%
        assert mock_evt.call_count == 1

    @patch("main.record_system_event")
    def test_garbage_prices_never_raise(self, mock_evt):
        """Observability must never break the entry path."""
        import main
        for sig, fill in [(0, 100.0), (100.0, 0), (None, 100.0), (100.0, None),
                          (-5.0, 100.0), (float("nan"), 100.0)]:
            main._record_entry_slippage("X_US_EQ", sig, fill, 1.0)
        mock_evt.assert_not_called()

    @patch("main.record_system_event", side_effect=RuntimeError("db down"))
    def test_event_failure_is_swallowed(self, _evt):
        import main
        main._record_entry_slippage("X_US_EQ", 100.0, 105.0, 3.0)   # must not raise
