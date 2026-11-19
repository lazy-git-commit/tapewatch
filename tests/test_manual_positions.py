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
v22 — the live-money lock and trailing-stop management of hand-opened trades.

This is the first code in the project that acts on a REAL-MONEY account, and
an ISA that also holds investments the system knows nothing about. The tests
are therefore organised around the safety properties rather than the features:

  * the automated strategy can never buy in the live account by accident —
    refused at startup, in the risk gates, in buy(), and at the HTTP layer;
  * the manual manager can never buy at all, and its account choice can never
    leak into a strategy job sharing the same worker thread;
  * only listed tickers are ever touched — other holdings are invisible;
  * the stop only moves UP, moves in paced steps, ignores a one-reading spike,
    and never fights an order a human placed;
  * when the manager cannot tell what happened (broker down, unknown order
    state), it does nothing rather than guess.
"""

import base64
import struct
import threading
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Fakes ─────────────────────────────────────────────────────────────────────

class _Clock:
    """Stands in for the manager's `time` module: monotonic() + sleep()."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return 1.7e9 + self.t  # wall clock, moving with the test clock

    def sleep(self, _seconds):
        pass  # never wait in a test


def _real(x):
    """Round-trip through a 32-bit float, exactly as a PostgreSQL REAL column does."""
    return struct.unpack("f", struct.pack("f", x))[0]


