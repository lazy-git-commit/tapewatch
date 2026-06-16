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


# ── Position sizing tests ─────────────────────────────────────────────────────

class TestPositionSizing:
    """Tests for trading/executor.py::calculate_quantity"""

    def _mock_cash(self, total, free):
        return {"total": total, "free": free, "invested": total - free}

    @patch("trading.executor._get")
    def test_quantity_respects_max_position_pct(self, mock_get):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=10000.0)
        # Hard cap binds: 5% of £10,000 = £500 (risk cap is 0.25%/2% = £1,250).
        # At £100/share = 5 shares
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
    def test_quantity_capped_by_adv_participation(self, mock_get):
        from trading.executor import calculate_quantity
        mock_get.return_value = self._mock_cash(total=10000.0, free=10000.0)
        # ADV participation cap: 0.5% of $20,000 ADV = £100 — binds below the
        # £500 hard cap. This is what keeps exits from moving thin books.
        quantity, err = calculate_quantity("THIN_US_EQ", price=100.0, avg_dollar_volume=20_000)
        assert err is None
        assert quantity == pytest.approx(1.0, rel=1e-4)

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
