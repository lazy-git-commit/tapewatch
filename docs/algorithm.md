# How the Algorithm Works

This is the authoritative description of the trading algorithm as of **v17**.
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
| Freshness | older than 3 min dropped (RTH; was 60s pre-v18) | The old 60s cutoff permanently lost every article the Benzinga feed indexed >60s after its publish timestamp, and every article landing while a cycle overran 60s (buy fills block up to 30s). 3 min captures those; the momentum/RVOL/VWAP gates decide whether the move is still live. Acting late still buys reversals — that judgement now lives in the price gates, not the fetch cutoff |
| Blocklist | `BLOCKLIST` env tickers dropped | Manual permanent exclusions |
| Dedup | `(article_id, ticker)` already in DB | Never re-score the same pair |
| Scored-once | `_scored_articles` session set (v18) | With the 3-min freshness window an article appears in ~3 consecutive fetches; this guarantees exactly one Claude scoring per article per session (failed batches stay eligible for retry) |
| Roundup | >3 tickers tagged → skip | Market digests have no per-stock catalyst (v11) |
| Analyst action | headline matches `_ANALYST_ACTION_RE` → skip | `analyst_action` is never in `TRADEABLE_CATALYSTS`. A regex on the raw headline is far cheaper than a Claude call. Catches "reiterates buy", "price target", "upgrades to overweight", "initiates coverage" etc. Added v17.4. |
| Digest/preview | headline matches `_DIGEST_RE` → skip | v20. Compilations ("Market-Moving News for July 10th", "Stocks To Watch", "Premarket Movers", "Earnings Scheduled For…", listicles) are written ABOUT the market with tickers tagged incidentally. One slid under the >3-ticker roundup filter with exactly 3 tickers on 2026-07-10 and was classified "earnings_beat, 80% conf" for THREE unrelated companies at once — the fabricated catalyst that bought the top of CRCL's 13% parabolic spike (−3.97%). Deterministic title check; no digest reaches Claude. The prompt also gained a "digests are never catalysts" rule as defense in depth (a digest classified fda_approval would still pass the catalyst gate — the regex is the reliable layer). |

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

**`fda_approval` means the US FDA specifically (v20.2, 2026-07-13):** a Health
Canada, EMA, or MHRA approval is `other`, not `fda_approval` — the measured
60-day forward-return edge (§3.3) was computed for US-regulator action only
and does not extend to foreign regulators. Found when NVS's Health Canada
approval was tagged `fda_approval` on 2026-07-13 (harmless that day — dead
tape rejected it anyway — but a mistag that happens to move on the tape would
trade on an edge that was never measured). The system prompt now carries an
explicit carve-out plus a contrastive example right next to the genuine-FDA
example.

**Every classification — positive, neutral, negative — is persisted to
`sentiment_scores`** for the eval loop (§9). The model classifies; code decides
what trades.

**Same-day same-ticker cross-reference (v19.5, 2026-07-09):** each article is
otherwise scored with zero memory of any OTHER article about the same stock
earlier that session. 2026-07-09: Benzinga ran two articles about LEVI's exact
same earnings print two hours apart with opposite framing — 09:39 ET *"Stock
Tumbles 4% Despite Q2 Earnings Beat"* (scored negative, correctly never
traded), then 11:30 ET *"Posts Beat-And-Raise Quarter, Analysts See More
Upside In 2H"* (scored positive, 85% confidence — the one traded, at the top
of the recovery bounce the first article's "tumble" had already produced).
A session-scoped, daily-reset `_ticker_history` dict (`news/fetcher.py`)
records every scored article per ticker (sentiment + headline + time); the
next article for that ticker carries up to 3 prior same-day verdicts as a
`PRIOR ARTICLE(S) TODAY ON THIS TICKER` line appended to its entry in the
(uncached, per-cycle) user message — the cache prefix is unaffected since
this is per-cycle content, not the static rubric. The system prompt gained a
"SAME-TICKER CONTEXT" instruction: a positive respin of a story that already
had a negative reaction today should be read with extra skepticism (lower
confidence, lean `already_moved=true`) unless it contains a genuinely new,
separate fact.

### 3.3 Trade gates (code, not model)

A positive classification only becomes a tradeable signal if **all** pass:

1. **Confidence** ≥ `MIN_SENTIMENT_CONFIDENCE` (default 7/10). This setting
   existed since v1 but was never enforced until v14.
2. **Catalyst class** in `TRADEABLE_CATALYSTS` — v20 default:
   **fda_approval, guidance_raise** only. Pruned 2026-07-10 using the
   system's own eval loop: forward returns of every positive,
   not-already-moved, confidence≥0.7 signal over 60 days (avg @5/15/60 min):

   | class          | n   | 5m     | 15m    | 60m    | verdict |
   |----------------|-----|--------|--------|--------|---------|
   | fda_approval   | 86  | −0.02  | +0.55  | +1.42  | keep    |
   | guidance_raise | 9   | +0.38  | +1.15  | +2.97  | keep    |
   | contract_win   | 152 | −0.42  | −0.84  | −1.34  | removed |
   | ma_target      | 71  | −0.08  | −0.07  | −0.37  | removed |
   | earnings_beat  | 33  | −0.74  | −1.13  | −1.60  | removed |
   | product_launch | 32  | −0.93  | −1.13  | −2.05  | removed |
   | short_squeeze  | 0   | —      | —      | —      | removed |

   The kept classes are binary regulatory/guidance surprises whose drift
   BUILDS over 15–60 min — the shape a 1–3 min entry latency captures.
   Earnings news at this latency gets sold (both July losses were
   earnings_beat); M&A targets pin to the deal price instantly, leaving
   nothing to capture. Catalyst magnitude was tested as an alternative
   filter and does NOT predict returns (magnitude 3/4/5 all averaged
   negative; magnitude 2 slightly positive) — the magnitude floor stays at
   2 and was deliberately NOT raised. Every class is still scored and
   persisted, so a class can be re-enabled the moment fresh forward-return
   evidence supports it — and only then.
3. **`already_moved` is false** — the model's own judgement that the move
   pre-dates the article.

---

## 4. Stage 2 — Price confirmation (`market/price_check.py`)

