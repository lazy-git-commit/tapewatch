# Changelog

All algorithm changes are recorded here. Each version notes what changed,
why it was changed, and the date it was deployed to production.

Format: `## v<N> — YYYY-MM-DD`

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
