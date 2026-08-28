# Getting started

This walks from an empty machine to a running system, verifying each piece
before the next. Budget about an hour, most of it waiting for provider signups.

**Nothing here trades real money.** Tapewatch defaults to demo mode and you have
to change a setting deliberately to alter that.

---

## 1. Prerequisites

- **Python 3.11 or newer**
- **PostgreSQL 14+** — local is fine
- A machine that can stay awake during market hours if you want it to trade
  unattended

```bash
python3 --version     # 3.11+
psql --version        # 14+
```

---

## 2. Install

```bash
git clone https://github.com/lazy-git-commit/tapewatch.git
cd tapewatch

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Verify before going further:**

```bash
pytest tests/ -q
```

All 563 tests should pass **with no credentials, no database and no network**.
If they do, your Python environment is correct. If they do not, fix that before
adding the complexity of live services.

---

## 3. Database

```bash
createdb tapewatch
psql tapewatch -c "CREATE USER tapewatch WITH PASSWORD 'choose-something-real';"
psql tapewatch -c "GRANT ALL PRIVILEGES ON DATABASE tapewatch TO tapewatch;"
```

Then in your `.env` (next step):

```
DB_URL=postgresql://tapewatch:choose-something-real@localhost:5432/tapewatch
```

Tables are created automatically on first run.

---

## 4. Configuration

```bash
cp .env.example .env
chmod 600 .env          # it will hold credentials
```

**Open `.env` and read it.** Every setting has a comment explaining what it
does, what happens if you change it, and — where relevant — the incident that
determined its default. It is the primary configuration reference; there is no
separate document that duplicates it.

You do not need every provider to start. The minimum to see the system run:

| Setting | Why |
|---|---|
| `DB_URL` | Storage |
| `TRADING_MODE=demo` | Already the default. Leave it. |
| Broker demo credentials | So it can place practice orders |

Without news and market-data keys the system starts, logs that it has no
sources, and does nothing — which is a perfectly good first run.

---

## 5. Provider accounts

Only the broker is strictly required. Add the rest as you want more of the
pipeline working.

| Provider | Purpose | Free tier? | Notes |
|---|---|---|---|
| **Broker** | Orders, positions, cash | Demo accounts are free | Start with a demo/paper account. Always. |
| **News source** | Breaking articles with tickers | Usually not | The signal source — without it there is nothing to trade |
| **Quote provider** | Current price | Yes, commonly | Needed for entry decisions |
| **Bars provider** | Intraday minute bars | Limited | Needed for momentum, relative volume and VWAP |
| **LLM** | Article classification | Pay per use | Costs roughly £2–3 per week at ~185 articles/day |

Not tied to any particular vendor — see [`PROVIDERS.md`](PROVIDERS.md) to use
your own. The shipped implementations are Trading 212, Benzinga (via Massive),
Finnhub, Twelve Data and Anthropic.

**Read each provider's terms.** Some restrict automated access or
redistribution, and that is your responsibility, not this project's.

---

## 6. Verify each piece

Check the parts individually before letting them run together.

```bash
# Configuration loads and validates
python -c "from config.settings import cfg; cfg.validate(); print('config OK')"

# Database reachable, schema created
python -c "from storage.database import init_db; init_db(); print('database OK')"

# Broker credentials work (demo account)
python -c "from trading.executor import get_portfolio_value; print(get_portfolio_value())"

# Quote provider works
python -c "from market.price_check import get_quote_with_fallback; print(get_quote_with_fallback('AAPL'))"
```

`cfg.validate()` is worth understanding: it refuses to start on a configuration
that is internally inconsistent — for example an entry-price tolerance wider
than the stop-loss, which would begin every trade already inside its own stop.
Those checks encode real mistakes.

---

## 7. First run

```bash
python main.py
```

On startup you will see the configuration summary, the scheduled jobs, and a
first news cycle. In demo mode, orders go to your broker's practice account.

Watch the log for the signal funnel — one line per cycle showing how many
articles were evaluated and where each was rejected. **Most signals are
rejected, and that is the system working**, not a fault. A typical day produces
a handful of candidates and zero or one trade.

Stop with `Ctrl+C`; it shuts down cleanly.

---

## 8. Monitoring (optional)

`grafana/dashboards/` contains a dashboard with 28 panels — trade history,
signal funnel, exit efficiency, classifier cost and cache behaviour, and system
events. Import it into any Grafana pointed at your database.

At minimum, watch the `system_events` table. It records outages and degradations
with severity, and it is how you learn that something broke without reading
logs.

---

## 9. Before you ever consider real money

Read [`RESULTS.md`](RESULTS.md) first. Applied to the bundled reference
strategy, the measurement tooling currently finds **no measurable edge** — and
that tooling ships with the system, so you can re-run it on anything you change.

If you intend to develop your own strategy — which is the point of a framework —
the workflow that matters is:

1. Collect signals for several weeks in demo mode.
2. Label them path-aware with `analysis/triple_barrier.py`. **Not** fixed-horizon
   forward returns; those systematically flatter anything with a stop-loss.
3. Validate out-of-sample with `analysis/validation.py`'s walk-forward.
4. Run the result through the deflated Sharpe ratio with an honest count of how
   many variants you tried.
5. Only if it survives all four, consider it real.

That sequence exists because skipping it produced two confidently wrong
conclusions in this project's own history, both documented in
[`../CHANGELOG.md`](../CHANGELOG.md).

---

## Troubleshooting

**Tests fail on a clean clone.** Almost always a Python version below 3.11.

**`cfg.validate()` raises on startup.** Read the message — it names the setting
and the constraint. These are deliberate refusals, not bugs.

**No signals ever appear.** Normal for a quiet day. Check the funnel line in the
log; if articles are being fetched and classified but everything is rejected,
the gates are doing their job. If *nothing* is fetched, check the news key.

**"No quote available" for a specific ticker.** Some instruments (OTC, foreign
listings) are not covered by every provider. After repeated misses the ticker is
suppressed for the day to avoid burning quota.

**Everything rejected as `low_momentum`.** Expected. It is the most common
rejection by a wide margin — most news does not move a stock.

---

## Getting help

- Bugs and questions: open a [GitHub issue](https://github.com/lazy-git-commit/tapewatch/issues)
- Security problems: **not** a public issue — see [`../SECURITY.md`](../SECURITY.md)
- Contributing: [`../CONTRIBUTING.md`](../CONTRIBUTING.md)
