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
python -m backtest.backtest --week               # last full Mon–Fri trading week

# DB-replay backtest: replays v12 logic against signals already in the production DB
# Uses yfinance for prices — no Benzinga API key needed, no Twelvedata credits used.
# Run this on the VM (or set DB_URL to the VM DB) to analyse last week's performance.
DB_URL=postgresql://<db-user>:<db-password>@<your-vm-host>:5432/momentum_trader \
  python -m backtest.backtest_db --week
DB_URL=postgresql://<db-user>:<db-password>@<your-vm-host>:5432/momentum_trader \
  python -m backtest.backtest_db --date 2026-06-05
```

## Architecture

**Full algorithm documentation lives in `docs/algorithm.md`** — every filter,
threshold, and the incident that motivated it. Keep it updated with any rule change.

The system runs four APScheduler background jobs from `main.py`:

**`news_cycle`** (every 1 minute, unconditional):
Market open → pre-market candidate evaluation (first 30 min) → retry queue → `news/fetcher.py` → `market/price_check.py` → risk gates → `trading/executor.py` (buy + resting TP) → `storage/database.py`. Market closed but in the pre-market window (08:00–09:30 ET) → `premarket/scanner.py` builds the at-open watchlist. Otherwise returns immediately (checked via `pandas_market_calendars` — no network call). Never reschedules its own trigger; rescheduling from within a running job causes APScheduler to silently drop the replacement.

**`monitor_positions`** (every `MONITOR_INTERVAL_SECONDS`, default 20s):
`monitor/position_monitor.py` manages exits: notices resting take-profit fills (order-status check), polls stop-loss (−2%) and time-stop (60 min), and force-flattens everything `EOD_FLATTEN_MINUTES` (10) before the close. Before any stop sell it cancels the resting TP and handles the cancel/fill race. Stop sells are bounded LIMIT orders (`SELL_LIMIT_SLACK_PCT`), with market fallback.

**`forward_returns`** (nightly 22:30 UTC): `analysis/forward_returns.py` fills 5/15/60-min forward returns for every Claude classification in `sentiment_scores` (yfinance — free, retrospective). This is the prompt-eval feedback loop.

**`symbol_map_rebuild`** (daily 08:00 UTC): refreshes the T212 symbol map.

Portfolio risk gates in `main.py` (checked before every entry, re-checked after every fill): daily kill switch (`MAX_DAILY_LOSS_PCT` realized → stand down until tomorrow, fail-closed), `MAX_OPEN_POSITIONS`, `MAX_TRADES_PER_DAY`, 24h per-ticker cooldown.

### Key data contracts

- `news/fetcher.py` fetches articles from Benzinga via massive.com and applies pre-filters before Claude: (1) freshness — older than `max_age_minutes` (60s during RTH) dropped; (2) crypto tickers — `X:`-prefixed stripped; (3) roundup articles — >3 tickers skipped. Remaining articles are scored by Claude Haiku in one batched call: `temperature=0`, rubric in a **cached system prompt**, **forced tool use** (schema-validated output). Each article gets `sentiment`, `confidence`, `catalyst_type` (14-class taxonomy), `already_moved`. **Every score is persisted to `sentiment_scores`** (eval loop). Positives must then pass three code gates to trade: confidence ≥ `MIN_SENTIMENT_CONFIDENCE`, catalyst in `TRADEABLE_CATALYSTS`, `already_moved == False`. Produces `NewsItem` dataclasses.
- `market/price_check.py` confirms a signal using Finnhub REST quote (current price, open, **previous close** `pc`) and Twelvedata bars. Momentum baseline selected **by timestamp** (`MOMENTUM_LOOKBACK_MINUTES`, staleness guard >10 min). Rejection filters (in order, with `reason_code`): `opening_block` (5 min), `penny_stock` (< `MIN_STOCK_PRICE` $5), `wide_spread` (last-bar range > `MAX_SPREAD_PCT`), `dead_cat` (< −`MAX_DAY_DROP_PCT` vs prev close), `extended_move` (> `MAX_DAY_MOVE_PCT` 25% vs prev close), `illiquid` (20-day ADV×price < `MIN_DAILY_DOLLAR_VOLUME` $5M — ADV-based, NOT today's volume), `low_momentum`/`high_momentum` (1.5%–15% band), `low_volume`/`high_volume` (RVOL band `MIN_RVOL`–`MAX_RVOL`, time-of-day normalized via `compute_rvol`).
- `market/finnhub_bars.py` provides `get_finnhub_quote()` — 3-attempt exponential backoff retry. Used for real-time price in confirmation and the monitor.
- `market/twelvedata_bars.py` provides `get_momentum_baseline()` (returns `(past_price, current_bar_price, spread_proxy_pct)`) and `get_volume_stats()` (returns `(today_volume, avg_daily_volume, avg_dollar_volume, prev_close)`). In-process credit metering warns at 80% of the 800/day budget.
- `trading/executor.py` calls the Trading 212 REST API directly. `build_symbol_map()` (retried, rebuilt daily) maps Benzinga symbols to T212 codes. `calculate_quantity()` sizes positions as min(hard cap, risk budget, ADV participation cap, cash). `buy()` retries once on `quantity-precision-mismatch`. `place_take_profit()` rests a limit sell at TP. `sell()` uses bounded limit orders with cancel-and-retry and market fallback. `get_order_status()` returns `"GONE"` for 404 (filled→history) vs `None` for network errors — callers must never treat `None` as filled.
- `premarket/scanner.py` — pre-market watchlist + at-open gap-and-go evaluation (gap band `MIN_GAP_PCT`–`MAX_GAP_PCT` + full standard confirmation). Never pre-places orders.
- `storage/database.py` manages a PostgreSQL DB (`DB_URL`) with tables: `news_signals`, `trades` (incl. `tp_order_id`), `portfolio_snapshots`, `sentiment_scores`, `premarket_candidates`, `heartbeat`. All DB access goes through the `get_conn()` context manager.

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
