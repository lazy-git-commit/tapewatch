# Changelog

All algorithm changes are recorded here. Each version notes what changed,
why it was changed, and the date it was deployed to production.

Format: `## v<N> — YYYY-MM-DD`

---

## v17.5 — 2026-06-30 (premarket eval window 30→45 min)

### Premarket eval window extended (`premarket/scanner.py`)
`_EVAL_WINDOW_MINUTES` raised from 30 to 45. Empirical finding from 2026-06-30
post-mortem: 12 of 33 premarket candidates expired as "eval window closed" despite
having Twelvedata coverage. Root cause: candidates added between 09:20–09:29 ET
clear the opening block at 09:35 but then compete with RTH news_cycle for 55
Twelvedata tokens/minute, leaving only 25 minutes of actual evaluation time. The
45-minute window gives those candidates until 10:15 ET, comfortably covering the
gap-and-go momentum edge without reaching the midday regime change.

### Algorithm docs: `partnership` empirically ruled out as TRADEABLE_CATALYST
60-day forward-return analysis (233 positive partnership signals, `already_moved=0`):
avg_5m = +0.010%, median = 0.000%, only 3 of 233 moved >1% in 5 minutes. The
catalyst class correctly remains excluded; it was initially considered as a candidate
expansion but the data confirms the current exclusion is correct.

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
