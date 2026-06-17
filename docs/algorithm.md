# How the Algorithm Works

This is the authoritative description of the trading algorithm as of **v15**.
Every filter exists because a specific, named loss demonstrated the need for it —
those incidents are cited inline. When changing any rule, update this document
and `CHANGELOG.md` in the same commit.

---

## 1. The strategy in one paragraph

The system trades **news-driven intraday momentum** on US equities. It watches
Benzinga for breaking articles, asks Claude whether each article is a genuine
buy-now catalyst, confirms with live price/volume data that real buyers are
acting on the news, and then takes a small, liquidity-capped long position with
a bracket exit: resting +5% take-profit, polled −2% stop-loss, 60-minute time
stop, and a hard end-of-day flatten. A separate pre-market pipeline catches
overnight catalysts (earnings, FDA, M&A) and trades them at the open with
gap-and-go confirmation.

**The edge hypothesis:** a real catalyst causes continued buying for minutes to
hours after publication; most "news" does not. The system's job is almost
entirely *rejection* — of recaps, halts, analyst noise, illiquid names, and
moves that already happened.

**The structural latency budget:** publication → fetch (0–60s poll) → Claude
(~3–5s) → price checks (~2s) → order. The system is 10–90 seconds late by
construction. Everything in the design biases toward catalysts that survive
being a minute late (earnings, FDA, M&A targets) and away from those that
don't (micro-cap halt spikes).

### Research map: why these rules are defensible

This is not a generic "buy green candles" bot. The live rules are mapped to
well-documented effects and professional execution discipline:

| System rule | Research / practitioner basis | Implementation |
|---|---|---|
| Trade only fresh, material catalysts | Post-earnings-announcement drift (Ball & Brown; Bernard & Thomas) and cross-sectional momentum (Jegadeesh & Titman) show that markets can underreact to new information. AQR's Asness/Frazzini work also stresses that momentum is real but implementation-sensitive. | Claude catalyst taxonomy + confidence gate + `already_moved` filter |
| Demand price/volume confirmation | Momentum is fragile around reversals; volume and intraday liquidity patterns matter. Admati & Pfleiderer's intraday-volume work supports time-of-day-aware volume interpretation. | timestamp momentum, RVOL normalized to the intraday U-curve, VWAP confirmation |
| Avoid crowded/halt/illiquid moves | Momentum profits are eroded by transaction costs and can crash; Korajczyk & Sadka, Barroso & Santa-Clara, and Daniel & Moskowitz motivate liquidity filters and hard risk brakes. | ADV floor, ADV participation cap, RVOL ceiling, max day-move and max 5-min move ceilings |
| Model execution as a first-class risk | Almgren-Chriss / implementation-shortfall practice says market impact and timing risk are part of the trade, not an afterthought. | resting TP, bounded stop exits, EOD market flatten, slippage-aware backtest |
| Do not optimize by vibes | López de Prado and related backtest-overfitting work warns that repeated historical tuning can manufacture false edge. | `sentiment_scores` eval loop, DB replay, explicit costs, documented rule rationale |

Useful references:
- Narasimhan Jegadeesh & Sheridan Titman, "Returns to Buying Winners and Selling Losers" (Journal of Finance, 1993): https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- Victor Bernard & Jacob Thomas, "Post-Earnings-Announcement Drift" (Journal of Accounting Research, 1989): https://doi.org/10.2307/2491062
- Clifford Asness, Andrea Frazzini, Ronen Israel & Tobias Moskowitz, "Fact, Fiction, and Momentum Investing" (Journal of Portfolio Management, 2014): https://doi.org/10.3905/jpm.2014.40.5.075
- Robert Korajczyk & Ronnie Sadka, "Are Momentum Profits Robust to Trading Costs?" (Journal of Finance, 2004): https://doi.org/10.1111/j.1540-6261.2004.00656.x
- Pedro Barroso & Pedro Santa-Clara, "Momentum Has Its Moments" (Journal of Financial Economics, 2015): https://doi.org/10.1016/j.jfineco.2014.11.010
- Kent Daniel & Tobias Moskowitz, "Momentum Crashes" (Journal of Financial Economics, 2016): https://doi.org/10.1016/j.jfineco.2016.07.002
- Robert Almgren & Neil Chriss, "Optimal Execution of Portfolio Transactions" (Journal of Risk, 2000/2001): https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf
- Anat Admati & Paul Pfleiderer, "A Theory of Intraday Patterns" (Review of Financial Studies, 1988): https://doi.org/10.1093/rfs/1.1.3
- Peter Carr & Marcos López de Prado, "Determining Optimal Trading Rules without Backtesting" (2014): https://arxiv.org/abs/1408.1159

