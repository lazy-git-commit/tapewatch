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

    def _trade(self, trade_id, ticker, qty=10.0):
        return {"id": trade_id, "ticker": ticker, "quantity": qty,
                "buy_price": 100.0, "buy_time": "2026-06-17T13:00:00+00:00",
                "tp_order_id": None, "mode": "demo"}

    @patch("monitor.position_monitor.get_broker_positions")
    def test_phantom_position_logged(self, mock_broker, caplog):
        """DB-open trade not present in broker portfolio → CRITICAL log."""
        import logging
        mock_broker.return_value = {}  # broker has nothing
        from monitor.position_monitor import _reconcile_positions
        with caplog.at_level(logging.CRITICAL, logger="monitor.position_monitor"):
            _reconcile_positions([self._trade(42, "AAPL_US_EQ")])
        assert any("RECONCILIATION" in r.message and "AAPL_US_EQ" in r.message
                   and "OPEN in DB but NOT in broker" in r.message
                   for r in caplog.records)

    @patch("monitor.position_monitor.get_broker_positions")
    def test_orphan_position_logged(self, mock_broker, caplog):
        """Broker holds position not in DB → CRITICAL log."""
        import logging
        mock_broker.return_value = {"TSLA_US_EQ": 5.0}  # broker has it, DB doesn't
        from monitor.position_monitor import _reconcile_positions
        with caplog.at_level(logging.CRITICAL, logger="monitor.position_monitor"):
            _reconcile_positions([])  # no DB trades
        assert any("RECONCILIATION" in r.message and "TSLA_US_EQ" in r.message
                   and "broker holds" in r.message
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

    @patch("trading.executor._get")
    def test_api_failure_returns_none(self, mock_get):
        from trading.executor import calculate_quantity
        mock_get.side_effect = Exception("HTTP 401")
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None
        assert err is not None


# ── Precision retry tests ─────────────────────────────────────────────────────

class TestBuyPrecisionRetry:
    """Tests for trading/executor.py::buy — precision mismatch auto-retry"""

    def _mock_cash(self, total=5000.0, free=5000.0):
        return {"total": total, "free": free, "invested": 0.0}

    def _precision_error(self, allowed: int) -> Exception:
        body = (
            f'{{"type":"/api-errors/quantity-precision-mismatch",'
            f'"title":"Error while placing the order",'
            f'"status":400,'
            f'"detail":"invalid quantity precision {allowed}",'
            f'"traceId":"abc"}}'
        )
        return Exception(f"HTTP 400 - {body}")

    @patch("trading.executor._fetch_fill", return_value=None)
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

    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_non_precision_error_does_not_retry(self, mock_get, mock_post):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        mock_post.side_effect = Exception("HTTP 500 - Internal server error")
        result = buy("AAPL_US_EQ", price=100.0)
        assert result.success is False
        assert mock_post.call_count == 1

    @patch("trading.executor._post")
    @patch("trading.executor._get")
    def test_precision_retry_still_fails(self, mock_get, mock_post):
        from trading.executor import buy
        mock_get.return_value = self._mock_cash()
        # Both attempts fail
        mock_post.side_effect = [self._precision_error(2), Exception("HTTP 500")]
        result = buy("BCDA_US_EQ", price=1.51)
        assert result.success is False
        assert mock_post.call_count == 2


class TestSellExecution:
    """Tests for trading/executor.py::sell execution policy"""

    @patch("trading.executor._fetch_fill", return_value=None)
    @patch("trading.executor._post", return_value={"id": "eod-1"})
    def test_eod_flatten_uses_market_order(self, mock_post, _mock_fill):
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="eod_flatten")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/market"

    @patch("trading.executor.time.sleep")
    @patch("trading.executor._fetch_fill", return_value=None)
    @patch("trading.executor.get_order_status", return_value="FILLED")
    @patch("trading.executor._post", return_value={"id": "sl-1"})
    def test_stop_loss_uses_limit_order(self, mock_post, _mock_status, _mock_fill, _mock_sleep):
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="stop_loss")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/limit"

    @patch("trading.executor._fetch_fill", return_value=None)
    @patch("trading.executor._post", return_value={"id": "em-1"})
    def test_emergency_flatten_uses_market_order(self, mock_post, _mock_fill):
        # An unrecorded buy must exit at market — a limit that fails to fill
        # leaves an invisible unmanaged position with no stop or EOD logic.
        from trading.executor import sell
        result = sell("AAPL_US_EQ", quantity=1.0, price=100.0, reason="eod_flatten")
        assert result.success is True
        assert mock_post.call_args[0][0] == "/equity/orders/market"


