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
```

## Architecture

The system runs two APScheduler background jobs from `main.py`:

**`news_cycle`** (every 1 minute during market hours):
`news/fetcher.py` → `market/price_check.py` → `trading/executor.py` → `storage/database.py`

When the market is closed the job reschedules itself to fire once at the next NYSE open (DateTrigger) instead of polling every minute.

**`monitor_positions`** (every 60 seconds):
`monitor/position_monitor.py` checks every open trade in the DB and calls `trading/executor.sell()` if take-profit (+5%), stop-loss (−2%), or time-stop (60 min) is triggered.

### Key data contracts

- `news/fetcher.py` fetches articles from Benzinga via massive.com, uses Claude Haiku to classify sentiment, and produces `NewsItem` dataclasses (ticker, headline, body, source, published_at, sentiment, confidence).
- `market/price_check.py` confirms a signal using Finnhub REST quote for the real-time current price, and yfinance 1-min bars for the momentum baseline (~15 min delayed, intentional — aligns with `MOMENTUM_WINDOW_MINUTES=15`). Volume ratio uses yfinance 20-day daily history.
- `market/finnhub_bars.py` provides `get_finnhub_quote()` — Finnhub REST quote wrapper used for real-time current price in both signal confirmation and the position monitor.
- `trading/executor.py` calls the Trading 212 REST API directly via `requests`. In demo mode (`cfg.is_live == False`) it logs simulated orders without hitting the API. Sell orders use a negative quantity.
- `storage/database.py` manages a PostgreSQL DB (`DB_URL`) with tables: `news_signals`, `trades`, `portfolio_snapshots`, `api_usage`. All DB access goes through the `get_conn()` context manager.

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