Data sources (v20 consolidation): **quote with fallback** — Finnhub `/quote`
(current price, open, **previous close**), falling back to Twelvedata
`/quote` when Finnhub has no coverage — plus **ONE Twelvedata 1-min session
pull** (`get_session_analysis`: momentum baseline by timestamp, spread proxy,
session volume, VWAP, session low/high) and **daily stats cached per symbol
per ET day** (`get_daily_stats`: 20-day ADV, dollar-ADV, prev-close backup —
immutable intraday, so re-fetching them per retry was pure waste). The old
plan made three sequential bar calls per confirmation (2–3 credits, re-paid
by every re-eval retry); a typical confirmation now costs 1 credit and ~2
HTTP round trips. Consolidating also fixed a long-standing subtle bug: right
after the open, the momentum baseline ("newest bar ≥5 min old") could match
YESTERDAY's 15:59 bar, silently treating the overnight gap as 5-minute
momentum — baseline selection is now restricted to today's session.

**Symbol hygiene (v15):** Benzinga tags carry routing cruft that breaks every
downstream consumer. `clean_benzinga_symbol()` (in `trading/executor.py`) drops
foreign-exchange-prefixed tags entirely (`TSX:MDA` → None — not US-tradeable)
and strips Benzinga's collision-disambiguation digit (`INBX1` → `INBX`,
`SAIL1` → `SAIL`). On 2026-06-15 these uncleaned tags reached the price check,
got no Finnhub quote, and burned 30-minute pre-market eval windows.

**Symbol identity round-trip (v19.2):** the REVERSE direction was also lossy.
Market-data lookups derived the exchange symbol by stripping `_US_EQ` off the
T212 code — but T212 re-uses historic symbols by appending a digit to its own
code (Firefly Aerospace: exchange symbol `FLY`, T212 code `FLY1_US_EQ`), so
the derived `FLY1` had no data coverage anywhere and both FLY candidates on
2026-07-07 expired unpriced. `build_symbol_map()` now also builds the inverse
map; `t212_to_symbol()` resolves the exact exchange symbol (suffix-strip only
as fallback before the map is built). All price checks — entry, premarket
eval, and the position monitor — go through it.

**Quote fallback (v15):** Finnhub's free tier silently omits many small caps and
recent IPOs — exactly the catalysts this strategy targets (2026-06-15:
CUPR/ELAN/WBD/INBX/SAIL all had no Finnhub quote, all priced fine on
Twelvedata). `get_quote_with_fallback()` tries Finnhub, then Twelvedata
`/quote`; both return the same `c`/`o`/`pc` keys so callers are source-agnostic.
Only when BOTH miss is a signal deemed unpriceable.

**Quote staleness (v19.2):** a quote older than 20 minutes (its own `t`
timestamp) is treated as **no coverage**, not a price. On 2026-07-07 Finnhub
served GLASF at $12.50 all afternoon while the market traded ~$11.53: the
frozen print manufactured the +2% "momentum" that confirmed the entry, made a
losing position look +6% up, and priced every exit limit above the real book
(459 consecutive unfilled sells). Quotes without a timestamp fail open — only
positive evidence of staleness rejects.

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
| 7 | `low_momentum` | < +0.2% over ~5 min (v15: dead-tape noise floor only) | Just rejects "the catalyst moved nothing"; VWAP does the real work (step 10). Moves below −0.2% log as "tape moving against the signal" (same code) |
| 8 | `high_momentum` | > +15% over ~5 min | Post-halt spike — halt articles publish AFTER the 30–120% pop. Runs before VWAP to save a credit |
| 9 | `low_volume` / `high_volume` | RVOL outside [1.5, 20] | See RVOL section (v20: session minute-bar volume is THE numerator — current, unlike the lagging daily bar the v19.2 "rescue" had to work around. v20.2: the FLOOR only bypasses to a held-VWAP check above `RVOL_BYPASS_MIN_ADV_DOLLAR` — see below) |
| 10 | `below_vwap` | price < session VWAP (− small tol) | v15: size-neutral accumulation test — see below |
| 10.2 | `overextended` | price > VWAP × (1 + **1.5%**) | v20: never park the stop on the far side of value. With a 2% stop, an entry >1.5% above VWAP is stopped out by a ROUTINE reversion to value — LEVI entered +1.9% above VWAP, CRCL +2.2%; both dead on arrival. Transient → re-eval queue = first-pullback entry |
| 10.5 | `exhausted_bounce` | day's range ≥ **5%** AND price recovered ≥ **75%** of it | v19.5: LEVI bought within 15¢ of the day's exact high, 3 min before the peak, after gapping down −7.8% at the open — see below |
| 11 | `insufficient_data` | no volume measurement AND no VWAP | v19.2: individually-reasonable fallbacks (open-price baseline, RVOL deferred, VWAP skipped) could stack into approving on a bare stale quote — how GLASF traded. At least one participation measure must positively exist |

**Transient vs terminal (v19.2, extended v20):** `low_volume`, `low_momentum`
and `overextended` describe the tape AT THIS MINUTE, not the instrument —
signals are scored within ~3 min of publication, often before participation
can exist (VERA's FDA approval was rejected on RVOL 0.71 measured the minute
the news broke), and an extended price pulls back into buyable range within
minutes on genuine movers. RTH signals rejected with these codes park in a
re-eval queue and re-confirm every cycle for 15 minutes; premarket candidates
stay pending until the eval window closes. Everything else is terminal on
first sight.

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

### VWAP extension ceiling — don't chase; buy the first pullback (v20)

`below_vwap` polices one side of value; `overextended` (gate 10.2) polices
the other. The geometry is mechanical: the stop sits `STOP_LOSS_PCT` (2%)
below entry. If entry is MORE than that above VWAP, then VWAP itself — the
level extended stocks routinely revisit even in healthy uptrends — lies
below the stop, and an ordinary reversion to value ends the trade at a loss
by construction. Both 2026-07 losses had exactly this shape: LEVI entered
+1.9% above VWAP, CRCL +2.2%, both with the 2% stop, both stopped/timed out
on nothing more than reversion.

`MAX_VWAP_EXTENSION_PCT` (1.5) is therefore DERIVED (stop minus margin), not
a tuned magic number, and validation enforces it stays below the stop. The
reject is **transient**: the re-eval queue re-confirms every cycle for 15
minutes, so a genuine mover that pulls back toward VWAP with its catalyst
intact gets entered ON the pullback — the professional "first pullback into
value" entry, implemented with machinery that already existed. The gate
evaluates whenever a VWAP exists, **independent of
`REQUIRE_VWAP_CONFIRMATION`** (v20.1): it is stop geometry, not an
accumulation test, and turning off the latter must not silently disable it.

