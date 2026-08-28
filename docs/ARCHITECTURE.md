# Architecture

How the pieces fit together, and why they are shaped the way they are. For the
specific thresholds and the incidents behind them, see
[`algorithm.md`](algorithm.md). For swapping any external service, see
[`PROVIDERS.md`](PROVIDERS.md).

---

## The shape of the system

Tapewatch is a set of scheduled jobs sharing a PostgreSQL database. There is no
web server, no message queue and no worker pool — a single process with a
scheduler, because the workload is a few hundred articles a day and anything
more elaborate would be complexity without purpose.

```
                    ┌──────────────────────────────────────────┐
   newswire ───────▶│  news_cycle            every 10s (fetch) │
                    │                        every 60s (rest)  │
                    │  ┌────────────────────────────────────┐  │
                    │  │ 1. fetch articles                  │  │
                    │  │ 2. pre-filter  (regex, dedup)      │  │
   LLM      ◀──────▶│  │ 3. classify    (batched, schema)   │  │
                    │  │ 4. price-confirm  ~14 gates        │  │
   market data ◀───▶│  │ 5. risk gates                      │  │
                    │  │ 6. execute                         │  │
   broker    ◀─────▶│  └────────────────────────────────────┘  │
                    └──────────────────────────────────────────┘
                                      │
                    ┌─────────────────┴────────────────────────┐
                    │  monitor_positions          every 5s     │
   broker    ◀─────▶│  stop fills, take-profit, time stop,     │
                    │  breakeven ratchet, EOD flatten,         │
                    │  broker reconciliation                   │
                    └──────────────────────────────────────────┘
                                      │
                    ┌─────────────────┴────────────────────────┐
                    │  forward_returns            nightly      │
                    │  labels every past signal for the        │
                    │  evaluation loop                         │
                    └──────────────────────────────────────────┘
                                      │
                             ┌────────┴────────┐
                             │   PostgreSQL    │
                             └─────────────────┘
```

---

## The four jobs

**`news_cycle`** — the trading pipeline. Runs at `NEWS_CYCLE_SECONDS` (10s in
production). Only the *fetch* runs at that cadence; the re-evaluation queue,
retry queue and pre-market steps are throttled to ~60s, because each
re-confirmation spends a market-data credit against a fail-closed quota. Running
everything six times as often would starve that quota and silently stop the
system trading, which is far worse than the latency it buys.

**`monitor_positions`** — exit management, every 5s. Notices broker-side stop
fills, polls the take-profit and time stop, arms the breakeven ratchet, flattens
before the close, and reconciles our view of positions against the broker's.

**`forward_returns`** — nightly. Fills forward returns for every classification
so the evaluation loop has data. This is what makes the measurement tooling
possible.

**`symbol_map_rebuild`** — daily. Refreshes broker instrument codes, which drift
as companies re-list or change tickers.

---

## Module map

| Module | Responsibility |
|---|---|
| `main.py` | Scheduler, the news cycle, portfolio risk gates, entry execution |
| `news/fetcher.py` | Article ingestion, pre-filters, LLM classification |
| `news/shadow_classifier.py` | Optional second model, fire-and-forget, never affects a trade |
| `market/price_check.py` | The gate chain — the single place a signal becomes tradeable or not |
| `market/*_bars.py`, `market/*_quotes.py` | Market data providers |
| `market/sessions.py` | Which trading session we are in, calendar-aware |
| `trading/executor.py` | Broker API: orders, positions, cash, symbol mapping |
| `monitor/position_monitor.py` | Exit management and reconciliation |
| `storage/database.py` | All persistence; every access goes through one connection manager |
| `analysis/triple_barrier.py` | Path-aware labelling — what a real trade would have done |
| `analysis/validation.py` | Deflated Sharpe ratio, walk-forward with embargo |
| `analysis/forward_returns.py` | The nightly evaluation loop |
| `premarket/scanner.py` | Overnight-catalyst watchlist and at-open evaluation |

---

## Five principles that explain most decisions

### 1. Fail closed

If the system cannot verify something, it does not trade. No market data means
no confirmation means no position. Every degradation path ends in "skip this
signal", never "assume and proceed."

The one deliberate exception is the drawdown breaker, which fails *open* — a
missing equity snapshot is an observability gap, not evidence of loss, and
halting on it once took the system offline for 44 consecutive cycles.

### 2. Transient and terminal rejections are different

A signal rejected because the tape has not moved *yet* is not the same as one
rejected because the stock is untradeable. Transient rejections go to a
re-evaluation queue and get another look for 15 minutes; terminal ones are
final. Conflating them discarded genuine catalysts that simply arrived before
the market reacted.

### 3. The loss side rests at the broker

The stop-loss is placed with the broker the moment the buy fills, so it executes
with zero polling latency and survives this process dying. The profit side is
polled, because being slightly late to a gain costs far less than being slightly
late to a loss.

### 4. Every rejection has a code

There is no anonymous "signal rejected." Each carries a machine-readable reason,
stored on the row, so the funnel is queryable and a change in behaviour is
visible rather than inferred. Most of this project's diagnoses started with a
`GROUP BY rejection_code`.

### 5. Observability is not optional

Outage detection with severity routing, heartbeats per job, one row per API call
including failures, and excursion tracking on every position. Several serious
faults here were silent for days precisely because the instrumentation for them
did not exist yet — each one is now a permanent counter or event.

---

## Data flow for one trade

1. An article arrives, tagged with a ticker.
2. Cheap deterministic filters run first — freshness, digests, recaps, analyst
   noise — because they are free and an LLM call is not.
3. Survivors are classified in one batched, schema-forced call. **Every**
   classification is stored, including ones that will never trade, because that
   store is the evaluation dataset.
4. Positive, tradeable-class, not-already-moved signals go to price confirmation:
   roughly fourteen gates in a deliberate order, cheapest and most decisive
   first.
5. Survivors hit the portfolio risk gates — kill switch, drawdown, losing
   streak, position caps, per-ticker cooldown.
6. Execution places a limit buy with a price ceiling, then immediately rests a
   stop at the broker.
7. The monitor manages the exit and records the best and worst unrealised
   excursion, so exit quality can be measured later.
8. That night, forward returns are computed for the signal — whether it traded
   or not.

Steps 3 and 8 are what make the system able to evaluate itself. Most of the
value in this repository is in that loop rather than in any particular
threshold.

---

## Deliberate non-goals

- **Not low latency.** We are minutes behind the wire and measurement shows that
  costs ~0.03pp per trade. Competing on speed would require co-location and a
  direct feed, and would still lose.
- **Not multi-asset.** US equities only. Options and futures would multiply the
  surface area without addressing anything measured as a constraint.
- **Not multi-tenant.** One operator, one broker account. Serving others is a
  different product with regulatory consequences.
- **Not a signal service.** It is a framework. The bundled strategy is a
  reference implementation, and a currently unprofitable one — see
  [`RESULTS.md`](RESULTS.md).
