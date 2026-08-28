# Tapewatch

**A news-catalyst trading framework — and the measurement tooling to find out
whether a strategy actually works.**

Tapewatch watches a live newswire, classifies every article with an LLM, confirms
the market agrees, and executes through a broker API. It is a complete,
production-hardened event-driven trading system: outage detection, broker
reconciliation, fail-closed data handling, 563 tests.

It also ships the part most trading projects leave out: tooling that measures,
honestly, whether a strategy makes money. Pointed at the **bundled reference
strategy** it currently reports no measurable edge — which is the tooling doing
its job, and the reason those numbers are published rather than quietly dropped.
Whether a strategy *you* build with it does better is an open question, and the
same tooling is there to answer it.

---

## ⚠️ This is research software

**Tapewatch executes real trades against real brokerage APIs.** It is published
for research and educational purposes.

**It is not investment advice.** Nothing here is a recommendation to buy or sell
any security. **No representation is made that it is profitable.** Measured over
22 trading days in a single market regime, the maintainers' own path-aware
measurements of the *bundled reference strategy* show no measurable edge; the
method and its limits are set out in [`docs/RESULTS.md`](docs/RESULTS.md) so you
can check them yourself. That is a result for one strategy over one window — not
a forecast, and not a claim about anything you build.

Trading involves risk of loss. You are solely responsible for any use of this
software, including any financial loss. **It ships in demo mode**; changing that
is a deliberate act with consequences that are yours.

Provided "AS IS" without warranty of any kind. See [`LICENSE`](LICENSE).

---

## Why this exists

Most retail algorithmic trading projects show you an equity curve. This one
ships the tooling that invalidates equity curves.

Twice during development a change was shipped on evidence that later turned out
to be wrong — and the modules in `analysis/` are what caught it both times:

- A strategy change was justified by **+0.667% per trade**, measured by sampling
  prices at fixed horizons. Re-measured by walking the actual minute-by-minute
  path a trade would take, the same signals came back at **−0.388%**. The
  sampling had estimated a 33% stop-out rate; the real rate was 48%.
- Prompt caching was believed to be working for over a thousand API calls. It
  had never engaged once — the cached prefix sat below the model's minimum
  length, so the API silently ignored the request and returned success anyway.

If you are building anything similar, the measurement modules are probably more
useful to you than the strategy.

---

## What's in the box

| Component | What it does |
|---|---|
| **News ingestion** | Polls a newswire, de-duplicates, and filters recaps, digests and analyst noise *before* spending an LLM call |
| **LLM classification** | 14-class catalyst taxonomy with schema-forced output, confidence, magnitude and an `already_moved` judgement. Optional shadow model for provider comparison |
| **Price confirmation** | ~14 sequential gates — quote freshness, liquidity, spread, momentum, relative volume, VWAP, extension, exhaustion — each with an explicit rejection code |
| **Risk gates** | Daily-loss kill switch, drawdown circuit breaker, losing-streak cooldown, position and trade caps, per-ticker cooldown |
| **Execution** | Limit entries with a price ceiling, broker-resting stop-loss, breakeven ratchet, bounded-limit exits with market fallback, broker reconciliation |
| **Measurement** | Triple-barrier labelling, deflated Sharpe ratio, walk-forward validation with embargo, nightly forward-return eval loop |
| **Operations** | Outage detection with severity routing, heartbeats, Grafana dashboards, 563 tests with mutation testing on critical paths |

---

## Quick start

Tapewatch needs **Python 3.11+**, **PostgreSQL**, and API credentials for the
providers you choose. Everything runs in **demo mode** by default.

```bash
git clone https://github.com/lazy-git-commit/tapewatch.git
cd tapewatch

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # every setting is documented inline
                                   # in that file — start there

pytest tests/ -q                   # 563 tests, no credentials needed
python main.py                     # starts in demo mode
```

**New here?** [`docs/GETTING-STARTED.md`](docs/GETTING-STARTED.md) walks through
provider signup, the minimum viable configuration, and how to verify each piece
works before letting anything trade.

---

## Swapping providers

Every external service sits behind a small, documented contract. To use a
different news source, quote provider or broker you implement a handful of
functions in a new module and point one environment variable at it — the trading
logic does not change.

[`docs/PROVIDERS.md`](docs/PROVIDERS.md) specifies each contract precisely, with
a worked example.

---

## Developing a strategy

The bundled strategy is a reference implementation, not a recommendation. If you
want to develop your own — which is the point of a framework — the sequence that
matters is:

1. **Collect.** Run in demo mode for several weeks. Every classification is
   stored whether or not it traded, so the dataset builds itself.
2. **Label path-aware.** `analysis/triple_barrier.py` walks each signal minute by
   minute and records which exit it would have hit **first**. Do not use
   fixed-horizon returns — checking the price at 5, 15 and 60 minutes cannot see
   the dip in between, and the dip is what fills a stop. Here, those two methods
   gave opposite conclusions on the same signals.
3. **Test out-of-sample.** `analysis/validation.py` runs walk-forward validation
   with an embargo: fit on earlier data, test on later data you never looked at,
   with a gap between the two so information cannot leak backwards.
4. **Correct for how much you searched.** The same module computes the *deflated
   Sharpe ratio* — Sharpe is return per unit of risk, and the deflated version
   asks how good the best of N attempts would look **by luck alone**.

**Tune freely, but judge the result at step 4, not at step 2.** For calibration:
a sweep of 48 exit-parameter combinations on the bundled strategy found none
profitable, and across roughly 384 variants the luckiest-by-chance result would
score about **+3.0** while the best actually observed scored **+0.09**. A number
that looks good after enough attempts usually isn't one.

That is the honest offer here: not a profitable strategy, but a system that runs
unattended and the means to find out whether what you feed it has an edge.

---

## Documentation

| Document | Contents |
|---|---|
| [Getting started](docs/GETTING-STARTED.md) | Setup from zero, provider signup, verification steps |
| [Architecture](docs/ARCHITECTURE.md) | How the pieces fit together, and why |
| [Algorithm](docs/algorithm.md) | Every filter and threshold, with the incident that motivated it |
| [Providers](docs/PROVIDERS.md) | Contracts for adding your own data sources and brokers |
| [Results](docs/RESULTS.md) | What the measurement tooling found, with the method and its limits |
| [Database schema](docs/database_schema.md) | Tables, columns, and what each is for |
| [API reference](docs/api_reference.md) | External API usage and quirks |
| [Changelog](CHANGELOG.md) | Every change, why it was made, and the incident behind it |

The changelog is worth reading on its own — it documents eighteen production
incidents and their root causes, including several where a previous conclusion
was overturned by later evidence.

---

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md)
first: it covers development setup, testing expectations (including mutation
testing for critical paths), and the contributor licence agreement, which is
signed automatically on your first pull request.

Security issues should **not** be raised as public issues — see
[`SECURITY.md`](SECURITY.md).

---

## Licence

Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE)
and [`NOTICE`](NOTICE).

Copyright 2026 **ParallaxTech Ltd and the Tapewatch contributors**. Contributors
keep the copyright in their own work — see [`CLA.md`](CLA.md).

Maintained by ParallaxTech Ltd. If this is the kind of engineering you need,
get in touch: `info@parallaxtech.co.uk`.