### Intraday exhaustion — has the stock already had its move today? (v19.5)

`day_change_pct` and `recent_move_pct` are both **endpoint** measures: distance
from yesterday's close, and distance from ~5 minutes ago. Neither sees the
**shape** of today's own session. A stock that gapped down hard at the open
and clawed most of the way back looks, on both those measures, identical to
one calmly grinding to fresh highs — yet the first is a fading bounce and the
second is a real breakout.

2026-07-09, LEVI: gapped as much as **−7.8%** at the open on an earnings beat
("sell the news" — Benzinga's own 09:39 ET article was headlined *"Stock
Tumbles 4% Despite Q2 Earnings Beat"*), then recovered to **+2.3%** vs
yesterday's close by 11:30 ET. Every existing gate read clean at that point
(momentum +0.32%, day change +2.09%, RVOL 1.5) and the trade was bought
within **15 cents of the exact high of the day**, three minutes before the
actual peak. It faded for the rest of the session, closing on the time-stop
at −1.19%.

```
day_range_pct   = (session_high − session_low) / session_low × 100
recovered_frac  = (current_price − session_low) / (session_high − session_low)
reject if day_range_pct ≥ EXHAUSTION_MIN_RANGE_PCT (5.0)
      AND recovered_frac ≥ EXHAUSTION_RECOVERY_THRESHOLD (0.75)
```

Both conditions must hold: the range floor keeps a normal day's noise from
tripping the gate (a stock that's only moved 2% top-to-bottom hasn't "had its
move"), and the recovery threshold only fires once price is deep inside the
already-recovered portion of a real round trip. `session_low`/`session_high`
come from the SAME 1-min-bar pull already spent on the RVOL rescue and VWAP
(`get_session_volume_and_vwap` — extended from a 3-tuple to a 5-tuple to add
them) — no extra Twelvedata credit. Toggle: `REQUIRE_EXHAUSTION_CHECK`.

**Considered and rejected: scaling take-profit/stop-loss by catalyst_magnitude.**
LEVI's catalyst was rated magnitude 2/5 (Claude's own "modest" judgement) and
the stock topped out at +0.56% — nowhere near the flat 5% take-profit every
trade uses. The tempting fix is to lower the TP target for low-magnitude
catalysts so a trade like this can lock in a smaller real gain. Rejected: a
2% profit target isn't worth taking the trade for at all — if that's genuinely
the ceiling for a magnitude-2 catalyst, the right response is not to trade it,
not to shrink the target. The exhaustion gate is the more precise fix anyway:
it targets what actually went wrong (bought at the top of an already-completed
move), not the catalyst class in general — a magnitude-2 catalyst caught EARLY
in its move is not disqualified by this gate at all.

### RVOL — time-of-day normalized relative volume

```
rvol = today's cumulative volume
       / (20-day ADV × expected fraction of a day's volume traded by now)
```

The expected fraction follows the intraday U-curve (~5% by 10:00, 42% by
13:00, 100% at close — recalibrated 2026-07-08, was ~16%/50%), linearly
interpolated. Without this, "1.5× the full-day average" is nearly impossible
at 10:00 and trivial at 15:45 — the old raw ratio was a different filter at
every hour of the day. RVOL ≈ 1.0 always means "a normal day so far". The 20×
ceiling is the halt-pattern signature (parabolic participation on micro-caps).

**Curve recalibration (v19.4, 2026-07-08):** the original curve assumed a
big-cap, open-auction-flow shape (16% traded by minute 30). Measured directly
against real volume that day for BZH/JNJ/CACI/ARQT, the true fraction by
minute 30 ran 1–4% — this system's catalyst population (small/mid-cap names
reacting to a news wire) doesn't front-load volume the way index constituents
do. The 4–14× mismatch pinned RVOL near-zero for the entire 30-min pre-market
eval window regardless of real participation (BZH: 4× normal full-day volume,
still read RVOL ~0.3 at minute 29) — the proximate cause of 12/19 pre-market
candidates expiring unevaluated that day. The 0–150 min anchors are now ~3×
less aggressive, reconverging with the original curve by minute 150 where
there's no contradicting evidence. First-pass empirical fit from one day's
data — revisit as more days of measured `today_volume`/`avg_daily_volume`
accumulate.

**Session volume is the numerator (v20; supersedes the v19.2 rescue):** the
RVOL numerator was Twelvedata's daily-bar `today_volume`, whose volume field
trails the live session by minutes — worst at the open, exactly when the
gap-and-go eval runs (2026-07-07: ZTS read RVOL 0.07 and AGIO 0.40 minutes
after gapping on real catalysts). v19.2 patched this with a conditional
"rescue" second fetch of the minute bars. v20 removes the lagging source
entirely: the session minute-bar sum from the single `get_session_analysis`
pull IS the numerator, so the lag class of false rejections died with the
mechanism that worked around it. Known caveat: minute bars can undercount
the consolidated tape, so RVOL reads slightly conservative — and
`low_volume` is transient, so a false low re-checks within minutes. A zero
measurement still counts as a measurement: GLASF traded on rvol=0.0 through
the old "volume unmeasured → skip the band" bypass, and must never again.

### RVOL floor bypass — size-neutral participation for mega-caps (v20.2)

RVOL step 9 runs strictly BEFORE the VWAP accumulation test (step 10) — so
even though VWAP is explicitly designed to be size-neutral (a deep-book
large-cap holds VWAP on a real catalyst while barely moving the tape %), it
never got a chance to prove that for a stock the RVOL floor had already
killed. This is the same failure class as VERA above, just at the opposite
end of the cap spectrum: a name too LARGE to spike RVOL, instead of too FRESH
to have accumulated it yet.

**2026-07-13 post-mortem (BMY):** "FDA Accepts NDA For Mezigdomide" —
confidence 0.75, magnitude 2 (correctly scored low — an NDA acceptance is a
much weaker catalyst than an approval). BMY drifted +0.14% → +2.14% → +1.60%
over the session, a real and sustained move, and held VWAP (10.45→trending)
essentially the whole way. RVOL never exceeded 0.3 against the 1.5 floor,
because BMY trades **$752M/day** in ADV$ — a $100B+ mega-cap doesn't need
1.5× normal relative volume to move 2%; that much dollar volume moving at all
is already a huge absolute number. The signal was rejected `low_volume` on
all 27 consecutive re-eval cycles across its 15-minute window (having already
burned its pre-market gap-and-go window the same way) — VWAP was never
consulted because RVOL vetoed first every single time.

