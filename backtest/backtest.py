"""
backtest/backtest.py
────────────────────
Replays the full trading strategy against historical news + price data.

Mirrors the live v12 logic:
  1. Classify sentiment with Claude Haiku (batched, same as production)
     Includes all v12 prompt improvements: LOI neutral, recap neutral,
     large-cap neutral, ticker relevance, acquirer neutral.
  2. Block signals in the first OPEN_BLOCK_MINUTES (5 min) after open
  3. Check price momentum: yfinance 1-min bars, ~5-min baseline
     (bar at t_news - 5 min, to match Twelvedata values[5] in production)
     Falls back to open price as baseline in the 5–15 min window
  4. Check volume:
       - 5–15 min after open: volume_ratio >= 0.5 required
       - after 15 min: >= 1.5× 20-day average required
  5. Liquidity filter: daily dollar volume >= MIN_DAILY_DOLLAR_VOLUME ($1M)
  6. Dead-cat bounce guard: reject if down > MAX_DAY_DROP_PCT from open
  7. Simulate a buy at the confirmation price, then exit at:
       +5% take profit / -2% stop loss / 60-min time stop
  8. Show a price window: 5 min before news → news time → 5 min after

Note: yfinance is used here instead of Twelvedata intentionally.
Production uses Twelvedata for near-real-time 1-min bars. The backtest
uses yfinance (free, historical, no API credit cost). The 5-min baseline
logic is identical — we just use historical yfinance bars instead of
live Twelvedata bars.

Usage:
  python -m backtest.backtest                     # yesterday's market session
  python -m backtest.backtest --date 2026-05-20   # specific date (YYYY-MM-DD)
  python -m backtest.backtest --no-sentiment       # skip Claude, use all articles
"""

import argparse
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytz
import requests
import yfinance as yf

from config.settings import cfg
from news.fetcher import _batch_score_sentiment, _fetch

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

TAKE_PROFIT_PCT      = cfg.take_profit_pct
STOP_LOSS_PCT        = cfg.stop_loss_pct
TIME_STOP_MINUTES    = cfg.time_stop_minutes
MIN_PRICE_MOVE_PCT   = cfg.min_price_move_pct
VOLUME_RATIO_MIN     = 1.5
VOLUME_RATIO_EARLY   = 0.5     # v12: 5–15 min window requires >= 0.5× (was > 0)
OPEN_BLOCK_MINUTES   = cfg.open_block_minutes   # v12: 5 min (was 1 min)
MIN_DAILY_DOLLAR_VOL = cfg.min_daily_dollar_volume  # v12: $1M liquidity filter
MAX_DAY_DROP_PCT     = cfg.max_day_drop_pct
MOMENTUM_BARS_BACK   = 6       # v12: ~5-min baseline (6 bars back at 1-min resolution)

_ET = pytz.timezone("America/New_York")


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class NewsEvent:
    article_id: str
    ticker: str
    headline: str
    published_at: datetime
    sentiment: str
    confidence: float


@dataclass
class TradeResult:
    ticker: str
    headline: str
    published_at: datetime
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    exit_reason: str | None
    pnl_pct: float | None
    # Price window
    price_minus_5: float | None     # price 5 min before news
    price_at_news: float | None     # price at publication time
    price_plus_5: float | None      # price 5 min after news (buy point)
    momentum_pct: float | None      # % move from baseline to entry
    volume_ratio: float | None
    daily_dollar_volume: float | None
    # Rejection reason (None = traded)
    rejected: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_market_day() -> datetime:
    """Return the most recent completed NYSE trading day (UTC open)."""
    today = datetime.now(timezone.utc)
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.replace(hour=13, minute=30, second=0, microsecond=0)