Important boundary: these papers support the *class* of effects and controls,
not a guaranteed profitable parameter set. The only honest answer for this
specific implementation is continual out-of-sample measurement: live/demo
trades, forward returns on all classified articles, and costed replay.

---

## 2. Pipeline overview

```
                        ┌────────────────────────────────────────────┐
 every 60s              │  news_cycle (main.py)                      │
┌──────────┐  closed +  ├────────────────────────────────────────────┤
│ Scheduler ├─premarket→│ premarket_scan → watchlist (DB)            │
└──────────┘  window    │                                            │
      │       market    │ 1. evaluate_premarket_candidates (≤30 min  │
      │       open  ──→ │    after open: gap gate + confirmation)    │
      │                 │ 2. retry queue (transient data failures)   │
      │                 │ 3. fetch_all_news → Claude → trade gates   │
      │                 │ 4. per signal: cooldown → price check →    │
      │                 │    risk gates → buy → resting TP           │
      │                 └────────────────────────────────────────────┘
      │
      │ every 20s       ┌────────────────────────────────────────────┐
      └────────────────→│ monitor_positions (position_monitor.py)    │
                        │  resting-TP fill? → record take_profit     │
                        │  stop-loss / time-stop → cancel TP → sell  │
                        │  close−10min → EOD flatten (everything)    │
                        └────────────────────────────────────────────┘

 nightly 22:30 UTC      forward_returns — fills 5/15/60-min returns for
                        every Claude classification (the eval loop)
 daily 08:00 UTC        symbol_map_rebuild — refresh T212 ticker map
```

---

## 3. Stage 1 — News ingestion (`news/fetcher.py`)

### 3.1 Pre-filters (before any Claude call, in order)

| Filter | Rule | Why |
|---|---|---|
| Crypto | `X:`-prefixed tickers stripped | Not equities; Finnhub/T212 can't trade them |
| Freshness | older than 60s dropped (RTH) | We poll every 60s; older news was seen or missed. Acting late buys reversals (GOOG re-index incident, v8) |
| Blocklist | `BLOCKLIST` env tickers dropped | Manual permanent exclusions |
| Dedup | `(article_id, ticker)` already in DB | Never re-score the same pair |
| Roundup | >3 tickers tagged → skip | Market digests have no per-stock catalyst (v11) |

### 3.2 Claude classification

Claude Haiku (`claude-haiku-4-5`) scores all eligible articles in **one
batched call** per cycle:

- **`temperature=0`** — classification, not generation.
- **System prompt with `cache_control`** — the rubric is static, the cycle runs
  every 60s, and the prompt cache TTL is 5 min, so the rubric is a cache hit on
  every call after the first.
- **Forced tool use** (`tool_choice`) — output is schema-validated JSON; no
  string parsing, no truncation recovery.
- The rubric is a **decision tree**: (1) is this NEW information, or a
  recap/halt article describing a move that already happened? (2) is the tagged
  ticker the actual subject (acquirer-vs-target)? (3) is the catalyst binding
  and material (LOI/MOU → neutral, offerings → negative)? (4) is the company
  small enough to move? — plus few-shot examples.

Each article gets four fields: `sentiment`, `confidence` (0–1),
`catalyst_type` (14-class taxonomy), `already_moved` (bool).

**Every classification — positive, neutral, negative — is persisted to
`sentiment_scores`** for the eval loop (§9). The model classifies; code decides
what trades.

### 3.3 Trade gates (code, not model)

A positive classification only becomes a tradeable signal if **all** pass:

1. **Confidence** ≥ `MIN_SENTIMENT_CONFIDENCE` (default 7/10). This setting
   existed since v1 but was never enforced until v14.
2. **Catalyst class** in `TRADEABLE_CATALYSTS` (default: earnings_beat,
   guidance_raise, fda_approval, ma_target, contract_win, product_launch,
   short_squeeze). Halt/recap/analyst/acquirer/offering classes are recorded
   but never traded — they are the classes that don't survive our latency.