**Fix:** for `avg_dollar_volume >= cfg.rvol_bypass_min_adv_dollar` (default
**$50M**, 10× the illiquidity floor — a first-pass estimate, not yet
validated against multiple days of measured mega-cap data), a held VWAP
(price at/above VWAP × (1 − `VWAP_TOLERANCE_PCT`), independent of
`REQUIRE_VWAP_CONFIRMATION` — same precedent as the `overextended` gate)
substitutes for the RVOL floor. The bypass touches the FLOOR only: the
20× ceiling still applies at any cap size, because parabolic relative volume
is the halt-pattern signature regardless of how large the normal book is.
Below the ADV$ floor, small/mid-cap behavior is completely unchanged — this
is where RVOL is best-validated (a real catalyst on a thin book produces a
genuine volume explosion) and where the bypass must NOT fire.

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

## 6. Stage 4 — Exits (`monitor/position_monitor.py`, every 5s)

The realized win/loss asymmetry was the system's biggest leak: designed
+5%/−2%, realized **avg win £5.52 vs avg loss £10.37**. v14 rested the
take-profit limit at the broker and polled the stop every 20s. **v20 inverts
that**, because the realized record proved it backwards: ONE resting-TP fill
in 11 trades, versus stop-side slippage on every fast reversal — VECO −3.4%,
CRCL −3.97% (falling ~1%/min; the 20s poll alone gave it a 20-second head
start), GOAI −18.99%, all on −2% triggers. T212 has no OCO and each sell
order reserves its shares, so only ONE side can rest: it must be the side
where latency costs capital. A missed TP costs opportunity; a slow stop
costs money on every single fast reversal.

| Exit | Mechanism (v20) | Latency |
|---|---|---|
| **Stop loss** | **Resting STOP-MARKET sell placed at buy time** (`stop_order_id` on the trade). The broker executes the instant price touches the trigger. Stop-market, not stop-limit: when it triggers, the book is moving against us — certainty is the point; the ADV liquidity floor bounds expected slippage. If placement fails, the monitor falls back to the old polled stop (bounded limit + v19.2 market escalation after 3 unfilled attempts) | zero |
| **Breakeven ratchet** | At +2% (1R) the resting stop is cancelled and re-placed at buy × 1.001, once per trade. A trade that has paid one risk unit may mean-revert but must not become a loser. If the replacement can't be placed, an in-process armed flag drives a POLLED breakeven stop — the ratchet only ever tightens protection | zero after arm |
| **Take profit** | Polled at the monitor cadence; sells via bounded limit at current price × (1 − 1%) after cancelling the resting stop | ≤ 5s |
| **Time stop** | 60 min after entry, polled; needs no price feed (fires even in a data outage). Kept at 60: the kept catalyst classes' measured drift is still building at the 60-minute horizon | ≤ 5s |
| **EOD flatten** | ALL positions force-closed 10 min before the close with a market sell, regardless of P&L. Stops don't work overnight; one gap erases a month | — |

Monitor cadence is 5s (was 20s): with the stop resting broker-side, the poll
no longer guards the loss side, but the POLLED side (take-profit) pays the
full cadence as latency at the exact moment price touches the target. The
exit loop early-outs on a local DB query when flat, but broker
reconciliation runs FIRST, before that early-out (v21.2) — throttled to once
per 60s, with resting-order status checks throttled to once per 15s per
trade so the faster loop doesn't multiply API load.

**Broker/DB reconciliation** compares DB-open trades against the live
portfolio and pages the operator (CRITICAL) on divergence — phantom
(DB-open, broker-flat: a missed `close_trade()`) or orphan (broker-open,
DB-flat: a failed `open_trade()` after a buy fill, or a manual trade). Two
rules, each bought by an incident:

- **Two-pass confirmation (v21.2).** A divergence fires CRITICAL only when it
  survives two consecutive reconcile passes (60s apart); the first sighting
  logs INFO ("confirming next pass"). Motivation: the reconcile compares a
  DB snapshot taken at cycle start against a broker portfolio fetched later,
  so every entry whose `open_trade()` was still committing looked like an
  orphan — a spurious CRITICAL fired on every single trade of 2026-07-16,
  training the operator to ignore CRITICALs. A commit race resolves within
  one pass; a real orphan/phantom cannot clear itself — persistence across
  passes is what separates them. (v21.1 briefly used a 30s time-since-fill
  grace window instead; it was replaced because it also suppressed the alert
  when `open_trade()` had genuinely failed — the exact case the alert
  exists for — and knew nothing about the phantom-side race.) Worst-case
  alert latency is ~2 minutes, acceptable for a manual-review page.
- **Reconcile runs even when the DB is flat (v21.2).** The single most
  dangerous orphan shape — the only open position's `open_trade()` failed
  after its buy filled — leaves the trades table empty; the pre-v21.2 code
  early-outed on the empty DB before reconciling, making exactly that orphan
  invisible until some unrelated trade opened.

Never auto-reconciles: a transient portfolio-endpoint timeout is
indistinguishable from "broker is flat", and auto-closing on that would
flatten real positions. The monitor's price
lookups run the quote chain in fast (single-attempt) mode — the next cycle
is the retry — and the 390-bar Twelvedata price fallback is throttled to one
attempt per 30s per symbol so a quote outage can't drain the credit bucket
that signal confirmation needs (v20.1 review findings).

The ratchet's armed flag is persisted on the trade row (`ratchet_armed`,
v20.1): a restart cannot regress an armed position back to the −2% stop, and
a crash between the ratchet's cancel and re-place self-repairs — the next
cycle above the trigger places a fresh breakeven stop directly.

**The cancel/fill race** (no OCO on T212): before any polled TP/time-stop/EOD
sell, the resting stop must be cancelled (it reserves the shares). If the
cancel fails because the stop filled while cancelling, the trade is recorded
as a stop_loss — never sold twice. Unknown order state (network error) →
defer to next cycle rather than risk a double exit. The same race handling
covers the ratchet's cancel-and-replace.