class _FakeDB:
    """The manual-trade slice of storage.database, in memory. Mirrors the real
    SQL semantics that matter — raise_manual_trail's widen-only GREATEST(), and
    quantity stored as a 32-bit REAL (which can read back MORE than was held)."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.events: list[tuple[str, str]] = []
        self._next = 100

    def open_manual_trade(self, ticker, quantity, buy_price, mode, buy_time,
                          trail_high_price, trail_stop_price):
        tid = self._next
        self._next += 1
        self.rows[tid] = dict(
            id=tid, ticker=ticker, quantity=_real(quantity), buy_price=buy_price,
            mode=mode, buy_time=buy_time, trail_high_price=trail_high_price,
            trail_stop_price=trail_stop_price, stop_order_id=None,
            status="open", origin="manual", exit_reason=None, sell_price=None,
        )
        return tid

    def get_open_manual_trades(self, mode):
        return [dict(r) for r in self.rows.values()
                if r["status"] == "open" and r["mode"] == mode]

    def raise_manual_trail(self, trade_id, high=None, stop=None):
        r = self.rows[trade_id]
        if high is not None:
            r["trail_high_price"] = max(r["trail_high_price"] or high, high)
        if stop is not None:
            r["trail_stop_price"] = max(r["trail_stop_price"] or stop, stop)

    def update_manual_position(self, trade_id, quantity, buy_price):
        self.rows[trade_id].update(quantity=_real(quantity), buy_price=buy_price)

    def set_stop_order_id(self, trade_id, order_id):
        self.rows[trade_id]["stop_order_id"] = order_id

    def close_trade(self, trade_id, sell_price, exit_reason, **kw):
        self.rows[trade_id].update(status="closed", sell_price=sell_price,
                                   exit_reason=exit_reason, **kw)

    def record_system_event(self, event_type, detail=""):
        self.events.append((event_type, detail))

    # helpers
    def only(self):
        assert len(self.rows) == 1, self.rows
        return next(iter(self.rows.values()))

    def event_types(self):
        return [e for e, _ in self.events]


class _FakeBroker:
    """The Trading 212 slice the manager uses, in memory."""

    def __init__(self):
        self.positions: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.fills: dict[str, dict] = {}
        self.status_override: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.refuse_gtc = False       # GTC rejected as an invalid order (HTTP 400)
        self.fail_place = False       # every placement fails transiently (HTTP 503)
        self.throttle_next = False    # the NEXT placement is throttled (HTTP 429)
        self.portfolio_down = False
        self.fill_lookups = 0
        self._next = 1

    def hold(self, code, qty, avg, price, pie=0.0, fill_date=None):
        self.positions[code] = dict(
            ticker=code, quantity=qty, averagePrice=avg, currentPrice=price,
            pieQuantity=pie, initialFillDate=fill_date,
        )

    # API surface
    def get_broker_portfolio(self):
        self.calls.append(("portfolio",))
        if self.portfolio_down:
            return None
        return [dict(p) for p in self.positions.values()]

    def get_pending_orders(self):
        return [dict(o) for o in self.orders.values() if o["status"] == "NEW"]

    def place_stop_order(self, ticker, quantity, stop_price, time_validity="DAY"):
        from trading.executor import T212HTTPError
        self.calls.append(("place", ticker, quantity, round(stop_price, 2), time_validity))
        if self.throttle_next:
            self.throttle_next = False
            return None, T212HTTPError(429, "TooManyRequests")
        if self.fail_place:
            return None, T212HTTPError(503, "Service Unavailable")
        if self.refuse_gtc and time_validity == "GOOD_TILL_CANCEL":
            return None, T212HTTPError(400, '{"detail": "timeValidity not supported"}')
        if quantity > self._sellable(ticker) + 1e-12:
            # T212 will not accept a sell for more shares than are held.
            return None, T212HTTPError(400, '{"code": "SellingEquityNotOwned"}')
        oid = f"o{self._next}"
        self._next += 1
        self.orders[oid] = dict(id=oid, ticker=ticker, quantity=-quantity,
                                stopPrice=round(stop_price, 2), status="NEW",
                                timeValidity=time_validity)
        return oid, None

    def cancel_order(self, order_id):
        order = self.orders.get(order_id)
        self.calls.append(("cancel", order["ticker"] if order else None, order_id))
        if not order or order["status"] != "NEW":
            return False
        order["status"] = "CANCELLED"
        return True

    def get_order_status(self, order_id):
        if order_id in self.status_override:
            return self.status_override[order_id]
        order = self.orders.get(order_id)
        return order["status"] if order else "GONE"

    def fetch_fill(self, order_id):
        self.fill_lookups += 1
        return self.fills.get(order_id)

    def _sellable(self, ticker):
        pos = self.positions.get(ticker)
        if not pos:
            return 0.0
        return pos["quantity"] - (pos.get("pieQuantity") or 0.0)

    def sell(self, ticker, quantity, price, reason, *, force_market=False, extended=False):
        from trading.executor import OrderResult
        self.calls.append(("sell", ticker, quantity, reason, force_market))
        if quantity > self._sellable(ticker) + 1e-12:
            return OrderResult(success=False, ticker=ticker, quantity=quantity, price=price,
                               order_id=None, error="HTTP 400 - SellingEquityNotOwned")
        self.positions.pop(ticker, None)
        return OrderResult(success=True, ticker=ticker, quantity=quantity,
                           price=price, order_id="sell-1", error=None)

    # scenario helpers
    def mark_filled(self, order_id, price, remove_position=True):
        order = self.orders[order_id]
        order["status"] = "FILLED"
        self.fills[order_id] = {"price": price, "walletImpact": {}}
        if remove_position:
            self.positions.pop(order["ticker"], None)

    def user_cancel(self, order_id):
        self.orders[order_id]["status"] = "CANCELLED"

    def vanish_order(self, order_id):
        """The order leaves the book and no fill detail is (yet) available."""
        self.orders.pop(order_id, None)

    def resting(self):
        return [o for o in self.orders.values() if o["status"] == "NEW"]

    def kinds(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def h():
    """A manager wired to fake broker/DB/clock, in regular hours, with
    MANUAL_TICKERS=ACME, initial stop 8%, trail 10%, step 0.5%, 30 s pacing."""
    import monitor.manual_positions as mp
    from config.settings import cfg

    db, broker, clock = _FakeDB(), _FakeBroker(), _Clock()
    now = datetime.now(timezone.utc)
    ns = SimpleNamespace(
        mp=mp, cfg=cfg, db=db, broker=broker, clock=clock, prices={},
        session=mp.REGULAR,
        bounds=(now - timedelta(hours=2), now + timedelta(hours=4)),
        feed_down=False,    # market-data feed returns nothing (broker price only)
        quote_t=None,       # pin the quote's timestamp (a frozen feed)
        quote_age=None,     # every quote this many seconds old (a lagging feed)
        quote_calls=0,
    )

    def quote(symbol, fast=True):
        ns.quote_calls += 1
        price = ns.prices.get(f"{symbol}_US_EQ")
        if ns.feed_down or price is None:
            return None
        if ns.quote_t is not None:
            t = ns.quote_t
        else:
            t = clock.time() - (ns.quote_age or 0)
        return {"c": price, "t": t}

    def run(price=None, dt=5.0, n=1, code="ACME_US_EQ"):
        for _ in range(n):
            if price is not None:
                ns.prices[code] = price
                if code in broker.positions:
                    broker.positions[code]["currentPrice"] = price
            clock.t += dt
            mp.manage_manual_positions()

    ns.run = run
    with ExitStack() as stack:
        stack.enter_context(patch.multiple(
            cfg, manual_tickers=["ACME"], manual_account="live",
            manual_trail_pct=10.0, manual_initial_stop_pct=8.0,
            manual_stop_step_pct=0.5, manual_stop_min_move_seconds=30,
            manual_max_hold_days=0, trading_mode="demo", live_entries_enabled=False,
        ))
        # Real cadences are covered by their own tests; here every cycle syncs
        # and checks status so scenarios read cycle-by-cycle.
        for name, value in (("_SYNC_EVERY_SECONDS", 0), ("_STATUS_CHECK_EVERY_SECONDS", 0),
                            ("_PLACE_RETRY_SECONDS", 0), ("_PRICE_EVERY_SECONDS", 0),
                            ("_time", clock)):
            stack.enter_context(patch.object(mp, name, value))
        stack.enter_context(patch.object(mp, "get_trading_session", lambda *a, **k: ns.session))
        stack.enter_context(patch.object(mp, "session_bounds", lambda *a, **k: ns.bounds))
        stack.enter_context(patch.object(mp, "get_quote_with_fallback", quote))
        stack.enter_context(patch.object(mp, "t212_to_symbol", lambda code: code.split("_")[0]))
        for name in ("get_broker_portfolio", "get_pending_orders", "place_stop_order",
                     "cancel_order", "get_order_status", "sell"):
            stack.enter_context(patch.object(mp, name, getattr(broker, name)))
        stack.enter_context(patch.object(mp, "_fetch_fill", broker.fetch_fill))
        for name in ("open_manual_trade", "get_open_manual_trades", "raise_manual_trail",
                     "update_manual_position", "set_stop_order_id", "close_trade",
                     "record_system_event"):
            stack.enter_context(patch.object(mp, name, getattr(db, name)))
        yield ns


# ── 1. The live-money lock on the AUTOMATED strategy ──────────────────────────

class TestLiveEntryLock:

    def test_live_buy_is_refused_before_any_broker_call(self):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch.object(ex, "calculate_quantity") as sizing, \
             patch("trading.executor.requests") as http:
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is False
        assert "LIVE_ENTRIES_ENABLED" in res.error
        sizing.assert_not_called()  # not even the balance lookup
        http.post.assert_not_called()
        http.get.assert_not_called()

    def test_http_layer_refuses_a_live_buy_from_any_code_path(self):
        # The chokepoint: even code that bypasses buy() cannot reach the broker.
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch("trading.executor.requests") as http:
            for path in ("/equity/orders/market", "/equity/orders/limit"):
                with pytest.raises(ex.OrderBlocked):
                    ex._post(path, {"ticker": "ACME_US_EQ", "quantity": 1.0})
        http.post.assert_not_called()

    @pytest.mark.parametrize("payload", [
        {"ticker": "X"}, {"ticker": "X", "quantity": None},
        {"ticker": "X", "quantity": "abc"}, {"ticker": "X", "quantity": float("nan")},
        {"ticker": "X", "quantity": 0},
    ])
    def test_an_unreadable_quantity_is_treated_as_a_buy(self, payload):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch("trading.executor.requests") as http:
            with pytest.raises(ex.OrderBlocked):
                ex._post("/equity/orders/market", payload)
        http.post.assert_not_called()

    def test_a_non_order_write_is_refused_in_a_blocked_context(self):
        # A Pie can buy indirectly; in a sell-only context a sell is the ONLY
        # write the system ever needs.
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch("trading.executor.requests") as http:
            with pytest.raises(ex.OrderBlocked):
                ex._post("/equity/pies", {"instrumentShares": {"X": 1.0}})
        http.post.assert_not_called()

    def test_sells_still_pass_the_guard(self):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch("trading.executor.requests") as http:
            ex._post("/equity/orders/stop", {"ticker": "X", "quantity": -3.0, "stopPrice": 9.0})
        http.post.assert_called_once()

    def test_the_lock_opens_only_when_explicitly_enabled(self):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=True), \
             patch.object(ex, "calculate_quantity", return_value=(None, "stop here")) as sizing:
            res = ex.buy("ACME_US_EQ", 100.0)
        sizing.assert_called_once()  # got past the lock to sizing
        assert res.error == "stop here"

    def test_demo_strategy_is_unaffected(self):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="demo", live_entries_enabled=False), \
             patch.object(ex, "calculate_quantity", return_value=(None, "sized")) as sizing:
            ex.buy("ACME_US_EQ", 100.0)
        sizing.assert_called_once()

    def test_a_local_refusal_is_not_retried(self):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch("trading.executor.requests"), \
             patch("trading.executor.time.sleep") as sleep:
            order, exc = ex._post_order_with_retry(
                "/equity/orders/market", {"ticker": "X", "quantity": 1.0}, "X")
        assert order is None and isinstance(exc, ex.OrderBlocked)
        sleep.assert_not_called()

    def test_risk_gates_stand_down_before_touching_the_database(self):
        import main
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="live", live_entries_enabled=False), \
             patch.object(main, "get_today_realized_pnl") as pnl:
            ok, reason = main._risk_gates_pass()
        assert ok is False and "LIVE ENTRIES DISABLED" in reason
        pnl.assert_not_called()


# ── 2. Account routing — and why it cannot leak ───────────────────────────────

class TestAccountRouting:

    @staticmethod
    def _keys():
        from config.settings import cfg
        return patch.multiple(
            cfg, trading_mode="demo",
            trading212_api_key_id="LIVE_ID", trading212_api_key="LIVE_KEY",
            trading212_demo_api_key_id="DEMO_ID", trading212_demo_api_key="DEMO_KEY",
        )

    @staticmethod
    def _basic(key_id, key):
        return "Basic " + base64.b64encode(f"{key_id}:{key}".encode()).decode()

    def test_calls_go_to_the_context_account_and_back(self):
        import trading.executor as ex
        with self._keys():
            assert ex._base_url() == ex._T212_DEMO
            assert ex._auth_header()["Authorization"] == self._basic("DEMO_ID", "DEMO_KEY")
            with ex.use_account("live"):
                assert ex.active_account() == "live"
                assert ex._base_url() == ex._T212_LIVE
                assert ex._auth_header()["Authorization"] == self._basic("LIVE_ID", "LIVE_KEY")
            assert ex._base_url() == ex._T212_DEMO

    def test_the_account_is_restored_when_the_block_raises(self):
        import trading.executor as ex
        with self._keys():
            with pytest.raises(RuntimeError):
                with ex.use_account("live"):
                    raise RuntimeError("boom")
            assert ex.active_account() == "demo"

    def test_the_account_is_per_thread(self):
        # The scheduler runs the manual job and strategy jobs on a shared pool.
        import trading.executor as ex
        inside, release = threading.Event(), threading.Event()
        seen = {}

        def manual_job():
            with ex.use_account("live"):
                seen["worker"] = ex.active_account()
                inside.set()
                release.wait(5)

        with self._keys():
            t = threading.Thread(target=manual_job)
            t.start()
            assert inside.wait(5)
            seen["strategy"] = ex.active_account()  # while the worker is INSIDE the block
            release.set()
            t.join(5)
        assert seen == {"worker": "live", "strategy": "demo"}

    def test_an_unknown_account_is_rejected(self):
        import trading.executor as ex
        with pytest.raises(ValueError):
            with ex.use_account("paper"):
                pass

    @pytest.mark.parametrize("account", ["live", "demo"])
    def test_the_manual_context_is_sell_only_whatever_the_lock_says(self, account):
        import trading.executor as ex
        from config.settings import cfg
        with self._keys(), patch.multiple(cfg, live_entries_enabled=True), \
             patch.object(ex, "calculate_quantity") as sizing, \
             patch("trading.executor.requests") as http:
            with ex.use_account(account):
                res = ex.buy("ACME_US_EQ", 100.0)
                with pytest.raises(ex.OrderBlocked):
                    ex._post("/equity/orders/market", {"ticker": "ACME_US_EQ", "quantity": 1})
        assert res.success is False and "sell-only" in res.error
        sizing.assert_not_called()
        http.post.assert_not_called()

    def test_the_job_cannot_leak_its_account_into_the_worker_thread(self):
        # Even a job that sets the override and never resets it leaves the
        # calling thread untouched: the cycle runs in a COPIED context.
        import monitor.manual_positions as mp
        import trading.executor as ex
        from config.settings import cfg

        def leaky_cycle():
            ex._account_override.set("live")  # deliberately never reset

        with self._keys(), patch.multiple(cfg, manual_tickers=["ACME"]), \
             patch.object(mp, "_cycle", leaky_cycle):
            mp.manage_manual_positions()
            assert ex.active_account() == "demo"

    def test_the_cash_cache_never_serves_one_account_to_another(self):
        import trading.executor as ex
        with self._keys(), patch.object(ex, "_get", side_effect=[
            {"total": 100.0, "free": 50.0}, {"total": 9999.0, "free": 9999.0},
        ]) as get:
            assert ex.get_portfolio_value() == 100.0
            with ex.use_account("live"):
                assert ex.get_portfolio_value() == 9999.0
        assert get.call_count == 2


# ── 3. The strategy never sees a hand-opened trade ────────────────────────────

def _capture_sql(fn, *args, fetchone=None, fetchall=(),
                 target="storage.database.get_conn"):
    executed: list[str] = []

    class _Cur:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params=None): executed.append(" ".join(sql.split()))
        def fetchone(self): return fetchone
        def fetchall(self): return list(fetchall)

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    with patch(target, return_value=_Conn()):
        fn(*args)
    return executed


class TestStrategyIgnoresManualTrades:

    @pytest.mark.parametrize("name,args,fetchone", [
        ("get_open_trades", (), None),
        ("count_open_trades", (), {"n": 0}),
        ("count_trades_today", (), {"n": 0}),
        ("was_recently_traded", ("ACME_US_EQ",), None),
        ("get_loss_streak", (), None),
        ("get_today_realized_pnl", (), {"pnl": 0}),
        ("trading_days_since_last_trade", (), {"d": None}),
    ])
    def test_every_strategy_query_is_scoped_to_strategy_rows(self, name, args, fetchone):
        # These drive the kill switch, loss streak, position cap, ticker
        # cooldown, drought alert and the strategy's own exits. A hand-opened
        # position must reach none of them, whatever the modes are set to.
        import storage.database as db
        sql = _capture_sql(getattr(db, name), *args, fetchone=fetchone)
        assert sql and all("origin = 'strategy'" in s for s in sql), sql

    def test_the_performance_report_excludes_manual_trades(self):
        from reporting.report import generate_report
        sql = _capture_sql(generate_report, target="reporting.report.get_conn")
        assert len(sql) == 2 and all("origin = 'strategy'" in s for s in sql)

    def test_the_migration_labels_every_existing_row_as_strategy(self):
        import storage.database as db
        sql = _capture_sql(db.init_db)
        assert any("ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'strategy'" in s
                   for s in sql)

    def test_manual_writes_can_never_touch_a_strategy_row(self):
        import storage.database as db
        for fn, args in ((db.raise_manual_trail, (1, 120.0, 108.0)),
                         (db.update_manual_position, (1, 5.0, 100.0))):
            sql = _capture_sql(fn, *args)
            assert "origin = 'manual'" in sql[0], sql

    def test_the_trail_is_widen_only_in_sql(self):
        import storage.database as db
        sql = _capture_sql(db.raise_manual_trail, 1, 120.0, 108.0)[0]
        assert "trail_high_price = GREATEST(trail_high_price" in sql
        assert "trail_stop_price = GREATEST(trail_stop_price" in sql


# ── 4. Configuration ──────────────────────────────────────────────────────────

_REQUIRED = {
    "MASSIVE_BENZINGA_API_KEY": "x", "FINNHUBIO_API_KEY": "x", "TWELVEDATA_API_KEY": "x",
    "ANTHROPIC_API_KEY": "x", "TRADING212_DEMO_API_KEY": "x", "TRADING212_DEMO_API_KEY_ID": "x",
}
_MANUAL_KEYS = ("MANUAL_TICKERS", "MANUAL_ACCOUNT", "MANUAL_TRAIL_PCT", "MANUAL_INITIAL_STOP_PCT",
                "MANUAL_STOP_STEP_PCT", "MANUAL_STOP_MIN_MOVE_SECONDS", "MANUAL_MAX_HOLD_DAYS",
                "LIVE_ENTRIES_ENABLED", "TRADING212_API_KEY", "TRADING212_API_KEY_ID")


def _settings(monkeypatch, **env):
    from config.settings import Settings
    for key in _MANUAL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRADING_MODE", "demo")
    for key, value in {**_REQUIRED, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings()


class TestConfiguration:

    def test_defaults_ship_with_the_lock_closed_and_the_feature_off(self, monkeypatch):
        s = _settings(monkeypatch)
        assert s.live_entries_enabled is False
        assert s.manual_tickers == [] and s.manual_enabled is False
        s.validate()  # a default deployment is valid and unchanged

    def test_live_mode_needs_the_explicit_second_key(self, monkeypatch):
        live = {"TRADING_MODE": "live", "TRADING212_API_KEY": "k", "TRADING212_API_KEY_ID": "i"}
        with pytest.raises(ValueError, match="LIVE_ENTRIES_ENABLED"):
            _settings(monkeypatch, **live).validate()
        _settings(monkeypatch, **live, LIVE_ENTRIES_ENABLED="true").validate()

    def test_manual_needs_an_explicit_account(self, monkeypatch):
        with pytest.raises(ValueError, match="MANUAL_ACCOUNT"):
            _settings(monkeypatch, MANUAL_TICKERS="ACME", MANUAL_TRAIL_PCT="10").validate()

    def test_manual_live_needs_the_live_key(self, monkeypatch):
        with pytest.raises(EnvironmentError, match="TRADING212_API_KEY"):
            _settings(monkeypatch, MANUAL_TICKERS="ACME", MANUAL_ACCOUNT="live",
                      MANUAL_TRAIL_PCT="10").validate()

    @pytest.mark.parametrize("trail", ["", "0", "-5", "75"])
    def test_manual_needs_a_sane_trail(self, monkeypatch, trail):
        with pytest.raises(ValueError, match="MANUAL_TRAIL_PCT"):
            _settings(monkeypatch, MANUAL_TICKERS="ACME", MANUAL_ACCOUNT="live",
                      MANUAL_TRAIL_PCT=trail, TRADING212_API_KEY="k",
                      TRADING212_API_KEY_ID="i").validate()

    def test_demo_strategy_with_a_live_manual_position_is_valid(self, monkeypatch):
        s = _settings(monkeypatch, MANUAL_TICKERS="acme, ACME_US_EQ", MANUAL_ACCOUNT="LIVE",
                      MANUAL_TRAIL_PCT="12", TRADING212_API_KEY="k", TRADING212_API_KEY_ID="i")
        s.validate()
        assert s.manual_tickers == ["ACME", "ACME_US_EQ"] and s.manual_account == "live"
        assert s.manual_initial_stop_pct == 12.0  # falls back to the trail
        assert s.is_live is False                  # the strategy stays on demo

    def test_blank_values_mean_unset_instead_of_crashing_startup(self, monkeypatch):
        # The deploy writes `KEY=` for unset values; float("") would crash-loop.
        s = _settings(monkeypatch, MANUAL_TRAIL_PCT="", MANUAL_INITIAL_STOP_PCT="",
                      MANUAL_STOP_STEP_PCT="", MANUAL_MAX_HOLD_DAYS="",
                      MANUAL_STOP_MIN_MOVE_SECONDS="")
        assert (s.manual_trail_pct, s.manual_initial_stop_pct, s.manual_max_hold_days) == (0, 0, 0)
        assert s.manual_stop_step_pct == 0.5 and s.manual_stop_min_move_seconds == 30


# ── 5. Adoption — only what you list ──────────────────────────────────────────

class TestAdoption:

    def test_only_listed_tickers_are_ever_adopted_or_ordered_against(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.broker.hold("AAPL_US_EQ", 5, 150.0, 190.0)
        h.broker.hold("VODl_EQ", 100, 0.7, 0.72)
        h.run(100.0, n=3)
        assert [r["ticker"] for r in h.db.rows.values()] == ["ACME_US_EQ"]
        touched = {c[1] for c in h.broker.calls if c[0] in ("place", "cancel", "sell")}
        assert touched == {"ACME_US_EQ"}

    def test_adopts_with_broker_figures_and_rests_the_initial_stop(self, h):
        fill = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        h.broker.hold("ACME_US_EQ", 10, 100.0, 101.0, fill_date=fill)
        h.run(101.0)
        row = h.db.only()
        assert (row["quantity"], row["buy_price"], row["trail_stop_price"]) == (10, 100.0, 92.0)
        assert h.broker.kinds("place") == [("place", "ACME_US_EQ", 10, 92.0, "GOOD_TILL_CANCEL")]
        assert "manual_position_adopted" in h.db.event_types()

    def test_pie_shares_are_excluded_and_a_pie_only_position_is_not_adopted(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0, pie=4)
        h.run(100.0)
        assert h.db.only()["quantity"] == 6
        h2_rows_before = len(h.db.rows)
        h.cfg.manual_tickers = ["ACME", "XYZ"]
        h.broker.hold("XYZ_US_EQ", 5, 10.0, 10.0, pie=5)
        h.run(100.0)
        assert len(h.db.rows) == h2_rows_before  # nothing sellable → nothing adopted

    def test_a_near_miss_code_is_named_but_other_holdings_are_not(self, h, caplog):
        h.broker.hold("ACME1_US_EQ", 10, 100.0, 100.0)  # T212's re-used-symbol shape
        h.broker.hold("ACMEX_US_EQ", 3, 400.0, 410.0)
        h.broker.hold("AAPL_US_EQ", 5, 150.0, 190.0)
        with caplog.at_level("WARNING"):
            h.run(100.0)
        assert not h.db.rows
        text = caplog.text
        assert "ACME1_US_EQ" in text
        assert "ACMEX" not in text and "AAPL" not in text  # never list the portfolio

    def test_the_startup_check_reports_counts_not_holdings(self, caplog):
        import monitor.manual_positions as mp
        from config.settings import cfg
        portfolio = [{"ticker": "AAPL_US_EQ"}, {"ticker": "ACME_US_EQ"}]
        with patch.multiple(cfg, manual_tickers=["ACME"], manual_account="live"), \
             patch.object(mp, "get_broker_portfolio", return_value=portfolio), \
             patch.object(mp, "t212_to_symbol", lambda c: c.split("_")[0]), \
             caplog.at_level("INFO"):
            assert mp.check_manual_account() is True
        assert "ACME_US_EQ" in caplog.text and "AAPL" not in caplog.text

    def test_an_unreadable_account_is_a_critical_event_not_a_crash(self):
        import monitor.manual_positions as mp
        from config.settings import cfg
        with patch.multiple(cfg, manual_tickers=["ACME"], manual_account="live"), \
             patch.object(mp, "get_broker_portfolio", return_value=None), \
             patch.object(mp, "record_system_event") as event:
            assert mp.check_manual_account() is False
        assert event.call_args[0][0] == "manual_account_unreachable"


# ── 6. Trailing ───────────────────────────────────────────────────────────────

class TestTrailing:

    def _adopt(self, h, price=100.0):
        h.broker.hold("ACME_US_EQ", 10, 100.0, price)
        h.run(price)

    def test_a_confirmed_new_high_raises_the_stop(self, h):
        self._adopt(h)                    # stop 92 (8% under the 100 average)
        h.run(120.0)                      # first sight of 120: not yet confirmed
        assert len(h.broker.kinds("cancel")) == 0
        h.run(120.0, dt=30)               # confirmed → 120 × 0.9 = 108
        row = h.db.only()
        assert (row["trail_high_price"], row["trail_stop_price"]) == (120.0, 108.0)
        assert [o["stopPrice"] for o in h.broker.resting()] == [108.0]
        assert row["stop_order_id"] == h.broker.resting()[0]["id"]

    def test_a_single_spike_cannot_drag_the_stop_up(self, h):
        self._adopt(h)
        for price in (200.0, 100.0, 100.0):  # one bad print between normal ones
            h.run(price, dt=40)
        assert h.db.only()["trail_high_price"] == 100.0
        assert len(h.broker.kinds("place")) == 1 and not h.broker.kinds("cancel")

    def test_the_stop_never_moves_down(self, h):
        self._adopt(h)
        h.run(120.0, dt=40)
        h.run(120.0, dt=40)                 # stop → 108
        h.cfg.manual_trail_pct = 20.0       # widening the trail …
        h.run(115.0, dt=40, n=3)            # … and a lower price
        assert h.db.only()["trail_stop_price"] == 108.0
        assert [o["stopPrice"] for o in h.broker.resting()] == [108.0]

    def test_a_polled_stop_keeps_its_level_when_the_trail_is_widened(self, h):
        # With no resting order the trigger level IS the computed target, so it
        # must include the stop already reached — not just the current formula.
        h.broker.fail_place = True
        self._adopt(h)
        h.run(120.0, dt=40, n=2)            # polled stop rises to 108
        assert h.db.only()["trail_stop_price"] == 108.0
        h.cfg.manual_trail_pct = 20.0       # formula alone would now say 96
        h.run(110.0, dt=40)
        assert not h.broker.kinds("sell")
        h.run(105.0)                        # through 108, still above 96
        assert h.broker.kinds("sell") == [("sell", "ACME_US_EQ", 10, "trailing_stop", True)]

    def test_moves_smaller_than_the_step_are_ignored(self, h):
        h.cfg.manual_initial_stop_pct = 10.0     # stop starts at 90.00
        self._adopt(h)
        h.run(100.4, dt=40, n=2)                 # → 90.36, under 90 × 1.005 = 90.45
        assert not h.broker.kinds("cancel")
        h.run(100.6, dt=40, n=2)                 # → 90.54 clears the step
        assert [o["stopPrice"] for o in h.broker.resting()] == [90.54]

    def test_moves_are_paced(self, h):
        self._adopt(h)                       # placed at t
        h.run(120.0, dt=5, n=2)              # confirmed high at t+10: too soon
        assert not h.broker.kinds("cancel")
        h.run(120.0, dt=25)                  # t+35 ≥ 30 s
        assert [o["stopPrice"] for o in h.broker.resting()] == [108.0]

    def test_gtc_refused_falls_back_to_day_and_latches(self, h):
        h.broker.refuse_gtc = True
        self._adopt(h)
        assert [c[4] for c in h.broker.kinds("place")] == ["GOOD_TILL_CANCEL", "DAY"]
        h.run(120.0, dt=40, n=2)
        assert h.broker.kinds("place")[-1][4] == "DAY"  # no second GTC attempt
        assert h.mp._stop_validity == "DAY"

    def test_a_filled_stop_closes_the_record_without_selling(self, h):
        self._adopt(h)
        h.broker.mark_filled("o1", 91.5)
        h.run(91.5)
        row = h.db.only()
        assert (row["status"], row["exit_reason"], row["sell_price"]) == ("closed", "trailing_stop", 91.5)
        assert not h.broker.kinds("sell")

    def test_a_fill_that_races_the_move_is_recorded_not_doubled(self, h):
        with patch.object(h.mp, "_STATUS_CHECK_EVERY_SECONDS", 1e9):  # presume live
            self._adopt(h)
            h.broker.mark_filled("o1", 91.0, remove_position=False)  # fill not yet visible
            h.run(120.0, dt=40, n=2)  # new high → cancel fails: it already FILLED
        row = h.db.only()
        assert (row["status"], row["exit_reason"], row["sell_price"]) == ("closed", "trailing_stop", 91.0)
        assert len(h.broker.kinds("place")) == 1 and not h.broker.kinds("sell")

    def test_a_failed_placement_falls_back_to_a_polled_stop(self, h):
        h.broker.fail_place = True
        self._adopt(h)
        h.run(95.0)
        assert not h.broker.kinds("sell")
        h.run(91.0)  # watched it fall THROUGH 92
        assert h.broker.kinds("sell") == [("sell", "ACME_US_EQ", 10, "trailing_stop", True)]
        assert h.db.only()["exit_reason"] == "trailing_stop"

    def test_already_below_the_stop_is_alerted_never_sold_into(self, h):
        self._adopt(h, price=85.0)          # adopted late, already under 92
        h.run(84.0, n=3)
        assert not h.broker.kinds("sell") and not h.broker.kinds("place")
        assert "manual_below_stop" in h.db.event_types()
        h.run(95.0)                         # back above → protected normally
        assert [o["stopPrice"] for o in h.broker.resting()] == [92.0]

    def test_an_unknown_order_state_is_presumed_live(self, h):
        self._adopt(h)
        h.broker.status_override["o1"] = None  # status lookup failed
        h.run(90.0, n=3)
        assert len(h.broker.kinds("place")) == 1 and not h.broker.kinds("sell")
        assert h.db.only()["status"] == "open"

    def test_nothing_is_placed_outside_regular_hours(self, h):
        h.session = "premarket"
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0, n=3)
        assert h.db.only()["status"] == "open"     # the sync still adopts …
        assert not h.broker.kinds("place")         # … but stops wait for RTH

    def test_overnight_makes_no_broker_calls_at_all(self, h):
        h.session = "overnight"
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0, n=3)
        assert h.broker.calls == []


# ── 7. Never fight a human ────────────────────────────────────────────────────

class TestHumanInteraction:

    def test_your_own_sell_order_puts_the_bot_on_standby(self, h):
        h.broker.orders["u1"] = dict(id="u1", ticker="ACME_US_EQ", quantity=-10,
                                     limitPrice=150.0, status="NEW")
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0, n=3)
        assert not h.broker.kinds("place")
        assert "manual_standby_user_order" in h.db.event_types()
        h.broker.orders["u1"]["status"] = "CANCELLED"  # you hand control back
        h.run(100.0)
        assert [o["stopPrice"] for o in h.broker.resting()] == [92.0]

    def test_cancelling_the_bots_stop_in_the_app_means_hands_off(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        h.broker.user_cancel("o1")
        h.run(100.0)                     # sees it cancelled → stand-by
        h.run(100.0, dt=300)
        assert len(h.broker.kinds("place")) == 1
        assert "manual_stop_cancelled_externally" in h.db.event_types()
        h.run(100.0, dt=301)             # 10 minutes on, still held → protect again
        assert len(h.broker.kinds("place")) == 2

    def test_selling_it_yourself_closes_the_record_after_two_syncs(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        h.broker.user_cancel("o1")
        h.broker.positions.pop("ACME_US_EQ")
        h.run(100.0)
        assert h.db.only()["status"] == "open"  # one absence proves nothing
        h.run(100.0)
        row = h.db.only()
        assert (row["status"], row["exit_reason"]) == ("closed", "manual_exit")

    def test_an_unreachable_broker_never_closes_anything(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        h.broker.portfolio_down = True
        h.run(100.0, n=5)
        assert h.db.only()["status"] == "open"
        assert len(h.broker.resting()) == 1

    def test_its_own_stop_is_recognised_after_a_restart(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        h.db.only()["stop_order_id"] = None  # crash between placing and recording
        h.mp.reset_state()
        h.run(100.0, n=2)
        assert h.db.only()["stop_order_id"] == "o1"
        assert len(h.broker.kinds("place")) == 1
        assert "manual_standby_user_order" not in h.db.event_types()

    def test_a_size_change_is_confirmed_then_the_stop_is_resized(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        h.broker.positions["ACME_US_EQ"].update(quantity=15, averagePrice=104.0)
        h.run(100.0)
        assert h.db.only()["quantity"] == 10      # one sighting is not enough
        h.run(100.0)
        row = h.db.only()
        assert (row["quantity"], row["buy_price"]) == (15, 104.0)
        resting = h.broker.resting()
        # Re-placed for the full new size, floor lifted with the new average.
        assert [(o["quantity"], o["stopPrice"]) for o in resting] == [(-15, 95.68)]

    def test_removing_a_ticker_from_the_list_releases_it(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        calls_before = len(h.broker.calls)
        h.cfg.manual_tickers = ["XYZ"]
        h.run(80.0, n=3)
        assert h.db.only()["status"] == "open"
        assert len(h.broker.resting()) == 1        # its last stop keeps protecting
        assert not [c for c in h.broker.calls[calls_before:] if c[0] != "portfolio"]


# ── 8. Maximum hold ───────────────────────────────────────────────────────────

class TestMaxHold:

    @staticmethod
    def _held_for(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def test_off_by_default(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0, fill_date=self._held_for(100))
        h.run(100.0, n=3)
        assert not h.broker.kinds("sell")

    def test_sells_at_market_once_due_cancelling_the_stop_first(self, h):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0, fill_date=self._held_for(6))
        h.run(100.0)                               # adopted + stop resting
        h.cfg.manual_max_hold_days = 5
        h.run(100.0)
        kinds = [c[0] for c in h.broker.calls if c[0] in ("cancel", "sell")]
        assert kinds == ["cancel", "sell"]         # never two sell orders at once
        assert h.broker.kinds("sell")[0][3:] == ("max_hold", True)
        assert h.db.only()["exit_reason"] == "max_hold"

    def test_waits_out_the_opening_minutes(self, h):
        now = datetime.now(timezone.utc)
        h.bounds = (now - timedelta(minutes=5), now + timedelta(hours=6))
        h.cfg.manual_max_hold_days = 5
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0, fill_date=self._held_for(6))
        h.run(100.0, n=2)
        assert not h.broker.kinds("sell")


# ── 9. Audit trail ────────────────────────────────────────────────────────────

class TestLogging:

    def test_every_broker_write_names_its_account_before_it_is_sent(self, caplog):
        import trading.executor as ex
        from config.settings import cfg
        with patch.multiple(cfg, trading_mode="demo"), \
             patch("trading.executor.requests") as http, caplog.at_level("INFO"):
            http.post.side_effect = RuntimeError("connection dropped mid-request")
            with ex.use_account("live"), pytest.raises(RuntimeError):
                ex._post("/equity/orders/stop", {"ticker": "ACME_US_EQ", "quantity": -10,
                                                 "stopPrice": 92.0, "timeValidity": "GOOD_TILL_CANCEL"})
            ex._delete("/equity/orders/o7")
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("T212 ")]
        assert lines[0].startswith("T212 LIVE -> POST /equity/orders/stop")
        assert "stopPrice=92.0" in lines[0] and "quantity=-10" in lines[0]
        assert lines[1] == "T212 DEMO -> DELETE /equity/orders/o7 "

    def test_a_stop_move_logs_old_and_new_levels(self, h, caplog):
        h.broker.hold("ACME_US_EQ", 10, 100.0, 100.0)
        h.run(100.0)
        with caplog.at_level("INFO"):
            h.run(120.0, dt=40, n=2)
        assert "moving stop $92.00 -> $108.00 (new high" in caplog.text

    def test_the_session_open_is_announced_once_even_with_nothing_held(self, h, caplog):
        with caplog.at_level("INFO"):
            h.run(100.0, n=3)
        opens = [r for r in caplog.records if "regular session open" in r.getMessage()]
        assert len(opens) == 1 and "held: none yet" in opens[0].getMessage()


# ── 10. Review fixes (PR #33) ─────────────────────────────────────────────────

class TestReviewFixes:
    """Each test pins one defect found in review before the first live deploy."""

    def _adopt(self, h, price=100.0):
        h.broker.hold("ACME_US_EQ", 10, 100.0, price)
        h.run(price)

    def test_a_transient_failure_never_downgrades_the_stop_to_day(self, h):
        # A 429 right after cancelling the old stop says nothing about GTC.
        # Downgrading on it would leave every later stop expiring at 16:00.
        h.broker.throttle_next = True
        self._adopt(h)
        h.run(100.0)
        validities = [c[4] for c in h.broker.kinds("place")]
        assert validities == ["GOOD_TILL_CANCEL", "GOOD_TILL_CANCEL"]
        assert h.mp._stop_validity == "GOOD_TILL_CANCEL"
        assert [o["timeValidity"] for o in h.broker.resting()] == ["GOOD_TILL_CANCEL"]

    def test_one_observation_seen_twice_cannot_confirm_a_spike(self, h):
        self._adopt(h)
        h.quote_t = h.clock.time() + 5     # the next quote's timestamp …
        h.run(200.0)                       # … a bad print
        h.run(200.0)                       # the SAME quote again (frozen feed)
        h.quote_t = None
        h.run(100.0)                       # a genuinely new observation
        assert h.db.only()["trail_high_price"] == 100.0
        assert not h.broker.kinds("cancel")

    def test_one_broker_snapshot_read_twice_cannot_confirm_a_spike(self, h):
        # Feeds have no price (a new listing), so the broker's position price is
        # used — and it only refreshes with each sync.
        self._adopt(h)
        h.feed_down = True
        with patch.object(h.mp, "_SYNC_EVERY_SECONDS", 1e9):  # no new snapshot
            h.broker.positions["ACME_US_EQ"]["currentPrice"] = 200.0
            h.mp._broker_positions["ACME_US_EQ"]["currentPrice"] = 200.0
            h.run(dt=40, n=3)
        assert h.db.only()["trail_high_price"] == 100.0

    def test_a_stale_quote_is_not_acted_on(self, h):
        self._adopt(h)
        h.quote_age = 600                  # each quote a DIFFERENT, ten-minute-old one
        h.prices["ACME_US_EQ"] = 200.0     # the lagging feed says 200 …
        h.broker.positions["ACME_US_EQ"]["currentPrice"] = 100.0  # … the broker 100
        h.run(dt=40, n=3)
        assert h.db.only()["trail_high_price"] == 100.0

    def test_after_a_restart_a_cancelled_stop_is_replaced_not_waited_on(self, h):
        # The order's validity is unknown after a restart: read it as expiry,
        # never as "you took over" — that would mean ten minutes with no stop.
        self._adopt(h)
        h.mp.reset_state()
        h.broker.user_cancel("o1")
        h.run(100.0)
        assert len(h.broker.kinds("place")) == 2
        assert "manual_stop_cancelled_externally" not in h.db.event_types()

    def test_a_vanished_stop_is_not_replaced_until_the_account_confirms(self, h):
        self._adopt(h)
        h.broker.vanish_order("o1")        # off the book, no fill detail yet
        h.run(100.0, n=3)                  # within the grace period
        assert len(h.broker.kinds("place")) == 1, "must not stop shares that may be sold"
        h.run(100.0, dt=25)                # grace over, newer sync, all shares held
        assert len(h.broker.kinds("place")) == 2

    def test_a_stop_that_vanishes_mid_move_is_not_replaced_on_top(self, h):
        with patch.object(h.mp, "_STATUS_CHECK_EVERY_SECONDS", 1e9):  # presume live
            self._adopt(h)
            h.broker.vanish_order("o1")
            h.run(120.0, dt=40, n=2)  # new high → move → cancel fails: it is GONE
        assert len(h.broker.kinds("place")) == 1, "no second sell stop on maybe-sold shares"
        assert not h.broker.kinds("sell")

    def test_a_vanished_stop_with_the_shares_gone_counts_as_the_stop_filling(self, h):
        self._adopt(h)
        h.broker.vanish_order("o1")
        h.broker.positions.pop("ACME_US_EQ")
        h.run(100.0, n=3)
        row = h.db.only()
        assert (row["status"], row["exit_reason"], row["sell_price"]) == ("closed", "trailing_stop", 92.0)
        assert row["sell_order_id"] == "o1"
        assert len(h.broker.kinds("place")) == 1

    def test_fill_detail_is_fetched_once_not_twice(self, h):
        # _fetch_fill blocks for up to 30 s; asking twice stalls every position.
        # The double ask lived on the cancel/fill race path: the stop filled
        # while being moved, and its fill detail was not yet available.
        with patch.object(h.mp, "_STATUS_CHECK_EVERY_SECONDS", 1e9):  # presume live
            self._adopt(h)
            h.broker.orders["o1"]["status"] = "FILLED"
            before = h.broker.fill_lookups
            h.run(120.0, dt=40, n=2)  # new high → move → cancel fails: it FILLED
        assert h.db.only()["exit_reason"] == "trailing_stop"
        assert h.broker.fill_lookups - before == 1

    def test_prices_are_read_at_most_every_ten_seconds(self, h):
        with patch.object(h.mp, "_PRICE_EVERY_SECONDS", 10.0):
            self._adopt(h)
            before = h.quote_calls
            h.run(100.0, dt=5, n=6)
        assert h.quote_calls - before == 3


class TestFractionalQuantity:
    """2026-11-19, the first live position: 3.14159265 shares. The first stop
    (sized from the broker) rested; every re-placement (sized from the REAL
    column, 3.1415927 — more than held) was refused, so no stop rested."""

    QTY = 3.14159265

    def test_the_record_really_is_rounded_up(self):
        assert _real(self.QTY) > self.QTY  # the premise of the bug

    def test_a_moved_stop_is_sized_from_the_broker(self, h):
        h.broker.hold("ACME_US_EQ", self.QTY, 100.0, 100.0)
        h.run(100.0)
        h.run(120.0, dt=40, n=2)  # new high → cancel + re-place
        resting = h.broker.resting()
        assert [o["stopPrice"] for o in resting] == [108.0]
        assert -resting[0]["quantity"] == pytest.approx(self.QTY, abs=0) and \
            -resting[0]["quantity"] <= self.QTY

    def test_an_exit_sells_exactly_what_is_held(self, h):
        h.broker.fail_place = True  # polled stop only
        h.broker.hold("ACME_US_EQ", self.QTY, 100.0, 100.0)
        h.run(100.0)
        h.run(95.0)
        h.run(91.0)  # falls through 92 → market sell
        assert h.broker.kinds("sell")[0][2] <= self.QTY
        assert h.db.only()["exit_reason"] == "trailing_stop"

    def test_without_a_snapshot_the_record_is_floored_below_the_holding(self):
        import monitor.manual_positions as mp
        mp._broker_positions.clear()
        assert mp._order_quantity({"ticker": "X", "quantity": _real(self.QTY)}) <= self.QTY

    def test_a_pending_size_change_never_oversells(self):
        import monitor.manual_positions as mp
        mp._broker_positions["X"] = {"quantity": 4.0, "pieQuantity": 0}
        assert mp._order_quantity({"ticker": "X", "quantity": 6.0}) == 4.0


class TestReviewFixesOutsideTheManager:

    def test_the_manager_cannot_share_the_strategys_account(self, monkeypatch):
        with pytest.raises(ValueError, match="different account"):
            _settings(monkeypatch, MANUAL_TICKERS="ACME", MANUAL_ACCOUNT="demo",
                      MANUAL_TRAIL_PCT="10").validate()

    def test_strategy_panels_exclude_manual_trades(self):
        import json
        import pathlib
        dash = json.loads(pathlib.Path("grafana/dashboards/momentum_trader.json")
                          .read_text(encoding="utf-8"))

        def panels(node):
            for p in node.get("panels", []):
                yield p
                yield from panels(p)

        queries = [(p.get("title"), t.get("rawSql", "")) for p in panels(dash)
                   for t in p.get("targets", []) if "FROM trades" in t.get("rawSql", "")]
        assert len(queries) >= 12
        for title, sql in queries:
            if title == "Open Trades":
                # A live hand-opened position SHOULD be visible here.
                assert "origin" not in sql
            else:
                assert "origin = 'strategy'" in sql, title

    def test_each_listed_ticker_is_masked_before_the_journal_can_be_printed(self):
        import pathlib
        text = pathlib.Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
        mask = text.index('echo "::add-mask::${entry}"')
        assert "secrets.MANUAL_TICKERS" in text[:mask]
        assert mask < text.index("name: Post-restart health check")


# ── 11. Scheduler wiring ──────────────────────────────────────────────────────

class TestSchedulerWiring:

    @staticmethod
    def _jobs(manual_tickers):
        import main
        from config.settings import cfg
        jobs = {}

        class _Sched:
            def __init__(self, *a, **k): pass
            def add_job(self, func, trigger=None, id=None, **kw):
                jobs[id] = {"func": func, "trigger": trigger, **kw}
            def start(self): pass

        with patch.object(main, "BackgroundScheduler", _Sched), \
             patch.multiple(cfg, manual_tickers=manual_tickers, monitor_interval_seconds=5):
            main.setup_scheduler()
        return jobs

    def test_not_scheduled_when_off(self):
        assert "manual_positions" not in self._jobs([])

    def test_scheduled_once_at_a_time_when_on(self):
        job = self._jobs(["ACME"])["manual_positions"]
        assert job["trigger"].interval.total_seconds() == 5
        # Two overlapping cycles could both cancel-and-replace the same stop.
        assert job["max_instances"] == 1 and job["coalesce"] is True
