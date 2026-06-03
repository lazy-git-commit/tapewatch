"""
backtest/backtest.py
────────────────────
Replays the full trading strategy against historical news + price data.

Mirrors the live v7 logic:
  1. Classify sentiment with Claude Haiku (batched, same as production)
  2. Block signals in the first minute after open (09:30–09:31 ET)
  3. Check price momentum: yfinance 1-min bars (15-min delayed baseline)
     Falls back to open price as baseline in the 1–15 min window
  4. Check volume:
       - 1–15 min after open: current_volume > 0 required
       - after 15 min: ≥1.5× 20-day average required
  5. Simulate a buy at the confirmation price, then exit at:
       +5% take profit / -2% stop loss / 60-min time stop
  6. Show a price window: 15 min before news → 60 min after news

Usage:
  python -m backtest.backtest                     # yesterday's market session
  python -m backtest.backtest --date 2026-05-20   # specific date (YYYY-MM-DD)
  python -m backtest.backtest --date 2026-05-20 --no-sentiment  # skip Claude, use all articles
"""

import argparse
import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

from config.settings import cfg
from news.fetcher import _batch_score_sentiment, _fetch

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

TAKE_PROFIT_PCT = cfg.take_profit_pct
STOP_LOSS_PCT = cfg.stop_loss_pct
TIME_STOP_MINUTES = cfg.time_stop_minutes
MIN_PRICE_MOVE_PCT = cfg.min_price_move_pct
VOLUME_RATIO_MIN = 1.5


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
    price_minus_15: float | None   # price 15 min before news
    price_at_news: float | None    # price at publication time
    price_plus_15: float | None    # price 15 min after news (buy point)
    momentum_pct: float | None     # % move from price_at_news to price_plus_15
    volume_ratio: float | None
    # Rejection reasons (None = traded)
    rejected: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_market_day() -> datetime:
    """Return the most recent completed NYSE trading day (UTC open)."""
    today = datetime.now(timezone.utc)
    # Walk back until we find a weekday
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
        # Ensure timezone-aware index
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
    # Find the last bar at or before ts
    mask = bars.index <= ts
    if not mask.any():
        return None
    return float(bars.loc[mask, "Close"].iloc[-1])


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
            pnl_pct = TAKE_PROFIT_PCT
            return ts, tp_price, "take_profit", pnl_pct
        if row["Low"] <= sl_price:
            pnl_pct = -STOP_LOSS_PCT
            return ts, sl_price, "stop_loss", pnl_pct

    return None, None, "still_open", None


def _get_volume_ratio(ticker: str, date: datetime, intraday: pd.DataFrame) -> float:
    """Compute today's cumulative volume vs 20-day daily average."""
    try:
        daily = yf.Ticker(ticker).history(
            start=(date - timedelta(days=30)).strftime("%Y-%m-%d"),
            end=date.strftime("%Y-%m-%d"),
            interval="1d",
        )
        if len(daily) < 2:
            return 0.0
        avg_vol = float(daily["Volume"].iloc[:-1].mean()) if len(daily) > 1 else 0.0
        current_vol = float(intraday["Volume"].sum()) if intraday is not None else 0.0
        return current_vol / avg_vol if avg_vol > 0 else 0.0
    except Exception:
        return 0.0


# ── Fetch articles ────────────────────────────────────────────────────────────