If a resting order disappears from the pending-order endpoint, the monitor
checks fill detail before closing the DB trade. A missing pending order can
be a fill, but it can also be a DAY-order expiry/cancel; treating every 404
as an exit would corrupt P&L and leave real positions unmanaged.

Legacy trades opened before v20 that still carry a resting TP
(`tp_order_id`) are managed under the old rules until they close — both
regimes coexist across the deploy.

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

**Known failure mode — data-storm starvation of the eval window (2026-06-18).**
Single-candidate handling is correct: a missing `pc` or `opening_block` leaves a
candidate *pending* to retry next cycle (above). But with **many** candidates,
that per-candidate retry is run **serially** every cycle and each retry re-fetches
quotes. On 2026-06-18, 18 candidates + Finnhub `pc=0` through the whole 5-min
block + Twelvedata 404/rate-limit backoff (3+6+9s per no-coverage small-cap)
produced 205 "prev close unavailable" and 174 Twelvedata-failure lines in one
30-min window. A single eval cycle exceeded 60s (the scanner skipped whole
minutes), the first *real* evaluations landed only at open+6min, and **9
candidates expired with `eval window closed` having never been price-checked
once**. The ones that *were* evaluated were correctly rejected (gap-and-crap /
RVOL<1.5) — so the defect is starvation, not a wrong verdict. **Fixed in v16.0**
(parallel confirm under a 30s wall-clock budget) and **v16.1** (07:00 scan start).

**Known failure mode — data-budget collapse (2026-06-11 → 06-23, nine sessions
of zero trades).** A deeper instance of the same class, found 2026-06-23. The
service was up the whole time, all four heartbeats green, news scoring healthy
(1283 articles → 80 fully-qualified candidates on 06-22). Yet the last trade was
2026-06-10. Two compounding causes, both of which fail **closed and silently**:

1. **Twelvedata 800/day credit budget exhausted by mid-morning** (06-22: ~14:01
   ET; 06-23: ~11:46 ET). The premarket fan-out (≈4 credits/candidate/cycle ×
   ~35 candidates × ~30 cycles) plus RTH retry loops that re-bill a failing
   signal every minute drain the free-tier budget before noon. After that, the
   entire RTH path goes dark: `get_momentum_baseline` returns `None` →
   `confirm_price_signal` returns `None` → signal parked → dropped. No trade can
   ever confirm. (Finnhub gives only a quote, never the bars the gates need, so
   there is **no fallback** — the system correctly does not trade blind.)
2. **The credit meter was cosmetic.** It logged "EXHAUSTED" but did **not** stop
   the call — execution fell through into the HTTP request, which 429'd, burning
   the full 3+6+9 = 18s backoff *per call*. The v16.0 storm, reincarnated in the
   RTH path (the `fast=` no-retry path had only been threaded into the quote
   call, not the three `_get_time_series` consumers).

**Fixed in v17 (`market/twelvedata_bars.py`):**
- `credits_exhausted()` is now a **hard gate** every public entry point checks
  *before* any HTTP call — once spent (soft cap 780 = 800 − 20 headroom for
  under-counting), the call is skipped entirely and the caller gets its
  "unavailable" sentinel. This kills the 18s-per-call 429 storm. The transition
  is logged once per UTC day and written to `system_events`.
- `fast=` is threaded into `_get_time_series` and all three bar consumers
  (`get_momentum_baseline`, `get_volume_stats`, `get_session_vwap`), so the
  pre-market eval is now *fully* no-retry — a single slow ticker can no longer
  blow the 30s budget. VWAP (the 4th/last credit) degrades to "confirm on
  momentum + RVOL only" on a fast miss, matching a genuine data gap.
  **v19.1 closed the last gap in this contract:** `get_finnhub_quote()` — the
  PRIMARY quote source, called before any Twelvedata fallback — had no fast
  mode at all, so a slow/down Finnhub could still hold a premarket pool thread
  for ~17s (3×5s timeouts + backoff sleeps). It now takes `fast=` too (one
  attempt, no sleeps), propagated by `get_quote_with_fallback`.
  **Also v19.1:** `get_gbp_usd_rate()` (position sizing FX) used to bypass
  both the credit meter and the minute token bucket entirely — the one
  unmetered Twelvedata call in the system. It now runs behind the same two
  gates and serves the cached/fallback rate when gated.
- The pre-market eval phase short-circuits when credits are exhausted (no thread
  pool spun up, candidates left pending for next cycle / window expiry).

**Twelvedata Grow plan (upgraded 2026-06-25):** The system now runs on the
Grow $29/month plan — no hard daily credit cap, 55 calls/minute (was Basic:
800/day, 8/minute). The internal backstop (`_DAILY_CREDIT_LIMIT=50_000`,
soft-cap 49,900) is a safety ceiling, not a budget. The 8/minute burst problem
is fixed by a thread-safe token-bucket (`_claim_minute_token()`) that returns
the "unavailable" sentinel rather than sending HTTP requests that would 429.

**Session no-quote blackout (v17.1, 2026-06-24):** A separate class of
permanent-retry loop was found: tickers with zero Finnhub/Twelvedata coverage
(e.g. EGGF, OXAC on 2026-06-24) were parked in the retry queue every cycle for
hours — each re-fetch consuming credits and log noise. After 2 **consecutive**
failed retries (`_NO_QUOTE_BLACKOUT_RETRIES`) a ticker is added to
`_no_quote_blackout` and suppressed for the rest of the session. Strikes reset
on service restart (next day) **and on any successful price check for that
ticker** (v19.1 — before that, strikes were effectively cumulative-per-session:
two unrelated transient misses hours apart blacklisted a ticker with perfectly
good coverage). This is distinct from the 24h per-ticker cooldown
(`main.py::COOLDOWN_HOURS`), which tracks tickers we *traded*, not tickers we
couldn't price.

**Premarket no-coverage expiry (v17.4, 2026-06-29):** The RTH no-quote
blackout had no counterpart in the pre-market evaluator. Tickers with zero
Finnhub/Twelvedata coverage retried every minute for the full eval window
— ~600–900 wasted API calls per session. A per-candidate strike counter
(`_no_quote_strikes`, module-level dict) in `_apply_confirmation()` now expires
a candidate after `_NO_QUOTE_EXPIRE_AFTER=3` consecutive `conf=None` returns
rather than letting it exhaust the window. The threshold absorbs 1–2 transient
token-bucket misses (those resolve within one cycle).