3. **`already_moved` is false** — the model's own judgement that the move
   pre-dates the article.

---

## 4. Stage 2 — Price confirmation (`market/price_check.py`)

Data sources: **quote with fallback** — Finnhub `/quote` (current price, open,
**previous close**), falling back to Twelvedata `/quote` when Finnhub has no
coverage; Twelvedata 1-min bars (momentum baseline by timestamp, spread proxy);
Twelvedata daily bars (20-day ADV, prev-close backup); Twelvedata session VWAP.

**Symbol hygiene (v15):** Benzinga tags carry routing cruft that breaks every
downstream consumer. `clean_benzinga_symbol()` (in `trading/executor.py`) drops
foreign-exchange-prefixed tags entirely (`TSX:MDA` → None — not US-tradeable)
and strips Benzinga's collision-disambiguation digit (`INBX1` → `INBX`,
`SAIL1` → `SAIL`). On 2026-06-15 these uncleaned tags reached the price check,
got no Finnhub quote, and burned 30-minute pre-market eval windows.

**Quote fallback (v15):** Finnhub's free tier silently omits many small caps and
recent IPOs — exactly the catalysts this strategy targets (2026-06-15:
CUPR/ELAN/WBD/INBX/SAIL all had no Finnhub quote, all priced fine on
Twelvedata). `get_quote_with_fallback()` tries Finnhub, then Twelvedata
`/quote`; both return the same `c`/`o`/`pc` keys so callers are source-agnostic.
Only when BOTH miss is a signal deemed unpriceable.

**Market-open detection (v15.5):** `is_market_open()` uses
`_NYSE.open_at_time(sched, now_utc)` rather than a manual `market_open <=
now_utc < market_close` comparison. The manual approach is fragile: on
2026-06-17 a long-running pmc calendar object (process started at midnight)
had stale DST state — in summer (EDT = UTC-4) it evaluated the NYSE open as
14:30 UTC instead of 13:30 UTC. The system spent the entire 13:30–14:29 UTC
window scanning for pre-market news instead of evaluating the watchlist.
`open_at_time()` re-derives open/close from first principles on each call and
is immune to this stale-state problem.

**Previous-close backfill (v15.2):** Finnhub being reachable is not enough to
trust its `pc` (previous close). In the first minutes after the open Finnhub's
free tier routinely returns `pc=0` before its daily rollover settles (2026-06-16:
OTLK/SLP/SPCB all had a valid Finnhub price but `pc=0` at 09:30 ET). Because the
gap gate and the dead-cat/extended-move filters all measure vs prev close, a
missing `pc` made `day_change_pct` `None`, which **terminally rejected every
pre-market candidate as "no prev close"** — the real reason zero gap-and-go
trades fired despite multiple genuine catalysts. `get_quote_with_fallback()` now
backfills `pc` from Twelvedata whenever Finnhub's `pc` is ≤ 0, keeping Finnhub's
(good) real-time price. The pre-market evaluator also treats a still-missing prev
close as a **transient, retryable** condition (stay pending, retry within the
30-min window) rather than a verdict — same handling as `opening_block`.

Checks run cheapest-first; each rejection records a `reason_code`:

| # | Code | Rule (defaults) | Motivating incident |
|---|---|---|---|
| 1 | `opening_block` | < 5 min after open | GOAI: entire spike in 09:30 bar, bought 09:32 into collapse |
| 2 | `penny_stock` | price < **$5** | Every Jun 8–11 loss was sub-$5 |
| 3 | `wide_spread` | last-bar range > 3% of price | No bid/ask feed; bar range proxies effective spread |
| 4 | `dead_cat` | < −3% vs **prev close** | Prev close (not open) so gap-downs count: a stock down 25% overnight but flat since open is still a falling knife |
| 5 | `extended_move` | > +25% vs **prev close** | Closes the v13 hole: stock up 80% on the day but flat in the last 5 min passed the 5-min ceiling |
| 6 | `illiquid` | 20-day ADV × price < **$5M** | **ADV-based on purpose**: spike-day volume explodes and would pass exactly the halt patterns this blocks. Exit slippage depends on the NORMAL book (GOAI: $390k ADV → −18.99% stop fill) |
| 7 | `low_momentum` | < +0.2% over ~5 min (v15: dead-tape noise floor only) | Just rejects "the catalyst moved nothing"; VWAP does the real work (step 10) |
| 8 | `high_momentum` | > +15% over ~5 min | Post-halt spike — halt articles publish AFTER the 30–120% pop. Runs before VWAP to save a credit |
| 9 | `low_volume` / `high_volume` | RVOL outside [1.5, 20] | See RVOL section |
| 10 | `below_vwap` | price < session VWAP (− small tol) | v15: size-neutral accumulation test — see below |

