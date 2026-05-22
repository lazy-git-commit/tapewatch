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
        should_exit, reason = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is True
        assert reason == "take_profit"

    @patch("monitor.position_monitor.get_current_price", return_value=97.0)
    def test_stop_loss_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, reason = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is True
        assert reason == "stop_loss"

    @patch("monitor.position_monitor.get_current_price", return_value=101.0)
    def test_time_stop_triggered(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # Trade opened 65 minutes ago — past the 60-minute time stop
        should_exit, reason = check_exit_conditions(self._trade(minutes_ago=65))
        assert should_exit is True
        assert reason == "time_stop"

    @patch("monitor.position_monitor.get_current_price", return_value=101.5)
    def test_no_exit_when_in_range(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        # Price is +1.5% — not yet at take profit (+5%) or stop loss (-2%)
        should_exit, reason = check_exit_conditions(self._trade(buy_price=100.0))
        assert should_exit is False

    @patch("monitor.position_monitor.get_current_price", return_value=None)
    def test_no_exit_when_price_unavailable(self, _mock):
        from monitor.position_monitor import check_exit_conditions
        should_exit, _ = check_exit_conditions(self._trade())
        assert should_exit is False


# ── Sentiment scoring tests ───────────────────────────────────────────────────

class TestSentimentScoring:
    """Tests for news/fetcher.py::_score_sentiment"""

    def _mock_claude_response(self, text: str) -> MagicMock:
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=text)]
        return mock_msg

    @patch("news.fetcher._claude")
    def test_positive_sentiment_parsed(self, mock_claude):
        from news.fetcher import _score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '{"sentiment": "positive", "confidence": 0.9}'
        )
        sentiment, confidence = _score_sentiment("Apple beats earnings", "Record revenue quarter")
        assert sentiment == "positive"
        assert confidence == pytest.approx(0.9)

    @patch("news.fetcher._claude")
    def test_neutral_sentiment_parsed(self, mock_claude):
        from news.fetcher import _score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '{"sentiment": "neutral", "confidence": 0.5}'
        )
        sentiment, confidence = _score_sentiment("Company files routine 10-K", "Annual report")
        assert sentiment == "neutral"

    @patch("news.fetcher._claude")
    def test_malformed_json_returns_neutral(self, mock_claude):
        from news.fetcher import _score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response("not json")
        sentiment, confidence = _score_sentiment("Some headline", "Some teaser")
        assert sentiment == "neutral"
        assert confidence == 0.0

    @patch("news.fetcher._claude")
    def test_markdown_fenced_json_parsed(self, mock_claude):
        from news.fetcher import _score_sentiment
        mock_claude.messages.create.return_value = self._mock_claude_response(
            '```json\n{"sentiment": "positive", "confidence": 0.85}\n```'
        )
        sentiment, confidence = _score_sentiment("Good news", "Positive outlook")
        assert sentiment == "positive"
        assert confidence == pytest.approx(0.85)

    @patch("news.fetcher._claude")
    def test_api_exception_returns_neutral(self, mock_claude):
        from news.fetcher import _score_sentiment
        mock_claude.messages.create.side_effect = Exception("API timeout")
        sentiment, confidence = _score_sentiment("Some headline", "Some teaser")
        assert sentiment == "neutral"
        assert confidence == 0.0


# ── Position sizing tests ─────────────────────────────────────────────────────

class TestPositionSizing:
    """Tests for trading/executor.py::calculate_quantity"""

    @patch("trading.executor.get_portfolio_value", return_value=10000.0)
    @patch("trading.executor.get_available_cash", return_value=10000.0)
    def test_quantity_respects_max_position_pct(self, _c, _p):
        from trading.executor import calculate_quantity
        # 5% of £10,000 = £500. At £100/share = 5 shares
        quantity = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity == pytest.approx(5.0, rel=1e-4)

    @patch("trading.executor.get_portfolio_value", return_value=10000.0)
    @patch("trading.executor.get_available_cash", return_value=200.0)  # less than 5%
    def test_quantity_capped_by_available_cash(self, _c, _p):
        from trading.executor import calculate_quantity
        # 5% of £10,000 = £500, but only £200 cash available → use £200
        quantity = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity == pytest.approx(2.0, rel=1e-4)

    @patch("trading.executor.get_portfolio_value", return_value=None)
    @patch("trading.executor.get_available_cash", return_value=None)
    def test_no_funds_data_returns_none(self, _c, _p):
        from trading.executor import calculate_quantity
        quantity = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None

    @patch("trading.executor.get_portfolio_value", return_value=100.0)
    @patch("trading.executor.get_available_cash", return_value=0.0)
    def test_zero_cash_returns_none(self, _c, _p):
        from trading.executor import calculate_quantity
        quantity = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None