**Opening-block log misattribution (v17.4, 2026-06-29):** The opening-block
rejection (`reason_code="opening_block"`) returns from `confirm_price_signal()`
before `prev_close` is computed, so `conf.day_change_pct=None` on all
opening-block cycles. `_apply_confirmation()` was checking `gap_pct is None`
BEFORE `reason_code == "opening_block"`, so every covered stock logged "prev
close unavailable" for the first 5 minutes after the open — observed 2026-06-29:
all 40 candidates mis-logged even for liquid names like AMGN/PFE. Behavior was
correct (candidates stayed pending), but the log message was misleading. Fixed
by moving the opening_block guard above the `gap_pct is None` check.

**Premarket prev-close strike counter (v17.5, 2026-06-30):** Post-mortem of
2026-06-30 (33 candidates, 12 expired as "eval window closed") found the root
cause: `gap_pct=None` (prev close unavailable) had no retry bound. When Finnhub
returns `pc=0` for a ticker and Twelvedata's daily bar hasn't rolled yet, every
evaluation cycle returns `gap_pct=None` and the candidate silently retries until
the 30-min eval window closes — all 12 expired candidates hit this path. Note:
`news_cycle` already runs `evaluate_premarket_candidates()` **before** fetching
RTH news within the same sequential job — there is no cross-job token contention.
The gap_pct=None is not a bug in the APIs: Finnhub `pc=0` is expected for thinly
covered tickers; Twelvedata's daily bar legitimately lags by 1–2 minutes at open.
Fixed by `_gap_pct_strikes` (module-level dict) in `_apply_confirmation()`: after
`_GAP_PCT_EXPIRE_AFTER=5` consecutive `gap_pct=None` returns the candidate expires
with reason `"prev_close: no previous close after 5 consecutive retries"`. Five
cycles = 5 minutes; genuine transient cases (TD bar delay at 09:30) resolve in
1–2 cycles. The eval window remains 30 min — with both strike counters in place,
all candidates resolve within 10 minutes of the opening block lifting.

**Premarket execution-boundary crash — the real drought root cause (v19,
2026-07-06).** The single most costly bug in the system's history, and the actual
reason for the 2026-06-11 → 07-06 drought (16 consecutive zero-trade sessions,
last trade 2026-06-10). It was masked by all the *upstream* premarket fixes above:
those genuinely improved the funnel, so candidates finally started reaching
`APPROVED` (e.g. 2026-06-29: PIRS +8.82%, MEG +10.17%, CYBN +7.64%, ERJ +1.51% —
clean gap-and-go setups), yet **still no trade fired.** Root cause found by tracing
one approval through to execution: `catalyst_magnitude` became a **required**
positional field on `NewsItem` in v15.8 (`018ae7c`), but `main._candidate_to_news_item()`
— which reconstructs a `NewsItem` from a `premarket_candidates` row so the approved
candidate can go through `_execute_entry` — was never updated to supply it. Every
premarket approval therefore raised `TypeError: NewsItem.__init__() missing 1
required positional argument: 'catalyst_magnitude'` at line 416. The exception was
swallowed by the broad `try/except` around the premarket loop in `news_cycle`
(logged as `ERROR __main__ — Pre-market candidate evaluation failed: ...`), which
**aborted the entire premarket execution loop** — so the log then read the benign
`No positive signals this cycle.` The RTH path was unaffected (it builds `NewsItem`
in `news/fetcher.py`, which does pass the field). Two lessons: (1) a required
dataclass field added in one place must be grep'd across *all* construction sites
(`NewsItem(` had exactly two — the fetcher and this converter); (2) a broad
`try/except` around an execution loop turned a hard crash into a silent no-op that
survived four weeks and four separate "premarket zero-trade" post-mortems. Fixed
by passing `catalyst_magnitude=int(cand.get("catalyst_magnitude") or 1)` through
(the value is already stored on every candidate row since v15.8; the `or 1` is a
can't-crash fallback for pre-v15.8 legacy rows). Regression test:
`TestPremarketCandidateToNewsItem`. Verified against the deployed `NewsItem`
contract on the VM: the old signature reproduces the exact `TypeError`, the fixed
converter builds a valid item.

**Premarket eval verdicts restructured (v19.2, 2026-07-07):** post-mortem of
the first executable session found two verdict bugs in `_apply_confirmation()`:
(1) **masked terminal rejections** — `penny_stock`/`wide_spread` fire before
prev_close is computed, so `day_change_pct=None`; the flow fell into the
prev-close strike counter and, after 5 wasted eval cycles re-checking a
terminally rejected stock, recorded "no previous close after 5 retries" (PLUG,
$2.65, was a penny reject every single cycle — its row blames a data problem
that never existed). Any non-transient rejection now records its REAL reason
immediately. (2) **premature terminal rejections** — `low_volume`/
`low_momentum` measure the tape at this minute; AGIO (+11.1% gap, FDA
catalyst) was terminally rejected at minute 5 on a lagged RVOL of 0.40 and
never got a second look. These two codes now leave the candidate PENDING
(re-evaluated every cycle until the eval window closes). The RTH pipeline got
the equivalent fix as a 15-minute re-eval queue in `main.py` (see §4).

**Opening no-quote grace period (v19.4, 2026-07-08):** In the first ~90s after
the open, Twelvedata served a quote timestamped exactly 24h old for ~19
tickers simultaneously (its own snapshot cache not yet rotated for the new
session) while Finnhub's quote was still genuinely carrying yesterday's close
— both correctly read as "no live coverage" for a systemic, predictable,
self-healing reason unrelated to any ticker's real coverage. It resolved
within one retry every time, but was burning one of only 3 no-quote strikes
across the board. `_OPEN_GRACE_MINUTES=2.0`: a `conf=None` miss inside this
window no longer increments `_no_quote_strikes`, preserving the full budget
for tickers with a genuine, not-provider-wide outage.

**Pre-market candidates no longer die at the 30-min cutoff (v19.4,
2026-07-08):** Post-mortem of a zero-trade day found 12/19 pre-market
candidates — including a live M&A bid war and three FDA approvals — expired
"eval window closed" having never been terminally rejected: they were still
PENDING (only ever hit transient `low_volume`/`low_momentum`), and were simply
discarded. Unlike RTH signals (unlimited re-checks via `_reeval_queue`,
§4), a premarket candidate got exactly one 30-minute shot. Verified against
real closing prices that several of that day's expired candidates (KGS +2.7%,
ARQT +3.0%, AYA +1.8%, URGN +0.7%) drifted favorably over the rest of the
session with no mechanism to ever look at them again. `_live_candidates` now
returns `(live, graduated)` — `graduated` is still-PENDING candidates whose
window just closed (stale prior-day candidates are excluded: those are just
dead, not graduated). `main.news_cycle` hands each graduated candidate into
the SAME standing RTH re-evaluation queue via a synthetic transient
`PriceConfirmation` (`reason_code="low_momentum"`) routed through the
existing `_execute_entry` → `_queue_reeval` path — no new persistence
mechanism, reuse of the already-tested one. This deliberately does NOT relax
the 30-minute gap-and-go window itself (buying a stale morning gap late is
exactly the "buying the top" failure v13 eliminated); it only gives the
underlying catalyst a second life as an ordinary (non-gap) momentum-
confirmation signal, identical to how a fresh RTH headline about the same
stock is already evaluated.

