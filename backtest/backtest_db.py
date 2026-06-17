"""
backtest/backtest_db.py
────────────────────────
Replays the CURRENT (v15) trading logic against signals already stored in the
production DB.

Unlike backtest.py (which re-fetches Benzinga articles), this module reads the
news_signals table for a given date range and replays every positive signal
through run_v15_check() — which mirrors market/price_check.py gate-for-gate
(opening block, penny, spread, dead-cat & extended-move vs prev close, ADV
liquidity, dead-tape momentum floor, momentum ceiling, RVOL band, VWAP) — using
yfinance price data.

PARITY IS THE WHOLE POINT: if run_v15_check drifts from confirm_price_signal,
the backtest lies. The constants are sourced from cfg and a test
(TestBacktestParity) asserts they match production.

This is the right tool for retroactive analysis — it doesn't need the Benzinga
API key (articles are already in the DB) and uses free yfinance for prices.

Usage:
  python -m backtest.backtest_db                        # last full trading week
  python -m backtest.backtest_db --date 2026-06-05      # single day
  python -m backtest.backtest_db --since 2026-06-01 --until 2026-06-05

Requires DB_URL in .env pointing to a reachable PostgreSQL instance.
  DB_URL=postgresql://<db-user>:<db-password>@<host>:5432/momentum_trader python -m backtest.backtest_db --week
"""

import argparse
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytz
import yfinance as yf

from config.settings import cfg
from market.price_check import compute_rvol  # same RVOL math as production
from storage.database import get_conn

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

# ── v15 strategy constants (mirrors config/settings.py defaults) ──────────────
# CARDINAL RULE: this list and run_v15_check() must stay in lockstep with
# market/price_check.py. A backtest that tests different logic than production
# is worse than no backtest — it manufactures false confidence.
TAKE_PROFIT_PCT      = cfg.take_profit_pct           # 5.0
STOP_LOSS_PCT        = cfg.stop_loss_pct             # 2.0
TIME_STOP_MINUTES    = cfg.time_stop_minutes         # 60
MIN_PRICE_MOVE_PCT   = cfg.min_price_move_pct        # 0.2  (v15: dead-tape floor only)
MAX_PRICE_MOVE_PCT   = cfg.max_price_move_pct        # 15.0 (halt-article ceiling)
MIN_RVOL             = cfg.min_rvol                  # 1.5  (time-normalized)
MAX_RVOL             = cfg.max_rvol                  # 20.0 (halt-pattern ceiling)
MIN_STOCK_PRICE      = cfg.min_stock_price           # 5.0  (penny-stock filter)
MAX_SPREAD_PCT       = cfg.max_spread_pct            # 3.0  (v15 parity)
OPEN_BLOCK_MINUTES   = cfg.open_block_minutes        # 5
MIN_DAILY_DOLLAR_VOL = cfg.min_daily_dollar_volume   # 5_000_000 (ADV-based)
MAX_DAY_DROP_PCT     = cfg.max_day_drop_pct          # 3.0
MAX_DAY_MOVE_PCT     = cfg.max_day_move_pct          # 25.0 (extended-move ceiling)
REQUIRE_VWAP_CONFIRM = cfg.require_vwap_confirmation # True (v15 size-neutral gate)
VWAP_TOLERANCE_PCT   = cfg.vwap_tolerance_pct        # 0.1
MOMENTUM_BARS_BACK   = 6   # ~5 min ago
_ET = pytz.timezone("America/New_York")

# ── Cost model (v14) ──────────────────────────────────────────────────────────
# The old backtest assumed frictionless fills, making every projection an
# upper bound. Real round-trip costs on T212 (GBP account, USD stocks):
#   - FX conversion fee: 0.15% each way = 0.30% round trip
#   - slippage: spread crossing + book impact, scaled by liquidity
ENTRY_LATENCY_BARS = 1     # production enters ~10–90s after publication:
                           # fill at the NEXT bar's open, not the signal bar's close
FX_COST_RT_PCT     = 0.30  # T212 currency conversion, round trip