def _get_intraday(ticker: str, date: datetime) -> pd.DataFrame | None:
    """Fetch 1-min bars for ticker on a specific date via yfinance."""
    try:
        df = yf.Ticker(ticker).history(
            start=date.strftime("%Y-%m-%d"),
            end=(date + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
        )
        if df.empty:
            return None
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")
        return df
    except Exception:
        return None


def _price_at(bars: pd.DataFrame, ts: datetime) -> float | None:
    """Return the close price of the 1-min bar that contains ts."""
    if bars is None or bars.empty:
        return None
    mask = bars.index <= ts
    if not mask.any():
        return None
    return float(bars.loc[mask, "Close"].iloc[-1])


def _price_n_bars_before(bars: pd.DataFrame, ts: datetime, n: int) -> float | None:
    """
    Return the close price of the bar n bars before the bar at ts.
    Mirrors Twelvedata values[n] logic used in production.
    """
    if bars is None or bars.empty:
        return None
    mask = bars.index <= ts
    eligible = bars.loc[mask]
    if len(eligible) < n + 1:
        return None
    return float(eligible["Close"].iloc[-(n + 1)])


def _simulate_trade(
    bars: pd.DataFrame,
    entry_time: datetime,
    entry_price: float,
) -> tuple[datetime | None, float | None, str | None, float | None]:
    """
    Simulate holding from entry_time with take-profit/stop-loss/time-stop rules.
    Returns (exit_time, exit_price, exit_reason, pnl_pct).
    """
    tp_price = entry_price * (1 + TAKE_PROFIT_PCT / 100)
    sl_price = entry_price * (1 - STOP_LOSS_PCT / 100)
    time_stop_at = entry_time + timedelta(minutes=TIME_STOP_MINUTES)

    future = bars[bars.index > entry_time]
    for ts, row in future.iterrows():
        if ts >= time_stop_at:
            exit_price = float(row["Close"])
            pnl_pct = (exit_price - entry_price) / entry_price * 100
            return ts, exit_price, "time_stop", pnl_pct
        if row["High"] >= tp_price:
            return ts, tp_price, "take_profit", TAKE_PROFIT_PCT
        if row["Low"] <= sl_price:
            return ts, sl_price, "stop_loss", -STOP_LOSS_PCT

    return None, None, "still_open", None


def _get_volume_stats(
    ticker: str, date: datetime, intraday: pd.DataFrame, confirm_time: datetime
) -> tuple[float, float, float | None]:
    """
    Returns (volume_ratio, today_volume_at_confirm, daily_dollar_volume).
    volume_ratio = today volume up to confirm_time / 20-day ADV
    daily_dollar_volume = today's close × today's full-day volume (projection)
    """
    try:
        daily = yf.Ticker(ticker).history(
            start=(date - timedelta(days=30)).strftime("%Y-%m-%d"),
            end=date.strftime("%Y-%m-%d"),
            interval="1d",
        )
        if len(daily) < 2:
            return 0.0, 0, None
        avg_vol = float(daily["Volume"].mean()) if not daily.empty else 0.0

        # Volume up to confirmation time
        bars_to_confirm = intraday[intraday.index <= confirm_time] if intraday is not None else None
        current_vol = float(bars_to_confirm["Volume"].sum()) if bars_to_confirm is not None and not bars_to_confirm.empty else 0.0

        volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0.0

        # Daily dollar volume: today's close × full-day volume (from last daily bar)
        last_daily = daily.iloc[-1]
        daily_dollar_volume = float(last_daily["Close"]) * float(last_daily["Volume"])
        if daily_dollar_volume == 0:
            daily_dollar_volume = None

        return volume_ratio, int(current_vol), daily_dollar_volume
    except Exception:
        return 0.0, 0, None


# ── Fetch articles ────────────────────────────────────────────────────────────

def fetch_market_day_articles(date: datetime) -> list[dict]:
    """Fetch all Benzinga articles published during market hours on date."""
    market_open  = date.replace(hour=13, minute=30, second=0, microsecond=0)
    market_close = date.replace(hour=21, minute=0,  second=0, microsecond=0)

    all_articles = []
    seen_ids: set = set()

    # Paginate in 2-hour windows to avoid hitting the 100-article limit
    window_start = market_open
    while window_start < market_close:
        window_end = min(window_start + timedelta(hours=2), market_close)
        params = {
            "published.gte": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "published.lte": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 100,
            "sort": "published.asc",
        }
        try:
            resp = requests.get(
                "https://api.massive.com/benzinga/v2/news",
                headers={"Authorization": f"Bearer {cfg.benzinga_api_key}"},
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            articles = resp.json().get("results", [])
        except Exception as exc:
            print(f"  [WARN] Benzinga fetch failed for window {window_start.strftime('%H:%M')}–{window_end.strftime('%H:%M')}: {exc}")
            articles = []

        for a in articles:
            aid = str(a.get("benzinga_id", a.get("url", "")))
            if aid not in seen_ids:
                seen_ids.add(aid)
                all_articles.append(a)
        window_start = window_end

    return all_articles


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(date: datetime, use_sentiment: bool = True) -> list[TradeResult]:
    date_str = date.strftime("%Y-%m-%d")
    print(f"\nBacktest — {date_str}")
    print(f"  Strategy: TP={TAKE_PROFIT_PCT}% | SL={STOP_LOSS_PCT}% | time-stop={TIME_STOP_MINUTES}min")
    print(f"  Filters:  open-block={OPEN_BLOCK_MINUTES}min | momentum>={MIN_PRICE_MOVE_PCT}% | vol>={VOLUME_RATIO_MIN}× | DDV>=${MIN_DAILY_DOLLAR_VOL/1e6:.0f}M")
    print("=" * 72)

    print("Fetching news articles...", end=" ", flush=True)
    try:
        articles = fetch_market_day_articles(date)
    except Exception as exc:
        print(f"\n  ERROR: {exc}")
        return []
    print(f"{len(articles)} articles found")

    # Apply pre-filters matching production:
    # 1. Must have tickers
    # 2. No crypto (X: prefix)
    # 3. No roundup (>3 tickers)
    pre_filtered = []
    for a in articles:
        tickers = [t for t in (a.get("tickers") or []) if t and not t.startswith("X:")]
        if not tickers:
            continue
        if len(tickers) > 3:
            continue
        a["_tickers"] = tickers
        pre_filtered.append(a)
    print(f"Pre-filtered (tickers, no crypto, no roundup): {len(pre_filtered)}")

    # Score sentiment
    print(f"Scoring sentiment {'with Claude Haiku (v12 prompt)' if use_sentiment else '(skipped — all articles positive)'}...")
    news_events: list[NewsEvent] = []
    seen_article_tickers: set = set()

    if use_sentiment:
        scores: dict = {}
        to_score = [
            {
                "id": str(a.get("benzinga_id", "")),
                "headline": html.unescape(a.get("title", "")),
                "teaser": html.unescape(a.get("teaser") or a.get("body", "")[:200]),
            }
            for a in pre_filtered
        ]
        # Chunk into batches of 20 to stay within Claude token limits
        chunk_size = 20
        for i in range(0, len(to_score), chunk_size):
            chunk = to_score[i:i + chunk_size]
            chunk_scores = _batch_score_sentiment(chunk)
            scores.update(chunk_scores)
            print(f"  Scored {min(i + chunk_size, len(to_score))}/{len(to_score)} articles...", end="\r", flush=True)
        print()
    else:
        scores = {}

    for a in pre_filtered:
        article_id = str(a.get("benzinga_id", ""))
        headline = html.unescape(a.get("title", ""))

        if use_sentiment:
            sentiment, confidence = scores.get(article_id, ("neutral", 0.0))
            if sentiment != "positive":
                continue
        else:
            sentiment, confidence = "positive", 1.0

        try:
            published_at = datetime.fromisoformat(
                a.get("published", "").replace("Z", "+00:00")
            )
        except (ValueError, AttributeError):
            continue

        for ticker in a["_tickers"]:
            key = (article_id, ticker)
            if key in seen_article_tickers:
                continue
            seen_article_tickers.add(key)
            news_events.append(NewsEvent(
                article_id=article_id,
                ticker=ticker,
                headline=headline,
                published_at=published_at,
                sentiment=sentiment,
                confidence=confidence,
            ))

    print(f"Positive ticker signals: {len(news_events)}")

    if not news_events:
        print("No signals to backtest.")
        return []

    # Per-ticker intraday cache
    bar_cache: dict[str, pd.DataFrame | None] = {}
    results: list[TradeResult] = []

    print(f"\nRunning price checks and simulations...")
    print("-" * 72)

    market_open_utc = date.replace(hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc)

    for ev in news_events:
        # Strip T212 suffix to get plain ticker
        yf_ticker = ev.ticker.split("_")[0] if "_" in ev.ticker else ev.ticker

        if yf_ticker not in bar_cache:
            bar_cache[yf_ticker] = _get_intraday(yf_ticker, date)

        bars = bar_cache[yf_ticker]
        if bars is None:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=ev.published_at, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=None, price_at_news=None, price_plus_5=None,
                momentum_pct=None, volume_ratio=None, daily_dollar_volume=None,
                rejected="no_price_data",
            ))
            continue

        minutes_since_open = (ev.published_at - market_open_utc).total_seconds() / 60

        # ── v12: block first OPEN_BLOCK_MINUTES (5 min) after open ───────────
        if minutes_since_open < OPEN_BLOCK_MINUTES:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=ev.published_at, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=None, price_at_news=None, price_plus_5=None,
                momentum_pct=None, volume_ratio=None, daily_dollar_volume=None,
                rejected=f"opening_block: {minutes_since_open:.1f} min since open (block={OPEN_BLOCK_MINUTES} min)",
            ))
            continue

        # Price window
        t_confirm = ev.published_at  # In backtest we evaluate at publication time
        price_at_news = _price_at(bars, t_confirm)
        price_minus_5 = _price_n_bars_before(bars, t_confirm, 5)  # 5 bars back = ~5 min ago

        if price_at_news is None:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=price_minus_5, price_at_news=None, price_plus_5=None,
                momentum_pct=None, volume_ratio=None, daily_dollar_volume=None,
                rejected="no_price_data",
            ))
            continue

        # ── v12: momentum baseline — 5-min lookback ───────────────────────────
        # Use the bar 5 bars back (MOMENTUM_BARS_BACK=6 means values[5] in
        # Twelvedata / iloc[-6] in a sorted DataFrame). In the 5–15 min window
        # where we don't have 5 full bars yet, fall back to open-bar price.
        if minutes_since_open < 15:
            open_bar = bars.iloc[0] if not bars.empty else None
            baseline_price = float(open_bar["Close"]) if open_bar is not None else price_at_news
        else:
            baseline_price = price_minus_5 if price_minus_5 is not None else price_at_news

        momentum_pct = (price_at_news - baseline_price) / baseline_price * 100 if baseline_price else 0.0

        # ── Dead-cat bounce guard ─────────────────────────────────────────────
        open_price = float(bars.iloc[0]["Open"]) if not bars.empty else price_at_news
        day_move_pct = (price_at_news - open_price) / open_price * 100 if open_price else 0.0
        if day_move_pct < -MAX_DAY_DROP_PCT:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=price_at_news,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=price_minus_5, price_at_news=price_at_news, price_plus_5=price_at_news,
                momentum_pct=momentum_pct, volume_ratio=None, daily_dollar_volume=None,
                rejected=f"dead_cat: down {day_move_pct:.1f}% on day (max -{MAX_DAY_DROP_PCT}%)",
            ))
            continue

        # ── v12: volume + liquidity filter ────────────────────────────────────
        volume_ratio, today_volume, daily_dollar_volume = _get_volume_stats(
            yf_ticker, date, bars, t_confirm
        )

        # Liquidity filter: reject below minimum daily dollar volume
        if daily_dollar_volume is not None and daily_dollar_volume < MIN_DAILY_DOLLAR_VOL:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=price_at_news,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=price_minus_5, price_at_news=price_at_news, price_plus_5=price_at_news,
                momentum_pct=momentum_pct, volume_ratio=volume_ratio,
                daily_dollar_volume=daily_dollar_volume,
                rejected=f"illiquid: DDV=${daily_dollar_volume:,.0f} < ${MIN_DAILY_DOLLAR_VOL:,.0f}",
            ))
            continue

        # Momentum filter
        if momentum_pct < MIN_PRICE_MOVE_PCT:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=price_at_news,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=price_minus_5, price_at_news=price_at_news, price_plus_5=price_at_news,
                momentum_pct=momentum_pct, volume_ratio=volume_ratio,
                daily_dollar_volume=daily_dollar_volume,
                rejected=f"momentum {momentum_pct:+.2f}% < {MIN_PRICE_MOVE_PCT}% threshold",
            ))
            continue

        # Volume filter
        if minutes_since_open < 15:
            volume_ok = volume_ratio >= VOLUME_RATIO_EARLY
            volume_reject = f"early-session volume {volume_ratio:.2f}× < {VOLUME_RATIO_EARLY}× threshold"
        else:
            volume_ok = volume_ratio >= VOLUME_RATIO_MIN
            volume_reject = f"volume {volume_ratio:.2f}× < {VOLUME_RATIO_MIN}× threshold"

        if not volume_ok:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=price_at_news,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_5=price_minus_5, price_at_news=price_at_news, price_plus_5=price_at_news,
                momentum_pct=momentum_pct, volume_ratio=volume_ratio,
                daily_dollar_volume=daily_dollar_volume,
                rejected=volume_reject,
            ))
            continue

        # ── All filters passed — simulate trade ───────────────────────────────
        exit_time, exit_price, exit_reason, pnl_pct = _simulate_trade(
            bars, t_confirm, price_at_news
        )

        # price_plus_5: what happened 5 min after entry (for the price window display)
        price_plus_5 = _price_at(bars, t_confirm + timedelta(minutes=5))

        results.append(TradeResult(
            ticker=ev.ticker, headline=ev.headline,
            published_at=ev.published_at,
            entry_time=t_confirm, entry_price=price_at_news,
            exit_time=exit_time, exit_price=exit_price,
            exit_reason=exit_reason, pnl_pct=pnl_pct,
            price_minus_5=price_minus_5, price_at_news=price_at_news, price_plus_5=price_plus_5,
            momentum_pct=momentum_pct, volume_ratio=volume_ratio,
            daily_dollar_volume=daily_dollar_volume,
            rejected=None,
        ))

    return results


