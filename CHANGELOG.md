# Changelog

All algorithm changes are recorded here. Each version notes what changed,
why it was changed, and the date it was deployed to production.

Format: `## v<N> — YYYY-MM-DD`

---

## v21.16 — 2026-08-18 (enter at the signal; drop the class that doesn't pay; the prompt cache was never on)

Three changes from one review. The entry gates were never the binding
constraint — **the entry price was**.

### 1. The momentum gate is a real filter that costs more than it is worth

`low_momentum` parks a signal in the 15-min re-eval queue until the tape starts
moving. It genuinely selects better stocks: signals that eventually cleared it
returned **+0.86%/60m (n=20)** against **+0.11% (n=66)** for those that never
did. But clearing it takes TIME, and the queue means we buy *after* the move
instead of into it.

The bill arrives at the stop. Entering ~2% higher leaves the −2% stop sitting at
the price the news originally fired at, where ordinary reversion takes us out.
LAMR (2026-08-06) is the clean case: signalled at $161.09, filled at $164.30 —
above the stock's high for the entire session — stopped out 28 seconds later on
a drift back to $161. Not isolated: of the last five closed trades, **three
never traded above their own entry price at any point** (`max_favorable_pct`:
ITT −0.12%, CEG −0.36%, LAMR −1.91%).

Simulated over every signal from 2026-06 → 08 with the live exit rules (−2%
stop, +5% take-profit, 120-min hold, 0.46pp round-trip costs):

| class | n | gross | **net/trade** | win rate | t |
|---|---|---|---|---|---|
| guidance_raise | 193 | +1.127% | **+0.667%** | 50% | 5.51 |
| fda_approval | 106 | +0.314% | **−0.146%** | 32% | 1.44 |

Live, for comparison, is **−1.65%/trade at a 21% win rate over 29 trades**. The
mechanism is entirely in the stop-out rate: 48% live, 33% simulated at the
signal price. We are buying a better horse at a worse price and losing on the
deal.

`SKIP_MOMENTUM_CATALYSTS` (default `guidance_raise`) skips the momentum FLOOR
for the listed classes. Deliberately narrow, in three ways:

- **Flat tape only.** The gate already distinguished "nobody has reacted yet"
  from "sellers are in control despite the positive catalyst", and only the
  first is what costs us. The backtest cannot tell them apart — it sampled
  prices at 5/15/60/120 min, so a signal that was −4% at the moment of entry
  looks identical to a flat one — so buying active selling was never the
  measured strategy and stays rejected (still TRANSIENT, so it re-evaluates).
- **Floor only.** The momentum *ceiling*, VWAP, RVOL, liquidity, spread, the
  day-move ceiling and entry-quote freshness all still reject. `cfg.validate()`
  now refuses `SKIP_MOMENTUM_CATALYSTS` without `REQUIRE_VWAP_CONFIRMATION`:
  VWAP is the remaining market-agreement evidence once the floor is off, and
  nothing else coupled these two independent knobs.
- **Regular-hours signals only.** The pre-market gap-and-go path deliberately
  does NOT pass `catalyst_type`, so its floor stays enforced — a gap candidate
  is on the watchlist *because* the move already happened, which is the
  opposite of the case that was measured.

### 2. `TRADEABLE_CATALYSTS` pruned to `guidance_raise`

v20 ranked classes by **raw forward return**, which asks "does the stock drift
up?" — not "does a trade with our stop survive to collect the drift?".
fda_approval's +1.42%/60m is real but too small and too volatile to clear a 2%
stop plus costs: a 32% simulated win rate against the **33% break-even** that
Trading 212's 0.15%-each-way FX conversion imposes.

t=1.44 means *no measurable edge in either direction*, not "proven to lose", so
this is a capital-allocation decision rather than a verdict: at ~0.5 trades/day
throughput is the scarce resource, and splitting it with a zero-edge class
halves the sample on the class measuring +0.667%. Live P&L cannot arbitrate —
fda_approval has exactly **one** closed trade (−0.38%). The class is still
scored and still accrues forward returns while switched off, so re-enabling it
is a one-line config change backed by fresh evidence.

### 3. `trades.signal_price` — measure the wait instead of arguing about it

The cost of waiting was only estimable from `sentiment_scores` forward returns,
which sample at 5/15/60/120 min and cannot see the path in between. That
sampling is exactly why the simulation's 33% stop-out rate is optimistic against
a live 48% — and that gap is the single number deciding whether change 1 was
right. `buy_price − signal_price` on a real row settles it per trade.

Carried through the re-eval queue (`_queue_reeval(..., signal_price=)`) so it
records the price at the signal's FIRST evaluation, not at entry — passing
`confirmation.current_price` at entry time would have reported a wait cost of
exactly zero on every trade. Non-finite and ≤0 values store as NULL rather than
0.0: the graduated pre-market hand-off builds a synthetic confirmation with
`current_price=0.0`, and stored as 0.0 every downstream query divides by zero.
Pure observability — no entry, exit or sizing decision reads it.

### 3b. Entry provenance — `entry_reason` / `entry_momentum_pct` / `entry_delay_seconds`

`signal_price` alone is not enough to judge change 1. It records what waiting
**cost**, not **which path produced the trade** — and a trade that confirmed on
its first look because momentum was already present looks identical, in price
terms, to one that confirmed because the floor was skipped. Both show a wait cost
of ~zero. Without a recorded path there is no control group and the change is
unfalsifiable in hindsight.

- `entry_reason` — `momentum_confirmed` | `momentum_skipped` | `premarket_gap`.
  Set from a new `PriceConfirmation.momentum_skipped` flag that gate 7 raises, so
  it is recorded from the gate that actually made the decision rather than
  re-derived later. `premarket_gap` is separate on purpose: the overnight move
  already happened, so pooling those rows into either momentum bucket would
  corrupt the comparison.
- `entry_momentum_pct` — the reading the decision was made on, so the outcome can
  be **regressed against it** rather than only bucketed. The open question is
  where between "flat" and "clearly moving" the edge lives, and a boolean can't
  answer that.
- `entry_delay_seconds` — 0 on a first-look entry, real seconds out of the
  re-eval queue. Separates "2% wait cost over 30 seconds" (a fast mover) from
  "2% over 14 minutes" (the queue grinding).

`news_cycle()`'s premarket approved-entry loop was extracted to
`_enter_premarket_approved()` so its `entry_path="premarket_gap"` label is
testable at the call site — a caller that silently stops passing it produces
plausible-looking rows in the wrong group, which is worse than a crash. The
extraction also brought that loop's risk-gate abort and its per-candidate
exception isolation (the 2026-06-11→07-06 drought shape) under test for the
first time.

### 4. Prompt caching has never worked — it was a silent no-op

`cache_control` is a request HINT. **Claude Haiku 4.5 will not cache a prefix
below 4,096 tokens, and says nothing when it declines**: the field is accepted,
ignored, 200 OK, and `usage` reports zero cached tokens. Measured across the
first 1,140 Claude calls, `tokens_cached` summed to **exactly 0** — while Qwen,
whose OpenAI-compatible endpoint caches automatically with no minimum,
accumulated 38,272. The cached prefix (tools + system) was ~3.5k tokens, about
600 short, so every call since v21.14 paid full input price for the rubric
(3.18M full-price tokens in the last 7 days alone).

Fixed by giving the rubric content it should have had anyway: **worked examples
drawn from real production misclassifications** — the BE post-earnings-rally
explainer, the CRCL 3-ticker digest, acquirer-vs-target, offering dilution, and
a `guidance_raise` boundary section (what is and is not a raise). This is the
deeper fix for three downstream regexes: `_DIGEST_RE` and `_EXPLAINER_RE` patch
two specific headline SHAPES, while the examples teach the principle behind
them. With `guidance_raise` now the only tradeable class, both of its error
directions cost money, so its definition is spelled out explicitly.

Two guards, because an estimate is not a measurement:
- `_note_cache_usage()` raises `claude_cache_ineffective` after 25 consecutive
  calls with zero cached tokens. Deliberately **warning, not critical** — a cost
  regression, not a trading outage; nothing stops being scored and no trade is
  affected. Adding it to `_CRITICAL_EVENT_TYPES` would page a human for a
  billing inefficiency and dilute the four event types that mean the system has
  stopped working. That exclusion is a decision recorded in the code, not the
  v21.15.1 oversight repeating.
- `tokens_cached` now counts cache **creation** as well as reads. Anthropic
  reports `input_tokens` as the non-cached remainder only, so counting reads
  alone made the first call of every 5-min TTL window — the write — look
  identical to no caching at all, both in this column and in the cost figures
  `analysis/classifier_compare.py` derives from it.

### Grafana

Four panels: *Entry Path: did entering at the signal work?* (the verdict panel —
buckets closed trades by the RECORDED `entry_reason`, comparing win rate, stop-out
share and realised P&L across `momentum_skipped` vs `momentum_confirmed`),
*Entry Path per trade* (full provenance per row), *Classifier Prompt Cache*, and
*Momentum Gate: which classes are still waiting?*.

### Tests

`tests/test_v21_16.py` (38 tests), **mutation-tested — 13 mutations, 13 caught**:
deleting the against-the-tape guard, making the skip ignore `catalyst_type`,
reverting `tokens_cached` to reads-only, dropping `signal_price` from the queue
or from `open_trade`, deleting either new prompt section, removing the
once-only alert latch, and removing the VWAP/skip coupling check from
`validate()` each fail at least one test. The provenance columns were mutation-tested separately and three gaps were found and closed: nothing asserted that the premarket CALL SITE passed its label, that `_enter_confirmed` fed the real momentum reading through, or — worst — that the re-eval path handed `first_seen_at` to the entry, which would have made every trade that DID wait record a delay of zero. ⚠️ Run mutation checks on a COPY of the repo: an in-place mutate/restore script corrupted `news/fetcher.py` and left a stale `__pycache__` that made three tests fail against source that was actually correct. `TestCatalystPrune` was rewritten to
assert the *invariant* (no measured-negative class is tradeable by default)
rather than restate the current list.

The cache-threshold test derives its chars/token ratio from production
(`min(tokens_in)=3641` for a 1-article batch against a known 10,511-char prefix
→ 2.94–3.24 chars/token) and asserts against a pessimistic 3.5, so it cannot
pass on a favourable tokenizer and fails if the prompt is trimmed ~9%.

**⚠️ Operational note:** the momentum skip is the change with live risk. The
simulation's 33% stop-out rate is optimistic by construction, and the edge
survives only until the true rate reaches ~50% — live is currently 48%. Panels
25/26 are the read-out; ~20 new trades are needed before they mean anything.
Reverting is a one-line config change (`SKIP_MOMENTUM_CATALYSTS=`), no code
deploy required.

---

## v21.15.1 — 2026-08-18 (cap the batch; make the v21.15 tests actually test v21.15)

Review pass on v21.15. The diagnosis held up; the fix and its tests did not.

**1. Raising the budget rescaled the cliff — capping the batch removes it.**
`max(1024, n*150 + 256)` is still a function of `n`, and nothing bounded `n`:
`to_score` is the entire unscored backlog, and a truncated cycle *grows* it
(`_mark_scored` fires only on success). So the death spiral was intact, just at
a higher trigger threshold. `_batch_score_sentiment` now chunks to
**`_MAX_ARTICLES_PER_BATCH` (25)** and merges — exactly what
`backtest/backtest.py` has done for years — making `max_tokens` a bounded
constant (4,006 against a measured need of ~1,800) and limiting any single
failure to one chunk instead of the whole cycle. Every property of the outage
(deterministic recurrence, self-reinforcement, session-boundary clustering) was
a consequence of unbounded `n`, not of the constant 60.

**2. The v21.15 tests were vacuous — proved by mutation.** Reverting
`max_tokens` to the exact original bug left **all 45 tests green**: both tests
with "budget" in the name asserted against the *shadow* (Qwen) client, and
nothing anywhere inspected the live Claude call. Deleting the truncation branch
left **6 of 7** green: both truncation tests used `content=[]`, which falls into
the pre-existing `no_tool_use` branch and produces byte-identical behaviour,
rather than the real truncated shape — a `tool_use` block whose `input` never
finished serialising. Rewritten: the budget is asserted on the live call across
three batch sizes, the truncation tests use the real shape and are paired
against a non-truncated control, the cooldown test runs
`_EMPTY_BATCH_COOLDOWN_TRIGGER + 1` cycles (one cycle could never reach the
trigger, so the old assertion could not fail), `tokens_out` recording is pinned
(it is the entire evidence chain), and two tautologies are gone. Both mutations
are now caught.

**3. `python -m backtest.backtest` was broken, and v21.15 would have crashed
it.** `sentiment, confidence = scores.get(article_id, ("neutral", 0.0))`
unpacks a **five-key dict** into two names → `ValueError`. It never fired
because this path's 20-article chunks were *also* truncating under the old
budget (20×60+64 = 1,264 vs ~1,400 needed), so `scores` was always empty and the
default tuple was always used — i.e. **the legacy replay has been silently
scoring nothing**. Fixing the budget made the latent crash reachable.

**4. `claude_truncated_batch` raised no alert.** It was not in
`_CRITICAL_EVENT_TYPES`, so it landed at `warning` while the documented Grafana
alert queries `severity = 'critical'`. A session-long scoring blackout would
have produced one routine-looking row. Now critical — it is the same
operational state as a billing error (no scores → no signals → no trades) and,
unlike an empty batch, cannot self-heal: the remedy is a code change.

**5. Our config bug was being charged to the provider.** `truncated` written as
`ok=False` degraded Claude's `success_rate` and `worst_failure_streak` — the
figure `classifier_compare` tells the reader to weigh above all others — in the
report used to decide whether to *replace* Claude. That is precisely the
accounting error v21.14.1 removed on the Qwen side, mirrored. `truncated`,
`bad_shape`, `client_unavailable` and `dropped_backlog` are now excluded from
liveness and reported separately as `OUR-FAULT calls`.

**6. `_record_claude_event` was not `live`-guarded** while
`_record_claude_call` two lines above it was. `record_system_event` stamps
`event_day` from *today* and de-dupes on `(event_type, event_day)` with `ON
CONFLICT DO NOTHING`, so one backtest replay could consume the day's only alert
slot and silently suppress the genuine production event. All six event call
sites now pass `live`.