class TestGoneTpOrderResolution:
    """Tests for monitor/position_monitor.py::_handle_gone_tp_order

    A TP order that 404s on the pending endpoint is NOT automatically a fill.
    DAY orders expire at close; treating expiry as profit corrupts P&L and
    leaves the real position unmanaged with no stop or EOD flatten.
    """

    def _trade(self, buy_price=100.0):
        buy_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        return {
            "id": 42,
            "ticker": "TEST_US_EQ",
            "buy_price": buy_price,
            "quantity": 10.0,
            "buy_time": buy_time,
            "tp_order_id": "tp-order-99",
            "mode": "demo",
        }

    @patch("monitor.position_monitor.set_tp_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill")
    def test_gone_with_fill_closes_trade_as_tp(self, mock_fetch, mock_close, mock_set_tp):
        """GONE + fill detail → trade closed as take_profit, returns True."""
        mock_fetch.return_value = {
            "price": "105.00",
            "walletImpact": {"netValue": "52.30", "fxRate": "1.25", "taxes": []},
        }
        from monitor.position_monitor import _handle_gone_tp_order
        trade = self._trade()
        result = _handle_gone_tp_order(trade, "tp-order-99")
        assert result is True
        mock_close.assert_called_once()
        mock_set_tp.assert_not_called()

    @patch("monitor.position_monitor.set_tp_order_id")
    @patch("monitor.position_monitor.close_trade")
    @patch("monitor.position_monitor._fetch_fill", return_value=None)
    def test_gone_without_fill_reverts_to_polled_exits(self, _mock_fetch, mock_close, mock_set_tp):
        """GONE + no fill detail → stale TP id cleared, trade stays open, returns False."""
        from monitor.position_monitor import _handle_gone_tp_order
        trade = self._trade()
        result = _handle_gone_tp_order(trade, "tp-order-99")
        assert result is False
        mock_close.assert_not_called()
        mock_set_tp.assert_called_once_with(42, None)
        assert trade["tp_order_id"] is None


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
        from market.price_check import compute_rvol
        # 25% of ADV traded by 10:30 is a NORMAL day (RVOL ≈ 1), not "low volume".
        # The old full-day ratio would have called this 0.25× and rejected it.
        rvol = compute_rvol(250_000, 1_000_000, 60)
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


# ── VWAP computation tests (v15) ──────────────────────────────────────────────

class TestVwap:
    """Tests for market/twelvedata_bars.py::get_session_vwap"""

    def _bar(self, dt, h, l, c, v):
        return {"datetime": dt, "high": str(h), "low": str(l), "close": str(c), "volume": str(v)}

    def _today_bars(self):
        """Two same-day ET bars (newest first), so the session filter keeps both."""
        from datetime import datetime
        import pytz
        now_et = datetime.now(pytz.timezone("America/New_York"))
        d = now_et.strftime("%Y-%m-%d")
        # Heavy volume (900) at price 10, light (100) at 20.
        # Use times safely in the past relative to "now" so they're today.
        return [
            self._bar(f"{d} 09:31:00", 20, 20, 20, 100),  # newest first
            self._bar(f"{d} 09:30:00", 10, 10, 10, 900),
        ]

    @patch("market.twelvedata_bars._get_time_series")
    def test_vwap_weighted_by_volume(self, mock_ts):
        import market.twelvedata_bars as td
        mock_ts.return_value = self._today_bars()
        vwap, last = td.get_session_vwap("AAPL")
        # typical prices 20 and 10; volume-weighted (20*100 + 10*900)/1000 = 11.0
        assert vwap == pytest.approx(11.0, rel=1e-6)
        assert last == 20.0  # last_price = most recent bar's close

    @patch("market.twelvedata_bars._get_time_series")
    def test_vwap_none_when_no_data(self, mock_ts):
        import market.twelvedata_bars as td
        mock_ts.return_value = None
        vwap, last = td.get_session_vwap("AAPL")
        assert vwap is None and last is None


class TestTwelvedataVolumeStats:
    """Tests for market/twelvedata_bars.py::get_volume_stats"""

    def _daily_bar(self, dt, close=10, volume=1000):
        return {"datetime": dt, "close": str(close), "volume": str(volume)}

    @patch("market.twelvedata_bars._get_time_series")
    def test_daily_bar_must_be_today_for_rvol(self, mock_ts):
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        before = (now_et - timedelta(days=2)).strftime("%Y-%m-%d")
        mock_ts.return_value = [
            self._daily_bar(yesterday, close=10, volume=5000),
            self._daily_bar(before, close=9, volume=1000),
        ]
        assert td.get_volume_stats("AAPL") == (None, None, None, None)

    @patch("market.twelvedata_bars._get_time_series")
    def test_date_only_daily_bar_parses(self, mock_ts):
        import pytz
        import market.twelvedata_bars as td
        now_et = datetime.now(pytz.timezone("America/New_York"))
        today = now_et.strftime("%Y-%m-%d")
        yesterday = (now_et - timedelta(days=1)).strftime("%Y-%m-%d")
        mock_ts.return_value = [
            self._daily_bar(today, close=11, volume=500),
            self._daily_bar(yesterday, close=10, volume=1000),
        ]
        today_vol, avg_vol, adv_dollars, prev_close = td.get_volume_stats("AAPL")
        assert today_vol == 500
        assert avg_vol == 1000
        assert adv_dollars == pytest.approx(10_000)
        assert prev_close == pytest.approx(10)


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
        approved = evaluate_premarket_candidates()
        elapsed = _t.monotonic() - t0

        # 6 confirms at 0.3s each = 1.8s serial; in parallel (pool of 8) the wall
        # time is ~one call. <1.0s proves they ran concurrently, not summed.
        assert elapsed < 1.0
        assert len(approved) == 6  # all confirmed

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
        approved = evaluate_premarket_candidates()
        # T0 resolves and is approved; T1 blows the budget → NOT given a verdict
        # (no status write) so it stays pending for the next cycle.
        approved_ids = {c["id"] for c, _ in approved}
        assert 0 in approved_ids
        assert 1 not in approved_ids
        # T1 must NOT have been written to any terminal status this cycle.
        written_ids = {call.args[0] for call in mock_upd.call_args_list}
        assert 1 not in written_ids


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
    def test_low_momentum_rejected_terminally(self, mock_upd):
        from premarket.scanner import _apply_confirmation
        conf = _mk_conf(day_change_pct=5.0, is_confirmed=False,
                        reason_code="low_momentum")
        assert _apply_confirmation({"id": 1, "ticker": "A"}, conf) is None
        assert mock_upd.call_args.args[1] == "rejected"

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
