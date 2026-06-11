"""
tests/test_core.py
───────────────────
Unit tests for the most critical logic:
  - Exit condition evaluation (no external calls)
  - Sentiment scoring (news/fetcher.py)
  - Position sizing (trading/executor.py)

Run with: pytest tests/
"""

import pytest
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

    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_stop_loss_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason, price = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is True
        assert reason == "stop_loss"
        assert price == 97.0

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


# ── Sentiment scoring tests ───────────────────────────────────────────────────

class TestSentimentScoring:
    """Tests for news/fetcher.py::_batch_score_sentiment"""

    def _mock_claude_response(self, text: str) -> MagicMock:
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=text)]
        return mock_msg

    def _article(self, id="1", headline="Earnings beat", teaser="Revenue up"):
        return {"id": id, "headline": headline, "teaser": teaser}

    @patch("news.fetcher._claude")
    def test_positive_sentiment_parsed(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '[{"id": "1", "sentiment": "positive", "confidence": 0.9}]'
        )
        scores = _batch_score_sentiment([self._article()])
        assert scores["1"] == ("positive", pytest.approx(0.9))

    @patch("news.fetcher._claude")
    def test_neutral_sentiment_parsed(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '[{"id": "1", "sentiment": "neutral", "confidence": 0.2}]'
        )
        scores = _batch_score_sentiment([self._article()])
        assert scores["1"][0] == "neutral"

    @patch("news.fetcher._claude")
    def test_malformed_json_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response("not json")
        scores = _batch_score_sentiment([self._article()])
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_markdown_fenced_json_parsed(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '```json\n[{"id": "1", "sentiment": "positive", "confidence": 0.85}]\n```'
        )
        scores = _batch_score_sentiment([self._article()])
        assert scores["1"] == ("positive", pytest.approx(0.85))

    @patch("news.fetcher._claude")
    def test_api_exception_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        mock_claude.messages.create.side_effect = Exception("API timeout")
        scores = _batch_score_sentiment([self._article()])
        assert scores == {}

    @patch("news.fetcher._claude")
    def test_truncated_json_recovered(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        # Simulates a truncated response with only the first of two objects complete
        truncated = '[{"id": "1", "sentiment": "positive", "confidence": 0.9}, {"id": "2"'
        mock_claude.messages.create.return_value = self._mock_claude_response(truncated)
        scores = _batch_score_sentiment([self._article("1"), self._article("2")])
        assert "1" in scores
        assert scores["1"][0] == "positive"

    @patch("news.fetcher._claude")
    def test_empty_articles_returns_empty(self, mock_claude):
        from news.fetcher import _batch_score_sentiment
        scores = _batch_score_sentiment([])
        mock_claude.messages.create.assert_not_called()
        assert scores == {}


# ── Position sizing tests ─────────────────────────────────────────────────────

class TestPositionSizing:
    """Tests for trading/executor.py::calculate_quantity"""

    def _mock_cash(self, total, free):
        return {"total": total, "free": free, "invested": total - free}

    @patch("trading.executor._get")
    def test_quantity_respects_max_position_pct(self, mock_get):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=10000.0)
        # 5% of £10,000 = £500. At £100/share = 5 shares
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert err is None
        assert quantity == pytest.approx(5.0, rel=1e-4)

    @patch("trading.executor._get")
    def test_quantity_capped_by_available_cash(self, mock_get):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=200.0)
        # 5% of £10,000 = £500, but only £200 cash available → use £200
        quantity, err = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert err is None
        assert quantity == pytest.approx(2.0, rel=1e-4)

    @patch("trading.executor._get")
    def test_zero_cash_returns_none(self, mock_get):
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
        # Second call should use quantity rounded to 2 decimal places
        # _post is called as _post(path, payload_dict)
        second_call_payload = mock_post.call_args[0][1]
        second_call_qty = second_call_payload["quantity"]
        # str(165.93) → "165.93" → split on "." → ["165", "93"] → len("93") == 2
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