### Momentum confirmation: why VWAP, not a fixed % (v15)

The v14 fixed momentum floor (+1.5% over 5 min) was the strategy's binding
constraint — **1,077 of all-time rejections were `low_momentum`**, and on
2026-06-15 *every* genuine large-cap catalyst was rejected at near-zero
5-minute change: DXCM (FDA) +0.14%, SNY (FDA) +0.07%, LLY (product) +0.01%.

The reason is structural, not a bug: **a deep order book reprices slowly.** A
real catalyst on a $50B+ name is absorbed by liquidity and drifts over hours
(post-earnings-announcement drift), where a micro-cap with the same news jumps
several percent in seconds. **No single % threshold can serve both** a $2
micro-cap and a $1000 mega-cap.

The research-backed fix is to confirm with **VWAP-relative position**, which is
*size-neutral*:

- **VWAP** (volume-weighted average price, accumulated from the open) is the
  intraday "fair value" line institutions benchmark against.
- A stock **held at or above VWAP** is being *accumulated* — net institutional
  buying — regardless of whether its 5-min % change is +0.2% or +5%.
- A stock **below VWAP** is being *distributed* (the classic "gap-and-crap":
  gap up at the open, fade all day) — regardless of % change.

So v15 replaces the fixed floor with: a tiny **dead-tape noise floor** (0.2%,
just "did it move at all"), the unchanged **post-halt ceiling** (15%), the
**RVOL band** (participation), and finally the **VWAP gate** (accumulation).
VWAP runs last because it costs an extra Twelvedata credit (a full-session bar
pull); every cheaper gate filters first.

**Research basis:**
- Post-earnings-announcement drift: large-caps price the immediate surprise
  fast then drift; the tradeable signal is direction/accumulation, not the
  magnitude of the first 5-minute candle.
- Practitioner VWAP-reclaim / catalyst playbooks: "RVOL ≥ 1.5–2× is the proxy
  for whether a stock is in play"; "enter on the first candle that closes back
  above VWAP" — confirmation by *structure* (VWAP) rather than a fixed % move.
