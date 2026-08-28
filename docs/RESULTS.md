# Measured results

**Summary: the bundled reference strategy does not currently have a measurable
edge.** This document shows how that was measured and why earlier, more
favourable numbers were wrong.

Two things this is *not*. It is not a statement about the framework, which runs,
trades and reconciles as designed — it is a statement about one strategy over one
22-day window. And "no measurable edge" is not "proven to lose": the sample is
too small and covers a single market regime, so the honest reading is that the
question is open and this particular configuration has not answered it. See
[Limitations](#limitations).

It exists because a trading project that only publishes its good results is not
telling you anything. It is also the most useful part of this repository for
anyone building something similar — the errors documented here are ordinary ones
that are easy to repeat.

> **No representation is made that this software is profitable.** These figures
> are historical measurements of a reference strategy, not a forecast, not a
> recommendation, and not an inducement to do anything. See the disclaimer in
> [`../README.md`](../README.md).

---

## Headline

Measured with `analysis/triple_barrier.py` over **1,235 signals across 22
trading days**, at the live exit parameters (+5% target, −2% stop, 120-minute
time limit, 0.46pp round-trip costs):

| Catalyst class | n | Net per trade | Win rate | t | Stopped out |
|---|---|---|---|---|---|
| other | 135 | +0.135% | 46% | +0.61 | 33% |
| earnings_beat | 420 | −0.259% | 33% | −2.19 | 37% |
| contract_win | 214 | −0.270% | 34% | −1.67 | 36% |
| **guidance_raise** | 123 | **−0.388%** | 32% | −1.68 | **48%** |
| product_launch | 98 | −0.504% | 35% | −2.02 | 47% |
| fda_approval | 31 | −0.696% | 26% | −1.83 | 45% |
| **All signals** | **1,235** | **−0.264%** | 35% | **−3.82** | 38% |

*Net per trade* is after costs. *t* is a t-statistic: above +2 or below −2 means
a result is unlikely to be chance. *Stopped out* is the share of trades that
ended at the stop-loss rather than a target or timeout.

**Every tradeable class is negative.** The only positive row, `other`, has
t = +0.61 — indistinguishable from zero.

---

## It is not the exit parameters

A sweep of 48 combinations — stops from 1.5% to 5%, targets from 2% to 8%, holds
from 60 to 390 minutes — found **no profitable configuration**. The best,
+5%/−4%/60 minutes, still loses 0.165% per trade at t = −0.59.

## It is not the quality filters

Neither the classifier's confidence score nor its magnitude rating isolates a
profitable subset. Within `guidance_raise`, confidence runs the *wrong* way:
signals at confidence ≥ 0.8 return **−0.567%** (t = −2.09), which is the only
statistically significant result in the entire study — and it is negative.

## It is not latency

Re-labelling every signal with an artificial entry delay shows the outcome is
essentially flat in latency:

| Entry delay | Net per trade |
|---|---|
| 0 minutes | −0.264% |
| 3 minutes | −0.285% |
| 60 minutes | −0.294% |

Trading instantly versus a full hour late differs by **0.031 percentage
points**. Speed is not the constraint.

## What it actually is

Gross return before costs is **+0.196%**. Costs are **0.46pp**. The signal has a
small positive gross edge that transaction costs more than consume — of which
0.31pp is a currency-conversion fee charged on every round trip, unrelated to
whether the trade wins.

---

## The multiple-testing correction

Roughly 384 variants were examined across this work. `analysis/validation.py`
implements the deflated Sharpe ratio, which asks: *given that many attempts, how
good would the best one look purely by luck?*

The answer is a Sharpe of **+2.972**. The best subset actually observed scores
**+0.091**.

**Every deflated Sharpe ratio in this study is 0.000.** Nothing here survives
correction for the size of the search.

---

## The two errors that made earlier numbers look better

Both are documented in full in [`../CHANGELOG.md`](../CHANGELOG.md).

### 1. Sampling a path

An earlier measurement put `guidance_raise` at **+0.667% per trade** and a
change was shipped on it. That figure came from checking the price at 5, 15, 60
and 120 minutes and *inferring* a stop-out rate of 33%.

Walking the actual minute-by-minute path gives **−0.388%** and a **48%**
stop-out rate — which matches the live trading record almost exactly.

**Four checkpoints cannot see the dip in between, and the dip is what fills a
stop.** A stock can finish two hours higher having passed through your stop at
minute twelve. Fixed-horizon forward returns systematically flatter any strategy
with a stop-loss.

This is why `analysis/triple_barrier.py` exists, and why
[`../CONTRIBUTING.md`](../CONTRIBUTING.md) requires path-aware evidence before
any gate is relaxed.

### 2. Reporting the winner of a search

Several conclusions were drawn from "the best of the variants I tried" without
correcting for how many were tried. With ~384 variants, a strategy with no edge
at all produces a best-case Sharpe near +3 by luck alone.

This is why `analysis/validation.py` exists.

---

## Live trading record

30 closed trades in demo mode over roughly three months: **−1.62% per trade**,
21% win rate.

That sample is far too small to conclude anything on its own — but it agrees
with the path-aware measurement, and disagrees with the sampled one. When a
measurement method and live experience disagree, the method is usually wrong.

---

## Limitations

State these whenever citing any figure above:

- **22 trading days, one market regime.** This is "no *measurable* edge", not
  "proven worthless."
- **~30-day data ceiling.** The minute-bar source retains about a month, so the
  window cannot be extended without a paid data vendor.
- **234 of 1,469 signals could not be labelled** — delisted names and micro-caps
  with no data. Those skew toward the worst outcomes, so the results above are
  if anything *flattered*.
- **The labeller is deliberately pessimistic.** When one minute's range spans
  both the target and the stop, it scores the stop, because intrabar order is
  unknowable from OHLC data.
- **Costs are modelled as a flat 0.46pp**, not from real bid-ask spreads.

---

## Reproducing this

```bash
# Export signals from your own database
psql -tAF',' -c "COPY (SELECT ticker, published_at, catalyst_type, confidence,
  catalyst_magnitude FROM sentiment_scores
  WHERE sentiment='positive' AND already_moved=0) TO STDOUT WITH CSV HEADER" \
  > signals.csv

# Path-aware labelling at the live parameters
python -m analysis.triple_barrier --signals signals.csv \
  --tp 5 --sl 2 --hold 120 --group catalyst_type
```

Then run the result through `analysis.validation.deflated_sharpe_ratio` with an
honest count of how many variants you examined. The honest count is usually
larger than it feels.