def fetch_market_day_articles(date: datetime) -> list[dict]:
    """Fetch all Benzinga articles published during market hours on date."""
    market_open = date.replace(hour=13, minute=30, second=0, microsecond=0)
    market_close = date.replace(hour=21, minute=0, second=0, microsecond=0)

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
        resp = requests.get(
            "https://api.massive.com/benzinga/v2/news",
            headers={"Authorization": f"Bearer {cfg.benzinga_api_key}"},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("results", [])
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
    print(f"\nBacktest — {date_str}  (take-profit={TAKE_PROFIT_PCT}% | stop-loss={STOP_LOSS_PCT}% | time-stop={TIME_STOP_MINUTES}min)")
    print("=" * 72)

    print("Fetching news articles...", end=" ", flush=True)
    articles = fetch_market_day_articles(date)
    print(f"{len(articles)} articles found")

    # Filter to those with tickers
    articles = [a for a in articles if a.get("tickers")]
    print(f"With tickers: {len(articles)}")

    # Score sentiment in one batched Claude call (or accept all)
    print(f"Scoring sentiment {'with Claude Haiku (batched)' if use_sentiment else '(skipped — all articles used)'}...")
    news_events: list[NewsEvent] = []
    seen_article_tickers: set = set()

    if use_sentiment:
        to_score = [
            {
                "id": str(a.get("benzinga_id", "")),
                "headline": html.unescape(a.get("title", "")),
                "teaser": html.unescape(a.get("teaser") or a.get("body", "")[:200]),
            }
            for a in articles
        ]
        scores = _batch_score_sentiment(to_score)
    else:
        scores = {}

    for a in articles:
        article_id = str(a.get("benzinga_id", ""))
        headline = html.unescape(a.get("title", ""))
        teaser = html.unescape(a.get("teaser") or a.get("body", "")[:200])

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

        for ticker in a.get("tickers", []):
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

    print(f"Positive signals: {len(news_events)}")

    if not news_events:
        print("No signals to backtest.")
        return []

    # Per-ticker intraday cache
    bar_cache: dict[str, pd.DataFrame | None] = {}
    results: list[TradeResult] = []

    print(f"\nRunning price checks and trade simulations...")
    print("-" * 72)

    market_open_utc = date.replace(hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc)

    for ev in news_events:
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
                price_minus_15=None, price_at_news=None, price_plus_15=None,
                momentum_pct=None, volume_ratio=None,
                rejected="no_price_data",
            ))
            continue

        # ── v7: block signals in the first minute after open ─────────────────
        minutes_since_open = (ev.published_at - market_open_utc).total_seconds() / 60
        if minutes_since_open < 1:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=ev.published_at, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_15=None, price_at_news=None, price_plus_15=None,
                momentum_pct=None, volume_ratio=None,
                rejected="first_minute_block",
            ))
            continue

        # Price window — confirmation point is 15 min after publication
        t_minus_15 = ev.published_at - timedelta(minutes=15)
        t_confirm  = ev.published_at + timedelta(minutes=15)

        price_minus_15  = _price_at(bars, t_minus_15)
        price_at_news   = _price_at(bars, ev.published_at)
        price_at_confirm = _price_at(bars, t_confirm)

        if price_at_news is None:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=ev.published_at, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_15=price_minus_15, price_at_news=None,
                price_plus_15=None, momentum_pct=None,
                volume_ratio=None, rejected="no_price_data",
            ))
            continue

        # ── v7: momentum baseline ─────────────────────────────────────────────
        # In the 1–15 min window, yfinance has no bars; use the open-bar price
        # as the baseline (first bar of the day). After 15 min, the last bar
        # ~15 min ago is the baseline (standard production logic).
        if minutes_since_open < 15:
            open_bar = bars.iloc[0] if not bars.empty else None
            baseline_price = float(open_bar["Close"]) if open_bar is not None else price_at_news
            confirm_price = price_at_news  # enter at current price, baseline is open
        else:
            # baseline = price 15 min ago (last yfinance bar before news)
            baseline_price = price_at_news
            confirm_price = price_at_confirm

        if confirm_price is None:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=0,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_15=price_minus_15, price_at_news=price_at_news,
                price_plus_15=None, momentum_pct=None,
                volume_ratio=None, rejected="no_price_data",
            ))
            continue

        momentum_pct = (confirm_price - baseline_price) / baseline_price * 100

        # ── v7: volume check ──────────────────────────────────────────────────
        volume_ratio = _get_volume_ratio(yf_ticker, date, bars)
        # Compute volume at confirmation time (not end of day)
        bars_at_confirm = bars[bars.index <= t_confirm]
        current_volume = int(bars_at_confirm["Volume"].sum()) if not bars_at_confirm.empty else 0

        if minutes_since_open < 15:
            volume_ok = current_volume > 0
            volume_reject_reason = f"no volume yet ({current_volume} shares)"
        else:
            volume_ok = volume_ratio >= VOLUME_RATIO_MIN
            volume_reject_reason = f"volume {volume_ratio:.2f}× < {VOLUME_RATIO_MIN}× threshold"

        # Apply confirmation filters
        if momentum_pct < MIN_PRICE_MOVE_PCT:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=confirm_price,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_15=price_minus_15, price_at_news=price_at_news,
                price_plus_15=confirm_price, momentum_pct=momentum_pct,
                volume_ratio=volume_ratio,
                rejected=f"momentum {momentum_pct:+.2f}% < {MIN_PRICE_MOVE_PCT}% threshold",
            ))
            continue

        if not volume_ok:
            results.append(TradeResult(
                ticker=ev.ticker, headline=ev.headline,
                published_at=ev.published_at,
                entry_time=t_confirm, entry_price=confirm_price,
                exit_time=None, exit_price=None, exit_reason=None, pnl_pct=None,
                price_minus_15=price_minus_15, price_at_news=price_at_news,
                price_plus_15=confirm_price, momentum_pct=momentum_pct,
                volume_ratio=volume_ratio,
                rejected=volume_reject_reason,
            ))
            continue

        # Simulate trade — enter at confirmation price
        exit_time, exit_price, exit_reason, pnl_pct = _simulate_trade(
            bars, t_confirm, confirm_price
        )

        results.append(TradeResult(
            ticker=ev.ticker, headline=ev.headline,
            published_at=ev.published_at,
            entry_time=t_confirm, entry_price=confirm_price,
            exit_time=exit_time, exit_price=exit_price,
            exit_reason=exit_reason, pnl_pct=pnl_pct,
            price_minus_15=price_minus_15, price_at_news=price_at_news,
            price_plus_15=confirm_price, momentum_pct=momentum_pct,
            volume_ratio=volume_ratio, rejected=None,
        ))

    return results