**Empirical ruling on `partnership` as TRADEABLE_CATALYST (2026-06-30):** Forward
returns from 60-day history (233 positive partnership signals, `already_moved=0`)
show avg_5m = +0.010%, median = 0.000%, only 3 of 233 moved >1% in 5 minutes. The
`partnership` catalyst class correctly remains excluded from `TRADEABLE_CATALYSTS`;
the magnitude gate (`MIN_CATALYST_MAGNITUDE`) alone is insufficient because high-
magnitude partnerships (e.g. a major NVIDIA AI tie-up) are rare exceptions whereas
most partnership news produces no intraday price action at all.

---

## 8. Reliability and failure policy

| Failure | Behaviour |
|---|---|
| Finnhub quote down | 3 retries (1s/2s/4s) RTH; `fast=` single-attempt inside the time-boxed pre-market eval (v19.1); position monitor falls back to Twelvedata bar close |
| Benzinga feed down | per-cycle WARNING; after 10 **consecutive** failed fetches (~10 min) one `benzinga_outage` `system_events` row + ERROR (v19.1 — previously a dead feed looked identical to a quiet news day) |
| Twelvedata down | 3 retries (1.5s/3s/6s + 429-aware) RTH; `fast=` no-retry inside the time-boxed pre-market eval; signal parked in the **retry queue** (5-min TTL) — previously "will retry next cycle" was a lie because the freshness filter dropped the aged article (SPCX, Jun 12) |
| Twelvedata daily backstop hit | **v17: hard gate** — `credits_exhausted()` short-circuits every call *before* HTTP once past 49,900 (Grow plan: soft cap 50,000 − 100 headroom; was Basic: 780 = 800 − 20). System stops trading (no bar data = no confirmation = fail-closed) but keeps scoring news; logged once/day + `system_events` row. Backstop is a safety ceiling — the Grow plan has no hard daily cap. |
| Twelvedata per-minute limit | **v17.1: token bucket** — `_claim_minute_token()` blocks the call and returns `None` if the 55-call/minute budget is spent (was Basic: 8/min burst caused systematic 429 storms at market open with 35 pre-market candidates). No HTTP, no backoff. |
| Claude outage / overload (529/500/network) | typed-exception handled: fail-closed (no scores → no trades) + short cooldown (`_CLAUDE_OUTAGE_COOLDOWN_SECONDS`, 120s) so we don't hammer a struggling API; auto-resumes; `system_events` row (`claude_outage`) |
| Claude out-of-credits / billing (403 `billing_error`) or auth (401) | does **not** self-heal — CRITICAL log + long cooldown (`_CLAUDE_BILLING_COOLDOWN_SECONDS`, 30 min) + `system_events` (`claude_billing_error`/`claude_auth_error`) so the journal shows what actually broke |
| Both feeds down with open position | TP/SL skipped that cycle; time stop still fires (needs no price) |
| Quote frozen / stale (source up, data dead) | **v19.2:** quote `t` older than 20 min → treated as no coverage (falls to next source / fail-closed). A frozen print is not a price (GLASF: entry, P&L, and every exit limit priced off a $12.50 quote that never moved) |
| Malformed payloads (wrong types, NaN, nulls, mis-scaled values) | **v19.3: normalized at every seam** — Finnhub quotes require a positive finite `c`; Twelvedata quote fields coerce individually (bad secondary field ≠ dead quote); bar arrays must be lists and non-dict bars are skipped; Claude records validated individually with out-of-range values REJECTED not clamped; article-feed nulls/scalars/bare-string tickers skipped per element. Contract enforced by `tests/test_adversarial.py`: garbage may never crash a cycle nor produce an approval |
| Exit limit sells never fill | **v19.2: escalation** — after 3 consecutive unfilled limit attempts for one trade, the next attempt is a market order; one-per-day `exit_stuck` `system_events` row (warning). Execution certainty over slippage control once the bounded path has demonstrably failed |
| DB down | 3 retries on OperationalError; eval-loop writes never block the trading path |
| T212 symbol map 429 at startup | retries with 30s backoff + daily 08:00 UTC rebuild (a single startup 429 used to poison the whole session) |
| Service crash | systemd `Restart=always` + deploy-time config validation + post-restart health check + **heartbeat table** (below) |
| Silent zero-trade drought | **v17: tripwire** — `check_zero_trade_drought` (startup + daily 21:30 UTC) fires CRITICAL + `system_events` after `ZERO_TRADE_ALERT_SESSIONS` (3) consecutive NYSE sessions with no trades while up and scoring. Alerts only; never stands the system down (a drought can be a legitimately bad tape) |

**Heartbeat / alerting:** every job updates `heartbeat(job, last_beat_at)`.
Grafana alert query (fires when the news cycle is silent >10 min):

```sql
SELECT EXTRACT(EPOCH FROM (NOW() - last_beat_at::timestamptz)) / 60 AS minutes_stale
FROM heartbeat WHERE job = 'news_cycle';
```

**System events / degradation alerting (v17):** the heartbeat catches a *dead*
process, but the nine-session drought (above) had green heartbeats the whole
time — the process was alive and degraded. `system_events(event_type, severity,
detail, created_at, event_day)` records the silent-failure class so a
degraded-but-up system is visible. One row per `(event_type, event_day)`,
de-duped atomically by a UNIQUE index + `ON CONFLICT DO NOTHING` (safe under the
8-worker pre-market pool). Critical types:
`twelvedata_credits_exhausted`, `claude_billing_error`, `claude_auth_error`,
`zero_trade_session`; warning: `claude_outage`, `benzinga_outage` (v19.1 —
these can self-heal), `exit_stuck` (v19.2 — a position's limit exits failed
repeatedly and the monitor escalated to a market order). Grafana alert query (fires on any critical event today):