def print_results(results: list[TradeResult], date_str: str = "") -> None:
    traded  = [r for r in results if r.rejected is None and r.pnl_pct is not None]
    rejected = [r for r in results if r.rejected is not None]

    print(f"\n{'=' * 72}")
    print(f"RESULTS SUMMARY{' — ' + date_str if date_str else ''}")
    print(f"{'=' * 72}")
    print(f"  Total signals evaluated : {len(results)}")
    print(f"  Rejected by filters     : {len(rejected)}")
    print(f"  Trades executed         : {len(traded)}")

    if traded:
        wins   = [r for r in traded if r.pnl_pct and r.pnl_pct > 0]
        losses = [r for r in traded if r.pnl_pct and r.pnl_pct <= 0]
        avg_pnl  = sum(r.pnl_pct for r in traded if r.pnl_pct) / len(traded)
        win_rate = len(wins) / len(traded) * 100 if traded else 0
        print(f"  Win rate                : {win_rate:.0f}%  ({len(wins)} wins / {len(losses)} losses)")
        print(f"  Average P&L per trade   : {avg_pnl:+.2f}%")
        if traded:
            print(f"  Best trade              : {max(r.pnl_pct for r in traded if r.pnl_pct):+.2f}%")
            print(f"  Worst trade             : {min(r.pnl_pct for r in traded if r.pnl_pct):+.2f}%")

    # Rejection breakdown
    if rejected:
        from collections import Counter
        codes = Counter()
        for r in rejected:
            code = r.rejected.split(":")[0] if r.rejected else "unknown"
            codes[code] += 1
        print(f"\n  Rejection breakdown:")
        for code, count in codes.most_common():
            print(f"    {code:<30} {count}")

    # Detailed trade log
    if traded:
        print(f"\n{'─' * 72}")
        print("EXECUTED TRADES")
        print(f"{'─' * 72}")
        for r in traded:
            pnl_str = f"{r.pnl_pct:+.2f}%" if r.pnl_pct is not None else "open"
            vol_str = f"{r.volume_ratio:.1f}×" if r.volume_ratio else "?"
            ddv_str = f"${r.daily_dollar_volume/1e6:.1f}M" if r.daily_dollar_volume else "?"
            result_icon = "✓" if (r.pnl_pct or 0) > 0 else "✗"
            print(
                f"  {result_icon} {r.ticker:<14} {r.published_at.strftime('%H:%MZ')}  "
                f"mom={r.momentum_pct:+.2f}%  vol={vol_str}  ddv={ddv_str}  "
                f"entry=${r.entry_price:.2f}  {r.exit_reason}  P&L={pnl_str}"
            )
            print(f"      {r.headline[:68]}")

    # Price window for signals with data
    has_window = [r for r in results if r.price_at_news is not None]
    if has_window:
        print(f"\n{'─' * 72}")
        print("PRICE WINDOW (−5min → news → +5min)")
        print(f"  {'TICKER':<12} {'TIME':>8} {'−5min':>8} {'AT NEWS':>8} {'+5min':>8} {'MOM':>7} {'VOL':>6} {'DDV':>8}  STATUS")
        print(f"  {'─' * 72}")
        for r in sorted(has_window, key=lambda x: x.published_at):
            minus = f"${r.price_minus_5:.2f}" if r.price_minus_5 else "  n/a"
            at_n  = f"${r.price_at_news:.2f}" if r.price_at_news else "  n/a"
            plus  = f"${r.price_plus_5:.2f}"  if r.price_plus_5 else "  n/a"
            mom   = f"{r.momentum_pct:+.2f}%" if r.momentum_pct is not None else "   n/a"
            vol   = f"{r.volume_ratio:.1f}×"  if r.volume_ratio is not None else "  n/a"
            ddv   = f"${r.daily_dollar_volume/1e6:.1f}M" if r.daily_dollar_volume else "  n/a"
            if r.rejected:
                status = f"SKIP: {r.rejected}"
            elif r.pnl_pct is not None:
                status = f"TRADE: {r.exit_reason} {r.pnl_pct:+.2f}%"
            else:
                status = "TRADE: open"
            print(f"  {r.ticker:<12} {r.published_at.strftime('%H:%MZ'):>8} {minus:>8} {at_n:>8} {plus:>8} {mom:>7} {vol:>6} {ddv:>8}  {status}")


