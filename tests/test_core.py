"""
tests/test_core.py
───────────────────
Unit tests for the most critical logic:
  - Exit condition evaluation (no external calls)
  - Sentiment result parsing
  - Position sizing

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


# ── Sentiment parsing tests ───────────────────────────────────────────────────

class TestSentimentParsing:
    """Tests for analysis/sentiment.py — focuses on JSON parsing robustness"""

    def _make_news_item(self):
        from news.fetcher import NewsItem
        from datetime import datetime, timezone
        return NewsItem(
            article_id="test-001",
            ticker="AAPL_US_EQ",
            headline="Apple reports record quarterly earnings",
            body="Apple beat analyst estimates by 20% with record services revenue.",
            source="test",
            published_at=datetime.now(timezone.utc),
            is_wiim=False,
        )

    @patch("analysis.sentiment._get_client")
    def test_bullish_high_confidence_is_actionable(self, mock_get_client):
        from analysis.sentiment import analyse

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"sentiment": "BULLISH", "confidence": 9, "reason": "Earnings beat"}')
        ]
        mock_get_client.return_value.messages.create.return_value = mock_response

        result = analyse(self._make_news_item())

        assert result is not None
        assert result.sentiment == "BULLISH"
        assert result.confidence == 9
        assert result.is_actionable is True

    @patch("analysis.sentiment._get_client")
    def test_bullish_low_confidence_not_actionable(self, mock_get_client):
        from analysis.sentiment import analyse

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"sentiment": "BULLISH", "confidence": 5, "reason": "Vague positive"}')
        ]
        mock_get_client.return_value.messages.create.return_value = mock_response

        result = analyse(self._make_news_item())
        assert result is not None
        assert result.is_actionable is False  # confidence 5 < threshold 7

    @patch("analysis.sentiment._get_client")
    def test_neutral_not_actionable(self, mock_get_client):
        from analysis.sentiment import analyse

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"sentiment": "NEUTRAL", "confidence": 3, "reason": "No catalyst"}')
        ]
        mock_get_client.return_value.messages.create.return_value = mock_response

        result = analyse(self._make_news_item())
        assert result is not None
        assert result.is_actionable is False

    @patch("analysis.sentiment._get_client")
    def test_malformed_json_returns_none(self, mock_get_client):
        from analysis.sentiment import analyse

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="This is not JSON at all")]
        mock_get_client.return_value.messages.create.return_value = mock_response

        result = analyse(self._make_news_item())
        assert result is None


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

    @patch("trading.executor.get_portfolio_value", return_value=1000.0)
    @patch("trading.executor.get_available_cash", return_value=10.0)
    def test_insufficient_funds_returns_none(self, _c, _p):
        from trading.executor import calculate_quantity
        # Price £100, max spend £50 (5% of £1000 capped at £10 cash) < price
        quantity = calculate_quantity("AAPL_US_EQ", price=100.0)
        assert quantity is None