```sql
SELECT event_type, detail, created_at FROM system_events
WHERE severity = 'critical'
  AND (created_at::timestamptz AT TIME ZONE 'Europe/London')::date =
      (NOW() AT TIME ZONE 'Europe/London')::date;
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

**Anchoring (v18):** returns are measured from `max(publish_time, session
open)`. yfinance serves RTH bars only, so measuring a pre-market article "from
publish time" resolves both window endpoints to the same 09:30 bar and records
an exact 0.0 — this poisoned 39% of the table before 2026-07-03, concentrated
on the pre-market earnings/FDA/M&A block. After-hours articles roll to the
NEXT session's open. Any historical analysis run before the v18 recompute
(including the v17.5 partnership ruling) must be re-validated: a suspicious
`median = 0.000` is this bug's fingerprint. The job drains its FULL backlog
nightly (batched, up to 12,500 rows) — the old single 500-row pass fell
~500 rows/day behind and was quietly heading for permanent-NULL territory
once rows aged past yfinance's ~30-day 1-min history window.

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

One deliberate exception (v19.2): the *data-availability* behaviours — quote
staleness, the RVOL daily-bar-lag rescue, `insufficient_data`, and the
transient re-eval queue — have no backtest counterpart, because retrospective
bars are always present and current. They alter WHICH data feeds a gate, not
the gate's threshold or order, so `TestBacktestParity` still holds; but a
backtest can never reproduce a stale-quote entry or a lag-rescued RVOL.

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

## 12. 24/5 extended-hours trading (v21, `market/sessions.py`)

T212's 24/5 offering splits the day into four sessions (ET): premarket
04:00–09:30, regular 09:30–16:00, after-hours 16:00–20:00, overnight
20:00–04:00. `market/sessions.py` classifies the current instant from the
NYSE calendar (early closes shift the after-hours window with the close:
13:00 close → after-hours 13:00–17:00 ET). Session policy:

| Session | Entries | Why |
|---|---|---|
| regular | always | unchanged pre-v21 pipeline |
| afterhours | `AFTERHOURS_TRADING_ENABLED` (default on) | FDA/guidance catalysts print 16:00–17:30; previously slept through |
| premarket | `PREMARKET_TRADING_ENABLED` (default off) | scanner + at-open gap-and-go already trades this news with confirmation; 4am books are thin |
| overnight | never | Blue Ocean venue — no Finnhub/Twelvedata coverage; no bars → no confirmation → no trade |

### The extended regime, gate by gate

Extended sessions reuse the standard pipeline with these variants
(`confirm_price_signal` branches on the session):

- **Anchored session analysis** — one prepost 1-min pull (960 bars), every
  aggregate restricted to bars at/after the session boundary. The
  accumulation test runs against the after-hours VWAP; a full-day VWAP is
  dominated by the pre-news RTH tape and would reject every legitimate
  after-hours mover as `overextended` by construction.
- **Session-start block** — the 5-min `opening_block` applies at 16:00 too
  (closing-auction unwind noise).
- **RVOL band → absolute participation floor** — the RVOL time-of-day curve
  is RTH volume shape; meaningless at 17:00. Instead: dollars printed
  in-session ≥ `EXTENDED_MIN_SESSION_DOLLAR_VOLUME` ($500k). Transient
  (`low_volume`) — re-eval queue re-checks as the tape builds.
- **Liquidity floor raised** to `EXTENDED_MIN_ADV_DOLLAR` ($50M ADV$): only
  institutional-depth names, matching T212's own 24/5 eligibility universe
  ("most liquid NYSE/NASDAQ securities").
- **Spread proxy ceiling tightened** to `EXTENDED_MAX_SPREAD_PCT` (1.5%).
- **No open-price momentum fallback** — there is no auction print to fall
  back to; no anchored bars yet → defer, never confirm on a bare quote.
- **Price freshness** — quote sources can serve the 16:00 close for minutes
  after a catalyst; confirmation uses the fresher of the quote and the
  newest anchored bar close.

### Extended execution (why the v20 exit inversion pauses out here)

T212 stop orders execute in regular hours only, and the public API accepts
`extendedHours` on MARKET orders only (the executor feature-detects the
limit-order case per process via `_extended_limit_supported` and will use
bounded-slippage limit exits automatically the day T212 accepts the flag;
`scripts/probe_t212_extended_hours.py` checks the current state on demand).
Consequences, all deliberate:

- **No resting stop on extended entries** — it would reserve the shares
  while protecting nothing until the next RTH open. The monitor polls BOTH
  sides at its 5s cadence — the pre-v20 regime, accepted here because the
  eligibility gates (deep book, tight spread, half size) bound what a 5s
  poll can cost, unlike the thin small-caps that motivated v20.
- **Half-size entries** (`EXTENDED_SIZE_FACTOR` 0.5) — risk cut at sizing
  since the loss side is polled.
- **Extended buys verify their fill** — an extended-hours market order that
  doesn't fill promptly is queued (not 24/5-eligible, or an uncrossable
  book); it is cancelled and the entry fails, because a queued buy filling
  at the next open is the gap-and-crap trap with extra steps.
- **Extended sells verify their fill** — never assumed filled; unfilled →
  cancelled → monitor retries next cycle (stuck-exit escalation unchanged).
- **After-hours flatten** — everything force-closed
  `EXTENDED_FLATTEN_BUFFER_MINUTES` (15) before 20:00 ET. Non-negotiable:
  past 20:00 the tape moves on a venue we cannot see. Premarket positions
  (if ever enabled) carry into RTH instead — data and management are
  continuous across 09:30, and the EOD flatten covers them.
- **Ratchet** arms the polled breakeven only (no resting re-place) in
  extended sessions.

RTH behavior is byte-for-byte unchanged when `EXTENDED_HOURS_ENABLED=false`,
and the EOD flatten before the 16:00 bell still applies to every RTH
position regardless — holding an RTH position into the after-hours book is a
different (unmeasured) trade and stays out of scope.
