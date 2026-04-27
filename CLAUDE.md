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

**`news_cycle`** (every `NEWS_POLL_INTERVAL_MINUTES`, default 5 min):
`news/fetcher.py` → `analysis/sentiment.py` → `market/price_check.py` → `trading/executor.py` → `storage/database.py`

**`monitor_positions`** (every 60 seconds):
`monitor/position_monitor.py` checks every open trade in the DB and calls `trading/executor.sell()` if take-profit (+5%), stop-loss (−2%), or time-stop (60 min) is triggered.

### Key data contracts

- `news/fetcher.py` produces `NewsItem` dataclasses (ticker, headline, body, source, published_at).
- `analysis/sentiment.py` consumes `NewsItem` and produces `SentimentResult` (ticker, sentiment, confidence, is_actionable). Calls Claude API (`claude-sonnet-4-20250514`) and expects a raw JSON response — markdown fence stripping is handled.
- `market/price_check.py` uses yfinance to confirm price moved ≥ `MIN_PRICE_MOVE_PCT` with a volume spike before a buy is placed.
- `trading/executor.py` wraps `trading212-connector`. In demo mode (`cfg.is_live == False`) it logs simulated orders without hitting the API. Sell orders use a negative quantity to the Trading 212 API.
- `storage/database.py` manages a SQLite DB (path from `DB_PATH`) with three tables: `news_signals`, `trades`, `portfolio_snapshots`. All DB access goes through the `get_conn()` context manager.

### Configuration

All settings live in `config/settings.py` as a `Settings` dataclass, loaded from `.env` via `python-dotenv`. Import anywhere as:
```python
from config.settings import cfg
```
`cfg.validate()` raises `EnvironmentError` on startup if any API key is missing.

### Ticker detection and blocklist

There is no watchlist — the system fetches broad market news and detects tickers by matching company name keywords against `COMPANY_TICKER_MAP` in `news/fetcher.py`. To expand coverage, add rows to that dict.

`BLOCKLIST` in `.env` is a comma-separated list of Trading 212 instrument codes (e.g. `TSLA_US_EQ`) to permanently exclude. Leave it empty to allow all detected tickers.

### Demo vs live mode

`TRADING_MODE=demo` (default) simulates all orders locally. `TRADING_MODE=live` executes real trades against your Trading 212 ISA. The `cfg.is_live` property gates this in `trading/executor.py`.

### Logging

Logs go to both stdout and `trader.log` in the working directory. Each module uses `logging.getLogger(__name__)`.
