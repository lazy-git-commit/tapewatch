# Momentum Trader

A news-driven momentum trading system for your Trading 212 Stocks ISA.

## How it works

```
Benzinga WIIM (Why Is It Moving) — stock already moving with known catalyst
Benzinga News — broad market news for early signals
       ↓
Price confirmation (yfinance) — price up ≥ 1.5% + volume spike?
       ↓
Buy order (Trading 212 API — demo or live)
       ↓
Position monitor (every 60s)
  → Take profit (+5%)  ✅
  → Stop loss  (-2%)   ❌
  → Time stop  (60min) ⏱️
       ↓
Trade logged to SQLite (tagged demo or live)
       ↓
Grafana dashboard — live activity and history
```

Only runs during US market hours (Mon–Fri, 14:30–21:00 UTC).

---

## Setup

### 1. Prerequisites

- Python 3.12
- A [Trading 212](https://www.trading212.com) account with a Stocks ISA (demo or live)
- A [Benzinga](https://www.benzinga.com/apis/) API key (News + WIIM)
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
| `DEMO_PORTFOLIO_VALUE` | `500.0` | Simulated balance for position sizing in demo mode (GBP) |
| `BLOCKLIST` | `` | Comma-separated Trading 212 codes to never trade (e.g. `TSLA_US_EQ`) |
| `MIN_PRICE_MOVE_PCT` | `1.5` | Price must be up this % from open to confirm a signal |
| `MAX_POSITION_SIZE_PCT` | `5.0` | Max % of portfolio per trade |
| `TAKE_PROFIT_PCT` | `5.0` | Sell when up this % |
| `STOP_LOSS_PCT` | `2.0` | Sell when down this % |
| `TIME_STOP_MINUTES` | `60` | Sell after this many minutes regardless |

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
INFO  __main__ — News cycle starting
INFO  news.fetcher — Benzinga WIIM: 3 signals
INFO  news.fetcher — Benzinga News: 8 articles matched
INFO  market.price_check — Price check [AAPL]: +2.1% move, 2.3× volume — confirmed=True
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
| `DEPLOY_HOST` | VM public IP |
| `DEPLOY_USER` | SSH user (e.g. `root`) |
| `DEPLOY_SSH_KEY` | SSH private key |
| `TRADING212_API_KEY` | Trading 212 API key |
| `BENZINGA_API_KEY` | Benzinga API key |

### Useful commands on the VM

```bash
# Live log stream
journalctl -u momentum_trader -f

# Check service status
systemctl status momentum_trader

# View trade history
sqlite3 /opt/tapewatch/trader.db "SELECT mode, ticker, buy_price, sell_price, profit_loss_pct, status FROM trades"

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
