# Changelog

All algorithm changes are recorded here. Each version notes what changed,
why it was changed, and the date it was deployed to production.

Format: `## v<N> — YYYY-MM-DD`

---

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