**7. Shadow truncation detection was broken three ways.** `finish_reason` was
read *after* the `no_tool_call` early return, so a Qwen completion truncated
before it emitted a tool call was filed as the provider refusing to use the
tool; the success path never consulted it at all, so a truncated-but-parseable
completion was recorded as `ok=True` with partial data (Claude in the identical
situation records a failure and saves nothing — a liveness *and* coverage bias
in the challenger's favour on exactly the largest batches); and there was no
shape guard, so a `classifications` value arriving as a JSON *string* would
report `scored_count` = the string's character length, while a non-dict payload
raised `AttributeError` into a DEBUG swallow and wrote **no row at all**.

**8. The budget formula was duplicated six times.** v21.15 shared the two
constants but copied the arithmetic, so a changed overhead term would still
have diverged silently — with both parity tests green, since they compared the
shadow against a third copy. One shared `_output_budget()` now.

**Also:** the cache-hit column computed `cached / in` uniformly, but
`tokens_in` means the uncached remainder for Anthropic and the *total* prompt
for OpenAI-compatible providers — producing hit rates above 100% for Claude.
And the orphaned `# …~55 tokens per article empirically; 60 gives` comment, left
dangling directly above the block disproving it, is deleted. `docs/algorithm.md`
(which still described the root cause as "unconfirmed") and
`docs/api_reference.md` (which still published the killed formula as a code
sample) are updated.

**Standing finding, not fixed here:** prompt caching has **never worked**.
1,072 Claude calls, zero cached tokens ever recorded. Claude Haiku 4.5's minimum
cacheable prefix is 4,096 tokens; the system prompt plus tool schema measures
~3,000-3,200, and below the minimum `cache_control` silently no-ops. The
"~90% input-cost reduction" claim in the code and docs is false. Tracked
separately.

---

## v21.15 — 2026-08-14 (the "empty batch" outages were our own truncation)

**The single most consequential bug found so far.** Three releases "fixed" it
without touching the cause.

`_batch_score_sentiment` budgeted `max(400, n * 60 + 64)` output tokens, from a
code comment claiming "~55 tokens per article empirically". Measured against
real `classifier_calls` rows, a classification actually costs **68-72 tokens per
article** — *above* the allowance. So once a batch grew past the point where the
fixed overhead stopped covering the gap, Claude's response was cut off mid
tool-call.

A truncated forced-tool-use response still returns **200 OK with a `tool_use`
block**, but its `input` never finished serialising — so
`block.input.get("classifications", [])` yields `[]`. Indistinguishable from a
genuine empty answer unless you read `stop_reason`.

### The evidence

Production rows, 2026-08-12..14:

| | |
|---|---|
| Claude calls with `tokens_out` **exactly equal** to the cap | 26 |
| Of those, calls recording `scored_count = 0` | **26** |
| `empty_batch` errors in the same window | **26** |

A 1:1 match, with `tokens_out` landing on the cap to the token
(`25×60+64 = 1564`, `21×60+64 = 1324`, `16×60+64 = 1024`, …).

### Why this explains everything the old theory did not

- **Why retries never worked** (v21.7 one retry, v21.12 three). Same batch, same
  size, same deterministic truncation — a retry re-runs the failure. 2026-08-06
  burned ~174 API calls across 58 cycles proving this.
- **Why it self-reinforced.** `_mark_scored()` only fires on success, so a failed
  cycle re-offers the same articles *plus* new ones. The batch grows, truncates
  harder, and the next one grows again. A death spiral, not a flaky API.
- **Why it always self-healed without intervention.** Articles aged out via
  `max_age_minutes`, shrinking the batch until it fit again.
- **Why it clustered at premarket and session boundaries** (2026-08-04 07:00 ET,
  2026-08-06 07:00-08:38 ET, 2026-07-27 16:06 ET). That is exactly when the
  overnight/boundary backlog makes batches largest.

The v21.13 cooldown, ironically, was the closest thing to a real mitigation —
standing the classifier down let the backlog age out, which shrank the batch.
It treated the symptom by accident.

### Fixes

1. **Budget raised and named**: `_TOKENS_PER_ARTICLE = 150`,
   `_MIN_OUTPUT_TOKENS = 1024` — roughly 2× measured need. `max_tokens` is a
   ceiling, not a charge: only generated tokens are billed, and forced tool use
   with a strict schema means the model cannot ramble to fill it. Under-budgeting
   has cost entire sessions; over-budgeting costs nothing.
2. **Truncation is now visible**: `stop_reason == "max_tokens"` is detected,
   logged as our budget fault, recorded as `error_type='truncated'`, and raises a
   `claude_truncated_batch` system_event. It returns immediately without
   consuming a retry and **without tripping the empty-batch cooldown** — a
   configuration bug must not stand the classifier down for 120s.
3. **Shadow shares the live constants** by import rather than a copied formula.
   The v21.14.1 duplicate truncated 10 Qwen batches in three days, every one with
   `tokens_out` exactly at the cap, all filed as Qwen's own `truncated` failure —
   our sizing blamed on the provider, concentrated on the busiest cycles, biasing
   the paired sample toward small batches.
4. The v21.14.1 test that asserted the old formula was **defending the bug**. It
   now asserts parity against the live constants instead.

### Zero-trade investigation (08-12, 08-13) — no gates changed

12 tradeable-catalyst positives; **10 printed premarket**, the same structural
pattern as the previous stretch. The two that reached regular-hours confirmation
were both correctly rejected on the `extended_move` ceiling:

- **SMCI** — `guidance_raise`, already **+13.99%** on the day.
- **BIRK** — `guidance_raise`, already **+17.39%** on the day.

**RIGL** (`fda_approval`, conf 0.90, mag 4 — the strongest signal of the period)
graduated to the at-open evaluation and was rejected on `low_momentum`: −0.38%
with RVOL 0.1 and a day change of +0.05%. An FDA approval the tape ignored
entirely.

The `confidence` gate was checked as a possible cause and ruled out: every
rejection at 5.5-6.5/10 was a `contract_win`/`product_launch`/`other` headline
that Gate 2 would have dropped anyway. No tradeable catalyst was lost to it.

`stale_bars` (v21.14.2) did not fire in this window — neither exercised nor
contradicted.

---

## v21.14.2 — 2026-08-11 (a stale minute bar is not "no coverage")

Investigating a three-session zero-trade stretch (08-07, 08-10, 08-11). The
stretch itself turned out to be the gates working — see the note below — but it
surfaced one real fault.

**SRRK (Scholar Rock, `fda_approval`, conf 0.75) and NVO were both blacklisted
for the entire 2026-08-10 session with "no Finnhub/Twelvedata coverage" — over a
minute bar that was 14.4 minutes old.** They were two of only four
regular-hours tradeable-catalyst candidates that day. Both are liquid, fully
covered listings.

The journal line one above the blacklist says it plainly:

```
Twelvedata: stale bar for SRRK — newest today-bar is 14.4 min old; momentum
            unavailable, session aggregates kept
Price check [SRRK]: momentum baseline unavailable and not in open window
Signal [SRRK_US_EQ] blacklisted for today — no quote after 2 retries
                    (no Finnhub/Twelvedata coverage)
```

The provider answered. `get_session_analysis()` returned usable aggregates. What
was missing was a bar recent enough to measure a 5-minute momentum window
against — which happens on any name whose minute stream has gaps, and says
nothing about coverage. But `confirm_price_signal()` returned `None` for it, and
`main._queue_retry()` counts a `None` as "no provider carries this instrument".
Two of those blacklist the ticker for the rest of the day.

**This is the fourth appearance of one bug.** A blackout strike asserts a
specific claim, and only a miss that actually proves that claim may count one:

| | Miss that doesn't prove it |
|---|---|
| v21.6 | extended-session miss (our plan has no pre/post bars at all) |
| v21.11 | miss during a demonstrably frozen feed |
| v21.12 | one dead ticker polled in a loop, mistaken for a frozen feed |
| **v21.14.2** | **stale bar — the provider answered, the bar is just old** |

Fixed by routing it through the normal transient path as `stale_bars`: parked in
the 15-minute re-eval queue, no strike, and — unlike a `None` — it leaves a
`news_signals` row with a `reason_code`, so the next occurrence is queryable
instead of journal-only.

**The fix is deliberately narrow.** `sa is None` (the session pull returned
nothing) is still a hard data failure and still returns `None` — that is the
EGGF/OXAC infinite-retry loop the blackout was built for. Only a pull that
SUCCEEDED without a recent bar gets the transient treatment.

### On the zero-trade stretch itself — no changes made, deliberately

Every other rejection over those three sessions was correct, and loosening a
gate to manufacture trades would undo thresholds that were calibrated on real
losses:

- **CDNL** (2026-08-11, `guidance_raise` conf 0.85, mag 4) — `dead_cat`: the
  stock was **−20.03%** on the day. A guidance raise on a name down 20% is the
  falling knife that guard exists for.
- **AAON** (2026-08-10, `guidance_raise` conf 0.88, mag 4) — `low_momentum` at
  −3.77%, then `dead_cat` at −3.45%. It raised guidance and sold off.
- **YPF** (2026-08-11, `guidance_raise`) — re-evaluated 15 times over the full
  TTL; momentum oscillated between +0.02% and +0.29% against a +0.2% floor with
  RVOL 0.2–0.3. There was no participation to confirm.
- **SCWO** — `penny_stock` at $2.23.

The structural observation, flagged rather than acted on because it is a
strategy decision: **most tradeable-catalyst news prints premarket**
(07:00–09:00 ET — 12 of the 19 `fda_approval`/`guidance_raise` positives over
these three sessions), and premarket entries are off. By the 09:30 open the move
has usually resolved, sometimes violently against the catalyst (CDNL −20%).

---

## v21.14.1 — 2026-08-11 (review pass: the comparison tool was measuring nothing)

A `/code-review max` sweep over v21.14. Nothing here changes a trading rule.
Almost everything here changes what the shadow-mode data *says*, and the whole
point of shadow mode is to make a decision from that data.

Three of these were silent in the worst way — the code ran, wrote rows, and
printed a report. The report was wrong.

### 1. The comparison tool read every column as its own name (critical)

`analysis/classifier_compare.py::_rows()` did `dict(zip(cols, row))` over
`cur.fetchall()`. But `get_conn()` sets `cursor_factory = RealDictCursor`, so
rows are **already dicts** — and iterating a dict yields its KEYS. Every row
came back as `{'provider': 'provider', 'latency_ms': 'latency_ms', …}`.

Consequences: the liveness loop compared the string `"provider"` against
`"claude"`, matched nothing, and printed `(no data yet)` for both providers
regardless of how much data had been collected; the prediction panel did
`float("fwd_return_5m")` and died with a `ValueError` the moment one paired
article existed. Every other reader in the repo already used `dict(r)`; this
one function did not. **The v21.14 `DISTINCT ON` fix could not have changed a
single reported number until this was fixed.**

### 2. Claude's failures were absent from the liveness record

Claude wrote a `classifier_calls` row only when the HTTP call succeeded *and*
returned a parseable body. It wrote nothing when suppressed by a cooldown, and
nothing from any of its five exception handlers (403 billing, 401 auth, 429,
`APIStatusError`/`APIConnectionError`, generic). Qwen, meanwhile, recorded every
one of its own failures.

`latency_and_liveness()` computes `success_rate` and `worst_failure_streak` per
provider **over the rows that exist**. So a total Claude blackout rendered as
`success_rate = 100%`, `worst_failure_streak = 0` — and the module docstring
tells the reader to weigh that streak above everything else. The fallback
decision would have been made on a dataset with Claude's outages deleted.

Now: `_record_claude_failure()` fires on all six paths, with `latency_ms = NULL`
for a suppressed cycle so the percentiles never see a fake zero. Qwen's
queue-pressure drops and unusable-client returns are recorded too, for the same
reason — those were the periods when Qwen was *most* degraded and they were
being excluded from Qwen's own denominator.

The drop row is buffered in memory and written by the next background job:
detecting a drop happens on the news-cycle thread, where `get_conn()`'s
three-attempt backoff could block the trading loop on a database blip.

### 3. A NaN fill price silently disarmed the stop loss (trading safety)

`float("NaN")` raises nothing, and `_parse_fill()` only caught `TypeError` /
`ValueError`. A NaN made it into `OrderResult.price`, so
`stop_price = price × (1 − stop_loss_pct/100)` was NaN, the broker rejected the
resting stop, and **the position held with no stop at all**. Every downstream
comparison is False against NaN — `current <= stop`, `current >= buy × 1.05`,
the MFE/MAE band, and the executor's own `abs(slippage) > 3.0` sanity check — so
nothing else caught it either. The position could only ever exit via the
time-stop or the EOD flatten.

v21.13 had added a `math.isfinite` guard to the entry-slippage *logging* helper
only. The guard belongs at the boundary: `_parse_fill()` now returns `None` for
any non-finite or non-positive price, routing to the known-safe signal-price
fallback that a missing fill already used.

### 4. Backtest replays wrote into production shadow tables

`backtest/backtest.py` calls `_batch_score_sentiment()`, which dispatches shadow
scoring unconditionally. `python -m backtest.backtest --week` with `QWEN_*` set
therefore replayed historical articles into `qwen_scores` — and because that
table is `UNIQUE(article_id)` with `ON CONFLICT DO NOTHING`, **a replayed row
permanently blocks the real one for the same article**. Replay latency over
20-article batches also entered the p50/p95 that decides whether a provider fits
inside the 60s news cycle, and inflated `min(calls)` toward the readiness
threshold.

`_batch_score_sentiment(live=False)` now governs both shadow dispatch and
`classifier_calls` recording. The backtest passes it, and supplies a ticker.

### 5. The tradeable-set comparison did not match the live gates

`would_trade` reproduced three of the four live gates and used a different
confidence test:

- **Gate 4 (`catalyst_magnitude`) was missing entirely**, though both tables
  carry the column — so signals production drops were counted as trades.
- `confidence >= min/10` instead of production's
  `round(confidence × 10) >= min`. With the deployed threshold of 7 those
  disagree across the whole `[0.65, 0.70)` band: the model says 0.68,
  production rounds to 7 and **trades**, the comparison did not count it.

Both errors fed the forward-return panel the module itself calls *the deciding
metric*. Extracted to `_would_trade()` with all four gates.

### 6. `DISTINCT ON` could keep the leg with no forward returns

`ORDER BY s.article_id, s.ticker, s.id` picked alphabetically.
`forward_returns.py` measures per (article, ticker) **row**, and yfinance
regularly has no bars for one leg of a multi-ticker article — so the kept row
could have all four horizons NULL while the other leg had a measured +3.1%, and
the article then contributed **zero** samples to either model. On an M&A article
the alphabetical pick is as likely to land on the flat acquirer as on the target
that ran 18%. `_MIN_PAIRS_FOR_VERDICT` counts pairs, not returns, so the report
could print "ENOUGH DATA to judge" over a panel silently thinned this way.
Now ordered by `(s.fwd_return_60m IS NULL)` first.

### 7. Shadow validation diverged from the live validator, in both directions

The comment claimed parity; there were four mismatches, and each one reshapes
Qwen's stored distribution for reasons unrelated to model quality. Anything
rejected here but kept by Claude vanishes from the INNER JOIN — which removes
Qwen's **worst** answers from the paired sample and flatters the challenger by
pure survivorship.

| Case | Live path | Shadow (was) |
|---|---|---|
| `sentiment: "Positive"` | lowercased, kept | hard-rejected |
| missing `confidence` | defaults to 0.5 | whole record dropped |
| `catalyst_magnitude: 3.0` (JSON float) | `int()` → 3 | NULLed (`isinstance(3.0, int)` is False) |
| `catalyst_magnitude: 7` | whole record rejected | kept with magnitude NULL |

Out-of-range values are still **rejected, never clamped** — that rule was right
and is unchanged.

Separately, `ok`/`scored_count` are now computed on the **raw** parsed list on
both sides. Computing them post-validation meant a batch of well-formed answers
that all missed the taxonomy was filed under the same `empty_batch` error as a
genuine outage, while Claude emitting identical answers recorded `ok = true`.

### 8. Truncated shadow completions were blamed on the provider

The shadow request sent no `max_tokens` while the live Claude call sizes its
budget explicitly (`max(400, n × 60 + 64)`). A 30-article batch needs ~1,900
tokens of tool arguments; the provider default cut the completion mid-object,
`json.loads` failed, and the row was written as Qwen's own `bad_json`. That
attributes **our** missing parameter to the provider in the exact liveness
dataset this feature exists to produce — and because batch size tracks news
volume, the bias concentrated on the busiest cycles. Now sends the same budget
and distinguishes `finish_reason == "length"` as `truncated`.

### 9. Tests fired real, billable API calls

v21.14 moved shadow dispatch above the Claude cooldown check, so the cooldown
tests — which previously returned before any dispatch — began submitting real
requests to Model Studio on fire-and-forget threads that outlive the test, on
any machine with `QWEN_*` in `.env` (i.e. the machine used to populate the
deploy secrets). The autouse fixture that already guarded the DB writers now
also stubs `news.fetcher.shadow_score` and resets the shadow module's
process-lifetime state.

### Also

- Stale docs corrected. `config/settings.py`, `.env.example` and
  `deploy.yml` all still said the `QWEN_*` secrets were read "only by the
  offline harness `analysis/qwen_eval.py`" and that "the live trading path never
  reads these" — a file that does not exist, and a claim that v21.14 had
  inverted. `docs/database_schema.md` documented a comparison query without the
  fan-out or NULL-leg protections, so the doc and the tool answered the same
  question with different numbers.
- 33 regression tests added (`tests/test_review_v21_14_1.py`). Suite: 443
  passing, 7m32s.

---

## v21.14 — 2026-08-07 (shadow-mode second classifier: Qwen alongside Claude)

Claude is the only external dependency with **no fallback path**, and it has now
failed twice in three sessions in a way retries provably cannot fix (25
consecutive empty-classification cycles on 08-04; 58 cycles / 98 minutes on
08-06, overlapping the premarket watchlist build). Picking a fallback needs
evidence, so this release starts collecting it from live production traffic.

**Every batch sent to Claude is now also sent to Qwen-Flash. Claude's verdict
remains the only one that reaches a trading decision** — nothing in the shadow
path returns a value to the news pipeline.

### Added — `news/shadow_classifier.py`

Fire-and-forget second classifier, with four deliberate safety properties:

1. **Runs on a background thread.** The news cycle never waits for Qwen, so a
   slow or hung provider cannot delay a trading decision by a millisecond.
2. **Single worker, bounded queue** (`_MAX_PENDING=2`). If Qwen is slow enough
   to back up, batches are DROPPED rather than queued — an unbounded queue would
   turn a provider slowdown into a memory leak and a thread explosion. The gap
   in the data is itself the signal.
3. **Every exception caught and recorded.** A shadow failure is data, never an
   incident.
4. **Disabled unless `QWEN_API_KEY` + `QWEN_BASE_URL` are both set**, and
   `cfg.validate()` deliberately does NOT require them — a missing secret
   degrades to "no shadow data", never to a startup crash-loop.

Records are validated exactly as the live path does — out-of-range values are
**rejected, never clamped**. A model emitting nonsense must score as having
emitted nonsense rather than being quietly corrected into looking competent.

### Added — two tables

- **`classifier_calls`** — one row per API call, **both providers**: latency,
  ok/failed, error type, batch size, tokens (incl. cached). The `ok=false` rows
  ARE the liveness record; failures are written as faithfully as successes.
  Claude's half comes from `fetcher._record_claude_call()`, added here — without
  it we would have Qwen's numbers with nothing to compare them to.
- **`qwen_scores`** — one row per article Qwen classified, `UNIQUE(article_id)`
  with `ON CONFLICT DO NOTHING` so a retried or overlapping batch cannot
  double-count and skew agreement statistics. Deliberately carries **no**
  forward-return columns: a forward return is a property of the ticker and
  timestamp, not of the model, so the comparison joins to `sentiment_scores` and
  reuses the values already computed there rather than letting two copies drift.

### Added — `analysis/classifier_compare.py`

Read-only assessment over the shadow data. No API calls, no cost, no side
effects. Design choices that matter:

- **Latency as p50/p95, not mean.** The cycle is 60s; the mean hides the tail
  that would blow it.
- **Liveness as success rate AND longest consecutive failure streak.** The
  streak is the more important figure — 08-06's 58 dead cycles in an unbroken
  run barely move a monthly success rate, yet they are a 98-minute blind spot.
- **Prediction judged on forward returns, not agreement.** A model can agree
  with Claude 90% of the time and still differ on exactly the
  `fda_approval`/`guidance_raise` calls that `TRADEABLE_CATALYSTS` acts on. The
  report shows each model's own tradeable set's forward returns, plus a
  "where they disagreed, who was right" panel.
- **Refuses to imply a verdict the sample can't support** — prints an explicit
  readiness check against `_MIN_CALLS_FOR_VERDICT` (200) and
  `_MIN_PAIRS_FOR_VERDICT` (300).

### Notes

- Qwen reaches Alibaba Model Studio over its **OpenAI-compatible** endpoint
  (their Anthropic-compatible shim would likely ignore our `cache_control`
  blocks, and their caching/function-calling docs are written for the OpenAI
  path). `openai==1.109.1` added to requirements — the live trading path does
  not import it.
- The workspace endpoint is **EU-hosted** (`eu-central-1`, Frankfurt), which
  settles the data-residency question raised when Qwen was first proposed.
- Use the **dedicated workspace Base URL** from the Model Studio console, not
  the shared `dashscope-intl` one; a Token Plan requires it.

---

## v21.13 — 2026-08-06 (retries don't fix an outage; entry slippage made visible)

Deep-dive on 2026-08-06 (4 trades, **−£17.96** — worst day by trade count).
All-time now −£111.57 over 29 trades.

**The trading story: every gap faded, 4 for 4.** ITT, CEG and LAMR were bought
in a five-minute window at the open (hitting `MAX_OPEN_POSITIONS=3` exactly), all
on "company raised guidance" stories; MDT followed at 10:00. Relative to our
fills they closed −4.65%, −3.83%, −2.77% and −0.80%. This was **not** a market
move — SPY traded a 0.26% range across the whole window. The post-earnings
gap-and-go setup simply failed that day.

**Three of the four never showed a single positive tick** (MFE −0.12%, −0.36%,
−1.91%). That is an entry-timing signature, not four unlucky trades.

### Changed — Claude empty-batch handling (the important one)

Retries do not work on this failure and the evidence is now unambiguous:

| version | budget | outcome |
|---|---|---|
| v21.7 | 1 retry | 2026-08-04: **25** consecutive all-empty cycles |
| v21.12 | 3 attempts | 2026-08-06: **58** consecutive all-empty cycles |

2026-08-06 ran 07:00–08:38 ET — **98 minutes, ~174 wasted API calls, nothing
scored** — and unlike 08-04 it overlapped the 08:00–09:30 ET premarket watchlist
build by 38 minutes. Articles aged out of the freshness window unscored.

- **`_EMPTY_BATCH_ATTEMPTS` 3 → 2.** The retry only ever earns its keep on a
  genuinely isolated empty response; across 83 observed failing cycles the extra
  attempts helped exactly zero times.
- **New: after `_EMPTY_BATCH_COOLDOWN_TRIGGER` (2) consecutive all-empty
  CYCLES, stand the classifier down for `_EMPTY_BATCH_COOLDOWN_SECONDS` (120)**
  via the same `_enter_claude_cooldown` machinery already used for 529/billing
  failures. The next two minutes then cost zero API calls instead of ~6, and the
  articles stay eligible (`_mark_scored` only fires on success) so they are
  re-offered when scoring resumes. Any successful cycle clears the streak.
- Fail-closed is unchanged throughout: no scores → no signals → no trades.

The v21.12 `claude_empty_batch` system_event did its job — the outage was visible
in Grafana on both 08-05 and 08-06 rather than needing a journal grep.

### Added — entry slippage instrumentation

`main._record_entry_slippage()` logs the **signal→fill gap** on every entry and
raises a WARNING + one `entry_slippage_high` system_event per day past
`_ENTRY_SLIPPAGE_ALERT_PCT` (1.0%).

LAMR made the case: sized at **$161.09**, filled at **$164.30** — **+1.99%**,
after **34 seconds** (the day's other three entries filled in 3–4s). $164.30 was
above LAMR's high for the *entire session*. The stop then sat 2% below a price we
never chose; the stock drifted back to ~$161 — the price we actually wanted — and
stopped us out 28 seconds after entry.

Re-running all four trades at their signal prices: **−3.70pp vs the actual
−6.17pp gross**. Roughly **£6 of the £17.96 was entry slippage**, and most of
that was this one fill. Until now that was only recoverable by hand-diffing two
log lines against external price data.

**Audit context (18 trades with usable minute data):** 10 buys filled above the
minute bar's high, but nine of those by +0.01% to +0.26% — that is ordinary
bid-ask spread (bars record trades; a market buy pays the ask) and is NOT a
fault. LAMR at +1.14% above the bar high is 5× the next largest and the only
genuine outlier. ITT's *sell* at −1.19% below its bar low is the second.

### Changed — portfolio gates

- **`MAX_OPEN_POSITIONS` 3 → 8**, to stop concurrency capping the sample size
  while the strategy is still being measured. Bounded: 8 × ~5% of portfolio =
  ~40% deployed, and 8 simultaneous −2% stop-outs is ~0.8% of portfolio, well
  inside `MAX_DAILY_LOSS_PCT` (2.0). `MAX_TRADES_PER_DAY` still caps the day at 10.
- ⚠️ **This gate has never rejected a signal** (0 hits since 2026-08-01).
  2026-08-06 hit 3 concurrent but no fourth signal arrived while they were open.
  Throughput is limited by the price gates, not by concurrency, so expect little
  or no change in trades/day from this alone.

### Verified this release (not changed)

- **`stale_volume` worked end-to-end on live data for the first time.** ITT
  09:35:40 rejected (day +8.33%, RVOL 0.07 — implausible); 09:36:40 approved
  (RVOL 0.98) once the volume feed caught up. Deferred, not discarded — exactly
  the transient-code design.
- **`stale_price` is NOT over-rejecting** (the v21.11 watch item, now closed).
  53 firings across 5 tickers at ages 100–256s against a 90s bar. Net effect:
  blocked one −2.00% loss (ESTA), cost one +0.91% (HROW). Keep 90s.
- **The v21.12 distinct-symbol fix held** — zero frozen-feed false alarms.
- `_EXPLAINER_RE` fired 3× (Datadog recap), no false positives observed.
- **`MAX_DAY_MOVE_PCT` stays at 10.** ITT entered at +9.96%, 0.04pp under the
  ceiling, and lost 2.19%. But the 9.9–10% bucket now holds ITT (−2.19%) AND
  GRMN (+3.86%): dropping to 8% would save 2.19 and cost 3.86, still net
  negative. Two points, opposite directions — no change earned.

---

## v21.12 — 2026-08-05 (BE post-mortem: traded a recap; two guards mis-scoped)

Deep-dive on 2026-08-04 (1 trade, −£7.04). Three findings, only one of which
cost money — but all three are failures of *scoping*: a filter that didn't
cover a template, a detector counting the wrong thing, and a retry budget set
one attempt too low.

**The trade.** Bloom Energy, bought $228.99 at 09:35:51 ET, resting stop filled
$223.25 at 09:42:49. Realised −2.82% (−2.51% at price level; the remaining
0.31pp is Trading 212's 0.15% FX conversion fee charged each way — worth
knowing, it lifts the break-even win rate for +5%/−2% from 28.6% to 33.0%).

The entry came from this headline:

> *"Bloom Energy Stock Charges Higher Tuesday: What's Driving the Post-Earnings Rally?"*

That is not news. It is a Benzinga **explainer** — an article about a move that
had already happened. Claude scored it `guidance_raise` / positive / conf 0.75
with **`already_moved` = FALSE**, while the headline says in its own words that
the rally was underway; the stock was already +3.99% on the day at entry.
`already_moved` is the one field that would have blocked the trade.

Two things this was NOT, both checked and ruled out:
- **Not entry slippage.** We approved at $227.04 and filled at $228.99 (+0.86%
  in 4 seconds — a legitimate fill, the 09:35 bar's high was $229.14). Re-run at
  the un-slipped price, the trade still stops out at −2.00%, one minute later.
- **Not a bad day for the gate set.** The eight rejected signals were simulated
  against real 1-minute bars: ROK, SYY, TDG, XGN, NVO and VOYG would all have
  been −2.00% stop-outs; only CTRI reached +5% (and it was rejected on a 3.85%
  spread proxy, then closed −12.57%). **Six losses avoided, one soft winner
  missed — net +7pp saved,** more than the trade lost.

### Changed — news filtering

- **`_EXPLAINER_RE` (`news/fetcher.py`)** — a third pre-Claude headline filter,
  alongside `_ANALYST_ACTION_RE` and `_DIGEST_RE`. Explainer/recap templates
  (*"What's Driving…"*, *"Here's Why…"*, *"…Stock Charges Higher"*, *"…Shares
  Are Trading Higher"*, *"post-earnings rally"*) never reach the classifier.
  Same reasoning as the v20 digest filter: a deterministic title check beats
  hoping the model reads through a template it has demonstrably misread.
  Validated against **2,415 real scored headlines** (2026-07-25 → 2026-08-04):
  49 matches (2.0%), **zero false positives**, and exactly **one** of the 49 had
  scored positive — the Bloom Energy article above.

### Changed — frozen-feed detection

- **A stale streak must now span `_STALE_QUOTE_MIN_DISTINCT_SYMBOLS` (3)
  distinct symbols** before a provider is declared frozen
  (`market/price_check.py`). MZDAY — Mazda's OTC ADR, whose entire 2026-08-04
  range was $3.38–$3.55 — sat in the re-eval queue and was polled **221 times**;
  ten in a row tripped the tripwire on **both** providers (Twelvedata 09:33 ET,
  Finnhub 09:51) while the feeds were healthy: BE was quoted correctly two
  minutes later and traded all session.
- This was not just noise, it closed a loop. `quote_feed_degraded()` suppresses
  the no-quote strikes that would blacklist a dead ticker — deliberately, that
  is the whole v21.11 fix. So MZDAY tripped the alarm, the alarm protected MZDAY
  from being blacklisted, and MZDAY kept polling to re-trip it. All day.
- The original protection is intact: on 2026-07-31 the real outage served the
  previous day's close for **every** symbol asked, SONY included, so it clears a
  distinct-symbol bar trivially. `_note_quote_fresh()` clears the symbol set
  with the streak, so a feed alternating fresh/stale can't accumulate its way
  over the bar.

### Changed — Claude empty-batch handling

- **Retry budget 2 → `_EMPTY_BATCH_ATTEMPTS` (3), with a 2s backoff between
  attempts**, and a **`claude_empty_batch` system_event when all are exhausted**
  (`news/fetcher.py`). The v21.7 single retry is not enough: on 2026-08-04, **25
  consecutive cycles** across two windows (07:00–07:18 and 07:31–07:36 ET) saw
  both the call and its retry return an empty `classifications` list.
- The unscored backlog grew `10 → 19 → 27 → 35 → 36` and then shrank again as
  articles **aged out of the freshness window unscored** — discarded without
  ever being evaluated.
- Impact was contained only because both windows fell before the 08:00 ET
  watchlist build. The same 25 minutes landing on 09:30–09:55 would blind the
  system through its most productive window — and **nothing recorded a
  system_event**, so it was invisible to Grafana and to every monitoring surface
  except a journal grep. Worst case is now ~30s, inside the 60s news cycle.
- A missing `tool_use` block is a *parsing* failure, not an empty batch, and
  still returns immediately without burning the retry budget.

### Verified this release (not changed)

- **The v21.11 cash-collision fix works.** 2026-08-04 is the first day since it
  shipped on which realised P&L went negative — the only condition under which
  `main.py` calls the rate-limited cash endpoint every cycle. 31 July: 64
  rejections, 44 news cycles stood down. 4 August: **0 rejections, 0 stand-downs,
  0 cash errors.**
- **The v21.11 outage guard works.** Zero tickers blacklisted despite the feed
  alarm firing (see above for why it fired at all).
- **The v21.11 exit-excursion fix works.** Trade 25 recorded MAE −2.51%,
  correctly folding in the realised stop fill; trade 24 had an impossible
  +0.75%. ⚠️ **Analysis filter correction:** use `sell_time >= '2026-08-01'`, NOT
  `max_adverse_pct <= profit_loss_pct` — MFE/MAE are gross *price* moves while
  `profit_loss_pct` is net of FX fees, so that comparison wrongly discards valid
  rows.
- **The resting stop earned its keep again.** During the exit the polled feed
  ran a full minute behind: at 09:41 the monitor read $229.22 when the bar range
  was $225.01–$227.79, and at 09:42 it read $227.30 against $223.51–$226.23 —
  both readings *outside* the actual bar. A polled stop would have held while BE
  fell to $219.21 (−4.3%).

---

## v21.11 — 2026-08-01 (NVT post-mortem: the system traded a stale price)

Deep-dive on 2026-07-31 (1 trade, −£6.30). The loss is not the story — the
resting stop took the *best* outcome available after entry. The story is that
**both price feeds froze at the opening bell and the confirmation traded on a
3-minute-old quote without knowing it.**

What happened, in order:

- Finnhub served the previous day's close — timestamped **1,051–1,092 minutes
  old** — for every symbol asked, **SONY included**. Twelvedata's quote was
  stuck at the 09:30 value, still 42 minutes stale at 10:11 ET. 153 stale-quote
  warnings across 9 symbols. The v21.10 `stale_quote_feed` tripwire fired at
  09:31:56, correctly, on its first live day.
- NVT's quote then passed the 20-minute *coverage* check while carrying the
  **09:33 bar's close ($167.37)**. Real tape at the 09:35:57 decision: ~$165.50
  and falling.
- Every gate agreed, because every gate was reading the same stale photograph:
  momentum +1.74% (truly negative), RVOL 0.28 (truly >1 — the first minute
  alone traded ~10% of an average day), VWAP "held" by $1.44 (price was *at*
  VWAP). The low RVOL additionally triggered the size-neutral bypass — the rule
  built for genuinely quiet mega-caps — so the participation gate excused
  evidence that never existed.
- Filled $166.13; stopped out **42 seconds later** at $162.32 (−2.29%).

**The resting stop is the reason this was a small loss.** For all 42 seconds
the monitor's polled price never moved off $167.37 (MFE +0.81%, MAE +0.75% —
it never saw a negative price). A polled stop would not have fired. NVT was
−6.80% at the 120-minute mark and −7.36% at the close.

### Changed — entry gates

- **`stale_price` (new gate 0).** `MAX_ENTRY_QUOTE_AGE_SECONDS` (90s) caps the
  age of the quote an ENTRY is decided on. Deliberately separate from the
  20-minute staleness guard: that one answers *"does any provider carry this
  instrument?"* (failure ⇒ no coverage ⇒ strike toward a blacklist), this one
  answers *"is this price safe to buy against right now?"* (failure ⇒ transient
  reject, re-eval queue, **no strike**). RTH only — extended sessions expect a
  lagging quote and deliberately substitute the fresher anchored bar close.
  `get_quote_with_fallback()` gained a soft `prefer_fresher_than`: when the
  primary lags the entry bar, the fallback is consulted and the fresher wins.
- **`stale_volume` (new gate 5.5).** RVOL and the day move come from different
  sources; when they disagree hard (|day move| ≥ 5% with RVOL < 0.5) the volume
  side is lagging, not calm — a stock cannot reprice several percent on a
  fraction of its normal volume. Defers instead of letting the bypass excuse
  it. Placed *after* the extended-move ceiling so a permanently-too-big move
  stays terminal rather than being re-queued forever. Thresholds are
  conservative by design: the BMY case the bypass exists for (+2.1%, RVOL ~0.3)
  is nowhere near them.
- **`MAX_DAY_MOVE_PCT` 25% → 10%**, calibrated on all 24 closed trades. 25%
  never bound on a real trade. 10% is the tightest ceiling that blocks only
  losers — it removes CRCL (−3.97%), TMO (−2.03%) and NVT (−2.56%) and costs no
  winner; 8% would also cut GRMN (+3.86%, entered at +9.99%). Mean P&L across
  kept trades −0.91% → −0.57%. `cfg.validate()` now refuses a ceiling at or
  below `TAKE_PROFIT_PCT`. n=20 is calibration, not proof.

### Fixed — 44 news cycles a day stood down for a self-inflicted reason

`/equity/account/cash` had three callers on independent schedules. APScheduler
anchors every `IntervalTrigger` to process start and **5 min is an exact
multiple of 1 min**, so the kill-switch call (`news_cycle`) and the
`portfolio_snapshot` call landed on the same instant every fifth minute,
forever — and T212 rejected one every time. `_fetch_cash()` had no retry, and
for the kill switch a failed lookup means *stand the whole cycle down*.

64 rejections on 2026-07-31; **44 of them killed an entire news cycle**. The
trigger is the `if realized < 0` branch, so the system goes ~20% blind for the
rest of the day precisely on days it has already lost money. Per-day counts
confirm the mechanism: 07-27/28/30 (never negative) → 0; 07-29 (briefly
negative) → 2; 07-31 (negative from 09:36) → 64.

Fixed with a lock + short TTL cache (15s; 3s for order sizing) and one retry on
a retryable status. **The lock is the actual fix** — it serializes the racers so
the second finds a warm cache instead of issuing a competing request. This also
absorbed the duplicated v21.7 retry that lived in `calculate_quantity`.

### Fixed — MFE/MAE was wrong on exactly the trades that matter

`max_adverse_pct = +0.75%` on a trade that closed −2.56% is an impossible row.
Excursions only ever saw prices the *polling loop* observed, and a broker-side
resting stop fills without the poller involved — so MAE was biased toward zero
on every stop-out, the trades where "how much heat did this take?" is the whole
question. `_record_exit_excursion()` now folds the realised exit price into the
band on every close path. Rows where `max_adverse_pct > profit_loss_pct`
predate this fix and must be excluded from analysis.

### Fixed — liquid tickers blacklisted for a vendor outage

The v21.6 scoping exempted extended-session misses; a *regular*-session
provider freeze is a session where data is genuinely expected, so strikes still
counted. Two RTH signals (GTES, IRMD) were blacklisted for the day and four of
nine pre-market candidates expired as "no quote after 3 consecutive retries" —
all liquid, fully-covered names. The system already **detected** the freeze
(`stale_quote_feed`, 09:31:56); the blacklist was not listening.
`quote_feed_degraded()` now exposes the live streak, and both strike sites skip
the strike while any feed is frozen. It reads the current streak, not the
once-per-process alert latch, so one outage cannot suppress strikes all day.

### Fixed — phantom tickers

`resolve_t212_ticker()` fell back to `<symbol>_US_EQ` for anything missing from
the T212 instrument map. Since that map is the complete USD catalogue, an
absent symbol is not tradeable and the guess can only produce a phantom.
Benzinga tagged a Moog article with both `MOG.A` (real) and `MOG` (Moog trades
as MOG.A/MOG.B); `MOG_US_EQ` then burned quote retries all morning. Absent
symbols are now dropped; the fallback still applies when the map is empty.

### Observability

Grafana: MFE/MAE columns added to **Trade History** (plus a `left_on_table_pct`
column: MFE minus what was banked) and **Open Trades**; new panel **"Exit
Efficiency: MFE vs realised"** — the panel v21.10's instrumentation was shipped
for. Rejection-funnel description now names the two data-state codes so a spike
in them reads as "the feed is behind", not "the market was quiet".

Docs: `docs/database_schema.md` was four versions behind — added `session`
(v21.1), `stop_order_id`/`ratchet_armed` (v20/v20.1), the MFE/MAE block
(v21.10) and the v21.3 forward-return horizons. `docs/algorithm.md` gained the
two-staleness-questions table, volume plausibility, day-move calibration, the
scheduler-collision post-mortem and the MFE/MAE section.

### Tests

`TestEntryPriceFreshness`, `TestVolumePlausibility`, `TestDayMoveCeiling`,
`TestCashCacheCollision` (including a real 4-thread concurrency test asserting
one HTTP call), `TestPhantomTickerDropped`, `TestExitExcursionRecorded`,
`TestProviderOutageDoesNotBlacklistTickers`. 374 pass.

Two existing fixtures were corrected rather than worked around: `TestRvolBypass`
and `TestSessionVolumeGates` used `pc=10.0` (+5.0% on the day) with near-zero
RVOL — a combination the new plausibility gate correctly calls a data artifact.
Both now use `pc=10.28` (+2.14%), which is what the BMY incident they document
actually was.

---

## v21.10 — 2026-07-31 (MFE/MAE instrumentation; two undetected-outage tripwires)

Deep-dive on the 2026-07-30 session (2 trades, +$0.94 net) produced one
**correction** and three changes. All are observability — no entry or exit
decision changes behaviour.

### Correction: stop-loss slippage is NOT a live problem
An earlier read of the all-time record claimed stop-losses average −4.50%
against a −2% trigger, i.e. that slippage was doubling every loss. That figure
pools two different exit architectures and is wrong as a statement about the
current system. Split by era:

| era | n | mean slippage past the −2% trigger |
|---|---|---|
| polled stop (pre-v20, trades 1–16) | 7 | −2.92% (−0.52% excluding the GOAI microcap) |
| **resting stop (v20+, trades 17+)** | 2 | **−0.06%** (worst −0.25%) |

The v20 broker-side resting stop already fixed this. All-time −$80.27 is
legacy: −$79.10 predates v20; the v20+ era is −$1.17 across 7 trades. No code
change needed — recorded so the pooled figure isn't re-raised as a live bug.

### The real open question: the time-stop cuts live winners
FSS (2026-07-30) exited at +0.73% on the 120-min time-stop and closed **+5.01%**
— it reached $124.85 against a $124.52 take-profit target, so the TP would have
filled had the position been held. Simulating a trailing stop over the 8
time-stop exits with usable 1-min data:

- flat time-stop (actual): **+0.39%** mean
- trail −1.5% below the high: **+0.89%** mean, +1.14% median, 5/8 wins
- trail −2.5%: +1.00% mean but +0.01% median, and its lead collapses to +0.09%
  with the single FSS trade removed — an outlier artifact, not an edge.

Bootstrap on the −1.5% variant: **+0.51%/trade, 95% CI [−0.38%, +1.30%],
P(no real edge) ≈ 12% — not significant at n=8.** Also, the time-stop currently
acts as a *rescue*: on LEVI and SCHW it exited at −1.19%/−0.22% where a
trailing stop ran both to the full −2%. **A trailing stop was therefore NOT
shipped.** The sample is capped at 8 because yfinance retains 1-min bars for
only ~30 days — which is what the instrumentation below fixes.

### Added
- **MFE/MAE tracking** (`trades.max_favorable_pct`, `trades.max_adverse_pct`;
  `storage/database.py::update_trade_excursion`;
  `monitor/position_monitor.py::_record_excursion`): the monitor already
  computes unrealised P&L every 5s — the running extremes are now persisted,
  making the trailing-stop question answerable from our own data in ~30 trades
  rather than being permanently capped by a vendor's retention window.
  Widen-only via SQL `GREATEST`/`LEAST` (correct across restarts and
  out-of-order writes); an in-process cache means a DB write happens only on a
  genuinely new extreme, not every poll; a failed write is deliberately not
  cached so the next cycle retries it. Recorded *before* the exit checks so the
  peak that triggers a take-profit is itself captured. Pure observability — no
  exit path reads these columns.
- **Finnhub sustained-outage tripwire** (`_note_finnhub_failure`,
  threshold 8 consecutive total failures): the v21.9 latch only fires on a
  definitive 401/403. On 2026-07-30 Finnhub instead **timed out on every poll
  for ~5 minutes while a position was open**, each failure logged as an
  isolated per-symbol WARNING with nothing counting them. A 2xx or a 4xx
  (healthy provider, bad symbol) resets the streak, so one flaky ticker cannot
  trip it — only a genuinely unavailable provider.
- **Frozen-feed tripwire** (`market/price_check.py::_note_quote_stale`,
  threshold 10 consecutive stale reads per source): the staleness guard
  correctly *refused* Twelvedata's quote frozen at 14:30 ET for 71+ minutes on
  2026-07-30, but all **23 refusals** were isolated WARNINGs — no signal that a
  price feed had died while a take-profit was being polled. Counted per source
  (Finnhub being frozen says nothing about Twelvedata); any fresh quote resets.

## v21.9 — 2026-07-30 (exception-handling audit: 9 misclassification bugs across every module)

Full-codebase audit for one specific bug shape: a real exception (network
error, HTTP status, malformed response) being swallowed or logged as an
ordinary business outcome ("no data available") instead of a distinguishable
failure. This is the exact shape behind three prior incidents (the CDNS/SANM/
etc. permanent blacklist from a misclassified 403, the ITW zero-retry 429,
the SMCI/naive-ticker-split forward-returns poisoning). Five parallel
subagent reviews covered every production module; each finding was verified
against the actual code before being fixed. Encouragingly, most of the
codebase already handles this correctly (kill-switch fail-closed, GONE/None
order-status discipline, the ratchet's exception handling, reconciliation's
broker-failure-vs-genuinely-flat distinction) — these are the gaps that
remained.

### Fixed
- **`storage/database.py::trading_days_since_last_trade`**: a calendar-library
  exception was caught internally and converted to `None`, which its caller
  already treats identically to "no trades ever exist" (don't alert) — a
  calendar regression would have silently defeated the zero-trade-drought
  tripwire forever. Now propagates to the caller's existing `try/except`,
  which already logs at WARNING and bails correctly.
- **`trading/executor.py`**: introduced `T212HTTPError(status_code, body)` —
  `_get`/`_post`/`_delete` now raise a typed exception instead of a bare
  `Exception(f"HTTP {code} - {text}")` string, so callers can branch on
  `.status_code`/`.retryable` instead of substring-matching a formatted
  message (fragile — misclassifies if a response body happens to contain the
  same digits). `get_order_status`'s safety-critical GONE/None distinction
  now checks `exc.status_code == 404` directly. `calculate_quantity`'s cash-
  lookup retry and a new retry around `buy()`'s actual order-placement POST
  (previously zero retry at all — more consequential than the already-fixed
  cash-lookup path) both now skip the retry entirely for non-retryable
  4xx (401/403/404/400), instead of burning the one retry budget on a
  failure that will recur identically.
- **`market/twelvedata_bars.py::get_twelvedata_quote`**: a `status:error`
  payload was logged at DEBUG with no check for an auth/plan-entitlement
  message — same shape as the CDNS/SANM incident, this time on the Finnhub-
  fallback quote path used for exactly the small-cap/no-coverage names most
  likely to accumulate no-quote strikes. Added a 403 branch and an auth/plan
  substring check (mirrors `_get_time_series`'s existing prepost-403 handling)
  plus a missing catch-all `except Exception` (the function's retry loop had
  none, unlike its sibling).
- **`market/finnhub_bars.py`**: added an auth-failure latch
  (`_note_auth_failure`/`finnhub_auth_ok`, mirrors twelvedata_bars.py's
  prepost-denial latch) — a 401/403 is now recorded once, loudly, with a
  `system_event`, instead of being logged identically to a per-symbol 404.
  Without this, a revoked/expired Finnhub key would present as N independent
  "no coverage" tickers rather than one systemic failure.
- **`main.py::news_cycle`**: the pre-market candidate batch's `try/except`
  wrapped the ENTIRE evaluation+execution loop — one candidate's exception
  silently dropped every candidate queued behind it with no per-candidate DB
  trace. This is the same shape as the 2026-06-11→07-06 zero-trade drought
  (a `TypeError` in `_candidate_to_news_item` that used to abort the whole
  batch). Each candidate is now isolated in its own try/except and gets an
  explicit `rejected` status write on failure.
- **`news/fetcher.py::_fetch`**: a 200 OK whose body isn't the expected
  `{"results"|"articles": [...]}` envelope (schema change, an error wrapped
  in a 200) previously resolved to zero articles AND called
  `_note_benzinga_ok()` — meaning the Benzinga outage tripwire could never
  fire while every cycle silently starved. Now counts as a fetch failure.
- **`monitor/position_monitor.py::_clear_resting`**: the sole fully-silent
  `except Exception: pass` found in this file (otherwise well-engineered
  against this bug class). A failed DB write clearing a resting order id now
  logs at ERROR instead of leaving the DB/in-memory divergence with zero
  trace.
- **`analysis/forward_returns.py`**: (1) `_get_intraday_bars`'s yfinance
  exception was logged at DEBUG — invisible at production's INFO level, so a
  Yahoo-side rate limit/block would silently mark every row in a run NULL
  with no way to tell "genuinely no data" from "the provider is down." Bumped
  to WARNING and added a consecutive-failure counter/system_event mirroring
  the Benzinga tripwire. (2) `_compute_batch`'s call to `t212_to_symbol()` —
  already the site of one repeat ticker-mapping bug (the naive-split bug, the
  SMCI bug in v21.4) — was unguarded; an exception there would have crashed
  the entire nightly batch rather than marking one row unresolved.

### Not changed — lower severity, deferred
A handful of minor log-level/observability nitpicks surfaced (e.g. a fully
silent heartbeat-write swallow in the monitor, unvalidated sentiment/catalyst
enum values accepted without a schema-drift warning) that don't fit the
"exception misclassified as business outcome" pattern this pass targeted —
left as-is pending a future pass.

## v21.8 — 2026-07-30 (exit-horizon fix activated: TIME_STOP_MINUTES 60 → 120)

The v21.3 (2026-07-20) forward-return panel showed `guidance_raise` still
climbing at the 60-min mark (+3.8%/60m, 83% positive) and recommended
lengthening the hold — but the `.env` change was never actually shipped to
the deploy workflow, so production kept running the old 60-min time-stop for
over a week.

Reviewing 2026-07-29's three trades (all `guidance_raise`, all entered at the
open via the pre-market scanner path) gave a third live confirmation:
- **GRMN**: take-profit hit in 9 minutes (+3.86%) while the stock continued
  to an intraday high of $304 and closed +12.6% on the day — the 60-min hold
  was never even the constraint here, but the same undermoved-catalyst
  pattern the panel measured.
- **BIIB**: no clean trend either way for the full hour; closed on the
  60-min time-stop at a marginal +1.17% — a coin-flip exit, not a thesis
  playing out.
- **APH** (smaller catalyst magnitude, weaker confidence) stopped out at
  -2.56% within 90 seconds of entry — unrelated to the hold length, this one
  is an entry-timing issue (bought within $1 of the first-minute spike high).

### Changed
- `.github/workflows/deploy.yml`: `TIME_STOP_MINUTES=60` → `120`.
  `ENTRY_CUTOFF_MINUTES` now set explicitly to `60` (previously unset,
  silently inheriting `TIME_STOP_MINUTES`) so the longer hold does not also
  widen the entry-cutoff window — see docs/algorithm.md §12 for the
  decoupling rationale from v21.3.

### Not changed — needs more evidence
APH's entry-timing issue (chasing the first-minute spike high) is a
different failure mode than the hold-length one and is not addressed by
this change. Worth watching for recurrence before treating it as a pattern.

## v21.7 — 2026-07-28 (ITW post-mortem: broker-side retry gaps; Claude empty-batch retry)

Investigated another zero-trade day (2026-07-28, regular hours). Two eligible
signals (CTS, FELE) correctly failed to confirm real participation and expired
through the normal 15-min re-eval window — no bug there. A third, ITW
(`guidance_raise`, confidence 7, +4.3% day gap, VWAP-held RVOL bypass), cleared
**every** gate, was logged `APPROVED`, and was then lost outright:

```
calculate_quantity for ITW_US_EQ: T212 cash API failed: HTTP 429 - TooManyRequests
```

Only 2 total 429s occurred all day — this was one unlucky rate-limit blip
landing on the one call with zero retry protection, on the hot path of every
single entry.

### Fixed
- **Cash-balance lookup retry** (`trading/executor.py::calculate_quantity`):
  the `/equity/account/cash` call now retries once (2s backoff) on failure
  before giving up. Mirrors the retry pattern already used elsewhere in this
  file (`build_symbol_map`) — this call had none.
- **Pre-broker buy retry** (`main.py::_enter_confirmed`): a `buy()` failure is
  now retried once **only when it failed before reaching the broker**
  (`calculate_quantity` error — `order_id is None and quantity == 0` by
  construction of `buy()`'s early return). A failure after the broker was
  already contacted is never retried here — that would risk a second live
  order for the same signal. Previously `buy_failed` was not a transient
  rejection code and a fully-gated signal had no second chance at all.

### Also found: Claude batch-sentiment scoring returns empty at session boundaries
Not the cause of today's zero-trade day (the two articles that finally scored
after this cleared weren't tradeable/edge-positive anyway) but a real latent
gap found while checking Claude API health: `Batch sentiment: 0/N articles
scored` fired **8 consecutive cycles** today (07:00–07:07 ET, the
`PREMARKET_SCAN_START_ET` boundary) and **6 consecutive cycles** the day
before (16:06–16:12 ET, the regular→afterhours boundary) — both times the
exact first `news_cycle` tick after a session transition, both times a 200 OK
forced-`tool_choice` response with a genuinely empty `classifications` list
(no per-record validation warnings, so not a parsing failure — Claude itself
returned `[]` for a real, non-empty batch). Because `_mark_scored()` only
fires on a successful score, the unscored backlog grew every cycle
(10→11→16→22→21→20→17→11 articles) until it self-recovered, bounded only by
`max_age_minutes=3.0` aging the oldest out. A real catalyst published in
either window would have gone unscored and untraded with nothing but a
WARNING log to show for it.

### Fixed
- **Empty-classifications retry** (`news/fetcher.py::_batch_score_sentiment`):
  a well-formed but empty `classifications` list for a non-empty batch now
  triggers one immediate retry (escalated to ERROR, since this is a real
  data-loss risk, not routine) before giving up and returning `{}`.

### Not changed — needs more evidence
Why Claude returns `[]` specifically at session boundaries is unconfirmed —
plausibly a backlog/complexity edge in the first larger batch after an idle
gap, or an intermittent forced-tool-use quirk. The retry treats the symptom;
if this keeps recurring after the retry, the next step is capturing the raw
`msg` object (not just the parsed result) on an empty response to see what
the model actually produced.

## v21.6 — 2026-07-28 (zero-trade investigation: extended-hours entitlement wall, blackout scoping, opening_block transience)

Investigated a zero-trade session (2026-07-27). Regular hours were **correct
and uneventful**: exactly one qualifying signal all day (OTLK, `fda_approval`
conf 0.90) rejected as a $1.17 penny stock, and the premarket candidate ABT
correctly refused at the open on a tape moving against the signal (−0.25% →
−0.82%). Nothing to fix there — genuine signal scarcity, gates behaving.

The after-hours session was a different story: **14 qualifying tradeable-catalyst
signals, none reachable, nine tickers silently removed from the universe.**

### Root cause: Twelvedata pre/post-market data is not on our plan
Every `prepost=true` request returns HTTP 403 —
`"Pre-market and post-market data are available on the Pro plan (individual)
and the Venture plan (business) and above"` — for every symbol, permanently.
Verified directly against the API: identical requests without `prepost`
succeed, so it is purely the entitlement, not the symbol or the `outputsize`.
Finnhub is no help either: its free `/quote` timestamp freezes at the 16:00 ET
close (confirmed live mid-after-hours, `t` = 16:00:00 exactly), and our own
20-minute staleness guard correctly rejects it. **The v21 extended-hours entry
pipeline has therefore never been able to confirm a signal — 0 of 18 trades
all-time carry a non-`regular` session tag.** The system was fail-closed, as
designed; what it was not doing was failing *quietly*.

### Fixed
- **prepost capability latch** (`market/twelvedata_bars.py`): the 403 is now
  feature-detected once per process and remembered. Subsequent extended-hours
  bar requests short-circuit before any HTTP call — no credits, no 3× retry
  backoff, no 403 storm — and a `twelvedata_prepost_unavailable` system_event
  records it. An unrelated 403 (bad key, symbol not entitled) does not latch.
- **No-quote blackout scoped to sessions where a miss is informative**
  (`main.py`): the blackout means "no provider carries this instrument", so an
  extended-session miss — the *expected* state given the above — no longer
  accrues a strike. This was the real damage: each after-hours earnings
  release cost two strikes and a permanent blacklist, taking **CDNS, SANM,
  CLS, KFRC, LOKB, TFII, SJW, SUI and LC** — all liquid, all perfectly covered
  during RTH — out of the tradeable universe purely for reporting after the
  bell. Sixteen such names had accumulated over six days of uptime.
- **Blackout resets on a new ET trading day** (`main.py`): the original comment
  assumed "a daily restart gives a clean slate", but the service is a
  long-running systemd unit — it had gone six days with `NRestarts=0`. A
  per-day reset is what the design always meant, and it bounds any future
  false positive to one session.
- **`opening_block` is now TRANSIENT** (`main.py`, `premarket/scanner.py`): it
  is the only gate whose condition is a pure countdown — "N minutes since the
  session boundary, block lasts `OPEN_BLOCK_MINUTES`" is guaranteed false
  minutes later — yet it was terminal, so a catalyst printing inside the window
  was discarded outright. Eight signals died this way, four in the current
  tradeable set: **CDNS ×2** (`guidance_raise`, conf 0.88/0.85) at 4.0 and 4.1
  minutes into a 5-minute block, sixty seconds short, plus TXN (07-22) and THRM
  (07-20). Earnings and guidance print in the first minutes after 16:00 ET —
  precisely this window. The block itself is unchanged and still sound
  (auction/MOC noise is real); only its permanence was wrong. This also
  recovers the 09:30 RTH boundary, which needs no plan change to benefit.

### Follow-up (same day): after-hours entries turned off at the config level
The prepost latch makes leaving `AFTERHOURS_TRADING_ENABLED=true` harmless,
but harmless isn't the same as honest — the toggle's own name says trading is
on when it structurally cannot confirm a signal. `.github/workflows/deploy.yml`
now writes `AFTERHOURS_TRADING_ENABLED=false`, so the config reads as what is
actually true. `EXTENDED_HOURS_ENABLED` stays `true` on purpose:
`is_manage_session` (`market/sessions.py`) manages/flattens a position that
leaks into the session regardless of the entry toggle, so this change cannot
strand a position without its exit management. All VM config changes go
through this workflow file + a normal deploy — never a direct `.env` edit on
the VM, which would be silently overwritten by the next push anyway (see the
"Write .env" step, which rewrites the whole file from this hardcoded block).
Re-enable by flipping the flag back once the Twelvedata plan covers prepost
data. Note the eval loop measures after-hours articles from the *next*
session's open (`_bars_and_anchor`), so the measured edge for those catalysts
is already an RTH-follow-through edge the system can trade today — which is
exactly what the v21.6 blackout-scoping fix above protects.

---

## v21.5 — 2026-07-24 (HOG post-mortem: ratchet settle period, polled-price observability)

Trade review of the last few days' fills found HOG (2026-07-23, -1.52%,
time_stop) rode out its full 60-minute hold while sitting below its own
breakeven stop for at least 32 straight minutes with nothing protecting it.

### Root cause
HOG's buy filled 3.6% off its signal price ("book very thin" — the fill
warning already existed and fired correctly). One second later the ratchet
read that same noisy post-fill quote as +3.75%, cancelled the just-placed
−2% resting stop, and tried to replace it with a breakeven stop — the 4th
T212 order call for the position within one second (buy, place stop,
cancel, replace) — and drew an HTTP 429. The replacement never landed, and
the position was protected only by the polled fallback for the rest of its
hold. Independently reconstructed 1-min bars show price stayed below the
never-placed breakeven line for 32+ minutes; no stop_loss exit fired,
turning what should have been a flat exit into a realized loss. The exit
logic itself traced out correctly — `stop_order_id` was cleared, the
threshold was right, an INFO log would have fired had the polled check seen
price below it — pointing at a lagging live quote on a thin tape (HOG's
RVOL was 0.1 that session; it only got in via the ADV$ bypass) rather than
a logic bug, though this couldn't be confirmed directly: nothing logs a
polled price check that *doesn't* trigger an exit.

### Fixed
- **Ratchet settle period**: `_maybe_ratchet_stop` is not eligible until
  `_RATCHET_MIN_AGE_SECONDS` (15s) after the fill. Keeps a fresh fill's own
  quote noise from reaching the ratchet at all, and keeps the ratchet's
  cancel+replace from stacking onto the buy's own order burst. Legitimate
  ratchets are unaffected (SBRA armed 5+ minutes into its trade).
- **Polled-price observability**: `check_exit_conditions`' holding-state log
  — previously DEBUG-only, invisible at the service's INFO level — now
  promotes once per `_PRICE_LOG_EVERY_SECONDS` (60s) per trade to INFO. The
  price the monitor is actually comparing against during an open position is
  now provable from the logs instead of reconstructed after the fact from an
  external data source.

Also reviewed the 2026-07-21 to 07-24 window more broadly: the dead-cat
guard correctly blocked two guidance_raise headlines (Honeywell, Huntington
Bancshares) where the stock was already down 5-6% despite Claude scoring the
headline positive — a real classifier blind spot on mixed-guidance
headlines ("Raises EPS, Cuts Sales") that the price-confirmation layer
caught as designed. No changes made there; noted for awareness.

---

## v21.4 — 2026-07-22 (SMCI miss post-mortem: forward-returns ticker bug, guidance-raise routing)

SMCI's afterhours $60B-order/margin-guidance pop (+7% that session, +18% more
overnight) never traded. Investigation, not a vendor failure: Benzinga
delivered the article, Claude scored it sentiment=positive — and the code
correctly gated it out, because Claude tagged it `catalyst_type=earnings_beat`,
which v20 pruned from `TRADEABLE_CATALYSTS` on measured evidence. Working as
designed. Confirmed via `Gate [catalyst]: SMCIl_EQ positive but
catalyst=earnings_beat not tradeable — skipping` in the logs.

### Bug found and fixed
- **`analysis/forward_returns.py` derived the yfinance ticker with a naive
  `ticker.split("_")[0]`** instead of reusing `trading.executor.t212_to_symbol()`
  (already correct, already used by the live quote path). Any T212 code outside
  the plain `SYMBOL_US_EQ` shape — `SMCIl_EQ`, the known `FLY1_US_EQ` pattern, ETF
  `_EQ`-without-`_US` codes — mangled into a nonexistent ticker, yfinance failed
  ("possibly delisted"), and the row was permanently marked computed with
  all-NULL returns. Sized: **61 distinct malformed codes, 400+ poisoned
  `sentiment_scores` rows, 5,021 log errors over the prior 30 days**
  (`METAl_EQ` alone: 105 rows). Live trading was unaffected — this only
  blinded the eval loop the v20/v21.3 catalyst-pruning decisions are measured
  against. Fixed by reuse; `reset_for_ticker_fix()` (self-limiting, cutoff
  `2026-07-22`, mirrors `reset_for_extended_returns`) backfills the poisoned
  rows once.

### Also shipped (unvalidated — needs a forward-return measurement period)
- **Preliminary-results routing fix in the Claude classification prompt**:
  headlines shaped like "Reports Preliminary Q_ Revenue...; Margins Expected
  In Range Of X%-Y%" are now routed to `catalyst_type=guidance_raise` instead
  of defaulting to `earnings_beat` — this is the exact SMCI headline shape.
  Routing only: sentiment still requires an explicit comparison baseline in
  the text ("raised from X", "vs. prior guidance") to call a direction, so
  this specific SMCI article — which never stated the 8.2–8.4% baseline —
  would likely still have scored neutral. Closes the gap for future cases
  that do state the comparison. Not a re-opening of `earnings_beat`.

---

## v21.3 — 2026-07-20 (exit-horizon investigation: measure the hold, decouple the entry cutoff)

Third straight low/zero-trade day prompted a full funnel + service audit. Verdict:
**no data vendor is the bottleneck** — Benzinga (267 articles), Claude Haiku (one
3-min 529, handled), Finnhub, and Twelvedata all performed. The constraint is the
strategy's *exit horizon*. Of 51 high-confidence positives, only 3 had a tradeable
catalyst; the standout, IREN (guidance_raise, ADV$ $1.3B), ran +17% and we never
entered — blocked by `overextended` then `exhausted_bounce`.

### What the backtest found (and overturned)
- A 12-name intraday A/B (yfinance 1-min, one variable swapped at a time) **falsified
  the initial hypothesis** that the `overextended` VWAP anchor was the bug: swapping
  session-VWAP → trailing-15m-VWAP was *worse* (−2.52% vs −0.09%). Anchor left alone.
- The **+5% take-profit never fired** across 13 entries — these catalysts don't pop,
  they grind. Lowering it to +3% changed nothing.
- Extending the **time-stop 60 → 120 min** was the only lever that moved the book,
  but on that tiny sample it was IREN-dependent (fidelity-questionable), so it was
  not shipped blind.
- The **forward-return panel** (hundreds of samples, not 12) settled it: the two
  tradeable catalysts' edge is **horizon-increasing** — guidance_raise +1.59%/5m →
  +0.89%/15m → **+3.80%/60m** (83% positive); fda_approval −0.02% → +0.71% →
  **+1.13%/60m**. The 60-min time-stop clips the edge mid-development. (The panel also
  re-validated the v20 catalyst pruning: contract_win −0.12%, product_launch −0.29%,
  earnings_beat −0.28% at 60m — correctly untraded.)

### Shipped
- **Forward returns now measure 120-min and EOD horizons** (`fwd_return_120m`,
  `fwd_return_eod`). `update_forward_returns()` is COALESCE-guarded (a NULL recompute
  never clobbers a measured value); `reset_for_extended_returns()` re-opens recent
  in-yfinance-window rows once to backfill, self-limiting via `_FWD_RETURN_120M_DEPLOYED`.
  Maturity guard 65 → 125 min so the 120-min window is complete before finalizing.
  *This is the measurement that will size the hold on the full panel — deploy this,
  let a week accrue, then set `TIME_STOP_MINUTES`.*
- **Entry cutoff decoupled from the hold** (`ENTRY_CUTOFF_MINUTES`, new). Lengthening
  `TIME_STOP_MINUTES` no longer collapses the entry window: `is_too_late_to_buy()` now
  gates on the cutoff, not the hold. Unset → falls back to the time-stop value, so
  behavior is unchanged until both are set. **To activate the longer hold on the VM:**
  set `TIME_STOP_MINUTES=120` and `ENTRY_CUTOFF_MINUTES=60` in `.env`.

### Deliberately NOT shipped
- **Breakeven-ratchet trailing.** The 12-name sample was *underpowered* to judge it
  (almost nothing armed-then-reversed — the pathology it targets), and a live trailing
  stop adds resting-order churn + cancel/replace races to the gain-protection path for
  no measured benefit. Deferred until the 120m/EOD panel says whether the ratchet
  actually clips winners. No change to `RATCHET_*`.

---

## v21.2 — 2026-07-17 (reconciliation done right; code-review findings)

A same-day high-effort review of v21.1 found that its orphan-alert fix traded
one failure mode for two subtler ones. This release replaces the mechanism and
clears the review's remaining findings. No entry/exit-behavior change.

### Reconciliation: two-pass confirmation replaces the 30s grace window
- **The v21.1 flaw**: `recently_filled()` suppressed the orphan CRITICAL for
  30s after any fill — which also suppressed it when `open_trade()` had
  *genuinely failed* after the fill (the exact case the alert exists for),
  while logging "not an orphan" about a real one. It also did nothing for the
  mirror-image race on the sell side (phantom: sell filled, `close_trade()`
  still committing).
- **The v21.2 rule**: a divergence (orphan or phantom) fires CRITICAL only
  when it survives **two consecutive reconcile passes** (60s apart); the first
  sighting logs INFO "confirming next pass". A commit race cannot survive a
  full pass; a real divergence cannot clear itself — the cases are separated
  by observation, not by a tuned timeout. Covers both sides, needs no
  executor coupling (the `note_buy_filled`/`recently_filled` tracker and its
  unbounded per-ticker dict are deleted), and is restart-safe by construction.
- **Reconcile now runs before the flat-DB early-out.** Pre-v21.2, an orphan
  whose failed `open_trade()` was the *only* position left the trades table
  empty — and the monitor's flat-DB early-out then skipped reconciliation
  entirely, making precisely that orphan invisible until an unrelated trade
  opened. Cost: one portfolio call per 60s while flat.

### Session column: backfill once, stop deriving thrice
- One-time idempotent migration stamps `session='regular'` on every pre-v21.1
  row (`buy_time < '2026-07-17'` — extended-hours trading did not exist
  before then). The three Grafana panels that each carried a private
  ~350-char ET-hour CASE fallback now read `COALESCE(session, 'unknown')`:
  the CASE copies used fixed 09:30/16:00 boundaries that disagreed with the
  calendar-aware session model on early-close days, had no overnight bucket,
  and would have drifted independently. A future `'unknown'` row means an
  entry path skipped the session stamp — visible, not papered over.

### Small fixes from the same review
- Panel 23's description named an exit_reason `stop` that the code never
  writes; corrected to `stop_loss`.
- `docs/algorithm.md` now documents the reconciliation rules and the
  2026-07-16 spurious-CRITICAL incident that motivated them (the v21.1
  change had violated the repo's own keep-algorithm.md-updated rule).
- The reconciliation tests exercise the public API only (no more poking
  `executor._recent_fill_ts` internals) and cover both benign races, both
  confirmed divergences, and the flat-DB reconcile path.
- **Deploy-blocking test flakiness fixed**: the price-check gate tests mocked
  `pc.datetime` but not `get_trading_session()`, which reads the real wall
  clock — between 16:00 and 20:00 ET the entire gate suite silently ran the
  extended-regime variant against RTH fixtures and failed (18 tests). Since
  the deploy pipeline gates on pytest, **deploys pushed during US after-hours
  would have failed CI on unrelated changes.** The shared `_confirm_with`
  helper now pins the session to `regular`.

---

## v21.1 — 2026-07-17 (observability catch-up for v21)

v21 shipped the 24/5 trading logic but left three surfaces stale — this closes
them. No trading-behavior change.

### Trades are now session-tagged
- New `trades.session` column (idempotent `ADD COLUMN IF NOT EXISTS`), persisted
  at entry from `confirmation.session`. Without it, "is after-hours actually
  profitable?" — the question the whole v21 build exists to answer — was
  unanswerable: every panel lumped RTH and extended P&L together. Older rows are
  bucketed read-side from `buy_time` in ET, so history isn't blank.

### Grafana caught up to v21
- **Open Trades** and **Trade History** now show a `session` column.
- New **P&L by Session** panel (n / win-rate / avg-P&L / total-P&L per session) —
  the go/no-go readout for extended-hours trading.
- New **Exit Reason Distribution** panel — surfaces `afterhours_flatten` vs
  `eod_flatten` vs `time_stop` vs `stop`; a high `afterhours_flatten` count means
  entries are firing too late in the after-hours window to work the trade.

### README caught up to v20+v21
- The flow diagram still described a 20s monitor with a resting *take-profit* and
  a polled stop (inverted since v20) and claimed the system "skips cycles outside
  NYSE market hours" (contradicted by v21's after-hours pipeline). Rewritten:
  5s monitor, resting *stop* / polled TP, the session gate, and the extended-hours
  settings added to the config table. Also corrected the pre-v20 catalyst-taxonomy
  line (only `fda_approval`/`guidance_raise` are tradeable).

### Reconciliation no longer cries wolf on every entry
- The monitor's broker/DB reconciliation runs on a snapshot taken at cycle start;
  a buy that fills before `open_trade()` commits looked like an orphan and fired a
  spurious `CRITICAL` on *every* trade (all of 2026-07-16). The executor now records
  each fill's timestamp (`note_buy_filled` / `recently_filled`, thread-safe) and the
  reconcile suppresses the orphan alert inside a 30s grace window — far shorter than
  the 60s reconcile cadence, so a genuine orphan still surfaces next pass. A CRITICAL
  that fires on every trade trains the operator to ignore CRITICALs; that's the real
  cost removed.

---

## v21 — 2026-07-16 (24/5 extended-hours trading)

The user enabled T212's 24/5 trading on the account. v21 accommodates it —
selectively. The four T212 sessions (ET): premarket 04:00–09:30, regular
09:30–16:00, after-hours 16:00–20:00, overnight 20:00–04:00 (Blue Ocean).

### What is traded and what is not
- **After-hours (ON by default)** — the prize. FDA decisions and guidance
  raises overwhelmingly print 16:00–17:30 ET; the system used to sleep
  through all of them (the article was >3 min stale by the next morning and
  the premarket scanner only looks back so far). `news_cycle` now runs the
  full pipeline through the after-hours session.
- **Pre-market direct entries (OFF by default)** — the existing scanner +
  at-open gap-and-go evaluation stays the pre-market strategy; 4am books are
  thin and the at-open path trades the same news with confirmation.
  `PREMARKET_TRADING_ENABLED=true` turns direct entries on if evidence ever
  supports it.
- **Overnight (NEVER)** — Blue Ocean prices are not carried by Finnhub or
  Twelvedata. No bars → no confirmation → no trade (the standing fail-closed
  contract), and the monitor force-flattens everything by 19:45 ET
  (`EXTENDED_FLATTEN_BUFFER_MINUTES`) so nothing is ever held into a venue we
  cannot see.

### The extended-hours regime (all in market/sessions.py + gate variants)
Extended sessions are a DIFFERENT market and get a stricter gate set:
- **Session-anchored analysis** — one prepost 1-min pull anchored at the
  session boundary. VWAP/momentum/exhaustion all measure the post-catalyst
  regime only: for a 16:05 guidance raise the accumulation test runs against
  the after-hours VWAP (a full-day VWAP is dominated by pre-news RTH tape and
  would auto-reject every legitimate after-hours mover as overextended).
- **RVOL band replaced** by an absolute participation floor
  (`EXTENDED_MIN_SESSION_DOLLAR_VOLUME`, $500k printed in-session): the RVOL
  time-of-day curve is calibrated on RTH volume shape and means nothing at
  17:00. Transient reject — the re-eval queue re-checks as the tape builds.
- **Institutional-depth only**: ADV$ ≥ `EXTENDED_MIN_ADV_DOLLAR` ($50M).
- **Tighter spread proxy** (`EXTENDED_MAX_SPREAD_PCT` 1.5 vs 3.0).
- **Half size** (`EXTENDED_SIZE_FACTOR` 0.5).
- **No resting stop**: T212 stop orders execute in RTH only, and the public
  API accepts `extendedHours` on MARKET orders only (community-reported; the
  executor feature-detects per process — `_extended_limit_supported` — so if
  T212 ever accepts the flag on limit orders, bounded-slippage exits come
  back automatically). The monitor polls BOTH sides at its 5s cadence.
  Extended market sells verify their fill (a queued sell is cancelled and
  retried, never left to execute blind); extended buys that don't fill
  promptly are cancelled — a queued buy filling at the next open would be the
  gap-and-crap trap with extra steps.
- **Session-start block**: the same 5-min opening block applies after 16:00
  (closing-auction unwind noise).
- **Breakeven ratchet** arms the POLLED breakeven in extended sessions
  (placing a resting stop out here would reserve shares while protecting
  nothing).

`scripts/probe_t212_extended_hours.py` verifies the T212 demo API's actual
extendedHours support (run it on the VM; never-fill orders, cancelled
immediately, demo only).

Known residual risks, accepted deliberately: (a) the polled loss side after
hours has up to ~5s + sell latency — mitigated by deep-book eligibility, half
size, and the tight spread gate; (b) quote sources can briefly serve the
16:00 close after a catalyst — confirmation uses the fresher of quote vs
newest anchored bar, and the monitor's staleness guard rejects frozen quotes.

---

## v20.2 — 2026-07-14 (zero-trade post-mortem: mega-cap RVOL bypass + regulator taxonomy)

2026-07-13 (the first full session under the v20 catalyst prune) traded
nothing. Root-caused ticker-by-ticker against `sentiment_scores`/
`news_signals`/`premarket_candidates` and the full journalctl history: no
outage, no crash, no risk-gate block — 282 articles scored, only 10 (3.5%)
carried a tradeable catalyst, and every one was individually and mostly
*correctly* rejected (3 penny stocks, 1 illiquid, 2 low-confidence, BMRN a
genuine sell-the-news on the day's highest-confidence signal). One rejection
exposed a real gap.

### 1. RVOL floor bypass for mega-caps holding VWAP
BMY ("FDA Accepts NDA For Mezigdomide") drifted +2.1% all session, held VWAP
essentially the whole way, and was rejected `low_volume` on all 27
consecutive re-eval cycles — RVOL never exceeded 0.3 against the 1.5 floor,
because a $752M/day-ADV mega-cap doesn't need anomalous RELATIVE volume to
move 2%. The VWAP accumulation test (step 10) is explicitly designed to be
the size-neutral answer to exactly this — but RVOL (step 9) runs first and
vetoed every cycle before VWAP ever got a look. Same failure class as VERA
(§ RVOL section, docs/algorithm.md): a stock at the wrong end of the cap
spectrum for a flat relative-volume bar, just too LARGE instead of too FRESH.
Fix: above `RVOL_BYPASS_MIN_ADV_DOLLAR` (default $50M, 10× the illiquidity
floor), a held VWAP substitutes for the RVOL FLOOR only — the 20× ceiling is
unchanged at any cap size (parabolic volume is still the halt signature).
Below the ADV$ floor, small/mid-cap behavior — where RVOL is best-validated —
is untouched.

### 2. fda_approval requires the US FDA specifically
NVS's Health Canada approval was tagged `catalyst_type=fda_approval` on
2026-07-13 — harmless that day (dead tape rejected it anyway) but a mistag
that happens to move on the tape would trade on an edge the 60-day
forward-return study never measured (§3.3 measured US FDA action only). The
system prompt now carries an explicit FDA-only carve-out plus a contrastive
Health-Canada example next to the genuine-approval example.

Tests: 241 passing (5 new — `TestRvolBypass`; 1 new — prompt content guard).

---

## v20 — 2026-07-10 (first-principles rebuild: trade what's measured, protect the downside at broker speed)

Ground-up critical review of every strategy component, driven by the
system's own 60 days of forward-return data (the eval loop finally paying
for itself) and the realized record (1W/10L, −£85). The theme is REMOVAL
and INVERSION, not addition: stop trading the signal classes that measurably
lose, stop chasing extended prices, and put the broker's speed on the side
of the trade where speed is capital.

### 1. TRADEABLE_CATALYSTS pruned to the measured-positive classes
Forward returns of every positive, not-already-moved, confidence≥0.7 signal
over 60 days (avg @5/15/60 min):

| class          | n   | 5m     | 15m    | 60m    | verdict |
|----------------|-----|--------|--------|--------|---------|
| fda_approval   | 86  | −0.02  | +0.55  | +1.42  | KEEP    |
| guidance_raise | 9   | +0.38  | +1.15  | +2.97  | KEEP    |
| contract_win   | 152 | −0.42  | −0.84  | −1.34  | REMOVED |
| ma_target      | 71  | −0.08  | −0.07  | −0.37  | REMOVED |
| earnings_beat  | 33  | −0.74  | −1.13  | −1.60  | REMOVED |
| product_launch | 32  | −0.93  | −1.13  | −2.05  | REMOVED |
| short_squeeze  | 0   | —      | —      | —      | REMOVED |

The kept classes are binary regulatory/guidance surprises whose drift BUILDS
over 15–60 min — a shape a 1–3 min entry latency captures. Earnings news at
this latency gets sold (both July losses — LEVI, CRCL — were earnings_beat).
Magnitude was checked as an alternative filter and does NOT predict returns
(mag 3/4/5 all negative) — MIN_CATALYST_MAGNITUDE stays at 2. Every class is
still scored and persisted, so re-enabling stays evidence-based.

### 2. Exit inversion: the STOP rests at the broker, the TP is polled
T212 has no OCO and each sell reserves its shares — only one side can rest.
v14 rested the TP and polled the stop every 20s; the record proved that
backwards: 1 resting-TP fill in 11 trades vs stop slippage of −3.4% (VECO),
−3.97% (CRCL — falling ~1%/min, the poll gave it a 20s head start) and
−18.99% (GOAI) on −2% triggers. A missed TP costs opportunity; a slow stop
costs capital on EVERY fast reversal.
- `executor.place_stop_loss()` (stop-market, DAY) replaces
  `place_take_profit()` (deleted); `trades.stop_order_id` column added
  (tp_order_id kept for trades open across the deploy — both regimes
  coexist in the monitor until legacy positions close).
- Monitor notices stop fills (status + fill-detail, GONE≠filled), cancels
  the stop before any polled TP/time-stop/EOD sell, and handles the
  cancel/fill race in both directions.
- **Breakeven ratchet**: at +RATCHET_TRIGGER_PCT (2% = 1R) the stop is
  cancelled and re-placed at buy×(1+RATCHET_LOCK_PCT/100) (+0.1%), once.
  If the replacement fails the armed flag drives a POLLED breakeven stop —
  the ratchet can only tighten protection, never lose it.
- Monitor cadence 20s → **5s** (the polled TP side pays cadence as latency);
  reconciliation throttled to 60s so the faster loop doesn't multiply
  broker API load; cycle early-outs when flat.

### 3. Overextension gate — never park the stop on the far side of value
New `overextended` reject (gate 10.2): entry price more than
MAX_VWAP_EXTENSION_PCT (1.5%) above session VWAP is refused, because with a
2% stop, a routine mean-reversion to VWAP — the base case for any extended
stock — hits the stop by construction. LEVI entered +1.9% above VWAP and
CRCL +2.2%: both were structurally dead on arrival. TRANSIENT: the re-eval
queue re-checks every cycle, mechanically converting "chase the vertical
move" into the professional playbook's "enter on the first pullback into
value" with zero new infrastructure. Validation enforces
MAX_VWAP_EXTENSION_PCT < STOP_LOSS_PCT.

### 4. Digest/preview pre-filter (the CRCL fabrication class, killed deterministically)
`_DIGEST_RE` blocks compilation headlines ("Market-Moving News for July
10th", "Stocks To Watch", "Premarket Movers", "Earnings Scheduled For…",
listicles) before Claude ever sees them — on 2026-07-10 one such digest
(3 tickers, sliding under the >3-ticker roundup filter) was classified
"earnings_beat, 80% confidence" for THREE unrelated companies and bought
the top of CRCL's 13% parabolic spike. The system prompt also gained an
explicit "digests and previews are never catalysts" rule as defense in
depth (a digest classified as fda_approval would still pass the catalyst
gate — the regex is the reliable layer).

### 5. Data plan consolidation: 3 Twelvedata calls → 1 (+1 cached daily)
`get_session_analysis()` — ONE 390-bar 1-min pull — now feeds momentum
baseline, spread proxy, session volume, VWAP, and session low/high;
`get_daily_stats()` (ADV, dollar-ADV, prev close — immutable intraday) is
cached per symbol per ET day. `get_momentum_baseline`, `get_volume_stats`,
`get_session_vwap`, `get_session_volume_and_vwap` deleted. Effects:
- A re-evaluation retry costs **1 credit** (was 2–3) and ~2 HTTP round
  trips (was 3–4) — confirmation latency roughly halved, premarket window
  throughput up.
- Session minute-bar volume is now THE RVOL numerator: the lagging
  daily-bar `today_volume` and the v19.2 "rescue" second-fetch dance are
  gone wholesale (the failure class died with the mechanism).
- Fixed a long-standing subtle bug: right after the open, the momentum
  baseline ("newest bar ≥5 min old") could match YESTERDAY's 15:59 bar,
  silently treating the overnight gap as 5-minute momentum. Baseline
  selection is now restricted to today's session.

### Post-review hardening (pre-deploy code review, 7 findings, 6 fixed)
A recall-biased review of the diff before shipping surfaced and fixed:
- **Monitor price path at 5s** (`get_current_price`): quote chain now runs
  fast (single-attempt — at a 5s cadence the next cycle IS the retry, and
  in-call backoff sleeps overran the interval), and the 390-bar Twelvedata
  fallback is throttled to one attempt per 30s per symbol — an unthrottled
  quote outage would have burned ~12 pulls/min/position and saturated the
  55/min bucket, starving signal confirmation.
- **Digest regex over-match**: dropped "market update" ("Acme Provides
  Market Update On Phase 3" is a real single-stock PR template), "day
  ahead" and "stocks moving" — a false positive here is a silently-missed
  trade with no eval-loop trace.
- **Ratchet crash-safety**: the armed flag now lives ON the trade row
  (`trades.ratchet_armed`, v20.1 migration) and is set only after the move
  resolves; a crash between cancel and re-place self-repairs — the next
  above-trigger cycle places a fresh breakeven stop directly. An in-memory
  flag silently regressed protection to −2% after a restart.
- **Gate decoupling**: `overextended` now evaluates whenever a VWAP exists,
  independent of `REQUIRE_VWAP_CONFIRMATION` — disabling the accumulation
  test must not silently disable the chasing protection.
- **T212 order-endpoint bursts**: resting-order status checks throttled to
  once per 15s per trade (bookkeeping, not protection — the money is
  guarded broker-side; between checks the order is presumed live, the same
  safe assumption as the network-error path).
- **Stale docs**: main.py's "(20s)" monitor docstring, backtest_db's
  reference to the deleted `get_session_vwap`.
- *Accepted as designed*: a polled exit cancels the resting stop on its
  first sell attempt, so repeated unfilled limit sells leave polled-only
  protection — bounded by the 3-strike market escalation, which at the 5s
  cadence fires within ~15-25s (versus 60s+ under v19).

Also added `docs/how-the-trading-robot-works.html` — a self-contained,
shareable plain-English explainer of the whole algorithm with diagrams
(pipeline, catalyst evidence chart, safety-check funnel, trade lifecycle,
guardrails, learning loop).

Tests: 235 passing (26 new vs v19.5; suite runs ~10× faster — the mocked
retry-sleep paths went away with the old data plan). No changes to: TP 5% /
SL 2% (per user), TIME_STOP 60 (the kept classes' drift is still building
at 60m), MIN_SENTIMENT_CONFIDENCE 7, RVOL band, liquidity floor, sizing,
kill switch.

## v19.5 — 2026-07-09 (bad-trade post-mortem: intraday exhaustion + same-day news respin)

Post-mortem of the day's one trade, LEVI (Levi Strauss): bought $24.93 at
11:30 ET on an 85%-confidence "beat-and-raise" earnings headline, sold
$24.72 at 12:31 ET via time-stop, -1.19%. Every existing gate read clean
(momentum +0.32%, day change +2.09% vs prev close, RVOL 1.5). Real minute
bars from Yahoo Finance told a different story: LEVI had gapped down as
much as -7.8% at the open on the SAME earnings ("sell the news"), then
clawed back to +2.3% by the time of entry — bought within 15 cents of the
exact high of the day, three minutes before the actual peak, then faded for
the rest of the session. Two root causes, two fixes:

### Intraday exhaustion gate (`market/price_check.py`, `market/twelvedata_bars.py`, `config/settings.py`)
`day_change_pct` only measures distance from YESTERDAY's close; `recent_move_pct`
only measures the last ~5 minutes. Neither can see the SHAPE of today's own
session — a stock that gapped down hard and clawed most of the way back looks
identical, on both those measures, to one calmly grinding to fresh highs.
New gate 10.5, `exhausted_bounce`: reuses the SAME session-bar pull already
spent on RVOL rescue / VWAP (`get_session_volume_and_vwap`, now also returns
`session_low`/`session_high` — a 5-tuple, up from 3 — at no extra Twelvedata
credit cost). Rejects when today's low-to-high range is at least
`EXHAUSTION_MIN_RANGE_PCT` (5.0, i.e. a real round trip, not noise) AND price
has already recovered at least `EXHAUSTION_RECOVERY_THRESHOLD` (0.75) of that
range. Toggle: `REQUIRE_EXHAUSTION_CHECK` (default true).

### Same-day same-ticker article cross-reference (`news/fetcher.py`)
Benzinga published TWO articles about LEVI's exact same earnings print two
hours apart with opposite framing: 09:39 ET *"Stock Tumbles 4% Despite Q2
Earnings Beat"* (scored negative, correctly never traded), then 11:30 ET
*"Posts Beat-And-Raise Quarter, Analysts See More Upside In 2H"* (scored
positive, 85% confidence — the one traded). Claude scored the second article
with zero memory of the first. A new session-scoped, daily-reset
`_ticker_history` dict records every scored article per ticker; the next
article for that ticker carries up to 3 prior same-day verdicts as a
`PRIOR ARTICLE(S) TODAY ON THIS TICKER` line in the (per-cycle, uncached)
user message. The cached system prompt gained a new "SAME-TICKER CONTEXT"
paragraph instructing the model to read a positive respin of a story that
already had a negative reaction today with extra skepticism — lower
confidence, lean `already_moved=true` — unless the new article contains a
genuinely new, separate fact.

### Considered and rejected: scaling TP/SL by catalyst_magnitude
LEVI's catalyst_magnitude was 2/5 (Claude's own "modest" rating) and the
stock only reached +0.56% before fading, well short of the flat 5% take-
profit every trade uses regardless of catalyst size. Proposed scaling TP/SL
down for low-magnitude catalysts; rejected — a 2% target isn't worth taking
the trade for. The exhaustion gate directly addresses what actually
happened (bought at the top of an already-completed move) without touching
the profit target, which stays a flat 5%/2% for every trade.

Tests: 200 passing (12 new: 5 exhaustion-gate, 7 same-day cross-reference).

## v19.4 — 2026-07-08 (zero-trade post-mortem: pre-market RVOL miscalibration + candidate abandonment)

Investigation of a genuinely quiet trading day (2026-07-08, zero trades) found
that 12 of 19 pre-market candidates — including a live M&A bid, three FDA
approvals, and a $308M contract win — never received a real gate verdict at
all; they were retried every cycle for 30 minutes and simply expired. Root
cause verified against real end-of-day volume/price data (not assumed): the
system's own caution was directionally correct today (chasing the loudest
signal, BZH, would have bought the top — checked its actual close), but the
mechanism producing that caution was broken, and a handful of real, if
modest, gains (KGS +2.7%, ARQT +3.0%, AYA +1.8%, URGN +0.7%) were left
un-tracked. Three fixes:

### Intraday volume curve recalibrated (`market/price_check.py`)
`_VOLUME_CURVE` assumed 16% of a stock's daily volume trades by minute 30
after the open (a big-cap, open-auction-flow shape). Measured directly
against real 2026-07-08 volume for BZH/JNJ/CACI/ARQT, the true fraction by
minute 30 ran 1–4% — a 4–14× mismatch that pinned RVOL near-zero for the
entire pre-market eval window regardless of whether the stock was genuinely
trading well (BZH finished the day at 4× normal volume and was still reading
RVOL ~0.3 at minute 29). The 0–150 min anchors are now roughly 3× less
aggressive, reconverging with the original curve by minute 150 where there is
no contradicting evidence. A first-pass empirical fit from one day's data,
flagged in-code for revisiting as more days accumulate.

### Opening no-quote grace period (`premarket/scanner.py`)
In the first ~90s after the open, Twelvedata served a quote timestamped
exactly 24h old for ~19 tickers simultaneously (its own snapshot cache not
yet rotated for the new session) while Finnhub's quote was still genuinely
carrying yesterday's close — both correctly read as "no live coverage" for a
systemic, predictable, self-healing reason unrelated to any ticker's real
coverage. `_OPEN_GRACE_MINUTES = 2.0`: a no-quote miss in this window no
longer burns one of the 3 no-coverage strikes, preserving the full retry
budget for tickers with a genuine, not-provider-wide outage.

### Pre-market candidates no longer die at the 30-min cutoff (`premarket/scanner.py`, `main.py`)
A candidate still PENDING (never confirmed, never terminally rejected) when
its 30-minute gap-and-go window closed was simply discarded forever, even
though the underlying catalyst might still be developing — unlike
regular-hours signals, which get unlimited re-checks via `_reeval_queue`.
`_live_candidates` now returns `(live, graduated)`; `main.news_cycle` hands
`graduated` candidates into the exact same standing re-evaluation queue
regular-hours signals use (via a synthetic transient `PriceConfirmation`
routed through the existing `_execute_entry` → `_queue_reeval` path), instead
of a bespoke new mechanism. Stale (prior-day) candidates are not graduated —
those are just dead.

### Retracted during design (not implemented — verified as not a bug)
Initially suspected the v19.2 RVOL rescue (`get_session_volume_and_vwap`)
never firing today was gate-ordering dead code (it lives inside the RVOL gate,
which runs after the momentum-floor gate). On inspection this doesn't change
any outcome: momentum and RVOL are computed from independent data (price bars
vs. volume bars), so a momentum-floor rejection is valid regardless of RVOL
accuracy, and rescue only matters when RVOL is the deciding gate — which it
still runs for correctly. Direct comparison of Twelvedata's daily-bar volume
against minute-bar-summed volume for BZH/JNJ/CACI confirmed they converged
today (nothing to rescue); the mechanism remains correct for a genuinely
lagging-data day like 2026-07-07.

Tests: 188 passing (7 new: grace-period strike suppression, graduated-handoff
routing through `_live_candidates` and `main.news_cycle`).

## v19.3 — 2026-07-08 (adversarial hardening: garbage-input immunity at every integration seam)

Chaos-tested every external boundary with hostile payloads (malformed JSON,
wrong types, NaN, explicit nulls, mis-scaled values). New contract, enforced
by `tests/test_adversarial.py` (42 tests): nothing a service can send may
crash a scheduler cycle or produce a trade approval — garbage in → None /
reject / skip out, and one bad record never takes down its batch.

### Finnhub quote normalization (`market/finnhub_bars.py`)
The raw `/quote` dict was returned as-is after a `c == 0` check, so `c=None`,
`c="abc"`, `c=-5` and `c=NaN` all passed through to price math and gate
comparisons. NaN is the silent killer: it compares False against every
threshold, so a NaN price would sail through the penny/dead-cat/extended-move
gates. `_normalize_quote()` now requires a positive finite `c` (else no
quote); `o`/`pc` degrade to 0 ("missing"), a bad `t` degrades to None
(staleness fails open) instead of poisoning downstream.

### Twelvedata defensive coercion (`market/twelvedata_bars.py`)
- `/quote`: field-by-field coercion — a garbage `previous_close` or
  `timestamp` degrades that field to None instead of discarding an otherwise
  good quote (the old code dropped the whole quote via the outer except).
- `time_series`: `values` must be a non-empty LIST (a dict/string payload
  previously flowed into `values[0]` indexing at every caller).
- `_parse_bar_time`: non-dict bars (nulls/scalars smuggled into the array)
  parse as "no timestamp" → bar skipped, instead of AttributeError out of the
  whole series. `get_volume_stats` also catches AttributeError now.

### Position sizing guards (`trading/executor.py`)
`calculate_quantity` refused nothing: price=0 was a ZeroDivisionError, a
malformed T212 cash payload ({"total": "abc"}) an unhandled ValueError. Now:
non-finite/non-positive/unparseable price → `(None, reason)`; cash payload
parse failures and NaN values → `(None, reason)`. `t212_to_symbol(None/"")`
returns "" instead of AttributeError.

### Claude output per-record validation (`news/fetcher.py`)
One malformed classification record (confidence="high") raised out of the
parse loop and discarded the WHOLE batch. Records are now validated
individually — malformed ones are skipped with a warning, the rest survive.
Out-of-range values (confidence outside [0,1], magnitude outside [1,5], NaN)
are REJECTED, not clamped: a confidence of 7 is more likely a mis-scaled
0-10 answer than a genuine 100%+, and guessing the scale on a trading signal
is worse than not trading it. A non-list `classifications` payload → {}.

### Benzinga article hygiene (`news/fetcher.py`)
- A null/scalar in the article array, a non-string ticker (int/null), or a
  bare-string `tickers` field each crashed the ENTIRE fetch cycle. Type
  guards skip the bad element. (Bare-string tickers mattered doubly: "AAPL"
  iterated as characters, and "A" is a real NYSE ticker.)
- Explicit `null` title/teaser/body values crashed slicing/regex/unescape at
  six call sites — `.get(k, "")` only covers a MISSING key, not a null value.
  All are now `or ""` coalesced.

Tests: 181 passing (42 new adversarial). No thresholds or strategy behavior
changed — every fix is input validation at a service boundary.

---

## v19.2 — 2026-07-07 (post-mortem of the first live session: GLASF stuck exit + four classes of missed trades)

The first session where the pipeline could execute end-to-end (2026-07-07)
produced exactly one trade — GLASF, the candidate with the WORST data of the
day — which then sat stuck for 5h14m while the monitor placed and cancelled
459 limit sells, until the EOD flatten market-sold it (−2.33%). Meanwhile the
genuinely strong signals (VERA FDA approval 0.95/mag-5, AGIO +11.1% gap FDA
catalyst, ZTS +3.35%, FLY $13M NASA contract ×2) all died on data artifacts or
structurally-premature gates. Every mechanism is fixed below.

### Stuck exits now escalate to a market order (`monitor/position_monitor.py`)
Bounded-slippage limit sells protect against thin-book collapses (GOAI −19%),
but an unfilled limit retried forever is the opposite failure: GLASF's exit
limit was priced off a frozen quote sitting ABOVE the real market, so no
retry could ever fill. After 3 consecutive failed limit attempts for the same
trade, the next attempt goes straight to market (like the EOD flatten), and a
one-per-day `exit_stuck` system_event (warning) is recorded. Counter resets on
any successful sell.

### Stale quotes are no longer live prices (`market/price_check.py`)
Nothing checked a quote's own data timestamp. GLASF's Finnhub quote read
$12.50 all afternoon while the market traded ~$11.53: it manufactured the
+2% "momentum" that confirmed the entry, made a losing position look +6% up,
and priced every exit limit above the book. `get_quote_with_fallback` now
treats a quote older than 20 minutes (`_QUOTE_MAX_AGE_SECONDS`) as no
coverage — falls to the next source or fails closed. Twelvedata quotes now
carry their `timestamp` through normalisation so both sources get the check.
Missing timestamps fail open (only positive evidence of staleness rejects).

### Degraded-data conjunction can no longer approve (`market/price_check.py`)
Three individually-reasonable fallbacks — momentum baseline → today's open,
RVOL deferred when the daily bar hasn't rolled, VWAP skipped when bars are
missing — could all fire together, degrading "confirmation" to a bare (and in
GLASF's case, stale) quote. The candidates with the worst data got the
weakest checks. New `insufficient_data` rejection: at least ONE participation
measure (a real volume reading or a passing VWAP) must positively exist.

### At-open RVOL false-rejections rescued with session minute bars (`market/twelvedata_bars.py`, `market/price_check.py`)
`today_volume` comes from Twelvedata's DAILY bar, whose volume field trails
the session by minutes — worst at the open, exactly when the gap-and-go eval
runs. ZTS read RVOL 0.07 and AGIO 0.40 minutes after gapping up on real
catalysts; TTEK/CACI/ZTS/AGIO were all false-rejected on it. When the daily
bar is missing or reads below `MIN_RVOL`, the gate now pulls today's 1-min
bars (new `get_session_volume_and_vwap`, 1 credit, only spent when the gate
would otherwise fail), takes max(daily, minute-sum) volume, and re-computes.
The same bars are REUSED for the VWAP gate (no second credit). The old "skip
RVOL when the daily bar hasn't rolled" bypass is gone — a zero measurement is
now a measurement (GLASF traded on rvol=0.0 through that bypass).

### Transient rejections get re-checked instead of dying at first sight (`main.py`, `premarket/scanner.py`)
Signals are scored within ~3 min of publication — faster than the market can
express participation. Cumulative-session RVOL barely moves in the first
minutes after a midday catalyst, and the 5-min momentum window reads flat
before buyers arrive: VERA (FDA approval, 0.95 confidence, magnitude 5 — the
strongest signal of the day) was terminally rejected on RVOL 0.71 measured
the minute the news broke; CSCO/BTU/TEVA/RPRX died the same way. Now:
- RTH: signals rejected with `low_volume`/`low_momentum` park in a re-eval
  queue and re-confirm every cycle for 15 min (`_REEVAL_TTL_MINUTES`); if
  participation arrives they trade (row's rejection cleared via new
  `clear_rejection()`), otherwise the final rejection stands.
- Premarket: the same two codes now leave the candidate PENDING (retry every
  cycle until the eval window closes) instead of a terminal rejection at
  minute 5.

### T212 code → exchange symbol derivation was lossy (`trading/executor.py`, `market/price_check.py`)
Market-data lookups derived the symbol by stripping `_US_EQ` off the T212
code. T212 re-uses historic symbols by appending a digit to ITS code —
Firefly Aerospace is exchange symbol `FLY` but T212 code `FLY1_US_EQ` — so
the derived `FLY1` had no Finnhub/Twelvedata coverage and both FLY candidates
($13M NASA subcontract, 0.80 conf) expired unpriced. `build_symbol_map()` now
also builds the inverse map; new `t212_to_symbol()` resolves the exact
exchange symbol with suffix-strip as fallback.

### Premarket terminal rejections no longer masked as data problems (`premarket/scanner.py`)
`penny_stock`/`wide_spread` fire before prev_close is computed, so
`day_change_pct=None`; the scanner fell into the "prev close unavailable"
strike counter and — after 5 wasted eval cycles re-checking a terminally
rejected stock — recorded a data-problem epitaph. PLUG ($2.65, penny reject
every cycle) is recorded as "no previous close after 5 retries". Any
non-transient rejection now records its REAL reason immediately.

### Buy fill-slippage warning (`trading/executor.py`)
GLASF confirmed at $12.50 and filled at $11.79 (−5.7%) with no comment. A
fill >3% away from the signal price now logs a WARNING — the quote that
confirmed the entry did not reflect the real market, so position risk is not
what the signal implied.

### Negative tape no longer logged as "dead tape" (`market/price_check.py`)
DOCN fell −8.14% in 5 min and was rejected as "Dead tape: -8.14% (need
+0.2%...)". Same gate, distinct message: moves below −MIN_PRICE_MOVE_PCT now
log "tape moving against the signal". (Code unchanged: `low_momentum`.)

Tests: 139 passing (22 new — symbol inversion, quote staleness, RVOL rescue,
insufficient_data, sell escalation, re-eval queue, session volume/VWAP,
scanner transient/terminal verdicts).

---

## v19.1 — 2026-07-07 (full-system audit: data-budget leaks, premarket latency, observability)

Comprehensive whole-system review. No entry/exit thresholds changed; every
item below is a correctness, resource, or observability fix.

### FX rate call bypassed both Twelvedata budget gates (`market/twelvedata_bars.py`)
`get_gbp_usd_rate()` (called by position sizing at every buy) hit the /price
endpoint with NO `credits_exhausted()` check, NO `_claim_minute_token()`, and
NO `_record_credit_use()` — the one unmetered leak left in the credit budget,
and an uncounted call against the 55/min bucket. Now runs behind the same two
gates as every bar/quote call; when gated it serves the cached/fallback rate
(graceful — a stale rate is within the sizing safety margin).

### Finnhub had no fast mode — premarket "no backoff" contract broken (`market/finnhub_bars.py`)
The 2026-06-23 fix made every **Twelvedata** call single-attempt inside the
premarket eval pool, but the PRIMARY quote source still ran 3 attempts × 5s
timeout + backoff sleeps ≈ up to ~17s of a pool thread's time inside the 30s
wall-clock budget — the exact starvation class that produced the 2026-06-18
zero-trade day, just on the other API. `get_finnhub_quote(fast=True)` now
makes exactly one attempt with no sleeps; `get_quote_with_fallback` propagates
`fast` to both sources.

### Session no-quote blackout was cumulative, not consecutive (`main.py`)
`_no_quote_ticker_strikes` never reset on a successful price check, so two
unrelated transient misses hours apart (a token-bucket skip at 14:00, a
Twelvedata blip at 19:00) permanently blacklisted a ticker with perfectly
good coverage for the rest of the session. Any successful confirmation now
resets the ticker's strike count (`_note_price_data_ok`), making the
documented "consecutive" semantics real.

### Benzinga outage was the last silent external dependency (`news/fetcher.py`)
Twelvedata exhaustion and Claude outages emit system_events; a dead news feed
(expired key, API down) produced zero signals — indistinguishable on every
dashboard from a quiet news day until the zero-trade tripwire fired days
later. After 10 consecutive failed fetches (~10 min), one `benzinga_outage`
system_event is recorded (DB-deduped per day) and an ERROR logged.

### Fresh-database `init_db()` crash (`storage/database.py`)
The `ALTER TABLE sentiment_scores/premarket_candidates ADD COLUMN
catalyst_magnitude` migrations ran BEFORE those tables' `CREATE TABLE`
statements — fine on production (tables exist), UndefinedTable crash on any
fresh database (new dev env, disaster recovery). Migrations moved after their
CREATEs.

### Nightly bars cache grew forever (`analysis/forward_returns.py`)
`_bars_cache` (per-ticker-day 1-min DataFrames) was module-level and never
cleared: every nightly run added hundreds of DataFrames that lived for the
life of the service process. Now cleared at the start of each run — the cache
only exists to dedup fetches within one run.

### Premarket strike counters leaked (`premarket/scanner.py`)
`_no_quote_strikes`/`_gap_pct_strikes` entries survived candidates that
reached a terminal status via a different path (window expiry, gap reject,
final confirmation reject). All terminal verdicts now clear both counters
(`_clear_strikes`).

### Observability & cleanup
- **Grafana**: "Portfolio Value Over Time" cast `snapshot_at::timestamp`,
  silently dropping the timezone offset (1-hour shift all summer); now
  `::timestamptz` like every other panel. Added two panels the v18 backlog
  death-spiral had no dashboard signal for: **Fwd-Returns Backlog** (rows
  uncomputed) and **Fwd-Returns Job (hours ago)** heartbeat.
- Funnel log line now counts blackout skips separately instead of folding
  them into `already_seen`.
- Roundup articles (>3 tickers) are skipped before the per-ticker DB dedup
  queries instead of after them.
- Removed dead code: `next_market_open()` (price_check), `get_available_cash()`
  (executor), unused imports. `datetime.utcnow()` → `datetime.now(timezone.utc)`
  in reporting (deprecated in Python 3.12).

## v19 — 2026-07-06 (premarket execution-boundary crash — the real drought root cause)

`catalyst_magnitude` became a required `NewsItem` field in v15.8 (018ae7c),
but `main._candidate_to_news_item()` — which rebuilds a NewsItem from a
`premarket_candidates` row so an APPROVED candidate can reach
`_execute_entry` — was never updated to supply it. Every premarket approval
raised `TypeError: NewsItem.__init__() missing 1 required positional
argument`, caught by the broad try/except around the premarket loop in
`news_cycle`, which aborted the entire loop **before any buy**.

This was the true cause of the 2026-06-11 → 07-06 drought (16 consecutive
zero-trade sessions; last trade 2026-06-10). It was masked by four upstream
premarket fixes (v16/v17.1/v17.4/v17.5) that each repaired a genuine
starvation problem — and thereby pushed MORE candidates to APPROVED, straight
into the crash. The RTH path was unaffected (fetcher.py constructs NewsItem
with the field).

Fix: pass `catalyst_magnitude=int(cand.get("catalyst_magnitude") or 1)`
(stored on every candidate row since v15.8; `or 1` is a can't-crash fallback
for legacy rows). Regression tests pin the conversion. Full incident writeup
in docs/algorithm.md.

## v18 — 2026-07-03 (eval-loop integrity + execution/capture fixes)

Full-code + production-data audit prompted by the ongoing zero-trade run
(no trades since 2026-06-10). Verdict: the entry gates were mostly making
locally-correct calls, but three structural defects were (a) silently
destroying the strategy's ability to MEASURE its own edge and (b) leaking
confirmed signals at the capture and execution layers.

### Forward-return anchoring bug (`analysis/forward_returns.py`) — CRITICAL
Articles published outside RTH had both endpoints of every return window
resolve to the same first 09:30 bar (yfinance serves RTH bars only), so the
job recorded an EXACT 0.0 for 5/15/60-min returns. 2,241 of 5,721 computed
rows (39%) were poisoned — and the damage concentrates on the pre-market
earnings/FDA/M&A block, the strategy's highest-edge window. Every calibration
decision made from this table (incl. the v17.5 partnership ruling, whose
`median = 0.000%` is this bug's fingerprint) needs re-validation.
Fix: `_bars_and_anchor()` clamps the measurement anchor to the session open
for pre-market articles and rolls after-hours articles to the NEXT session's
open (up to 4 calendar days, crossing weekends). A one-time, self-limiting
repair (`reset_contaminated_forward_returns()`) nulls the poisoned rows so
the nightly job recomputes them; rows older than yfinance's ~30-day 1-min
history resolve to NULL and stop participating.

### Forward-return backlog death spiral (`analysis/forward_returns.py`)
The nightly job processed max 500 rows (oldest-first) while ~1,000 articles
are scored per day — the backlog grew ~500 rows/day (8,000 rows / 58% of the
table uncomputed by 2026-07-03), and new rows were computed ever later,
heading for permanent NULL once past yfinance's 30-day window.
Fix: the job now loops in 500-row batches (up to 25/night) until the backlog
is drained, stopping early when only still-maturing (<65 min old) rows remain.

### Precision-retry hardening (`trading/executor.py`)
The `quantity-precision-mismatch` retry parsed the allowed precision as
`detail.split()[-1]` — any wording variation aborted the retry — and used
`round()`, which can round UP past the cash/ADV budget just computed.
Production lost 6 fully-confirmed entries to this failure class
(RCAT/ONDS/CELZ/VOYG/VERU/BCDA, 2026-05-28→06-05); all-time, 17 of 25
approved signals died at `buy_failed` vs 8 executed. Fix: the allowed
precision is now the last integer anywhere in the detail (fallback: whole
shares), and the quantity is FLOORED to it, never rounded up.

### News-capture leak (`news/fetcher.py`, `main.py`)
The RTH freshness cutoff (1 min) silently and permanently dropped every
article the Benzinga feed indexed >60s after its publish timestamp, and every
article that landed while a news cycle overran its 60s interval (buy fills
block the cycle up to 30s polling for fill data). These articles were never
scored, never recorded — invisible loss of exactly the fast-breaking
catalysts the strategy targets. Fix: freshness widened to 3 min with a
session-scoped scored-article dedup set, so Claude still scores each article
exactly once (a failed Claude batch leaves its articles eligible for retry).
RTH Benzinga lookback widened 2 → 5 min to match. The price-confirmation
gates remain the arbiter of whether a 2-3-min-old move is still live.

### Audit findings recorded, no action yet (needs clean eval data)
- Clean RTH-only forward returns (n≈100/class) show unconditioned positive
  news is NEGATIVE-EV intraday at our latency (avg60 −0.2%…−1.6% across all
  catalyst classes) — do NOT loosen catalyst/confidence gates on volume
  grounds; the edge, if present, is premarket/at-open + confirmation.
- Claude confidence shows NO positive monotonic relationship with forward
  returns on clean data (0.8+ bucket performs worst). Re-examine the
  confidence gate once the recomputed table has a few weeks of clean data.
- Realized stops average −2.6% (ex-GOAI) on a −2% trigger: ~0.5-1% of poll
  latency + fees. Structural to polled stops on T212; acceptable while
  position sizes are small, revisit if sizing grows.

---

## v17.5 — 2026-06-30 (premarket prev-close strike counter)

### Premarket prev-close strike counter (`premarket/scanner.py`)
Post-mortem of 2026-06-30 (33 candidates, 12 expired as "eval window closed"):
root cause was `gap_pct=None` having no retry bound. When Finnhub returns `pc=0`
and Twelvedata's daily bar hasn't rolled at 09:30 yet, every evaluation cycle
returns `gap_pct=None` and the candidate retries silently until the 30-min window
closes. Added `_gap_pct_strikes` dict + `_GAP_PCT_EXPIRE_AFTER=5`: after 5
consecutive `gap_pct=None` cycles a candidate expires as `"prev_close: no previous
close after 5 consecutive retries"` rather than as the uninformative "eval window
closed." The eval window stays at 30 min — with both strike counters active, all
candidates should resolve within 10 minutes of the opening block lifting, well
within the gap-and-go momentum window. (A prior commit in this session had
incorrectly extended the window to 45 min — that was a symptom fix, not a root fix.)

Note: `news_cycle` already runs `evaluate_premarket_candidates()` before fetching
RTH news within the same sequential job — there is no cross-job token contention,
and no scheduling change is needed.

### Algorithm docs: `partnership` empirically ruled out as TRADEABLE_CATALYST
60-day forward-return analysis (233 positive partnership signals, `already_moved=0`):
avg_5m = +0.010%, median = 0.000%, only 3 of 233 moved >1% in 5 minutes. The
catalyst class correctly remains excluded.

---

## v17.4 — 2026-06-29 (premarket no-coverage expiry + opening-block log fix + Claude cost cuts + Grafana fixes)

### Premarket no-coverage expiry (`premarket/scanner.py`)
Uncoverable tickers (no Finnhub/Twelvedata quote) were retrying every minute for
the full 30-min eval window — ~600–900 wasted API calls per session. A per-candidate
strike counter (`_no_quote_strikes`) now expires candidates after 3 consecutive
`conf=None` returns. Threshold absorbs 1–2 transient token-bucket misses.

### Opening-block log misattribution fix (`premarket/scanner.py`)
`_apply_confirmation()` checked `gap_pct is None` BEFORE `reason_code == "opening_block"`.
The opening block returns before `prev_close` is computed, so `conf.day_change_pct=None`
on all opening-block cycles — every covered stock logged "prev close unavailable" for
the first 5 minutes after the open (observed 2026-06-29: all 40 candidates including
AMGN, PFE). Behavior was correct; logging was wrong. Reordered the checks.

### Pre-Claude analyst action filter + tighter max_tokens (`news/fetcher.py`)
Analyst rating events (price target, upgrades, downgrades, coverage initiations) are
never tradeable (`analyst_action` not in `TRADEABLE_CATALYSTS`) but previously consumed
Claude tokens before being rejected at Gate 2. A conservative `_ANALYST_ACTION_RE` regex
drops them before the API call, saving ~15–25% of Claude output tokens/day. Also tightened
`max_tokens` from `articles × 80 + 128` to `articles × 60 + 64` (empirical output is
~55 tokens/article; old formula over-budgeted ~45%).

### Grafana fixes (`grafana/dashboards/momentum_trader.json`)
- Panels 12, 13, 17: replaced `TO_CHAR(NOW(), 'YYYY-MM-DD')` date filters with proper
  `::timestamptz` casts — the old pattern used UTC midnight as a cutoff while timestamps
  are stored in London time, causing panels to go blank or miss data during BST.
- Panel 18 (System Events): fixed datasource from broken `"${DS_POSTGRES}"` variable
  reference to the consistent `{"type":"postgres","uid":"trader-postgres"}` used by all
  other panels. Panel 18 would silently fail on a clean provisioned Grafana install.
- New panel 19: Pre-market Scan Heartbeat — the `premarket_scan` job writes heartbeats
  but was invisible in Grafana; added alongside the existing news_cycle/monitor panels.

### Docs updated
- `docs/algorithm.md`: §3.1 analyst-action pre-filter, §7 no-coverage expiry + log fix.
- `docs/api_reference.md`: updated max_tokens formula; removed stale DEMO_PORTFOLIO_VALUE claim.
- `docs/database_schema.md`: added `catalyst_magnitude` to news_signals table; clarified what gets saved vs dropped.

---

## v17.3 — 2026-06-25 (prev-close available before daily bar rolls at 09:30 ET)

`get_volume_stats()` was returning `(None, None, None, None)` whenever
Twelvedata's daily bar hadn't rolled to today yet — a transient 5-minute
window at the start of every session. `prev_close` and `avg_daily_volume`
come from prior completed sessions (`values[1..]`) so they are always valid.
Only `today_volume` (required for RVOL) genuinely needs today's bar.

Root cause of the 10-session zero-trade drought (2026-06-11 to 2026-06-25):
every pre-market candidate hit "prev close unavailable — retrying" for the
full 31-minute evaluation window and expired without ever being evaluated.

Changes:
- `market/twelvedata_bars.py`: when today's bar hasn't appeared, use
  `values[0..]` (which is yesterday's completed bar) as `prior_bars` and
  return `(None, avg_daily_volume, avg_dollar_volume, prev_close)` instead
  of `(None, None, None, None)`.
- `market/price_check.py`: split the fail-closed guard — hard fail only on
  missing ADV/liquidity data; when `today_volume is None` log a warning and
  skip the RVOL gates but continue with dead_cat / extended_move / illiquid /
  momentum / VWAP. RVOL gate now guarded by `today_volume is not None`.
- `grafana/dashboards/momentum_trader.json`: added **System Events** panel
  showing the `system_events` table — critical events (zero_trade_session,
  twelvedata_credits_exhausted) were invisible in Grafana before this.
- Tests: `TestTwelvedataVolumeStats` updated to assert the new contract.

---

## v17.2 — 2026-06-25 (Twelvedata Grow plan — raise rate limits)

Upgraded from Twelvedata Basic (800 credits/day, 8 calls/minute) to Grow
$29/month (no daily cap, 55 calls/minute). Updated constants in
`market/twelvedata_bars.py`:

- `_DAILY_CREDIT_LIMIT` 800 → 50,000 (backstop safety ceiling only — no hard cap on Grow)
- `_CREDIT_HEADROOM` 20 → 100
- `_DAILY_CREDIT_SOFT_CAP` 780 → 49,900
- `_CREDIT_WARN_AT` 640 → 40,000
- `_PER_MINUTE_LIMIT` 8 → 55

The 8/minute Basic ceiling was the root cause of the systematic 429 burst at
09:30 ET: 35 pre-market candidates × ~4 TD calls each = ~140 in-flight requests
against an 8/minute allowance. On the Grow plan the token bucket never depletes
before the burst completes. No logic changes.

---

## v17.1 — 2026-06-24 (session no-quote blackout + per-minute TD rate-limit guard)

Post-mortem of 2026-06-24 zero-trade day revealed two compounding bugs:

### Session no-quote blackout (`main.py`)

Tickers with zero Finnhub/Twelvedata coverage (e.g. EGGF, OXAC on 2026-06-24)
were parked in the retry queue on every news cycle for hours. After
`_RETRY_TTL_MINUTES` (5 min) the item expired — and the next Benzinga article
about the same catalyst created a new queue entry. Net effect: a no-coverage
ticker consumed TD credits and log noise every minute for the entire session
with zero possibility of confirmation.

**Fix:** `_queue_retry()` now tracks per-ticker strike counts
(`_no_quote_ticker_strikes`). After `_NO_QUOTE_BLACKOUT_RETRIES=2` consecutive
retries with no price data, the ticker is added to `_no_quote_blackout` and
silently skipped for the rest of the session. Strikes and the blackout set reset
on service restart (i.e. next trading day).

### Per-minute Twelvedata token bucket (`market/twelvedata_bars.py`)

The 429 storm at 09:30 ET was not just a premarket fan-out problem — any RTH
cycle with many signals could burst past the per-minute rate limit.

**Fix:** Thread-safe token bucket (`threading.Lock`, `_bucket_tokens`,
`_bucket_last_refill`) refills at `_PER_MINUTE_LIMIT / 60` tokens per second.
`_claim_minute_token()` is called after `credits_exhausted()` in every public
entry point that calls `_get_time_series` or `get_twelvedata_quote`. A miss
returns `None` and logs at INFO — the caller treats it as a transient data gap
and parks the signal in the retry queue.

Tests: 91 → 94 (two new bucket tests in `TestTwelvedataCreditGuard`).

---

## v15.8 — 2026-06-17 (catalyst materiality scoring + broker reconciliation)

Two strategy and safety upgrades recommended by quant review:

### Catalyst materiality extraction (signal quality)

The system previously classified catalyst *type* (14 classes) but not
catalyst *magnitude* — an FDA approval for a $50M micro-cap and one for a
$5B mid-cap were treated identically. Bernard & Thomas (1992) PEAD evidence
shows post-event drift is proportional to surprise size, not just direction.

**Changes:**
- New `catalyst_magnitude` field (integer 1–5) added to the Claude tool schema
  and system prompt. Rubric anchored to expected intraday move relative to
  market cap: 5 = transformative (micro-cap FDA/M&A, >20% earnings surprise),
  4 = major, 3 = material, 2 = modest, 1 = noise (PT raise, reiteration, MOU).
- New Gate 4 in `fetch_all_news()`: signals below `MIN_CATALYST_MAGNITUDE`
  (default 2) are filtered before entering the trade path. Magnitude=1 signals
  (analyst reiterations, vague partnerships, conference attendance) that
  somehow score "positive" are now blocked at the gate.
- `catalyst_magnitude` persisted to `sentiment_scores` and `news_signals`
  tables (DB migration: `ADD COLUMN IF NOT EXISTS`). The nightly
  `forward_returns` job will accumulate magnitude-stratified return data —
  the eval loop can now measure whether high-magnitude signals actually
  outperform.
- `MIN_CATALYST_MAGNITUDE=2` in `.env`/deploy — set to 3 to restrict to
  material+ catalysts only.

### Broker reconciliation (safety)

The monitor previously trusted only the DB as source of truth. Two dangerous
divergences were invisible:
- **Phantom** (DB-open, broker-flat): `close_trade()` failed after a sell.
  Monitor kept trying to exit a flat position forever.
- **Orphan** (broker-open, DB-flat): `open_trade()` failed after a buy fill,
  and emergency flatten also failed. Live unmanaged position with no stop.

**Changes:**
- New `get_broker_positions()` in `trading/executor.py`: calls T212
  `/equity/portfolio`, returns `{ticker: quantity}` or `None` on API failure.
- New `_reconcile_positions()` in `monitor/position_monitor.py`: diffed every
  monitor cycle against `get_open_trades()`. Phantoms and orphans are logged
  at `CRITICAL` level for manual review. Never auto-reconciles — a transient
  API timeout looks identical to "broker has no positions" and auto-closing
  would flatten real positions.

Tests: 63 → 69.

---

## v15.7 — 2026-06-17 (three correctness fixes from third code review)

Three bugs identified by automated multi-angle code review of v15.6:

- **`get_gbp_usd_rate()` cache and zero-rate bugs (3-in-1).** (a) The fallback
  expression `if _FX_CACHE["rate"] else _FX_FALLBACK` used truthiness, so a
  cached `0.0` from a malformed Twelvedata response (`{"price": "0"}`) would
  either be served directly (ZeroDivisionError in `adv_cap_gbp = ... / fx`) or
  incorrectly replaced by the hardcoded fallback. Fixed: added an explicit
  `rate <= 0` guard that raises before caching, and changed the fallback
  expression to `is not None`. (b) On API failure, `_FX_CACHE["ts"]` was not
  updated, so every subsequent call during an outage fired a new HTTP request
  (retry storm) instead of throttling to one attempt per TTL window. Fixed:
  always update `ts` in the except branch.

- **`ValueError` discrimination in `is_market_open()`.** `open_at_time()` raises
  `ValueError` for two distinct reasons: (a) timestamp outside the session window
  — correct, means closed; (b) schedule column validation failure — a
  programming error (pmc schema/version mismatch). The broad `except ValueError:
  return False` silently swallowed case (b), bypassing the Finnhub fallback
  entirely. Fixed: check the exception message; only return `False` for the
  session-window case, let unexpected ValueErrors fall through to Finnhub.

- **EOD flatten `force_market` not explicit in `position_monitor`.** The
  `sell()` call for EOD exits passed `reason="eod_flatten"` without
  `force_market=True`. Market-order routing depended solely on a string literal
  match inside `sell()`. A reason-string refactor would silently revert to a
  bounded limit order near the close, risking overnight carry. Fixed:
  `sell(..., force_market=(reason == "eod_flatten"))` so the flag is always
  set explicitly at the call site.

Tests: 61 → 63 (two new: `test_outside_session_window_returns_false`,
`test_schema_valueerror_falls_back_to_finnhub`).

---

## v15.6 — 2026-06-17 (three correctness fixes from second code review)

Three bugs identified in a second independent code review of v15.3–v15.5:

- **`open_at_time()` ValueError in pre/post-market.** `open_at_time()` raises
  `ValueError("The provided timestamp is not covered by the schedule")` when
  called outside the session window (before open, after close). The broad
  `except Exception` handler was catching this and falling through to a Finnhub
  network call — so every news cycle tick before 13:30 UTC burned a Finnhub
  API credit unnecessarily. Fixed: catch `ValueError` separately and return
  `False` immediately. Generic exceptions still fall back to Finnhub.

- **`sell()` `force_market` parameter.** The emergency DB-failure flatten in
  `main.py` was using `reason="eod_flatten"` to force a market order — correct
  behaviour, wrong semantic. Logs would record `reason=eod_flatten` for a DB
  failure event, making forensics harder. Added `force_market: bool = False`
  keyword argument to `sell()`. The routing is now `force_market or reason ==
  "eod_flatten"`. Emergency flatten calls pass `reason="db_record_failed",
  force_market=True`. EOD flatten still passes `reason="eod_flatten"`.

- **GBP/USD currency mismatch in `calculate_quantity()`.** T212 cash API
  returns portfolio value and available cash in GBP. Stock prices and ADV are
  in USD. `quantity = max_spend / price` was dividing GBP by USD — dimensionally
  wrong — systematically undersizing by ~21% (at GBP/USD 1.27). Fixed: fetch
  live GBP/USD rate from Twelvedata (`get_gbp_usd_rate()`, 60-second in-process
  cache, falls back to 1.27 if the API is unavailable). All four sizing
  constraints computed in GBP; budget converted to USD before `/ price`.
  The ADV participation cap is also correctly converted (USD ADV → GBP before
  comparison, GBP budget → USD before division).

Tests: 61 (no new tests; existing TestPositionSizing tests updated to mock
`get_gbp_usd_rate=1.0` so they test sizing logic independently of FX).

---

## v15.5 — 2026-06-17 (market-open DST detection fix — zero pre-market trades)

Second zero-trade-day investigation (same session as v15.4). Root cause: a
long-running process whose `pandas_market_calendars` NYSE calendar object
accumulated stale DST state. In summer (EDT = UTC-4) the manual comparison
`market_open <= now_utc` was evaluating the open as 14:30 UTC instead of
13:30 UTC — a 60-minute delay. Every pre-market candidate was still pending
when the system finally called `evaluate_premarket_candidates()`, but by then
`_minutes_since_open()` had already exceeded 30 min and all 19 candidates
were immediately expired. Zero trades resulted.

**Evidence:** opening_block message at 14:30:31 UTC read "0.5 min since open"
(correct for a 14:30 UTC open) instead of ~60 min, proving the system believed
the market opened one hour late. Service had been running continuously since
before midnight (process 3939342 — no restart between sessions).

**Fix:** replaced the manual `market_open <= now_utc` comparison in
`is_market_open()` with `_NYSE.open_at_time(sched, now_utc)`, which
re-derives open/close from first principles on each call and is immune to
stale cached DST state in the long-running calendar object.

**Today's signals (correctly rejected by filters, not the DST bug):**
- ALOT M&A (+71% gap > 20% max) — correct: exhausted
- ICCM FDA (+162% gap) — correct: exhausted
- QURE extended_move (+72%) — correct: exhausted
- SPRO, FTHM, OBAI — correct: penny stocks
- GSK, JBL — correct: low_momentum (dead tape, catalyst not moving stock)
- ANGO FDA IDE approval — gap only +0.25%, market not believing the catalyst
- Most contract_win signals — gaps below 1% min, market unimpressed

Tests 61 → 62.

---

## v15.4 — 2026-06-17 (emergency flatten market order, TP/SL ratio guard, gone-TP tests)

Three correctness gaps identified in the v15.3 audit (by code review):

- **Emergency flatten uses market order.** If `open_trade()` fails after a
  broker buy fills, the position is unrecorded. The emergency sell was using
  `reason="db_record_failed"` which routes through a bounded limit order — a
  limit that fails to fill leaves the position invisible with no stop or EOD
  logic. Changed to `reason="eod_flatten"` so sell() uses a market order for
  execution certainty.
- **TP/SL ratio guard.** Config validation now rejects `TAKE_PROFIT_PCT <
  STOP_LOSS_PCT` at startup. A sub-1:1 R:R requires a >50% win rate just to
  break even; misconfiguring TP=1.0/SL=2.0 was silently accepted.
- **Tests for `_handle_gone_tp_order`.** The most dangerous code path from
  v15.3 — GONE TP order → fill-detail-check before closing as take_profit —
  had zero test coverage. Added 2 targeted tests.

Tests 58 → 61.

---

## v15.3 — 2026-06-17 (risk-control and execution audit)

Deep live-logic audit focused on failures that can corrupt P&L, leave positions
unmanaged, or make reports disagree with trading reality.

- **Mode-scoped position management.** `get_open_trades()` now filters by
  active `TRADING_MODE`. Without this, switching a shared DB from demo to live
  could make the live monitor act on demo rows.
- **Timestamp-safe risk gates.** 24h ticker cooldown, trades-today, and today's
  realized P&L now cast stored ISO text to `timestamptz` instead of relying on
  lexicographic text comparisons / `LIKE` date prefixes.
- **Cashflow-sign-safe P&L.** Broker fill cashflows are normalized as
  `abs(sell_net_gbp) - abs(buy_net_gbp)`, so a buy reported as a negative wallet
  impact cannot invert realized P&L or disable the daily kill switch.
- **No orphaned buys.** If the broker buy fills but the DB trade insert fails,
  the system immediately attempts an emergency flatten. If the trade row exists
  but `acted_on` marking fails, the position still gets its TP/monitoring.
- **No untracked resting TP.** If a TP order is placed but its order id cannot
  be stored, the system cancels that TP; otherwise a later stop/time-stop sell
  would not know about the reserved shares.
- **TP 404 is no longer assumed profitable.** A disappeared pending order is
  resolved through fill detail before the DB trade is closed as `take_profit`;
  missing fill detail is treated as expired/cancelled and falls back to polled
  exits.
- **EOD flatten uses market orders.** Stop/time-stop exits remain
  bounded-slippage limits, but the end-of-day flatten prioritizes execution
  certainty over slippage control to avoid overnight gap risk.
- **Daily volume freshness guard.** Twelvedata daily bars must be for today's ET
  date before they can feed RVOL. Yesterday's full-day volume can no longer
  masquerade as today's cumulative volume.
- **Config sanity checks.** Startup validation now rejects impossible trading
  settings (bad mode, inverted thresholds, zero/negative stops, invalid risk
  caps) before the service can trade.
- **Docs/research basis.** `docs/algorithm.md` now maps each major rule to
  respected momentum, PEAD, execution-cost, intraday-volume, and backtest-
  overfitting research instead of relying only on incident anecdotes.
- Tests 49 → 54.

---

## v15.2 — 2026-06-16 (prev-close backfill — fixes zero pre-market trades)

Investigation of "why no trades today" (212 positives scored, 31 gate-passing,
**0 trades**). Root cause was a single data bug masking every pre-market catalyst.

- **The bug.** Finnhub's free `/quote` returns `pc=0` (previous close) in the
  first minutes after the open, before its daily rollover settles. Every gap /
  dead-cat / extended-move filter measures vs prev close, so a zero `pc` made
  `day_change_pct = None`, and the pre-market evaluator rejected the candidate
  **terminally** as "no prev close — gap unknown". Confirmed in the DB: all 19
  rejected pre-market candidates today (incl. OTLK +27%, SPCB +18%, SLP M&A)
  carried that exact note. `get_quote_with_fallback()` never consulted
  Twelvedata because Finnhub *was* reachable — it just had a bad `pc`.

- **Fix 1 — `pc` backfill (`market/price_check.py`).** When Finnhub's quote has
  `pc ≤ 0`, backfill prev close from Twelvedata (which carried the correct value
  for every affected name) while keeping Finnhub's real-time price. One extra
  credit only on the names that need it.

- **Fix 2 — retryable missing-pc (`premarket/scanner.py`).** A still-missing prev
  close at the open is now a transient, retryable condition (stay pending, retry
  within the 30-min window) — same handling as `opening_block` — not a terminal
  rejection.

- **Honest scope note.** With the fix, most of today's hindsight "winners" are
  *still* correctly rejected — OTLK/TRNR are sub-$5 (`penny_stock`), SPCB/SLP
  are below the $5M ADV floor (`illiquid`) with RVOL ~1.3 (< 1.5). The fix does
  not loosen risk; it replaces a **false** rejection reason ("no prev close",
  which would also kill a legitimate liquid large-cap gapping on morning news)
  with the **correct** fundamental one. Genuinely unpriceable tags
  (OLIT/STI1/VHNA/IIVI — delisted/SPAC/merged) still 404 on both sources and
  expire correctly.

- Tests 47 → 49 (`TestQuoteFallback`: pc-backfill + no-backfill-when-valid).

---

## v15.1 — 2026-06-15 (deep logic audit)

Full code audit against quant-desk standards. Findings + fixes:

- **Backtest ↔ production parity (the big one).** `backtest_db.py` was still
  replaying v12-era logic: dead-cat measured vs today's OPEN (production uses
  prev close), no spread filter, no VWAP gate, stale `low_momentum` semantics.
  A backtest that tests different logic than production manufactures false
  confidence. Rewrote as `run_v15_check()` — mirrors `confirm_price_signal()`
  gate-for-gate (opening block → penny → spread → dead-cat/extended-move vs
  prev close → ADV liquidity → dead-tape floor → ceiling → RVOL → VWAP), with
  a `_session_vwap_at()` helper computing VWAP from the intraday bars. Added
  `TestBacktestParity` asserting the backtest's constants equal `cfg`.
- **Fail-CLOSED on missing volume data (live risk).** When Twelvedata volume
  was unavailable, `confirm_price_signal` set `avg_dollar_volume=None`, which
  *bypassed* both the liquidity gate AND the RVOL gate — a signal could trade
  on momentum+VWAP alone, i.e. risk relaxed exactly when data was least
  reliable. Now returns None (defer + retry) — no confirmation, no trade.
- **Momentum-baseline degenerate guard.** When only one usable bar exists
  (right after open), the baseline could equal the current bar → a spurious
  0.00% momentum reading. Now returns `past_price=None` so the caller's
  early-session open-price fallback (or a clean defer) applies instead.
- **Verified OK (no change):** `_parse_fill` fee summation is correct (T212's
  `taxes[].quantity` is a monetary amount, not a share count — confirmed
  against the API schema); all percentage math is divide-by-zero guarded.

---

## v15 — 2026-06-15

First full live session on v14 took **0 trades from 957 scored articles**. Root
causes found by querying the production DB + logs (no money lost — every safety
system worked; the strategy simply could not fire). Three fixes.

### Symbol hygiene — pre-market pipeline was unreachable
- `clean_benzinga_symbol()` drops foreign-exchange tags (`TSX:MDA`, `LON:…`,
  etc. → None — not US-tradeable) and strips Benzinga's disambiguation digit
  (`INBX1` → `INBX`, `SAIL1` → `SAIL`). These uncleaned tags reached the price
  check, got no quote, and burned 30-min pre-market eval windows.
- `resolve_t212_ticker()` now returns None for non-US tags; `fetcher.py` skips
  them (guard ordering fixed so None never hits `seen_checker`).

### Quote fallback — Finnhub doesn't cover the small caps we target
- `get_twelvedata_quote()` + `get_quote_with_fallback()`: Finnhub `/quote` →
  Twelvedata `/quote` fallback. Finnhub's free tier silently omits small caps
  and recent IPOs (2026-06-15: CUPR/ELAN/WBD/INBX/SAIL all had no Finnhub
  quote, all priced on Twelvedata). Both sources normalise to the same
  `c`/`o`/`pc` keys. Used by `confirm_price_signal` and `get_current_price`.

### Momentum confirmation — VWAP replaces the fixed % floor (researched)
- The v14 fixed +1.5%/5-min floor was the binding constraint: **1,077 of all
  all-time rejections were `low_momentum`**, and every real large-cap catalyst
  on 2026-06-15 was rejected at near-zero 5-min change (DXCM +0.14%, SNY
  +0.07%, LLY +0.01%) — deep order books reprice slowly (PEAD). A single %
  threshold cannot serve both a $2 micro-cap and a $1000 mega-cap.
- **Fix (size-neutral, research-backed):** confirm with **VWAP-relative
  position** — a stock held at/above session VWAP is being accumulated
  regardless of raw % change; below VWAP is gap-and-crap regardless. New
  `get_session_vwap()`. The fixed floor is reduced to a 0.2% dead-tape noise
  filter; VWAP (step 10, runs last to save a credit) does the real judgement.
  New reason code `below_vwap`. Toggle via `REQUIRE_VWAP_CONFIRMATION`.
- Research: post-earnings-announcement drift literature + VWAP-reclaim
  practitioner playbooks (citations in docs/algorithm.md §4).

### Observability
- Per-cycle **signal funnel** log line: `evaluated → seen, cooldown, no-data,
  rejected, OPENED` — the 25-gate-passing-→-0-trades attrition that took a
  manual DB dig on 2026-06-15 is now one line in journald.

Tests: 44 passing (was 30).

---

## v14 — 2026-06-12

The "execution & risk" release. Root cause addressed: the realized win/loss
asymmetry was inverted vs design (+5%/−2% designed; avg win £5.52 / avg loss
£10.37 realized). Full algorithm documentation added at `docs/algorithm.md`.

### Exit execution (the #1 P&L leak)
- **Resting take-profit limit order** placed at the exchange immediately after
  every buy (`tp_order_id` on trades). Zero polling latency, zero
  spread-crossing on the profit side (old polled TP realized +3.1% on a +5% target).
- **Bounded-slippage stop sells** — exits are LIMIT orders at trigger ×
  (1 − `SELL_LIMIT_SLACK_PCT` 1%), with market fallback if the limit is
  rejected and cancel-and-retry if unfilled. GOAI's −18.99% fill on a −2%
  market stop cannot recur.
- **Cancel/fill race handled** — resting TP is cancelled before any stop sell;
  if it filled mid-cancel, the trade closes as take_profit, never double-sold.
- **Monitor every 20s** (was 60s) — `MONITOR_INTERVAL_SECONDS`.
- **EOD flatten** — all positions force-closed `EOD_FLATTEN_MINUTES` (10)
  before the close. No overnight gap risk, ever.
- **Market-hours guard in monitor** — never sells into a closed market.

### Entry filter fixes
- **Dead-cat and day-change vs PREVIOUS CLOSE** (Finnhub `pc`) — overnight
  gap-downs now count; using today's open silently ignored them.
- **Extended-move ceiling `MAX_DAY_MOVE_PCT=25%`** vs prev close — closes the
  v13 hole where a stock up 80% on the day but flat in the last 5 min passed.
- **ADV-based liquidity filter** — was today's volume × price, which EXPLODES
  during a halt-spike and passed exactly the names it should block. Now
  20-day ADV × price. Floor raised $1M → **$5M**.
- **RVOL (time-of-day normalized volume)** replaces raw full-day volume ratio
  — `MIN_RVOL=1.5` / `MAX_RVOL=20`. The old "1.5× full-day average" was
  near-impossible at 10:00 and trivial at 15:45.
- **Momentum baseline by timestamp** — thin stocks skip 1-min bars; `values[5]`
  could silently be 20 min old. Bars now selected by actual age;
  `MOMENTUM_LOOKBACK_MINUTES=5` (removes dead `MOMENTUM_WINDOW_MINUTES`).
- **Spread proxy filter `MAX_SPREAD_PCT=3%`** — latest 1-min bar range/close.
- **Penny floor raised $2 → $5** (`MIN_STOCK_PRICE`).
- **`MIN_SENTIMENT_CONFIDENCE` finally enforced** — existed since v1, was
  never checked anywhere.

### Portfolio risk controls (new)
- **Daily kill switch** `MAX_DAILY_LOSS_PCT=2%` — realized daily loss beyond
  this stops all new entries until tomorrow. Fail-closed.
- **`MAX_OPEN_POSITIONS=3`**, **`MAX_TRADES_PER_DAY=10`** — re-checked after
  every fill (Jun 3: four correlated semi buys in 2 minutes).
- **Risk-based, liquidity-capped sizing** — min(5% hard cap, equity×0.25%/stop,
  0.5% of ADV dollars, cash).

### Pre-market pipeline (new)
- `premarket/scanner.py` — scores 08:00–09:30 ET news into a watchlist
  (`premarket_candidates`), evaluates at the open with a gap gate
  (`MIN_GAP_PCT=1%`–`MAX_GAP_PCT=20%`) plus full standard confirmation.
  Deliberately does NOT pre-place orders (gap-and-crap). Same risk gates and
  buy path as RTH.

### Claude prompt & classifier
- `temperature=0`; rubric moved to cached system prompt (~90% input-cost cut);
  **forced tool use** replaces JSON string parsing and truncation recovery.
- Rubric restructured as a decision tree with few-shot examples.
- New fields per article: **`catalyst_type`** (14-class taxonomy) and
  **`already_moved`**. Code gates trading on `TRADEABLE_CATALYSTS` — the model
  classifies, the system decides.
- **Offerings/dilution → negative** (small caps sell offerings into spikes).

### Eval loop (new)
- **Every classification persisted** to `sentiment_scores`; nightly job
  (`analysis/forward_returns.py`, 22:30 UTC) fills 5/15/60-min forward
  returns via yfinance. Prompt changes are now measurable (queries in
  docs/algorithm.md §9).

### Reliability
- **Retry queue** for signals hit by transient data outages — "will retry next
  cycle" used to be impossible (freshness filter ate the article; SPCX Jun 12).
  Scored signals now park for up to 5 min and bypass the fetch path.
- **Symbol-map startup retry** (30s backoff on 429) + daily 08:00 UTC rebuild.
- **Twelvedata credit metering** — WARNING at 80% of the 800/day budget.
- **Heartbeat table** per job + Grafana staleness alert query.
- **Deploy pipeline**: pytest gate before deploy; on-VM `cfg.validate()`
  BEFORE service restart; post-restart health check that fails the workflow
  on tracebacks. (All three would have caught the 2026-06-11 18-hour outage.)

### Backtest realism
- Entry at next-bar open (production is 10–90s late by construction).
- Same-bar stop-priority fills (was target-first — inflated win rate).
- Cost model: 0.30% FX round trip + liquidity-tiered slippage per side.
- RVOL via the same production helper.

### Same-day review fixes
- **sell() status race** — a network error during fill polling (`status=None`)
  was treated as "filled", which would close the trade in the DB while the
  real order stayed live on the book (position desync). None now means
  UNKNOWN → keep polling; only `FILLED`/`GONE` count as fills.
- **Pre-market candidates survived the opening block** — candidates evaluated
  on the 09:30/09:31 cycles were permanently rejected with `opening_block`
  before the block lifted at 09:35; the pipeline would never have traded.
  `opening_block` now leaves the candidate pending (it is a timing gate, not
  a verdict); all other rejections remain final (credit budget).
- **`portfolio_snapshots` was never written** — `save_snapshot()` existed
  since v1 with zero callers; Grafana's "Portfolio Value Over Time" panel was
  empty from day one. New 5-min `portfolio_snapshot` job (market hours only).
- **Dead-cat guard fallback** — when prev close is unavailable from both
  sources, fall back to the open-based day move instead of silently
  disabling the guard.
- **8 new Grafana panels** — news-cycle/monitor heartbeats (the 18h-outage
  panel), today's realized P&L vs kill switch, trades-today vs cap,
  7-day rejection funnel, today's pre-market candidates, classifier forward
  returns by sentiment and by catalyst class.

---

## v13 — 2026-06-11

### Changes

**Trading logic:**
- **Halt-article NEUTRAL rule in Claude prompt** — headlines containing "Shares Halted On Circuit Breaker To The Upside", "Stock Halted And Resumed", "Trading Halted", "Circuit Breaker Triggered", "Halt Lifted", or any mention of a regulatory halt/resumption are now rated NEUTRAL by Claude. These articles publish AFTER the 30–120% spike; buying on them = buying the absolute top. Every Jun 8–11 loss was a halt-article trade.
- **Momentum ceiling: `MAX_PRICE_MOVE_PCT=15%`** — signals where recent_move_pct > 15% are rejected with `reason_code="high_momentum"`. Complements the Claude prompt change: if a halt article slips through Claude, the price confirmation layer blocks it. Day-trader reasoning: a stock that's already up 15–120% in the last 5 min is a post-halt top, not an entry.
- **Raise momentum floor: `MIN_PRICE_MOVE_PCT` default 0.5% → 1.5%** — tighter entry quality. Weak 0.5–1.5% moves don't produce enough reward to justify the risk given the 2% stop-loss. The 5% take-profit target needs at least 1.5% of real momentum to be tradeable.
- **Volume ceiling: `MAX_VOLUME_RATIO=20×`** — signals with volume_ratio > 20× are rejected with `reason_code="high_volume"`. Extreme volume on micro-caps is the circuit-breaker pattern signature, not a genuine momentum catalyst. All halt-article trades this week had vol_ratio > 30×; legitimate entries rarely exceed 15×.
- **Penny stock filter: `MIN_STOCK_PRICE=$2.00`** — signals where current_price < $2.00 are rejected with `reason_code="penny_stock"`. Sub-$2 stocks have catastrophic bid-ask spread relative to price and are disproportionately targeted in halt-pump patterns. All Jun 8–11 losses were on stocks < $5 at entry.

**Backtest improvements (`backtest/backtest_db.py`):**
- **24-hour per-ticker cooldown** — production behaviour is to skip a ticker for 24h after a trade. The backtest now mirrors this: once a v13 trade is executed, subsequent signals for the same ticker within 24h are tagged `rejected:cooldown`. Without this, the backtest counted duplicate-ticker losses that production would have blocked, overstating loss count.
- **Updated to v13 constants** — all five new settings (`MAX_PRICE_MOVE_PCT`, `MAX_VOLUME_RATIO`, `MIN_STOCK_PRICE`, `MIN_PRICE_MOVE_PCT=1.5`, `MAX_VOLUME_RATIO`) applied in `run_v13_check`. Backtest header now shows the full filter set.

### Why
Post-mortem on Jun 8–11 trading week (6 signals, 6 losses, −34% cumulative):
- Every single loss was a halt-article trade. The pattern: "X Shares Halted On Circuit Breaker To The Upside" publishes after the stock has already spiked 30–120%. The system scored these as POSITIVE because the Claude v12 prompt didn't explicitly call out halt articles. By publication time, the move is over — we were buying the top every time.
- All losses were on stocks priced < $5 (INHD $0.66, GOAI $1.35, NVNI $1.32) — thin spreads magnify slippage on exit.
- Momentum readings on these signals were all > 30% (halt spike); volume ratios were all > 30× (halt pattern). Both new ceilings would have blocked all 6 trades.

---

## v12 — 2026-06-11

### Changes

**Trading logic:**
- **Replace yfinance with Twelvedata** — `market/twelvedata_bars.py` fetches 1-min intraday bars via Twelvedata `/time_series`. `values[5]` (5th bar, newest-first) = price ~5 min ago, replacing the stale yfinance 15-min baseline. yfinance was returning bars from hours ago on high-volume days (VECO: bar from 09:56 returned at 11:42, giving false +1.20% momentum reading). Twelvedata includes a **staleness guard** that rejects any bar whose timestamp is >10 minutes old.
- **Extend opening-auction block to 5 minutes** — `OPEN_BLOCK_MINUTES` (default 5, was hardcoded 1 min). GOAI's entire spike was in the 09:30 bar; the system bought at 09:32 into full collapse. Returns a `PriceConfirmation(reason_code="opening_block")` so the rejection is recorded.
- **Minimum liquidity filter** — `MIN_DAILY_DOLLAR_VOLUME` (default $1M). Rejects stocks whose daily dollar volume is below this threshold. GOAI had ~$390k ADV — the market sell order moved price 11.7% below the stop-loss trigger due to a thin order book. Rejection code: `illiquid`.
- **Strengthen early-session volume check** — in the 5–15 min window, require `volume_ratio >= 0.5` (was `current_volume > 0`). GOAI passed the old check at 0.7× volume.
- **5-minute momentum window** — momentum baseline now uses `values[5]` (~5 min) instead of the previous ~15-min yfinance baseline. More responsive to fast-moving catalysts.

**Claude prompt improvements (`news/fetcher.py`):**
- **LOI/MOU → NEUTRAL** — Letters of Intent and Memoranda of Understanding are non-binding, no financial terms, can be cancelled. Previously rated positive.
- **Recap article formats → NEUTRAL** — "What's Going On With X Stock?", "Why Is X Up Today?", "Here's Why X Is Moving" are published hours after the move. Previously caused late entries (VECO root cause #2).
- **Large-cap filter** — S&P 500 mega-caps (AAPL, MSFT, GOOGL, AMZN, etc.) rated NEUTRAL unless extraordinary catalyst (>10% earnings surprise, regulatory shutdown). Routine upgrades/PT raises for mega-caps don't produce 5%+ intraday moves.
- **Ticker relevance check** — acquirer tickers in M&A announcements rated NEUTRAL (acquirer almost always drops). Article must be specifically about a ticker for it to be rated POSITIVE.

**Reliability / retries:**
- **Finnhub retries** — `get_finnhub_quote()` retries 3× with exponential back-off (1s/2s/4s). Previously a single timeout silently dropped the signal.
- **Twelvedata retries** — both `get_momentum_baseline()` and `get_volume_stats()` retry 3× with back-off.
- **Position monitor Finnhub failure** — when price feed is unavailable, skip TP/SL check for that cycle but still fire time-stop (no price data needed). Previously fell back to buy_price which could trigger false stop-losses.
- **DB connection retries** — `get_conn()` retries 3× on `OperationalError` (transient TCP/server restart).
- **Benzinga error handling** — specific `Timeout` logging, per-status-code error messages.

**Logging:**
- `news_cycle`: cycle timestamp, per-signal trade log with net/fx/fees, elapsed time per cycle
- `confirm_price_signal`: all rejection reasons include daily dollar volume
- `monitor_positions`: per-trade exit log with buy price, current price, % change, quantities
- `get_finnhub_quote`: per-attempt retry logs, final failure log
- `get_momentum_baseline`: staleness guard details (bar age, bar timestamp)

### Why
Post-mortem on VECO (−3.37%) and GOAI (−18.99%):
- **VECO**: yfinance returned a stale bar from 09:56 at 11:42 giving false momentum; recap article format missed by Claude prompt → late entry at peak. Fixed by Twelvedata staleness guard + prompt update.
- **GOAI**: opening block only covered <1 min (entire spike in 09:30 bar); volume check let 0.7× through; $390k ADV caused 11.7% slippage on exit. Fixed by 5-min block + 0.5× minimum + $1M DDV filter.

---

## v11 — 2026-06-08

### Changes
- **T212 symbol map at startup** — on startup, `trading/executor.py` fetches T212's full instrument catalogue and builds a `shortName → ticker` map. `fetcher.py` uses this map when converting Benzinga symbols to T212 codes instead of blindly appending `_US_EQ`. Fixes ~16% of US small-cap tickers that 404 because T212 retains the original SPAC/IPO code after a company changes its exchange symbol (e.g. `SUNE → JCS_US_EQ` after a reverse merger with JCS).
- **Roundup article filter** — articles tagging more than 3 tickers are skipped before Claude scoring. Benzinga digest articles ("Big stocks moving higher on Monday") routinely tag 15–20 tickers with no per-stock catalyst. These generated large batches of price checks that all failed for low momentum, wasting Claude API calls and Finnhub quota.
- **Crypto ticker filter** — Benzinga tickers prefixed with `X:` (e.g. `X:BTCUSD`) are stripped before T212 ticker construction. These are crypto pair identifiers, not equities — Finnhub returns no quote for them and T212 doesn't trade them.

### Why
June 8 post-mortem: SUNE (SUNation Energy, +30% on the day, 792× volume, approved signal) failed with T212 404 — it trades as `JCS_US_EQ`. Investigation showed 2,798 of 17,143 T212 USD instruments (~16%) have a T212 code that doesn't match their current exchange symbol. The roundup article and crypto filters eliminate two categories of guaranteed-reject signals observed every session.

---

## v10 — 2026-06-07

### Changes
- **T212 quantity precision auto-retry** — when Trading 212 rejects an order with "invalid quantity precision N", the error message includes N (the maximum allowed decimal places). The executor now parses N from the error and retries the order once with the correct rounding. Previously these orders failed permanently; all 6 historical precision failures would have succeeded with this fix.
- **Sentiment stored as `"positive"` in DB** — was hardcoded to `"BULLISH"` in `main.py`. Now consistently uses the value from the Claude output. (No functional impact — filtering happens before the DB write.)

### Why
Friday June 5 post-mortem: BCDA (BioCardia, +38% on the day, 318× volume, +2.37% 15-min momentum) was the one signal that cleared all filters and should have traded. The T212 API rejected it with "invalid quantity precision 2" — we were sending 4 decimal places for a ticker that only allows 2. Review of the DB showed 6 historical failures of this same type across BCDA, VERU, VOYG, CELZ, ONDS, RCAT. All would have been recoverable with a one-retry logic.

---

## v9 — 2026-06-04

### Changes
- **Momentum trading expert prompt for Claude** — replaced the generic "financial news sentiment classifier" system prompt with a domain-specific prompt that teaches Claude which news actually drives intraday price movement. Claude now acts as an expert day trader evaluating whether each article is a genuine 15-minute momentum catalyst.
  - **Positive (0.8–1.0):** earnings beats, FDA approvals, M&A announcements, major contract wins with dollar values, guidance raises, short squeeze signals
  - **Explicitly neutral:** analyst price target raises, "Maintains Buy/Overweight" reiterations, sector commentary, conference attendance, stale news rewrites
  - **Confidence calibration:** genuine catalysts rated 0.8–1.0; weak or ambiguous signals 0.5–0.7

### Why
DB analysis showed ~90% of signals reaching the price confirmation stage were analyst "Maintains X, Raises PT to $Y" headlines (Goldman, Citigroup, BofA, JPMorgan reiterations) — these almost never cause intraday momentum and all got rejected for `low_momentum`. The generic prompt had no way to distinguish a real catalyst from routine sell-side coverage noise. The new prompt encodes that domain knowledge directly, reducing wasted Claude API calls and false-positive signals.

---

## v8 — 2026-06-03

### Changes
- **1-minute article freshness filter** — articles published more than 60 seconds before the fetch are discarded. The news cycle runs every minute, so anything older was either already processed or missed. Acting on stale news risks entering after the move has already happened and reversed.
- **Claude JSON truncation recovery** — `max_tokens` now scales dynamically with batch size (~40 tokens per article). If the response is still cut short, any complete JSON objects before the truncation point are recovered instead of dropping the entire batch.
- **Dead-cat guard added to backtest** — the backtest was missing this check, causing stocks in a sharp daily downtrend (e.g. MRLN −10.5% on the day) to appear as executed trades. Now mirrors production logic.

### Why
Backtest analysis of 2026-06-03 losses revealed:
- GOOG/GOOGL losses: articles were published days earlier but re-indexed by Benzinga; the market had fully priced the news. The freshness filter prevents acting on these.
- Claude batch failures: the hardcoded 512-token limit was too small for batches of >12 articles, silently dropping all scored results. Dynamic token budget and truncation recovery fix this.
- MRLN backtest anomaly: appeared as a trade despite being −10.5% on the day — dead-cat guard was missing from the backtest.

---

## v7 — 2026-06-03

### Changes
- **Block trades in the first minute after open** — hard gate added to `confirm_price_signal()`. The opening auction creates noisy price ticks (e.g. +1.5% in 30 seconds) that look like momentum but reverse within seconds. No buy signal is evaluated before 09:31 ET.
- **Require non-zero volume in the 1–15 min window** — previously the volume filter was completely bypassed for the first 15 minutes. A genuine gap-up will have volume; an auction tick will have 0 shares traded. Now requires `current_volume > 0` in the early window. The ≥1.5× threshold is still used after 15 min.
- **Finnhub open-price fallback only applies after minute 1** — the fallback that uses Finnhub's `o` (open price) as the momentum baseline is now restricted to the 1–15 min window, not the 0–15 min window.

### Why
On 2026-06-03 the system made 4 losing trades (AVGO, MRVL, IBM, MU) all bought at 09:30–09:31 ET. All were bought based on a +0.85–1.53% momentum reading vs the Finnhub open price, with volume=0.0×. All reversed within 5 minutes and hit the stop-loss within 8 minutes. Root cause: opening auction noise passed momentum filter; volume filter was entirely bypassed; 0 shares of actual volume traded.

---

## v6 — 2026-06-02

### Changes
- **`pandas_market_calendars` as authoritative market open/close source** — replaces all ad-hoc Finnhub/wall-clock logic. Handles NYSE holidays, early closes (Black Friday 13:00 ET, Christmas Eve), weekends, and Juneteenth. Fully local — no network call needed.
- **`is_too_late_to_buy()` uses calendar close time** — now respects early-close days correctly (previously hardcoded to 16:00 ET).
- **Fixed `open_at_time()` exception after market close** — replaced with direct `market_open <= now < market_close` comparison that doesn't raise outside session bounds.

### Why
Finnhub's `isOpen` flag lagged by up to 1 hour at session start (observed 2026-06-02: reported closed until 14:29 UTC, missing the entire first hour of trading). `pandas_market_calendars` is a local calendar library — no network dependency means no lag.

---

## v5 — 2026-06-01

### Changes
- **Fixed silent scheduler death** — removed all mid-job APScheduler rescheduling. `news_cycle` now runs every minute unconditionally on a fixed `IntervalTrigger` and returns early when market is closed. Previously, calling `add_job(replace_existing=True)` on the currently-executing job caused APScheduler to silently drop the replacement, leaving the scheduler with no jobs.
- **`Restart=always` in systemd unit** — previously `on-failure` only, which did not restart a hung-but-alive process.
- **APScheduler log level raised to ERROR** — previously WARNING, which suppressed executor job failure messages.

### Why
The scheduler silently died after the first cycle on 2026-05-29 and the process ran for 3+ days with no activity (no logs, no trades). The process was alive so systemd didn't restart it.

---

## v4 — 2026-05-31

### Changes
- **Don't save to DB when price data is unavailable** — previously, signals with `no_price_data` were saved to `news_signals` before the price check, permanently marking the `(article_id, ticker)` pair as seen. The article could never be re-evaluated in subsequent cycles. Now the price check runs before the DB write; a `None` result skips the save entirely.
- **Finnhub market-status retry with wall-clock fallback** — retries up to 3 times (60s between attempts) on API error; falls back to wall-clock check if all attempts fail.
- **Skip volume filter in first 15 minutes** *(superseded by v7)* — volume ratio is near zero at open; bypassed entirely for first 15 min.

### Why
At market open, yfinance had no 1-min bars for CRM/MRVL/SNOW/P on 2026-05-28, saving them as `no_price_data` and blocking re-evaluation for the rest of the day. Also, a single Finnhub timeout on 2026-05-28 caused the scheduler to sleep until the next day.

---

## v3 — 2026-05-28

### Changes
- **Finnhub `/stock/market-status` replaces yfinance SPY heuristic** for `is_market_open()` — authoritative API handles NYSE holidays, early closes, and weekends. The old approach fetched SPY 1-min bars and checked if the last bar was recent; this failed at market open (no bars yet) and on Memorial Day.
- **Finnhub open price as momentum baseline at open** — when yfinance has no intraday bars yet (first ~15 min), use the Finnhub quote's `o` field as the baseline instead of returning `None`.

### Why
On 2026-05-26 (Memorial Day) and 2026-05-27, the system slept all day because `is_market_open()` returned False (yfinance had no bars). Additionally, signals at market open were silently dropped because yfinance had no bars at 13:30 UTC.

---

## v2 — 2026-05-22

### Changes
- **Batched Claude sentiment scoring** — all eligible articles in a news cycle are now scored in a single Claude Haiku API call instead of one call per article. Reduces Claude API cost by ~90%.
- **Pre-filtering before Claude** — articles are filtered (must have tickers, not blocklisted, not already seen in DB) before any Claude call is made.
- **HTML entity decoding** — `html.unescape()` applied to Benzinga headlines and teasers (was storing raw `&#39;`, `&amp;` etc.).
- **T212 API error surfacing** — `calculate_quantity()` now returns `(quantity, error_reason)` tuple; actual HTTP error is stored in `rejection_reason` instead of generic "Could not calculate position size".
- **London time for all timestamps** — `fetched_at`, `published_at`, `buy_time`, `sell_time`, `snapshot_at` all stored as London time (BST/GMT) with offset. Previously `fetched_at` was UTC.

### Why
Claude API was called once per article (costly). `buy_failed` for MNTS showed "Could not calculate position size" with no detail — root cause was a transient T212 API failure. HTML entities appeared raw in headlines.

---

## v1 — 2026-05-20

### Initial release

- News pipeline: Benzinga (via massive.com) → Claude Haiku sentiment → Finnhub real-time price → yfinance momentum/volume → Trading 212 buy
- Exit conditions: take-profit (+5%), stop-loss (−2%), time-stop (60 min)
- Position monitor runs every 60 seconds
- Demo mode by default; live mode behind `TRADING_MODE=live` env var
- PostgreSQL storage with `news_signals`, `trades`, `portfolio_snapshots` tables
- Grafana dashboard for live monitoring
- GitHub Actions CI/CD deploy to a Linux VM over a private network
