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

# Legacy Benzinga replay (v12 logic; prompt/news experiments only, not live parity)
python -m backtest.backtest --date 2026-05-21
python -m backtest.backtest                      # defaults to yesterday
python -m backtest.backtest --no-sentiment       # skip Claude, use all positive articles
python -m backtest.backtest --week               # last full Mon–Fri trading week

# DB-replay backtest: replays current v15 logic against signals already in the production DB
# Uses yfinance for prices — no Benzinga API key needed, no Twelvedata credits used.
# NOTE: Postgres is bound to localhost on the VM (not exposed beyond the host), so the
# <your-vm-host> host below only resolves when this command is run ON the VM. From the dev
# box, either SSH in and run it there, or open an SSH tunnel first:
#   ssh -fNL 5432:localhost:5432 root@<your-vm-host> -i ~/.ssh/<your-ssh-key>
#   then use DB_URL=postgresql://<db-user>:<db-password>@localhost:5432/momentum_trader
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
`monitor/position_monitor.py` manages exits: notices resting take-profit fills (order-status + fill-detail check), polls stop-loss (−2%) and time-stop (60 min), and force-flattens everything `EOD_FLATTEN_MINUTES` (10) before the close. Before any stop sell it cancels the resting TP and handles the cancel/fill race. Stop/time-stop sells are bounded LIMIT orders (`SELL_LIMIT_SLACK_PCT`), with market fallback; **v19.2 stuck-exit escalation** — after `_SELL_ESCALATE_AFTER` (3) consecutive unfilled limit attempts for one trade, the next attempt is a market order + one `exit_stuck` system_event. EOD flatten uses a market sell because avoiding overnight gap risk takes priority.

**`forward_returns`** (nightly 22:30 UTC): `analysis/forward_returns.py` fills 5/15/60-min forward returns for every Claude classification in `sentiment_scores` (yfinance — free, retrospective). This is the prompt-eval feedback loop.

**`symbol_map_rebuild`** (daily 08:00 UTC): refreshes the T212 symbol map.

Portfolio risk gates in `main.py` (checked before every entry, re-checked after every fill): daily kill switch (`MAX_DAILY_LOSS_PCT` realized → stand down until tomorrow, fail-closed), `MAX_OPEN_POSITIONS`, `MAX_TRADES_PER_DAY`, 24h per-ticker cooldown. **Session no-quote blackout**: `_no_quote_blackout` (set) + `_no_quote_ticker_strikes` (dict) suppress tickers with no Finnhub/Twelvedata coverage after `_NO_QUOTE_BLACKOUT_RETRIES=2` failed retries — prevents perpetual retry loops that drain credits. Resets on service restart.

### Key data contracts

