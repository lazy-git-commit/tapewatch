# Momentum Trader

A news-driven momentum trading system for your Trading 212 Stocks ISA.

## How it works

```
Benzinga news (via massive.com) — breaking US equity news with tickers
       ↓  pre-filters (before Claude API call):
          • freshness: article must be <60s old at fetch time
          • crypto tickers stripped (X:BTCUSD etc. are not equities)
          • roundup articles skipped (>3 tickers = market digest, no catalyst)
          • T212 symbol map: Benzinga shortName → correct T212 ticker code
Claude Haiku — expert momentum day trader classifier
                     earnings beats / FDA / M&A / contract wins → positive (0.8–1.0)
                     analyst PT raises / "Maintains" ratings    → neutral  (ignored)
                     earnings misses / guidance cuts            → negative (ignored)
       ↓
Price confirmation — Finnhub quote + Twelvedata fallback/bars
                     blocks first 5 minutes after open (auction noise)
                     price ≥ $5 and not already extended vs prev close
                     timestamp-based momentum + time-normalized RVOL
                     ADV liquidity floor + spread proxy
                     VWAP confirmation (is the stock being accumulated?)
       ↓
Buy order (Trading 212 API — demo or live)
  auto-retries once if T212 rejects for quantity precision mismatch
       ↓
Position monitor (every 20s) — Finnhub quote + Twelvedata fallback
  → Resting take-profit limit (+5%)  ✅
  → Stop loss bounded-limit (-2%)    ❌
  → Time stop (60min)                ⏱️
  → EOD flatten before close
       ↓
Trade logged to PostgreSQL (tagged demo or live)
       ↓
Grafana dashboard — live activity and history
```

Polls every minute around the clock. Skips cycles outside NYSE market hours
(Mon–Fri, 13:30–20:00 UTC = 09:30–16:00 ET). Holidays and early closes are
handled automatically via `pandas_market_calendars` — no manual configuration needed.

---

## Setup

### 1. Prerequisites

- Python 3.12
- A [Trading 212](https://www.trading212.com) account with a Stocks ISA (demo or live)
- A [massive.com](https://massive.com) account with Benzinga news subscription
- A [Finnhub](https://finnhub.io) API key (free tier is sufficient)
- An [Anthropic](https://console.anthropic.com) API key for Claude Haiku sentiment
- A VM or server to deploy to (Linux recommended)

### 2. Install dependencies

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure your environment

```bash
cp .env.example .env
# Fill in your API keys
```

Key settings in `.env`:

| Variable | Default | Notes |
|---|---|---|
| `TRADING_MODE` | `demo` | **Keep as `demo` until confident** |
| `BLOCKLIST` | `` | Comma-separated Trading 212 codes to never trade (e.g. `TSLA_US_EQ`) |
| `MIN_PRICE_MOVE_PCT` | `0.2` | Dead-tape floor; VWAP/RVOL do the real confirmation |
| `MOMENTUM_LOOKBACK_MINUTES` | `5` | Timestamp-based momentum lookback |
| `MAX_DAY_DROP_PCT` | `3.0` | Reject if stock is down more than this % vs previous close |
| `MAX_DAY_MOVE_PCT` | `25.0` | Reject if the day move is already exhausted |
| `MIN_RVOL` / `MAX_RVOL` | `1.5` / `20.0` | Time-of-day-normalized participation band |
| `REQUIRE_VWAP_CONFIRMATION` | `true` | Require price to hold at/above session VWAP |
| `MAX_POSITION_SIZE_PCT` | `5.0` | Max % of portfolio per trade |
| `RISK_PER_TRADE_PCT` | `0.25` | Position risk budget if stop is hit |
| `MAX_DAILY_LOSS_PCT` | `2.0` | Daily kill switch on realized losses |
| `TAKE_PROFIT_PCT` | `5.0` | Sell when up this % |
| `STOP_LOSS_PCT` | `2.0` | Sell when down this % |
| `TIME_STOP_MINUTES` | `60` | Sell after this many minutes regardless |
| `EOD_FLATTEN_MINUTES` | `10` | Force-close before the bell |

### 4. Run the tests

```bash
pytest tests/ -v
```

### 5. Start the trader

```bash
python main.py
```

You'll see live output like:
```
INFO  __main__ — ── News cycle starting ──────────────────────────────────
INFO  news.fetcher — Benzinga: 12 article(s) fetched → 3 eligible → 2 positive ticker signal(s)
INFO  __main__ — Signal [AAPL_US_EQ] 85% confidence: Apple announces record quarter
INFO  market.price_check — Price check [AAPL]: recent=+1.8% day=+2.3% volume=2.1× — approved
INFO  trading.executor — BUY executed: AAPL_US_EQ × 2.381000 | order_id=...
```

### 6. View performance report

```bash
python -m reporting.report
```

---

## Deployment

The system deploys automatically to a Linux VM via GitHub Actions on every push to `main`.

### Required GitHub Secrets

| Secret | Description |
|---|---|
| `DEPLOY_HOST` | VM the private network IP (after setup) |
| `DEPLOY_USER` | SSH user (e.g. `root`) |
| `DEPLOY_SSH_KEY` | SSH private key |
| `NETWORK_AUTH_KEY` | the private network auth key |
| `TRADING212_API_KEY` | Trading 212 live API key |
| `TRADING212_API_KEY_ID` | Trading 212 live API key ID |
| `TRADING212_DEMO_API_KEY` | Trading 212 demo API key |
| `TRADING212_DEMO_API_KEY_ID` | Trading 212 demo API key ID |
| `MASSIVE_BENZINGA_API_KEY` | Benzinga news key from massive.com |
| `FINNHUBIO_API_KEY` | Finnhub API key |
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Haiku |
| `DASHBOARD_ADMIN_PASSWORD` | Grafana admin password |

### Useful commands on the VM

```bash
# Live log stream
journalctl -u momentum_trader -f

# Check service status
systemctl status momentum_trader

# View trade history
psql postgresql://<db-user>:<db-password>@localhost:5432/momentum_trader -c "SELECT mode, ticker, buy_price, sell_price, profit_loss_pct, status FROM trades"

# Performance report
cd /opt/tapewatch && .venv/bin/python -m reporting.report
```

### Grafana dashboard

Grafana runs on port 3000 of your VM. Open `http://<VM-IP>:3000` (default login: `admin` / `admin`).

The dashboard shows open trades, win rate, total P&L, trade history, and signal history. Use the **Mode** dropdown to switch between demo and live views.

To open the firewall port if needed:
```bash
firewall-cmd --permanent --add-port=3000/tcp && firewall-cmd --reload
```

---

## Going live ⚠️

Only switch `TRADING_MODE=live` when:

- [ ] You have run in demo mode for **at least 2–4 weeks**
- [ ] Your win rate in demo is positive and consistent
- [ ] You understand every line of the code
- [ ] You are comfortable with the maximum loss per trade
- [ ] You have reviewed Trading 212's API terms of service

This software is provided as-is. You are responsible for all trading decisions and outcomes. Always consult a financial adviser before deploying real capital.

---

## Extending the system

**Block a company**: Add its Trading 212 instrument code to `BLOCKLIST` in `.env` (e.g. `TSLA_US_EQ`).

**Tune your strategy**: All parameters are in `.env`. Start conservative and adjust after 20–30 trades.

**Add notifications**: In `monitor/position_monitor.py`, add an email/SMS alert after a sell executes or fails.
