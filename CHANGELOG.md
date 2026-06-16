# Changelog

All algorithm changes are recorded here. Each version notes what changed,
why it was changed, and the date it was deployed to production.

Format: `## v<N> — YYYY-MM-DD`

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