def _slippage_pct(avg_dollar_volume: float | None) -> float:
    """
    Estimated one-way slippage (%) by liquidity tier. Calibrated against
    observed fills: large caps fill near-touch; GOAI ($390k ADV) filled
    −16.99% past its trigger. Tiers are deliberately coarse — the point is
    to stop pretending fills are free, not to model microstructure.
    """
    if avg_dollar_volume is None or avg_dollar_volume <= 0:
        return 0.50  # unknown liquidity — assume thin
    if avg_dollar_volume >= 50_000_000:
        return 0.05
    if avg_dollar_volume >= 5_000_000:
        return 0.15
    if avg_dollar_volume >= 1_000_000:
        return 0.50
    return 1.00


def _apply_costs(gross_pnl_pct: float, avg_dollar_volume: float | None) -> float:
    """Gross → net P&L: FX round trip + slippage on both sides."""
    return gross_pnl_pct - FX_COST_RT_PCT - 2 * _slippage_pct(avg_dollar_volume)


@dataclass
class SignalRecord:
    signal_id: int
    ticker: str
    headline: str
    published_at: datetime
    confidence: int
    acted_on: int
    rejection_code: str | None


@dataclass
class BacktestResult:
    signal_id: int
    ticker: str
    headline: str
    published_at: datetime
    # Price check results
    entry_price: float | None
    momentum_pct: float | None
    volume_ratio: float | None
    daily_dollar_volume: float | None
    day_move_pct: float | None
    # Trade simulation
    exit_reason: str | None
    exit_price: float | None
    pnl_pct: float | None
    # What actually happened in production vs what v12 would do
    production_outcome: str  # "traded" | "rejected:<code>" | "not_reached" (pre-filter blocked)
    v12_outcome: str         # "traded" | "rejected:<reason>" | "no_data"


# ── DB helpers ────────────────────────────────────────────────────────────────

def fetch_signals_for_dates(start_date: str, end_date: str) -> list[SignalRecord]:
    """
    Fetch all positive signals from the DB for the given date range.
    Returns signals in published_at order.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, ticker, headline, published_at, confidence, acted_on, rejection_code
                FROM news_signals
                WHERE sentiment = 'positive'
                  AND (created_at::timestamptz AT TIME ZONE 'Europe/London')::date >= %s::date
                  AND (created_at::timestamptz AT TIME ZONE 'Europe/London')::date < %s::date
                ORDER BY published_at ASC
                """,
                (start_date, end_date),
            )
            rows = cur.fetchall()
    signals = []
    for r in rows:
        try:
            pub = datetime.fromisoformat(str(r["published_at"]).replace("Z", "+00:00"))
            if pub.tzinfo is None:
                pub = pytz.timezone("Europe/London").localize(pub)  # write path is _now_london()
            pub = pub.astimezone(timezone.utc)
        except (ValueError, TypeError):
            continue  # unparseable published_at — skip rather than corrupt backtest
        signals.append(SignalRecord(
            signal_id=r["id"],
            ticker=r["ticker"],
            headline=r["headline"],
            published_at=pub,
            confidence=r["confidence"],
            acted_on=r["acted_on"],
            rejection_code=r["rejection_code"],
        ))
    return signals


def fetch_actual_trades(start_date: str, end_date: str) -> dict[int, dict]:
    """Return a dict of signal_id → trade row for trades in the date range."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.*, t.signal_id
                FROM trades t
                WHERE (t.buy_time::timestamptz AT TIME ZONE 'Europe/London')::date >= %s::date
                  AND (t.buy_time::timestamptz AT TIME ZONE 'Europe/London')::date < %s::date
                """,
                (start_date, end_date),
            )
            rows = cur.fetchall()
    return {r["signal_id"]: dict(r) for r in rows if r["signal_id"]}


# ── Price helpers (yfinance) ──────────────────────────────────────────────────

_bar_cache: dict[str, pd.DataFrame | None] = {}
_vol_cache: dict[str, tuple] = {}


