# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your API keys
```

## Commands

```bash
# Run the trader (reads TRADING_MODE from .env — keep as "demo" until confident)
python main.py

# Run all tests
pytest tests/ -v

# Run a single test class or test
pytest tests/test_core.py::TestExitConditions -v
pytest tests/test_core.py::TestExitConditions::test_take_profit_triggered -v

# View performance report without running the trader
python -m reporting.report

# Run a backtest against a historical trading day (fetches real Benzinga + yfinance data)
python -m backtest.backtest --date 2026-05-21
python -m backtest.backtest                      # defaults to yesterday
python -m backtest.backtest --no-sentiment       # skip Claude, use all positive articles
```

## Architecture

The system runs two APScheduler background jobs from `main.py`:

**`news_cycle`** (every 1 minute, unconditional):
`news/fetcher.py` → `market/price_check.py` → `trading/executor.py` → `storage/database.py`

Returns immediately when the market is closed (checked via `pandas_market_calendars` — no network call). Never reschedules its own trigger; rescheduling from within a running job causes APScheduler to silently drop the replacement.

**`monitor_positions`** (every 60 seconds):
`monitor/position_monitor.py` checks every open trade in the DB and calls `trading/executor.sell()` if take-profit (+5%), stop-loss (−2%), or time-stop (60 min) is triggered.

### Key data contracts

- `news/fetcher.py` fetches articles from Benzinga via massive.com and applies three pre-filters before Claude: (1) freshness — articles older than 60 seconds are dropped; (2) crypto tickers — `X:`-prefixed tickers (e.g. `X:BTCUSD`) are stripped; (3) roundup articles — articles tagging more than 3 tickers are skipped as market digests with no per-stock catalyst. Remaining articles are scored by Claude Haiku acting as an expert momentum day trader. Produces `NewsItem` dataclasses (ticker, headline, body, source, published_at, sentiment, confidence). Only `"positive"` articles proceed further.
- `market/price_check.py` confirms a signal using Finnhub REST quote for the real-time current price, and yfinance 1-min bars for the momentum baseline (~15 min delayed, intentional — aligns with `MOMENTUM_WINDOW_MINUTES=15`). Market open/close is determined by `pandas_market_calendars` (local, no network). Volume ratio uses yfinance 20-day daily history. Blocks trades in the first minute after open (auction noise).
- `market/finnhub_bars.py` provides `get_finnhub_quote()` — Finnhub REST quote wrapper used for real-time current price in both signal confirmation and the position monitor.
- `trading/executor.py` calls the Trading 212 REST API directly via `requests`. At startup, `build_symbol_map()` fetches T212's full instrument catalogue and builds a `shortName → ticker` map used by `fetcher.py` to resolve Benzinga symbols to correct T212 codes (handles post-SPAC/rename mismatches where `SUNE_US_EQ` 404s but `JCS_US_EQ` exists). On a `quantity-precision-mismatch` error, it parses the allowed decimal places from the response and retries once. In demo mode (`cfg.is_live == False`) it logs simulated orders without hitting the API. Sell orders use a negative quantity.
- `storage/database.py` manages a PostgreSQL DB (`DB_URL`) with tables: `news_signals`, `trades`, `portfolio_snapshots`. All DB access goes through the `get_conn()` context manager.

### Configuration

All settings live in `config/settings.py` as a `Settings` dataclass, loaded from `.env` via `python-dotenv`. Import anywhere as:
```python
from config.settings import cfg
```
`cfg.validate()` raises `EnvironmentError` on startup if any API key is missing.

### Ticker detection and blocklist

There is no watchlist — the system relies on Benzinga tagging articles with stock tickers directly. `BLOCKLIST` in `.env` is a comma-separated list of Trading 212 instrument codes (e.g. `TSLA_US_EQ`) to permanently exclude.

### Demo vs live mode

`TRADING_MODE=demo` (default) simulates all orders locally. `TRADING_MODE=live` executes real trades against your Trading 212 ISA. The `cfg.is_live` property gates this in `trading/executor.py`.

### Logging

Logs go to stdout (captured by systemd journald on the VM). Each module uses `logging.getLogger(__name__)`.

### Deployment

Always deploy via `git push origin main`. GitHub Actions picks it up, rsyncs to the VM, and restarts the systemd service. **Never SSH and run commands directly** — changes would be overwritten on the next deploy.

### Test classes

- `TestExitConditions` — take-profit / stop-loss / time-stop logic (no external calls)
- `TestSentimentScoring` — `_batch_score_sentiment()` with mocked Claude responses
- `TestPositionSizing` — `calculate_quantity()` with mocked `_get`
- `TestBuyPrecisionRetry` — T212 precision-mismatch auto-retry logic
