"""
tests/test_extended_hours.py
─────────────────────────────
v21 (24/5 extended-hours) test suite:

  - TestTradingSessions        — session classification against the NYSE
                                 calendar (fixed timestamps: normal day,
                                 early close, weekend, overnight boundaries)
  - TestSessionConfigGates     — is_entry_session / is_manage_session vs cfg
  - TestExtendedConfirmation   — confirm_price_signal in the after-hours
                                 regime (anchored analysis, RVOL band
                                 replaced by session-dollar floor, extended
                                 liquidity/spread ceilings, price freshness)
  - TestExtendedOrders         — executor: extendedHours payloads, extended
                                 fill verification, the limit-capability
                                 latch, size factor
  - TestMonitorExtendedExits   — after-hours flatten, overnight guard,
                                 polled-only ratchet in extended sessions

All timestamps are explicit UTC pd.Timestamps — nothing here depends on the
wall clock of the machine running the tests.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pandas as pd

from config.settings import cfg
from market.sessions import (
    get_trading_session, session_bounds, minutes_until_session_end,
    is_entry_session, is_manage_session,
    REGULAR, PREMARKET, AFTERHOURS, OVERNIGHT, CLOSED,
)
from market.twelvedata_bars import SessionAnalysis
from trading.executor import OrderResult


# ── Session classification ────────────────────────────────────────────────────

class TestTradingSessions(unittest.TestCase):
    """2026-07-15 is a normal Wednesday session (EDT): RTH 13:30–20:00 UTC,
    premarket 08:00–13:30 UTC, afterhours 20:00–00:00 UTC (next day)."""

    def _at(self, ts: str) -> str:
        return get_trading_session(pd.Timestamp(ts, tz="UTC"))

    def test_regular_hours(self):
        self.assertEqual(self._at("2026-07-15 14:00"), REGULAR)

    def test_premarket(self):
        self.assertEqual(self._at("2026-07-15 09:00"), PREMARKET)   # 05:00 ET
        self.assertEqual(self._at("2026-07-15 13:29"), PREMARKET)

    def test_open_boundary_is_regular(self):
        self.assertEqual(self._at("2026-07-15 13:30"), REGULAR)

    def test_afterhours(self):
        self.assertEqual(self._at("2026-07-15 20:00"), AFTERHOURS)  # 16:00 ET
        self.assertEqual(self._at("2026-07-15 23:59"), AFTERHOURS)  # 19:59 ET

    def test_overnight_after_post_session(self):
        # 20:30 ET Wednesday = 00:30 UTC Thursday; Thursday trades → overnight.
        self.assertEqual(self._at("2026-07-16 00:30"), OVERNIGHT)

    def test_overnight_early_morning(self):
        # 02:00 ET on a trading day (before the 04:00 premarket start).
        self.assertEqual(self._at("2026-07-15 06:00"), OVERNIGHT)

    def test_friday_night_is_closed(self):
        # 20:30 ET Friday 2026-07-17 = 00:30 UTC Saturday → weekend, closed.
        self.assertEqual(self._at("2026-07-18 00:30"), CLOSED)

    def test_weekend_closed(self):
        self.assertEqual(self._at("2026-07-18 15:00"), CLOSED)

    def test_early_close_shifts_afterhours(self):
        # Fri 2026-11-27 (day after Thanksgiving): 13:00 ET close (18:00 UTC).
        # After-hours runs close → close+4h, i.e. 13:00–17:00 ET.
        self.assertEqual(self._at("2026-11-27 17:00"), REGULAR)      # 12:00 ET
        self.assertEqual(self._at("2026-11-27 19:00"), AFTERHOURS)   # 14:00 ET
        # 17:30 ET is past the shifted post-session; Saturday follows → closed.
        self.assertEqual(self._at("2026-11-27 22:30"), CLOSED)

    def test_session_bounds_afterhours(self):
        now = pd.Timestamp("2026-07-15 21:00", tz="UTC")
        start, end = session_bounds(AFTERHOURS, now)
        self.assertEqual(start, datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc))

    def test_minutes_until_session_end(self):
        now = pd.Timestamp("2026-07-15 23:30", tz="UTC")  # 19:30 ET
        mins = minutes_until_session_end(AFTERHOURS, now)
        self.assertAlmostEqual(mins, 30.0, delta=0.01)

    def test_minutes_until_session_end_outside_session(self):
        now = pd.Timestamp("2026-07-15 14:00", tz="UTC")  # RTH
        self.assertIsNone(minutes_until_session_end(AFTERHOURS, now))


# ── Config-driven session gates ───────────────────────────────────────────────

class TestSessionConfigGates(unittest.TestCase):
    def test_regular_always_tradeable(self):
        self.assertTrue(is_entry_session(REGULAR))
        self.assertTrue(is_manage_session(REGULAR))

    def test_overnight_never_tradeable(self):
        self.assertFalse(is_entry_session(OVERNIGHT))
        self.assertFalse(is_manage_session(OVERNIGHT))
        self.assertFalse(is_entry_session(CLOSED))

    def test_afterhours_follows_toggles(self):
        with patch.object(cfg, "extended_hours_enabled", True), \
             patch.object(cfg, "afterhours_trading_enabled", True):
            self.assertTrue(is_entry_session(AFTERHOURS))
        with patch.object(cfg, "extended_hours_enabled", True), \
             patch.object(cfg, "afterhours_trading_enabled", False):
            self.assertFalse(is_entry_session(AFTERHOURS))
        with patch.object(cfg, "extended_hours_enabled", False):
            self.assertFalse(is_entry_session(AFTERHOURS))

    def test_premarket_default_off(self):
        with patch.object(cfg, "extended_hours_enabled", True), \
             patch.object(cfg, "premarket_trading_enabled", False):
            self.assertFalse(is_entry_session(PREMARKET))

    def test_manage_broader_than_entry(self):
        # Entry toggle off mid-session must NOT orphan an open position.
        with patch.object(cfg, "extended_hours_enabled", True), \
             patch.object(cfg, "afterhours_trading_enabled", False):
            self.assertFalse(is_entry_session(AFTERHOURS))
            self.assertTrue(is_manage_session(AFTERHOURS))


# ── Extended-session price confirmation ───────────────────────────────────────

def _afterhours_env(now_utc):
    """(quote, session_analysis, daily, bounds) — a healthy after-hours mover."""
    quote = {"c": 100.0, "o": 99.0, "pc": 98.0, "t": now_utc.timestamp()}
    sa = SessionAnalysis(
        past_price=99.5, current_bar_price=100.0, spread_proxy_pct=0.5,
        session_volume=20_000, vwap=99.8, last_price=100.0,
        session_low=99.0, session_high=100.5,
        newest_bar_utc=now_utc - timedelta(seconds=30),
    )
    daily = (1_000_000, 80_000_000.0, 98.0)
    bounds = (now_utc - timedelta(minutes=30), now_utc + timedelta(hours=3, minutes=30))
    return quote, sa, daily, bounds


class TestExtendedConfirmation(unittest.TestCase):
    def _confirm(self, quote, sa, daily, bounds, session=AFTERHOURS):
        import market.price_check as pc
        with patch.object(pc, "get_trading_session", return_value=session), \
             patch.object(pc, "session_bounds", return_value=bounds), \
             patch.object(pc, "get_quote_with_fallback", return_value=quote), \
             patch.object(pc, "get_session_analysis", return_value=sa) as mock_sa, \
             patch.object(pc, "get_daily_stats", return_value=daily):
            conf = pc.confirm_price_signal("ACME_US_EQ")
        return conf, mock_sa

    def test_afterhours_approved(self):
        now = datetime.now(timezone.utc)
        conf, mock_sa = self._confirm(*_afterhours_env(now))
        self.assertIsNotNone(conf)
        self.assertTrue(conf.is_confirmed)
        self.assertEqual(conf.session, AFTERHOURS)
        # Anchored, extended bars were requested.
        _, kwargs = mock_sa.call_args
        self.assertTrue(kwargs["include_extended"])
        self.assertIsNotNone(kwargs["anchor_utc"])

    def test_rvol_band_not_applied_after_hours(self):
        # 20k shares vs 1M ADV at "30 min into the session" is an RVOL that
        # would fail the RTH floor outright — after hours it must not matter.
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertTrue(conf.is_confirmed)

    def test_session_dollar_floor(self):
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        sa.session_volume = 2_000  # $200k printed < $500k floor
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertFalse(conf.is_confirmed)
        self.assertEqual(conf.reason_code, "low_volume")  # transient → re-eval

    def test_extended_liquidity_floor(self):
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        daily = (1_000_000, 30_000_000.0, 98.0)  # fine for RTH ($5M), not extended ($50M)
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertFalse(conf.is_confirmed)
        self.assertEqual(conf.reason_code, "illiquid")

    def test_extended_spread_ceiling(self):
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        sa.spread_proxy_pct = 2.0  # passes RTH (3.0), fails extended (1.5)
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertFalse(conf.is_confirmed)
        self.assertEqual(conf.reason_code, "wide_spread")

    def test_bar_price_fresher_than_quote(self):
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        quote["t"] = (now - timedelta(minutes=10)).timestamp()  # stale-ish quote
        sa.last_price = 100.5
        sa.current_bar_price = 100.5
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertEqual(conf.current_price, 100.5)

    def test_no_bars_defers(self):
        now = datetime.now(timezone.utc)
        quote, _, daily, bounds = _afterhours_env(now)
        conf, _ = self._confirm(quote, None, daily, bounds)
        self.assertIsNone(conf)  # defer, never confirm on a bare quote

    def test_no_momentum_baseline_defers_no_open_fallback(self):
        # RTH falls back to the official open as a baseline; extended sessions
        # have no auction print to fall back to — they must defer.
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        sa.past_price = None
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertIsNone(conf)

    def test_session_start_block(self):
        now = datetime.now(timezone.utc)
        quote, sa, daily, bounds = _afterhours_env(now)
        bounds = (now - timedelta(minutes=2), now + timedelta(hours=4))  # 16:02
        conf, _ = self._confirm(quote, sa, daily, bounds)
        self.assertFalse(conf.is_confirmed)
        self.assertEqual(conf.reason_code, "opening_block")


# ── Executor: extended-hours order handling ───────────────────────────────────

_FILL = {"price": 100.0, "walletImpact": {"netValue": 80.0, "fxRate": 1.25, "taxes": []}}


class TestExtendedOrders(unittest.TestCase):
    def setUp(self):
        import trading.executor as ex
        ex._extended_limit_supported = None  # reset the capability latch

    def test_buy_extended_sets_flag_and_half_size(self):
        import trading.executor as ex
        with patch.object(ex, "calculate_quantity", return_value=(1.0, None)) as mock_calc, \
             patch.object(ex, "_post", return_value={"id": "o1"}) as mock_post, \
             patch.object(ex, "_fetch_fill", return_value=_FILL):
            result = ex.buy("AAPL_US_EQ", 100.0, 1e9, extended=True)
        self.assertTrue(result.success)
        self.assertTrue(mock_post.call_args[0][1]["extendedHours"])
        # Sizing was scaled by the extended factor.
        self.assertEqual(mock_calc.call_args[0][3], cfg.extended_size_factor)

    def test_buy_rth_has_no_flag(self):
        import trading.executor as ex
        with patch.object(ex, "calculate_quantity", return_value=(1.0, None)), \
             patch.object(ex, "_post", return_value={"id": "o1"}) as mock_post, \
             patch.object(ex, "_fetch_fill", return_value=_FILL):
            result = ex.buy("AAPL_US_EQ", 100.0, 1e9)
        self.assertTrue(result.success)
        self.assertNotIn("extendedHours", mock_post.call_args[0][1])

    def test_buy_extended_unfilled_is_cancelled(self):
        # A queued extended-hours buy (not 24/5-eligible / uncrossable book)
        # must be cancelled and reported as a failure — never left to fill
        # blind at the next open.
        import trading.executor as ex
        with patch.object(ex, "calculate_quantity", return_value=(1.0, None)), \
             patch.object(ex, "_post", return_value={"id": "o1"}), \
             patch.object(ex, "_fetch_fill", return_value=None), \
             patch.object(ex, "get_order_status", return_value="NEW"), \
             patch.object(ex, "cancel_order", return_value=True) as mock_cancel:
            result = ex.buy("AAPL_US_EQ", 100.0, 1e9, extended=True)
        self.assertFalse(result.success)
        self.assertIn("cancelled", result.error)
        mock_cancel.assert_called_once_with("o1")

    def test_sell_extended_latches_limit_rejection(self):
        import trading.executor as ex

        def post_side_effect(path, payload):
            if "limit" in path:
                raise Exception("HTTP 400 - {\"errorMessage\":\"Invalid payload\"}")
            return {"id": "m1"}

        with patch.object(ex, "_post", side_effect=post_side_effect) as mock_post, \
             patch.object(ex, "get_order_status", return_value="FILLED"), \
             patch.object(ex, "_fetch_fill", return_value=_FILL), \
             patch.object(ex.time, "sleep"):
            result = ex.sell("AAPL_US_EQ", 1.0, 100.0, "take_profit", extended=True)
        self.assertTrue(result.success)
        self.assertIs(ex._extended_limit_supported, False)
        # Market fallback carried the extended flag.
        market_call = mock_post.call_args_list[-1]
        self.assertIn("market", market_call[0][0])
        self.assertTrue(market_call[0][1]["extendedHours"])

        # Second extended sell must skip the dead limit attempt entirely.
        with patch.object(ex, "_post", return_value={"id": "m2"}) as mock_post2, \
             patch.object(ex, "get_order_status", return_value="FILLED"), \
             patch.object(ex, "_fetch_fill", return_value=_FILL), \
             patch.object(ex.time, "sleep"):
            result2 = ex.sell("AAPL_US_EQ", 1.0, 100.0, "take_profit", extended=True)
        self.assertTrue(result2.success)
        self.assertEqual(len(mock_post2.call_args_list), 1)
        self.assertIn("market", mock_post2.call_args[0][0])

    def test_sell_extended_market_verifies_fill(self):
        # Extended market sells must NOT assume a fill: unfilled → cancel →
        # failure (the monitor retries next cycle).
        import trading.executor as ex
        ex._extended_limit_supported = False
        with patch.object(ex, "_post", return_value={"id": "m1"}), \
             patch.object(ex, "get_order_status", return_value="NEW"), \
             patch.object(ex, "cancel_order", return_value=True), \
             patch.object(ex.time, "sleep"):
            result = ex.sell("AAPL_US_EQ", 1.0, 100.0, "stop_loss", extended=True)
        self.assertFalse(result.success)

    def test_rth_market_sell_still_assumes_fill(self):
        import trading.executor as ex
        with patch.object(ex, "_post", return_value={"id": "m1"}), \
             patch.object(ex, "_fetch_fill", return_value=_FILL), \
             patch.object(ex, "get_order_status") as mock_status:
            result = ex.sell("AAPL_US_EQ", 1.0, 100.0, "eod_flatten", force_market=True)
        self.assertTrue(result.success)
        mock_status.assert_not_called()

    def test_calculate_quantity_size_factor(self):
        import trading.executor as ex
        cash = {"total": 10_000.0, "free": 10_000.0}
        with patch.object(ex, "_get", return_value=cash), \
             patch.object(ex, "get_gbp_usd_rate", return_value=1.25):
            full, _ = ex.calculate_quantity("AAPL_US_EQ", 100.0)
            half, _ = ex.calculate_quantity("AAPL_US_EQ", 100.0, size_factor=0.5)
        self.assertAlmostEqual(half, full / 2, places=4)


# ── Monitor: extended-session exits ───────────────────────────────────────────

def _open_trade(**over) -> dict:
    trade = {
        "id": 1, "ticker": "AAPL_US_EQ", "quantity": 1.0, "buy_price": 99.0,
        "buy_time": datetime.now(timezone.utc).isoformat(),
        "stop_order_id": None, "tp_order_id": None, "ratchet_armed": 0,
    }
    trade.update(over)
    return trade


class TestMonitorExtendedExits(unittest.TestCase):
    def _run_monitor(self, session, mins_left, price=100.0, trade=None):
        import monitor.position_monitor as mon
        sell_mock = MagicMock(return_value=OrderResult(
            success=True, ticker="AAPL_US_EQ", quantity=1.0, price=price,
            order_id="s1", error=None,
        ))
        with patch.object(mon, "touch_heartbeat"), \
             patch.object(mon, "get_open_trades", return_value=[trade or _open_trade()]), \
             patch.object(mon, "get_broker_positions", return_value=None), \
             patch.object(mon, "get_trading_session", return_value=session), \
             patch.object(mon, "minutes_until_session_end", return_value=mins_left), \
             patch.object(mon, "get_current_price", return_value=price), \
             patch.object(mon, "sell", sell_mock), \
             patch.object(mon, "close_trade") as close_mock, \
             patch.object(mon, "place_stop_loss") as stop_mock, \
             patch.object(mon, "set_ratchet_armed"):
            mon.monitor_positions()
        return sell_mock, close_mock, stop_mock

    def test_afterhours_flatten_before_overnight(self):
        sell_mock, close_mock, _ = self._run_monitor(AFTERHOURS, mins_left=10.0)
        sell_mock.assert_called_once()
        _, kwargs = sell_mock.call_args
        self.assertTrue(kwargs["force_market"])
        self.assertTrue(kwargs["extended"])
        self.assertEqual(sell_mock.call_args[0][3], "afterhours_flatten")
        close_mock.assert_called_once()

    def test_afterhours_holding_no_flatten(self):
        sell_mock, _, _ = self._run_monitor(AFTERHOURS, mins_left=120.0)
        sell_mock.assert_not_called()

    def test_overnight_never_sells(self):
        sell_mock, _, _ = self._run_monitor(OVERNIGHT, mins_left=None)
        sell_mock.assert_not_called()

    def test_extended_ratchet_is_polled_only(self):
        # +2.5% above buy → ratchet trigger; in an extended session it must
        # arm the POLLED breakeven without placing a resting stop order.
        sell_mock, _, stop_mock = self._run_monitor(
            AFTERHOURS, mins_left=120.0, price=101.5,
        )
        sell_mock.assert_not_called()
        stop_mock.assert_not_called()

    def test_regular_session_unchanged(self):
        import monitor.position_monitor as mon
        with patch.object(mon, "minutes_until_close", return_value=200.0):
            sell_mock, _, _ = self._run_monitor(REGULAR, mins_left=None)
        sell_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