- `news/fetcher.py` fetches articles from Benzinga via massive.com and applies pre-filters before Claude: (1) freshness — older than `max_age_minutes` (3 min during RTH; the session-scoped `_scored_articles` set ensures Claude scores each article exactly once, so the window can exceed the poll cadence with no re-scoring cost — v18); (2) crypto tickers — `X:`-prefixed stripped; (3) roundup articles — >3 tickers skipped. Remaining articles are scored by Claude Haiku in one batched call: `temperature=0`, rubric in a **cached system prompt**, **forced tool use** (schema-validated output). Each article gets `sentiment`, `confidence`, `catalyst_type` (14-class taxonomy), `already_moved`. **Every score is persisted to `sentiment_scores`** (eval loop). Positives must then pass three code gates to trade: confidence ≥ `MIN_SENTIMENT_CONFIDENCE`, catalyst in `TRADEABLE_CATALYSTS`, `already_moved == False`. Produces `NewsItem` dataclasses.
- `market/price_check.py` confirms a signal using a **quote with fallback** (Finnhub `/quote` → Twelvedata `/quote` when Finnhub has no coverage; both yield current price, open, **previous close** `pc`) and Twelvedata bars. Momentum baseline selected **by timestamp** (`MOMENTUM_LOOKBACK_MINUTES`, staleness guard >10 min). Rejection filters (in order, with `reason_code`): `opening_block` (5 min), `penny_stock` (< `MIN_STOCK_PRICE` $5), `wide_spread` (last-bar range > `MAX_SPREAD_PCT`), `dead_cat` (< −`MAX_DAY_DROP_PCT` vs prev close), `extended_move` (> `MAX_DAY_MOVE_PCT` 25% vs prev close), `illiquid` (20-day ADV×price < `MIN_DAILY_DOLLAR_VOLUME` $5M — ADV-based, NOT today's volume), `low_momentum` (< `MIN_PRICE_MOVE_PCT`, v15: a 0.2% dead-tape noise floor only), `high_momentum` (> `MAX_PRICE_MOVE_PCT` 15%), `low_volume`/`high_volume` (RVOL band `MIN_RVOL`–`MAX_RVOL`, time-of-day normalized via `compute_rvol`), `below_vwap` (price < session VWAP — **v15 size-neutral accumulation test** that replaced the fixed-% momentum floor; gated by `REQUIRE_VWAP_CONFIRMATION`, runs last as it costs an extra Twelvedata credit), `insufficient_data` (v19.2 — no volume measurement AND no VWAP; stacked data fallbacks may no longer approve on a bare quote). **v19.2 data integrity**: quotes older than 20 min (`t` timestamp) are treated as no coverage (`_quote_is_stale`); a lagging daily-bar RVOL is rescued with session minute bars (`get_session_volume_and_vwap`, reused for VWAP); `low_volume`/`low_momentum` are TRANSIENT — RTH signals re-confirm via a 15-min re-eval queue in `main.py`, premarket candidates stay pending until the window closes. **Symbol hygiene**: `clean_benzinga_symbol()` drops foreign-exchange tags (`TSX:` etc.) and disambiguation digits (`INBX1`→`INBX`); the reverse mapping uses `t212_to_symbol()` (exact inverse of the instrument map — T212 re-uses symbols with digit suffixes, e.g. exchange `FLY` = T212 `FLY1_US_EQ`).
- `market/finnhub_bars.py` provides `get_finnhub_quote()` — 3-attempt exponential backoff retry. Primary quote source.
- `market/twelvedata_bars.py` provides `get_momentum_baseline()` (`(past_price, current_bar_price, spread_proxy_pct)`), `get_volume_stats()` (`(today_volume, avg_daily_volume, avg_dollar_volume, prev_close)`), `get_twelvedata_quote()` (Finnhub fallback, normalised `c`/`o`/`pc`/`t`), and `get_session_volume_and_vwap()` (`(session_volume, vwap, last_price)` from one 1-min-bars pull; `get_session_vwap()` is a thin wrapper); all take `fast=` (no-retry, for the time-boxed pre-market eval). Two HARD gates run before every HTTP call: (1) **`credits_exhausted()`** — daily backstop at 49,900 credits (Grow plan: no hard cap; backstop is a safety ceiling only; was Basic: 780 = 800 − 20); (2) **`_claim_minute_token()`** — thread-safe token bucket, 55 tokens/minute (Grow plan; was Basic: 8/min). Either gate returning false skips the HTTP call entirely — no 429 backoff storm. There is no bar-data fallback (Finnhub gives only a quote), so the system fails closed on data unavailability (no bar data → no confirmation → no trade) and keeps scoring news. See docs/algorithm.md §7 "data-budget collapse" and "session no-quote blackout".
- `trading/executor.py` calls the Trading 212 REST API directly. `build_symbol_map()` (retried, rebuilt daily) maps Benzinga symbols to T212 codes. `calculate_quantity()` sizes positions as min(hard cap, risk budget, ADV participation cap, cash). `buy()` retries once on `quantity-precision-mismatch`. `place_take_profit()` rests a limit sell at TP. `sell()` uses bounded limit orders with cancel-and-retry and market fallback. `get_order_status()` returns `"GONE"` for 404 (filled→history) vs `None` for network errors — callers must never treat `None` as filled.
- `premarket/scanner.py` — pre-market watchlist + at-open gap-and-go evaluation (gap band `MIN_GAP_PCT`–`MAX_GAP_PCT` + full standard confirmation). Never pre-places orders.
- `storage/database.py` manages a PostgreSQL DB (`DB_URL`) with tables: `news_signals`, `trades` (incl. `tp_order_id`), `portfolio_snapshots`, `sentiment_scores`, `premarket_candidates`, `heartbeat`, `system_events` (v17 degradation/outage markers — TD credit exhaustion, Claude outage/billing/auth, zero-trade drought; `record_system_event()` derives severity and atomically de-dupes one row per `(event_type, event_day)` via a UNIQUE index + `ON CONFLICT DO NOTHING`). All DB access goes through the `get_conn()` context manager.

### Configuration

All settings live in `config/settings.py` as a `Settings` dataclass, loaded from `.env` via `python-dotenv`. Import anywhere as:
```python
from config.settings import cfg
```
`cfg.validate()` raises `EnvironmentError` on startup if any API key is missing.

### Ticker detection and blocklist

There is no watchlist — the system relies on Benzinga tagging articles with stock tickers directly. `BLOCKLIST` in `.env` is a comma-separated list of Trading 212 instrument codes (e.g. `TSLA_US_EQ`) to permanently exclude.

### Demo vs live mode

`TRADING_MODE=demo` (default) sends orders to Trading 212's demo API. `TRADING_MODE=live` executes real trades against your Trading 212 ISA. The `cfg.is_live` property gates this in `trading/executor.py`.

### Logging

Logs go to stdout (captured by systemd journald on the VM). Each module uses `logging.getLogger(__name__)`.

### Deployment

Always deploy via `git push origin main`. GitHub Actions picks it up, rsyncs to the VM, and restarts the systemd service. **Never SSH and run commands directly** — changes would be overwritten on the next deploy.

### Test classes

- `TestExitConditions` — take-profit / stop-loss / time-stop logic (no external calls)
- `TestSentimentScoring` — `_batch_score_sentiment()` with mocked Claude responses
- `TestPositionSizing` — `calculate_quantity()` with mocked `_get`
- `TestBuyPrecisionRetry` — T212 precision-mismatch auto-retry logic
- `TestClaudeResilience` — typed Claude failure handling (529 outage → short cooldown, 403 `billing_error` → long cooldown, cooldown suppresses/lifts correctly)
- `TestTwelvedataCreditGuard` — `credits_exhausted()` daily backstop + `_claim_minute_token()` per-minute bucket both short-circuit before HTTP; `fast=` makes one attempt (no 429 backoff loop)