def print_results(results: list[TradeResult]) -> None:
    traded = [r for r in results if r.rejected is None and r.pnl_pct is not None]
    rejected = [r for r in results if r.rejected is not None]
    still_open = [r for r in results if r.exit_reason == "still_open"]

    print(f"\n{'=' * 72}")
    print(f"RESULTS SUMMARY")
    print(f"{'=' * 72}")
    print(f"  Total signals evaluated : {len(results)}")
    print(f"  Rejected by filters     : {len(rejected)}")
    print(f"  Trades executed         : {len(traded)}")

    if traded:
        wins  = [r for r in traded if r.pnl_pct and r.pnl_pct > 0]
        losses = [r for r in traded if r.pnl_pct and r.pnl_pct <= 0]
        avg_pnl = sum(r.pnl_pct for r in traded if r.pnl_pct) / len(traded)
        win_rate = len(wins) / len(traded) * 100 if traded else 0
        print(f"  Win rate                : {win_rate:.0f}%  ({len(wins)} wins / {len(losses)} losses)")
        print(f"  Average P&L per trade   : {avg_pnl:+.2f}%")
        print(f"  Best trade              : {max(r.pnl_pct for r in traded if r.pnl_pct):+.2f}%")
        print(f"  Worst trade             : {min(r.pnl_pct for r in traded if r.pnl_pct):+.2f}%")

    # Detailed trade log
    if traded:
        print(f"\n{'─' * 72}")
        print("EXECUTED TRADES")
        print(f"{'─' * 72}")
        for r in traded:
            pnl_str = f"{r.pnl_pct:+.2f}%" if r.pnl_pct is not None else "open"
            vol_str = f"{r.volume_ratio:.1f}×" if r.volume_ratio else "?"
            print(
                f"  {r.ticker:<14} {r.published_at.strftime('%H:%MZ')}  "
                f"mom={r.momentum_pct:+.2f}%  vol={vol_str}  "
                f"entry=${r.entry_price:.2f}  exit={r.exit_reason}  P&L={pnl_str}"
            )
            print(f"    {r.headline[:68]}")

    # Price window for all signals that had data (traded + rejected-by-filters)
    has_window = [r for r in results if r.price_at_news is not None and r.price_plus_15 is not None]
    if has_window:
        print(f"\n{'─' * 72}")
        print("PRICE WINDOW (all signals with data: 15min before → news → 15min after)")
        print(f"{'TICKER':<12} {'NEWS TIME':<10} {'−15min':>8} {'AT NEWS':>8} {'+15min':>8} {'MOVE':>7}  {'VOL':>6}  STATUS")
        print(f"{'─' * 72}")
        for r in sorted(has_window, key=lambda x: x.published_at):
            minus = f"${r.price_minus_15:.2f}" if r.price_minus_15 else "  n/a"
            at    = f"${r.price_at_news:.2f}" if r.price_at_news else "  n/a"
            plus  = f"${r.price_plus_15:.2f}" if r.price_plus_15 else "  n/a"
            mom   = f"{r.momentum_pct:+.2f}%" if r.momentum_pct is not None else "   n/a"
            vol   = f"{r.volume_ratio:.1f}×" if r.volume_ratio else "  n/a"
            if r.rejected:
                status = f"SKIP: {r.rejected}"
            elif r.pnl_pct is not None:
                status = f"TRADE: {r.exit_reason} {r.pnl_pct:+.2f}%"
            else:
                status = "TRADE: open"
            print(f"  {r.ticker:<10} {r.published_at.strftime('%H:%MZ'):<10} {minus:>8} {at:>8} {plus:>8} {mom:>7}  {vol:>6}  {status}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the momentum trading strategy")
    parser.add_argument("--date", default=None, help="Date to backtest (YYYY-MM-DD), default: last market day")
    parser.add_argument("--no-sentiment", action="store_true", help="Skip Claude sentiment and use all articles")
    args = parser.parse_args()

    if args.date:
        date = datetime.strptime(args.date, "%Y-%m-%d").replace(
            hour=13, minute=30, second=0, microsecond=0, tzinfo=timezone.utc
        )
    else:
        date = _last_market_day()

    results = run_backtest(date, use_sentiment=not args.no_sentiment)
    print_results(results)