def _get_intraday(ticker: str, date: datetime) -> pd.DataFrame | None:
    cache_key = f"{ticker}_{date.strftime('%Y-%m-%d')}"
    if cache_key in _bar_cache:
        return _bar_cache[cache_key]
    try:
        df = yf.Ticker(ticker).history(
            start=date.strftime("%Y-%m-%d"),
            end=(date + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
        )
        if df.empty:
            _bar_cache[cache_key] = None
            return None
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        _bar_cache[cache_key] = df
        return df
    except Exception:
        _bar_cache[cache_key] = None
        return None


def _get_volume_stats_yf(ticker: str, date: datetime, intraday: pd.DataFrame, t_confirm: datetime) -> tuple:
    """
    Returns (cum_volume, avg_daily_volume, avg_dollar_volume|None).
    avg_dollar_volume is ADV-based (20-day avg volume × last close) — the same
    liquidity definition production uses, immune to spike-day inflation.
    """
    cache_key = f"{ticker}_{date.strftime('%Y-%m-%d')}"
    if cache_key in _vol_cache:
        avg_vol, last_price = _vol_cache[cache_key]
    else:
        try:
            daily = yf.Ticker(ticker).history(
                start=(date - timedelta(days=30)).strftime("%Y-%m-%d"),
                end=date.strftime("%Y-%m-%d"),
                interval="1d",
            )
            if len(daily) < 2:
                _vol_cache[cache_key] = (0, 0)
                return 0.0, 0.0, None
            avg_vol = float(daily["Volume"].mean())
            last_price = float(daily["Close"].iloc[-1])
            _vol_cache[cache_key] = (avg_vol, last_price)
        except Exception:
            _vol_cache[cache_key] = (0, 0)
            return 0.0, 0.0, None

    try:
        bars_to_confirm = intraday[intraday.index <= t_confirm]
        cum_vol = float(bars_to_confirm["Volume"].sum()) if not bars_to_confirm.empty else 0.0
        avg_dollar_volume = avg_vol * last_price if avg_vol > 0 and last_price > 0 else None
        return cum_vol, avg_vol, avg_dollar_volume
    except Exception:
        return 0.0, avg_vol, None


def _price_at(bars: pd.DataFrame, ts: datetime) -> float | None:
    if bars is None or bars.empty:
        return None
    mask = bars.index <= ts
    if not mask.any():
        return None
    return float(bars.loc[mask, "Close"].iloc[-1])


def _price_n_bars_before(bars: pd.DataFrame, ts: datetime, n: int) -> float | None:
    if bars is None or bars.empty:
        return None
    mask = bars.index <= ts
    eligible = bars.loc[mask]
    if len(eligible) < n + 1:
        return None
    return float(eligible["Close"].iloc[-(n + 1)])


def _session_vwap_at(bars: pd.DataFrame, ts: datetime) -> float | None:
    """
    Session VWAP up to (and including) the bar at `ts`, computed from the
    intraday DataFrame we already hold. Mirrors production's
    twelvedata_bars.get_session_vwap():
        VWAP = Σ(typical × volume) / Σ(volume),  typical = (H+L+C)/3
    accumulated from the session open. Returns None if no volume yet.

    This keeps the BACKTEST's VWAP gate identical in spirit to production —
    the whole point of v15 backtest parity. (Production fetches VWAP from
    Twelvedata; here we compute the same quantity from the yfinance bars.)
    """
    if bars is None or bars.empty:
        return None
    session = bars[bars.index <= ts]
    if session.empty:
        return None
    typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
    vol = session["Volume"]
    total_vol = float(vol.sum())
    if total_vol <= 0:
        return None
    return float((typical * vol).sum() / total_vol)


def _entry_fill(bars: pd.DataFrame, signal_time: datetime) -> tuple:
    """
    Realistic entry: production is ~10–90s late by construction (poll cadence
    + Claude + price checks + order placement), so the fill is the OPEN of
    the bar AFTER the signal bar — never the signal bar's own close, which
    assumed an impossible zero-latency entry.
    Returns (entry_time, entry_price) or (None, None) if no later bar exists.
    """
    future = bars[bars.index > signal_time]
    if len(future) < ENTRY_LATENCY_BARS:
        return None, None
    idx = ENTRY_LATENCY_BARS - 1
    return future.index[idx], float(future.iloc[idx]["Open"])


def _market_open_for_signal(ts: datetime) -> datetime:
    """NYSE regular-session open for the signal's local ET date, in UTC."""
    ts_et = ts.astimezone(_ET)
    open_et = ts_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return open_et.astimezone(timezone.utc)


def _simulate_trade(bars: pd.DataFrame, entry_time: datetime, entry_price: float) -> tuple:
    """
    Walk forward bar-by-bar from entry. Conservative fill assumptions:
      - If a single bar touches BOTH the stop and the target, assume the
        STOP filled first. We can't know the intra-bar path; optimistic
        tie-breaking (the old behaviour) systematically inflated win rate.
      - Stops fill AT the stop price (production's bounded-limit sell caps
        the damage near the trigger, so this is now a fair assumption).
    Returns (exit_time, exit_price, exit_reason, gross_pnl_pct).
    """
    tp = entry_price * (1 + TAKE_PROFIT_PCT / 100)
    sl = entry_price * (1 - STOP_LOSS_PCT / 100)
    time_stop_at = entry_time + timedelta(minutes=TIME_STOP_MINUTES)
    future = bars[bars.index > entry_time]
    for ts, row in future.iterrows():
        if ts >= time_stop_at:
            exit_price = float(row["Close"])
            return ts, exit_price, "time_stop", (exit_price - entry_price) / entry_price * 100
        # Stop checked BEFORE target — conservative same-bar tie-breaking.
        if row["Low"] <= sl:
            return ts, sl, "stop_loss", -STOP_LOSS_PCT
        if row["High"] >= tp:
            return ts, tp, "take_profit", TAKE_PROFIT_PCT
    return None, None, "still_open", None


# ── v15 price confirmation logic (PARITY with market/price_check.py) ──────────

def run_v15_check(signal: SignalRecord) -> BacktestResult:
    """
    Apply v15 price-confirmation logic to a historical signal.

    Filter order and semantics MIRROR confirm_price_signal() exactly:
      1 opening_block  2 penny_stock  3 wide_spread  4 dead_cat (vs prev close)
      5 extended_move (vs prev close)  6 illiquid (ADV$)  7 low_momentum
      (dead-tape floor)  8 high_momentum  9 low/high_volume (RVOL)
      10 below_vwap (size-neutral accumulation test)
    Any drift between this and production makes the backtest lie, so every
    branch is annotated with the production step it corresponds to.
    """
    yf_ticker = signal.ticker.split("_")[0]
    pub = signal.published_at
    date = pub.replace(hour=0, minute=0, second=0, microsecond=0)
    market_open_utc = _market_open_for_signal(pub)

    if signal.acted_on:
        prod_outcome = "traded"
    elif signal.rejection_code:
        prod_outcome = f"rejected:{signal.rejection_code}"
    else:
        prod_outcome = "rejected:unknown"

    def _result(v15_outcome, *, entry=None, mom=None, vr=None, ddv=None, daymove=None,
                exit_reason=None, exit_price=None, pnl=None):
        """Compact BacktestResult builder — keeps the 10 branches readable."""
        return BacktestResult(
            signal_id=signal.signal_id, ticker=signal.ticker,
            headline=signal.headline, published_at=pub,
            entry_price=entry, momentum_pct=mom, volume_ratio=vr,
            daily_dollar_volume=ddv, day_move_pct=daymove,
            exit_reason=exit_reason, exit_price=exit_price, pnl_pct=pnl,
            production_outcome=prod_outcome, v12_outcome=v15_outcome,
        )

    bars = _get_intraday(yf_ticker, date)
    if bars is None:
        return _result("no_data")

    minutes_since_open = (pub - market_open_utc).total_seconds() / 60

    # ── 1. Opening block ──────────────────────────────────────────────────────
    if minutes_since_open < OPEN_BLOCK_MINUTES:
        return _result(f"rejected:opening_block ({minutes_since_open:.1f} min)")

    price_now = _price_at(bars, pub)
    if price_now is None:
        return _result("no_data")

    # ── 2. Penny stock floor ──────────────────────────────────────────────────
    if price_now < MIN_STOCK_PRICE:
        return _result(f"rejected:penny_stock (${price_now:.4f})", entry=price_now)

    # ── 3. Spread proxy (v15 parity): latest-bar range / close ────────────────
    bar_at = bars[bars.index <= pub]
    if not bar_at.empty:
        last = bar_at.iloc[-1]
        spread_proxy_pct = (
            (float(last["High"]) - float(last["Low"])) / price_now * 100
            if price_now > 0 else 0.0
        )
        if spread_proxy_pct > MAX_SPREAD_PCT:
            return _result(
                f"rejected:wide_spread ({spread_proxy_pct:.2f}%)", entry=price_now
            )

    # ── Momentum baseline (~5 min back, with degenerate-guard parity) ─────────
    if minutes_since_open < 15:
        baseline = float(bars.iloc[0]["Close"]) if not bars.empty else None
    else:
        baseline = _price_n_bars_before(bars, pub, MOMENTUM_BARS_BACK - 1)
    # If baseline is missing or equals current price (degenerate → false 0%),
    # treat momentum as 0 — same conservative stance as production's guard.
    momentum_pct = (price_now - baseline) / baseline * 100 if baseline else 0.0

    # ── Volume + prev close (fetched here, BEFORE dead-cat, matching prod) ────
    cum_vol, avg_vol, daily_dollar_volume = _get_volume_stats_yf(yf_ticker, date, bars, pub)
    prev_close = _vol_cache.get(f"{yf_ticker}_{date.strftime('%Y-%m-%d')}", (0, 0))[1]
    # day_change is vs PREV CLOSE (gap included) — the production metric for
    # both dead_cat and extended_move. day_move (vs open) is kept only for
    # reporting/the result row.
    open_price = float(bars.iloc[0]["Open"]) if not bars.empty else price_now
    day_move_pct = (price_now - open_price) / open_price * 100 if open_price else 0.0
    day_change_pct = (price_now - prev_close) / prev_close * 100 if prev_close else None

    # ── 4. Dead-cat guard (vs prev close; fall back to open like production) ──
    drop_metric = day_change_pct if day_change_pct is not None else day_move_pct
    if drop_metric < -MAX_DAY_DROP_PCT:
        return _result(
            f"rejected:dead_cat ({drop_metric:.1f}%)",
            entry=price_now, mom=momentum_pct, ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    # ── 5. Extended-move ceiling (vs prev close) ──────────────────────────────
    if day_change_pct is not None and day_change_pct > MAX_DAY_MOVE_PCT:
        return _result(
            f"rejected:extended_move ({day_change_pct:+.1f}% vs prev close)",
            entry=price_now, mom=momentum_pct, ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    # ── 6. Liquidity floor (ADV$) ─────────────────────────────────────────────
    if daily_dollar_volume is not None and daily_dollar_volume < MIN_DAILY_DOLLAR_VOL:
        return _result(
            f"rejected:illiquid (DDV=${daily_dollar_volume:,.0f})",
            entry=price_now, mom=momentum_pct, ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    volume_ratio = compute_rvol(int(cum_vol), int(avg_vol), minutes_since_open)

    # ── 7. Momentum noise floor (v15: dead-tape only) ─────────────────────────
    if momentum_pct < MIN_PRICE_MOVE_PCT:
        return _result(
            f"rejected:low_momentum ({momentum_pct:+.2f}%)",
            entry=price_now, mom=momentum_pct, vr=volume_ratio,
            ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    # ── 8. Momentum ceiling (before VWAP, matching prod ordering) ─────────────
    if momentum_pct > MAX_PRICE_MOVE_PCT:
        return _result(
            f"rejected:high_momentum ({momentum_pct:+.2f}%)",
            entry=price_now, mom=momentum_pct, vr=volume_ratio,
            ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    # ── 9. RVOL band ──────────────────────────────────────────────────────────
    if avg_vol > 0 and volume_ratio < MIN_RVOL:
        return _result(
            f"rejected:low_volume (RVOL {volume_ratio:.2f} < {MIN_RVOL})",
            entry=price_now, mom=momentum_pct, vr=volume_ratio,
            ddv=daily_dollar_volume, daymove=day_move_pct,
        )
    if avg_vol > 0 and volume_ratio > MAX_RVOL:
        return _result(
            f"rejected:high_volume (RVOL {volume_ratio:.1f})",
            entry=price_now, mom=momentum_pct, vr=volume_ratio,
            ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    # ── 10. VWAP confirmation (size-neutral accumulation test) ────────────────
    if REQUIRE_VWAP_CONFIRM:
        vwap = _session_vwap_at(bars, pub)
        if vwap is not None and vwap > 0:
            if price_now < vwap * (1 - VWAP_TOLERANCE_PCT / 100):
                return _result(
                    f"rejected:below_vwap (px ${price_now:.2f} < vwap ${vwap:.2f})",
                    entry=price_now, mom=momentum_pct, vr=volume_ratio,
                    ddv=daily_dollar_volume, daymove=day_move_pct,
                )
        # vwap None (no volume) → fall through, same as production.

    # ── All gates passed — realistic entry (next-bar open) + cost model ───────
    entry_time, entry_price = _entry_fill(bars, pub)
    if entry_time is None:
        return _result(
            "no_data", entry=price_now, mom=momentum_pct, vr=volume_ratio,
            ddv=daily_dollar_volume, daymove=day_move_pct,
        )

    exit_time, exit_price, exit_reason, gross_pnl_pct = _simulate_trade(bars, entry_time, entry_price)
    pnl_pct = (
        _apply_costs(gross_pnl_pct, daily_dollar_volume)
        if gross_pnl_pct is not None else None
    )
    return _result(
        "traded", entry=entry_price, mom=momentum_pct, vr=volume_ratio,
        ddv=daily_dollar_volume, daymove=day_move_pct,
        exit_reason=exit_reason, exit_price=exit_price, pnl=pnl_pct,
    )


# Back-compat alias (callers / older invocations may reference the old name).
run_v12_check = run_v15_check


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_day_results(results: list[BacktestResult], date_str: str) -> None:
    traded  = [r for r in results if r.v12_outcome == "traded" and r.pnl_pct is not None]
    rejected = [r for r in results if r.v12_outcome.startswith("rejected") or r.v12_outcome == "no_data"]

    print(f"\n{'═' * 72}")
    print(f"  {date_str}")
    print(f"{'═' * 72}")
    print(f"  Signals in DB       : {len(results)}")
    print(f"  v15 rejected        : {len(rejected)}")
    print(f"  v15 trades          : {len(traded)}")

    if traded:
        wins   = [r for r in traded if (r.pnl_pct or 0) > 0]
        losses = [r for r in traded if (r.pnl_pct or 0) <= 0]
        avg_pnl  = sum(r.pnl_pct for r in traded if r.pnl_pct) / len(traded)
        win_rate = len(wins) / len(traded) * 100
        print(f"  Win rate            : {win_rate:.0f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Avg P&L             : {avg_pnl:+.2f}%")

    # Rejection breakdown
    codes = Counter()
    for r in rejected:
        code = r.v12_outcome.split(":")[1].split(" ")[0] if ":" in r.v12_outcome else r.v12_outcome
        codes[code] += 1
    if codes:
        print(f"  v15 rejection codes : {dict(codes)}")

    # Production vs v12 comparison
    prod_traded = [r for r in results if r.production_outcome == "traded"]
    v12_would_block_prod = [r for r in prod_traded if r.v12_outcome != "traded"]
    v12_new_trades = [r for r in traded if r.production_outcome != "traded"]

    if prod_traded or traded:
        print(f"\n  Production/v15 diff:")
        print(f"    Production traded                  : {len(prod_traded)}")
        print(f"    v15 traded                         : {len(traded)}")
        if v12_would_block_prod:
            print(f"    v15 would have BLOCKED (saved loss): {len(v12_would_block_prod)}")
            for r in v12_would_block_prod:
                print(f"      ✗ {r.ticker:<14} {r.published_at.strftime('%H:%MZ')}  blocked: {r.v12_outcome}")
                print(f"        {r.headline[:66]}")
        if v12_new_trades:
            print(f"    v15 NEW trades not in production   : {len(v12_new_trades)}")
            for r in v12_new_trades:
                pnl = f"{r.pnl_pct:+.2f}%" if r.pnl_pct is not None else "open"
                print(f"      {'✓' if (r.pnl_pct or 0) > 0 else '✗'} {r.ticker:<14} "
                      f"{r.published_at.strftime('%H:%MZ')}  "
                      f"mom={r.momentum_pct:+.2f}%  vol={r.volume_ratio:.1f}×  "
                      f"{r.exit_reason}  {pnl}")
                print(f"        {r.headline[:66]}")

    # Full v12 trade log
    if traded:
        print(f"\n  {'─' * 68}")
        print(f"  V15 EXECUTED TRADES")
        print(f"  {'─' * 68}")
        for r in sorted(traded, key=lambda x: x.published_at):
            pnl  = f"{r.pnl_pct:+.2f}%" if r.pnl_pct is not None else "open"
            vol  = f"{r.volume_ratio:.1f}×" if r.volume_ratio is not None else "?"
            ddv  = f"${r.daily_dollar_volume/1e6:.1f}M" if r.daily_dollar_volume else "?"
            icon = "✓" if (r.pnl_pct or 0) > 0 else "✗"
            print(
                f"  {icon} {r.ticker:<14} {r.published_at.strftime('%H:%MZ')}  "
                f"mom={r.momentum_pct:+.2f}%  vol={vol}  ddv={ddv}  "
                f"entry=${r.entry_price:.2f}  {r.exit_reason}  {pnl}"
            )
            print(f"    {r.headline[:68]}")


def print_weekly_summary(all_results: dict[str, list[BacktestResult]]) -> None:
    print(f"\n{'#' * 72}")
    print("  WEEKLY SUMMARY — v15 logic replay")
    print(f"{'#' * 72}")
    all_traded: list[BacktestResult] = []
    all_prod_traded: list[BacktestResult] = []

    for date_str, results in sorted(all_results.items()):
        traded   = [r for r in results if r.v12_outcome == "traded" and r.pnl_pct is not None]
        rejected = [r for r in results if r.v12_outcome != "traded"]
        wins     = [r for r in traded if (r.pnl_pct or 0) > 0]
        prod_t   = [r for r in results if r.production_outcome == "traded"]

        if traded:
            avg_pnl  = sum(r.pnl_pct for r in traded if r.pnl_pct) / len(traded)
            win_rate = len(wins) / len(traded) * 100
            print(
                f"  {date_str}  signals={len(results):>3}  "
                f"v15_trades={len(traded):>2}  wr={win_rate:.0f}%  avg={avg_pnl:+.2f}%  "
                f"prod_trades={len(prod_t)}"
            )
        else:
            print(
                f"  {date_str}  signals={len(results):>3}  "
                f"v15_trades=0   prod_trades={len(prod_t)}"
            )
        all_traded.extend(traded)
        all_prod_traded.extend(prod_t)

    print(f"\n  {'─' * 68}")
    if all_traded:
        wins   = [r for r in all_traded if (r.pnl_pct or 0) > 0]
        losses = [r for r in all_traded if (r.pnl_pct or 0) <= 0]
        total_pnl = sum(r.pnl_pct for r in all_traded if r.pnl_pct)
        avg_pnl   = total_pnl / len(all_traded)
        win_rate  = len(wins) / len(all_traded) * 100

        print(f"  V15 TOTAL  trades={len(all_traded)}  wins={len(wins)}  losses={len(losses)}")
        print(f"             win_rate={win_rate:.0f}%  avg_pnl={avg_pnl:+.2f}%")
        print(f"             best={max(r.pnl_pct for r in all_traded if r.pnl_pct):+.2f}%  "
              f"worst={min(r.pnl_pct for r in all_traded if r.pnl_pct):+.2f}%")

        reasons = Counter(r.exit_reason for r in all_traded if r.exit_reason)
        print(f"\n  Exit reasons:")
        for reason, count in reasons.most_common():
            pnl_for = [r.pnl_pct for r in all_traded if r.exit_reason == reason and r.pnl_pct]
            avg = sum(pnl_for) / len(pnl_for) if pnl_for else 0
            print(f"    {reason:<20} {count:>3}  avg={avg:+.2f}%")
    else:
        print("  V15 TOTAL  trades=0")

    # Production comparison
    prod_wins = [r for r in all_prod_traded if r.production_outcome == "traded"]
    v12_saved = [r for r in all_prod_traded if r.v12_outcome != "traded"]
    print(f"\n  PRODUCTION vs V15:")
    print(f"    Production executed          : {len(all_prod_traded)} trades")
    print(f"    v15 would have blocked       : {len(v12_saved)} production trades")
    if v12_saved:
        for r in v12_saved:
            print(f"      {r.ticker:<14} {r.published_at.strftime('%Y-%m-%d %H:%MZ')}  blocked: {r.v12_outcome}")
    print(f"{'#' * 72}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _trading_week(ref: datetime) -> tuple[str, str]:
    """Return (start_date, end_date) strings for the most recent completed trading week."""
    # Walk back to last Friday
    d = ref - timedelta(days=1)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    last_friday = d
    last_monday = last_friday - timedelta(days=4)
    return last_monday.strftime("%Y-%m-%d"), (last_friday + timedelta(days=1)).strftime("%Y-%m-%d")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay v15 strategy against production DB signals using yfinance prices"
    )
    parser.add_argument("--date", default=None, help="Single date (YYYY-MM-DD)")
    parser.add_argument("--since", default=None, help="Start date inclusive (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="End date exclusive (YYYY-MM-DD)")
    parser.add_argument("--week", action="store_true", help="Last full trading week (default)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.date:
        start = args.date
        end   = (datetime.strptime(args.date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    elif args.since and args.until:
        start, end = args.since, args.until
    else:
        start, end = _trading_week(now)

    print(f"\nDB-Replay Backtest (v15): {start} → {end}")
    print(f"  Strategy: TP={TAKE_PROFIT_PCT}% | SL={STOP_LOSS_PCT}% | time-stop={TIME_STOP_MINUTES}min")
    print(f"  Filters:  open-block={OPEN_BLOCK_MINUTES}min | "
          f"momentum(dead-tape) {MIN_PRICE_MOVE_PCT}%–{MAX_PRICE_MOVE_PCT}% | "
          f"RVOL {MIN_RVOL}–{MAX_RVOL:.0f} | "
          f"ADV$>={MIN_DAILY_DOLLAR_VOL/1e6:.0f}M | "
          f"price>=${MIN_STOCK_PRICE:.2f} | spread<={MAX_SPREAD_PCT}% | "
          f"day<=+{MAX_DAY_MOVE_PCT:.0f}% | "
          f"VWAP={'on' if REQUIRE_VWAP_CONFIRM else 'off'} | 24h cooldown")
    print(f"  Costs:    FX {FX_COST_RT_PCT}% RT | tiered slippage | entry at next-bar open | SL-priority fills")
    print(f"  DB: {cfg.db_url.split('@')[-1]}")

    try:
        signals = fetch_signals_for_dates(start, end)
    except Exception as exc:
        print(f"\nERROR: Could not connect to DB: {exc}")
        print("Set DB_URL env var to the production DB, e.g.:")
        print("  DB_URL=postgresql://<db-user>:<db-password>@<your-vm-host>:5432/momentum_trader \\")
        print("    python -m backtest.backtest_db --week")
        raise SystemExit(1)

    print(f"\n  {len(signals)} positive signals found in DB for {start} → {end}")

    if not signals:
        print("No signals. Check DB_URL and date range.")
        raise SystemExit(0)

    # Group by date
    by_date: dict[str, list[SignalRecord]] = defaultdict(list)
    for s in signals:
        day = s.published_at.strftime("%Y-%m-%d")
        by_date[day].append(s)

    # Apply 24-hour per-ticker cooldown (mirrors production behaviour).
    # Production skips a ticker for 24h after a trade to avoid re-entering
    # on the same catalyst. Without this, the backtest counts every duplicate
    # signal for a ticker that already traded, overstating both trades and losses.
    ticker_last_traded: dict[str, datetime] = {}

    all_results: dict[str, list[BacktestResult]] = {}
    for day_str in sorted(by_date.keys()):
        day_signals = by_date[day_str]
        print(f"\n  Processing {day_str}: {len(day_signals)} signals...", flush=True)
        day_results = []
        for s in day_signals:
            last_trade_time = ticker_last_traded.get(s.ticker)
            if last_trade_time is not None:
                hours_since = (s.published_at - last_trade_time).total_seconds() / 3600
                if hours_since < 24:
                    day_results.append(BacktestResult(
                        signal_id=s.signal_id, ticker=s.ticker,
                        headline=s.headline, published_at=s.published_at,
                        entry_price=None, momentum_pct=None, volume_ratio=None,
                        daily_dollar_volume=None, day_move_pct=None,
                        exit_reason=None, exit_price=None, pnl_pct=None,
                        production_outcome=(
                            "traded" if s.acted_on else f"rejected:{s.rejection_code or 'unknown'}"
                        ),
                        v12_outcome=f"rejected:cooldown ({hours_since:.1f}h since last trade)",
                    ))
                    continue
            result = run_v15_check(s)
            if result.v12_outcome == "traded":
                ticker_last_traded[s.ticker] = s.published_at
            day_results.append(result)
        all_results[day_str] = day_results
        print_day_results(day_results, day_str)

    if len(all_results) > 1:
        print_weekly_summary(all_results)
