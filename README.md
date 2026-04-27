# Momentum Trader 🚀

A news-driven momentum trading system for your Trading 212 Stocks ISA.

## How it works

```
News (NewsAPI + RSS)
       ↓
Sentiment analysis (Claude API) — BULLISH ≥ 7/10?
       ↓
Price confirmation (yfinance) — price up ≥ 1.5% + volume spike?
       ↓
Buy order (Trading 212 API)
       ↓
Position monitor (every 60s)
  → Take profit (+5%)  ✅
  → Stop loss  (-2%)   ❌
  → Time stop  (60min) ⏱️
       ↓
Trade logged to SQLite
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A [Trading 212](https://www.trading212.com) account with a Stocks ISA
- A [NewsAPI](https://newsapi.org) key (free tier is fine to start)
- An [Anthropic](https://console.anthropic.com) API key

### 2. Install dependencies

```bash
cd momentum_trader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your environment

```bash
cp .env.example .env
# Open .env in your editor and fill in your API keys
```

Key settings to review in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `TRADING_MODE` | `demo` | **Keep as `demo` until you're confident** |
| `WATCHLIST` | `AAPL_US_EQ,...` | Trading 212 equity instrument codes |
| `MIN_SENTIMENT_CONFIDENCE` | `7` | Raise to 8–9 to be more selective |
| `TAKE_PROFIT_PCT` | `5.0` | Sell when up this % |
| `STOP_LOSS_PCT` | `2.0` | Sell when down this % |
| `TIME_STOP_MINUTES` | `60` | Sell after this many minutes regardless |
| `MAX_POSITION_SIZE_PCT` | `5.0` | Max % of portfolio per trade |

### 4. Add your watchlist tickers

Edit `WATCHLIST` in `.env` with Trading 212 equity instrument codes.
You can find these in the Trading 212 app — they follow the pattern `TICKER_US_EQ` for US stocks.

Also update `TICKER_COMPANY_MAP` in `news/fetcher.py` to map your tickers to company names
(used for RSS feed keyword matching).

### 5. Run the tests

```bash
pytest tests/ -v
```

### 6. Start the trader (demo mode)

```bash
python main.py
```

You'll see live output like:
```
2026-01-15 09:32:01  INFO      main — News cycle starting
2026-01-15 09:32:03  INFO      news.fetcher — Fetched 12 unique news items
2026-01-15 09:32:05  INFO      analysis.sentiment — Sentiment [AAPL_US_EQ] BULLISH | 8/10 | Strong earnings beat
2026-01-15 09:32:06  INFO      market.price_check — Price check [AAPL]: +2.1% move, 2.3× volume — confirmed=True
2026-01-15 09:32:07  INFO      trading.executor — [DEMO] Simulated BUY: AAPL_US_EQ × 2.381000 @ £210.0000
```

### 7. View performance report

```bash
python -m reporting.report
```

---

## Going live ⚠️

Only switch `TRADING_MODE=live` in `.env` when:

- [ ] You have run in demo mode for **at least 2–4 weeks**
- [ ] Your win rate in demo is positive and consistent
- [ ] You understand every line of the code
- [ ] You are comfortable with the maximum loss per trade
- [ ] You have reviewed Trading 212's API terms of service

This software is provided as-is. You are responsible for all trading decisions
and outcomes. Always consult a financial adviser before deploying real capital.

---

## Project structure

```
momentum_trader/
├── main.py                    ← Entry point, scheduler
├── requirements.txt
├── .env.example               ← Copy to .env and fill in keys
├── config/
│   └── settings.py            ← All settings loaded from .env
├── news/
│   └── fetcher.py             ← NewsAPI + RSS ingestion
├── analysis/
│   └── sentiment.py           ← Claude API sentiment classification
├── market/
│   └── price_check.py         ← yfinance price + volume confirmation
├── trading/
│   └── executor.py            ← Trading 212 buy/sell orders
├── monitor/
│   └── position_monitor.py    ← Take profit / stop loss / time stop
├── storage/
│   └── database.py            ← SQLite: signals, trades, snapshots
├── reporting/
│   └── report.py              ← Performance summary
└── tests/
    └── test_core.py           ← Unit tests
```

---

## Extending the system

**Add more news sources**: Edit `news/fetcher.py` — add new RSS feed URLs to `RSS_FEEDS`
or implement a new fetch function and call it from `fetch_all_news()`.

**Tune your strategy**: All parameters are in `.env`. Start conservative and adjust based
on your trade log after 20–30 trades.

**Add notifications**: In `monitor/position_monitor.py`, add an email/SMS alert in the
`Sell order FAILED` block, or add a success notification after `close_trade()`.

**Connect Ghostfolio**: Export your trade history with `python -m reporting.report` and
import into a self-hosted [Ghostfolio](https://ghostfol.io) instance for charting.