def print_week_summary(all_results: dict[str, list[TradeResult]]) -> None:
    """Print an aggregated summary across multiple backtest days."""
    print(f"\n{'#' * 72}")
    print("  WEEKLY SUMMARY")
    print(f"{'#' * 72}")
    all_traded = []
    for date_str, results in sorted(all_results.items()):
        traded = [r for r in results if r.rejected is None and r.pnl_pct is not None]
        rejected = [r for r in results if r.rejected is not None]
        wins = [r for r in traded if (r.pnl_pct or 0) > 0]
        if traded:
            avg_pnl  = sum(r.pnl_pct for r in traded if r.pnl_pct) / len(traded)
            win_rate = len(wins) / len(traded) * 100
            print(
                f"  {date_str}  signals={len(results):>3}  rejected={len(rejected):>3}  "
                f"trades={len(traded):>2}  wr={win_rate:.0f}%  avg_pnl={avg_pnl:+.2f}%"
            )
        else:
            print(
                f"  {date_str}  signals={len(results):>3}  rejected={len(rejected):>3}  "
                f"trades=0"
            )
        all_traded.extend(traded)

    if all_traded:
        total_wins = [r for r in all_traded if (r.pnl_pct or 0) > 0]
        total_pnl = sum(r.pnl_pct for r in all_traded if r.pnl_pct)
        avg_pnl = total_pnl / len(all_traded)
        win_rate = len(total_wins) / len(all_traded) * 100
        print(f"\n  {'─' * 68}")
        print(f"  TOTAL  trades={len(all_traded):>2}  wins={len(total_wins)}  losses={len(all_traded)-len(total_wins)}")
        print(f"         win_rate={win_rate:.0f}%  avg_pnl={avg_pnl:+.2f}%  sum_pnl={total_pnl:+.2f}%")
        print(f"         best={max(r.pnl_pct for r in all_traded if r.pnl_pct):+.2f}%  "
              f"worst={min(r.pnl_pct for r in all_traded if r.pnl_pct):+.2f}%")

        # Per-exit-reason breakdown
        from collections import Counter
        reasons = Counter(r.exit_reason for r in all_traded if r.exit_reason)
        print(f"\n  Exit reasons:")
        for reason, count in reasons.most_common():
            pnl_for_reason = [r.pnl_pct for r in all_traded if r.exit_reason == reason and r.pnl_pct is not None]
            avg = sum(pnl_for_reason) / len(pnl_for_reason) if pnl_for_reason else 0
            print(f"    {reason:<20} {count:>3}  avg={avg:+.2f}%")
    print(f"{'#' * 72}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the momentum trading strategy (v12 logic)")
    parser.add_argument("--date", default=None, help="Date to backtest (YYYY-MM-DD), default: last market day")
    parser.add_argument("--week", action="store_true", help="Backtest the entire last trading week (Mon–Fri)")
    parser.add_argument("--no-sentiment", action="store_true", help="Skip Claude sentiment and use all articles")
    args = parser.parse_args()

    if args.week:
        # Last full trading week: find last Friday and go back to its Monday
        today = datetime.now(timezone.utc)
        # Walk back to last Friday
        d = today - timedelta(days=1)
        while d.weekday() != 4:  # 4 = Friday
            d -= timedelta(days=1)
        last_friday = d
        last_monday = last_friday - timedelta(days=4)

        trading_days = []
        for i in range(5):
            day = last_monday + timedelta(days=i)
            if day.weekday() < 5:
                trading_days.append(day.replace(hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc))

        print(f"\nRunning weekly backtest: {last_monday.strftime('%Y-%m-%d')} – {last_friday.strftime('%Y-%m-%d')}")
        all_results: dict[str, list[TradeResult]] = {}
        for day in trading_days:
            day_results = run_backtest(day, use_sentiment=not args.no_sentiment)
            print_results(day_results, date_str=day.strftime("%Y-%m-%d"))
            all_results[day.strftime("%Y-%m-%d")] = day_results

        print_week_summary(all_results)

    else:
        if args.date:
            date = datetime.strptime(args.date, "%Y-%m-%d").replace(
                hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc
            )
        else:
            date = _last_market_day()

        results = run_backtest(date, use_sentiment=not args.no_sentiment)
        print_results(results)