- Sources: [PEAD (Wikipedia)](https://en.wikipedia.org/wiki/Post%E2%80%93earnings-announcement_drift),
  [VWAP momentum strategy playbook](https://www.snappchart.app/blog/strategy-playbooks/vwap-momentum-trading-strategy),
  [volume confirmation for entries](https://www.quantvps.com/blog/using-volume-analysis-to-confirm-trade-entries-and-exits).

### RVOL — time-of-day normalized relative volume

```
rvol = today's cumulative volume
       / (20-day ADV × expected fraction of a day's volume traded by now)
```

The expected fraction follows the intraday U-curve (~16% by 10:00, 50% by
13:00, 100% at close), linearly interpolated. Without this, "1.5× the full-day
average" is nearly impossible at 10:00 and trivial at 15:45 — the old raw
ratio was a different filter at every hour of the day. RVOL ≈ 1.0 always means
"a normal day so far". The 20× ceiling is the halt-pattern signature
(parabolic participation on micro-caps).

### Momentum baseline honesty

Bars are selected **by timestamp**, not array index: thin stocks skip minutes,
so "bar #5" could silently be 20 minutes old, stretching the momentum window
per-stock. The baseline is the newest bar at least `MOMENTUM_LOOKBACK_MINUTES`
old, with a 10-minute staleness guard on the freshest bar (the VECO incident:
a bar from 09:56 served at 11:42 produced a false +1.20% momentum reading).

---

## 5. Stage 3 — Risk gates (`main.py`)

Checked before every entry and re-checked after every fill:

| Gate | Default | Why |
|---|---|---|
| **Daily kill switch** | stop entries after realized −2% of portfolio in a day | The one control that keeps a bad day from becoming a dead account. Fail-CLOSED: if today's P&L can't be verified, stand down |
| Max open positions | 3 | Momentum signals cluster: one macro headline produced 4 correlated semi trades in 2 minutes (Jun 3) |
| Max trades/day | 10 | Overtrading brake; a day that needs >10 attempts is a day the filters are wrong |
| 24h ticker cooldown | per ticker | Repeat articles on the same catalyst would re-enter the same fading spike |

### Position sizing (`trading/executor.py::calculate_quantity`)

Size = **minimum** of:
1. Hard cap: 5% of portfolio.
2. Risk budget: equity × 0.25% / 2% stop (caps the cost of a stop hit).
3. **Liquidity participation: 0.5% of the stock's ADV dollars** — keeps our
   own exit order from moving the price.
4. Available cash.

---

## 6. Stage 4 — Exits (`monitor/position_monitor.py`, every 20s)

The realized win/loss asymmetry was the system's biggest leak: designed
+5%/−2%, realized **avg win £5.52 vs avg loss £10.37** (INHD "take_profit"
filled +3.13%; GOAI "stop_loss" filled −18.99%). v14 restructures execution:

| Exit | Mechanism | Latency |
|---|---|---|
| **Take profit** | **Resting LIMIT sell placed at buy time** (`tp_order_id` on the trade). The exchange fills it the moment price touches target | zero |
| **Stop loss** | Polled every 20s (was 60s); sells via **bounded limit** at trigger × (1 − 1%) — caps slippage at ~1% instead of chasing a collapsing bid. Unfilled → cancel → retry next cycle at current price | ≤ 20s |
| **Time stop** | 60 min after entry, polled; needs no price feed (fires even in a data outage) | ≤ 20s |
| **EOD flatten** | ALL positions force-closed 10 min before the close with a market sell, regardless of P&L. Stops don't work overnight; one gap erases a month | — |

**The cancel/fill race** (no OCO on T212): before any stop/time-stop sell, the
resting TP must be cancelled (it reserves the shares). If the cancel fails
because the TP filled while cancelling, the trade is recorded as a
take_profit — never sold twice. Unknown order state (network error) → defer to
next cycle rather than risk a double exit.

If a resting TP disappears from the pending-order endpoint, the monitor now
checks fill detail before closing the DB trade. A missing pending order can be
a fill, but it can also be a DAY-order expiry/cancel; treating every 404 as a
profit would corrupt P&L and leave real positions unmanaged.

The monitor never sells into a closed market (guard + loud error if positions
somehow survive past the close).

---

## 7. Pre-market pipeline (`premarket/scanner.py`)

Most genuine catalysts publish **07:00–09:25 ET** — before v14 the system
structurally could not trade them (news cycle slept while closed; the 60s
freshness filter killed overnight articles by the open).

**Deliberate design decision — no pre-placed orders.** The gap prices the news
in before the open; a pre-placed order fills at the opening auction, buying
the entire gap with zero confirmation ("gap-and-crap"). Instead:

1. **Scan** (from 08:00 ET): score pre-market news with the same classifier
   and the same trade gates; survivors go to the `premarket_candidates`
   watchlist.
2. **Confirm at the open** (after the 5-min opening block, within 30 min of
   the open):
   - **Gap gate**: current price vs prev close must be within
     [`MIN_GAP_PCT`=1%, `MAX_GAP_PCT`=20%]. Below: the market doesn't believe
     the catalyst. Above: the move is exhausted.
   - **Full standard confirmation** (§4): post-open momentum and RVOL must
     show buyers following through *after* the auction.
3. Survivors execute through the **same risk gates and buy path** as RTH
   signals. Candidates expire at open+30min or end of day, every outcome
   recorded on the row (`status`, `eval_note`).

---

## 8. Reliability and failure policy

| Failure | Behaviour |
|---|---|
| Finnhub quote down | 3 retries (1s/2s/4s); position monitor falls back to Twelvedata bar close |
| Twelvedata down | 3 retries (1.5s/3s/6s + 429-aware); signal parked in the **retry queue** (5-min TTL) — previously "will retry next cycle" was a lie because the freshness filter dropped the aged article (SPCX, Jun 12) |
| Twelvedata credit budget | metered in-process; WARNING at 80% of the 800/day cap |
| Both feeds down with open position | TP/SL skipped that cycle; time stop still fires (needs no price) |
| DB down | 3 retries on OperationalError; eval-loop writes never block the trading path |
| T212 symbol map 429 at startup | retries with 30s backoff + daily 08:00 UTC rebuild (a single startup 429 used to poison the whole session) |
| Service crash | systemd `Restart=always` + deploy-time config validation + post-restart health check + **heartbeat table** (below) |

**Heartbeat / alerting:** every job updates `heartbeat(job, last_beat_at)`.
Grafana alert query (fires when the news cycle is silent >10 min):

```sql
SELECT EXTRACT(EPOCH FROM (NOW() - last_beat_at::timestamptz)) / 60 AS minutes_stale
FROM heartbeat WHERE job = 'news_cycle';
```

The 2026-06-11 incident — a missing `TWELVEDATA_API_KEY` crash-looping the
service for 18 hours unnoticed — is why the deploy workflow now (a) runs the
test suite first, (b) validates config on the VM **before** restarting the
service, (c) health-checks the service after restart and fails the deploy
loudly.

---

## 9. The eval loop (`analysis/forward_returns.py`)

Every Claude classification is stored; nightly at 22:30 UTC the job fills in
what the market actually did 5/15/60 minutes after each article (yfinance —
retrospective, so delay is irrelevant and no Twelvedata credits are spent).

This converts prompt engineering from guesswork into measurement:

```sql
-- Classifier precision: how often do positives actually move?
SELECT sentiment, COUNT(*) AS n, AVG(fwd_return_15m) AS avg_15m,
       AVG((fwd_return_15m > 2)::int) * 100 AS pct_moved_2pct
FROM sentiment_scores WHERE returns_computed_at IS NOT NULL
GROUP BY sentiment;

-- Which catalyst classes actually pay? (feeds TRADEABLE_CATALYSTS)
SELECT catalyst_type, COUNT(*) AS n, AVG(fwd_return_15m) AS avg_15m
FROM sentiment_scores
WHERE sentiment = 'positive' AND returns_computed_at IS NOT NULL
GROUP BY catalyst_type ORDER BY avg_15m DESC;
```

Run these after every prompt or threshold change.

---

## 10. Backtesting honestly (`backtest/backtest_db.py`)

The DB-replay backtest applies the current filter set to historical signals
with **costs that match reality**:

- Entry at the **next bar's open** after the signal (production is 10–90s
  late; the old signal-bar-close entry assumed zero latency).
- **Stop-priority same-bar fills**: if one bar touches both stop and target,
  assume the stop hit first (the old target-first assumption inflated win
  rate).
- **Cost model**: 0.30% FX round trip (T212 GBP↔USD) + liquidity-tiered
  slippage per side (0.05% above $50M ADV → 1.00% below $1M).
- 24h per-ticker cooldown, mirroring production.

A strategy that only profits under frictionless fills doesn't profit.

**Backtest ↔ production parity (cardinal rule).** `run_v15_check()` mirrors
`confirm_price_signal()` gate-for-gate — same order, same thresholds (sourced
from `cfg`, asserted equal by `TestBacktestParity`), same prev-close baselines,
same VWAP confirmation (computed from intraday bars via `_session_vwap_at()`).
If the two ever diverge, the backtest is testing a strategy you don't run, which
is worse than no backtest. Any change to a price-check gate must be made in both
places in the same commit.

---

## 11. Known limitations / future work

- **No real bid/ask feed** — the spread proxy (1-min bar range) is coarse.
  A quote feed upgrade would make the wide_spread filter exact.
- **Stops are fixed-percent** — ATR-scaled stops would adapt to each stock's
  volatility; the risk-based sizing formula already supports variable stops.
- **No trailing stop / scale-out** — winners are capped at +5%; a break-even
  move at +2% and a trail would let the right tail run.
- **Single news source** — Benzinga latency (visible as `published_at` vs
  `fetched_at` in `news_signals`) bounds the whole edge; measure it weekly.
- **Demo-mode fills** are T212's paper engine (top-of-book, no depth) —
  modestly optimistic vs live.
- **Earnings calendar awareness** — the system can currently buy a stock
  minutes before its own earnings release.
