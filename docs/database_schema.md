# Database Schema

PostgreSQL database: `momentum_trader`

---

## `news_signals`

One row per news article-ticker pair that the system evaluated. An article mentioning three tickers produces three rows.

Articles pass through pre-filters before reaching Claude or the DB:
1. **Freshness** — must be published within 60 seconds of the fetch time.
2. **Crypto filter** — tickers prefixed `X:` (e.g. `X:BTCUSD`) are stripped; not equities.
3. **Roundup filter** — articles tagging more than 3 tickers are skipped; these are market digests, not single-stock catalysts.
4. **Analyst action filter** — headlines matching price-target, upgrade/downgrade, or coverage-initiation patterns are dropped before the Claude call (added v17.4).

Articles that pass all pre-filters and are classified `"positive"` by Claude are saved here — **including ones that fail the subsequent price check** (those land with `acted_on=0` and a `rejection_code`). Neutral/negative classifications go to `sentiment_scores` only. Articles dropped by a pre-filter are never written to any table.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `article_id` | TEXT | Benzinga's own article ID. Used to deduplicate across poll cycles — if the same article appears on the next API call, it is skipped without re-analysis. |
| `ticker` | TEXT | Trading 212 instrument code (e.g. `AAPL_US_EQ`). |
| `headline` | TEXT | Article headline as returned by Benzinga. |
| `source` | TEXT | Always `"benzinga"` — the news provider. |
| `sentiment` | TEXT | Claude's classification: `"positive"`, `"neutral"`, or `"negative"`. Only `"positive"` articles proceed to price confirmation and are saved to this table. |
| `confidence` | INTEGER | Confidence score 1–10, derived from Claude Haiku's classification (raw 0.0–1.0 × 10, rounded). |
| `acted_on` | INTEGER | `0` = evaluated but no trade placed; `1` = a buy order was executed for this signal. |
| `rejection_reason` | TEXT | Human-readable explanation of why the signal was not traded (e.g. `"Insufficient recent momentum: +0.12% over last 15 min"`). NULL if the signal led to a trade. |
| `rejection_code` | TEXT | Short keyword for the rejection reason: `low_momentum`, `high_momentum`, `low_volume`, `high_volume`, `below_vwap`, `dead_cat`, `extended_move`, `wide_spread`, `no_price_data`, `buy_failed`, `illiquid`, `opening_block`, or `penny_stock`. NULL if the signal led to a trade (`acted_on = 1`). |
| `catalyst_type` | TEXT | Claude's catalyst classification (e.g. `earnings_beat`, `fda_approval`, `ma_target`). See `news/fetcher.py::CATALYST_TYPES` for the full taxonomy. |
| `catalyst_magnitude` | INTEGER | Relative-to-market-cap impact score 1–5 (5=transformative, 1=noise). Added v15.8. Used in Gate 4 (`MIN_CATALYST_MAGNITUDE`). |
| `published_at` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when Benzinga published the article. |
| `fetched_at` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when our news cycle fetched this article from the API. The gap between `published_at` and `fetched_at` shows detection latency. |
| `created_at` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when this row was inserted. |

---

## `trades`

One row per open or closed trade. A row is inserted when a buy order is placed (`status = 'open'`) and updated in-place when the position is closed (`status = 'closed'`).

### Identity

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `mode` | TEXT | `"demo"` or `"live"` — the trading mode active when the trade was placed. |
| `ticker` | TEXT | Trading 212 instrument code (e.g. `AMZN_US_EQ`). |
| `signal_id` | INTEGER FK | Foreign key to `news_signals.id` — the article that triggered this trade. |
| `status` | TEXT | `"open"` while the position is held; `"closed"` once sold. |
| `session` | TEXT | **(v21.1)** The trading session the ENTRY fired in: `premarket`, `regular`, `afterhours`. Enables per-session P&L reporting ("is after-hours worth trading?"). |

### Buy side (filled on order placement)

| Column | Type | Description |
|---|---|---|
| `quantity` | REAL | Number of shares (fractional) purchased. |
| `buy_price` | REAL | Actual fill price in USD, sourced from the Trading 212 fill data after the order is confirmed. |
| `buy_time` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when the buy order was placed. |
| `buy_order_id` | TEXT | Trading 212 order ID for the buy. |
| `buy_net_gbp` | REAL | GBP amount debited from the account for this purchase, net of FX conversion and fees, as reported by Trading 212. This is the true cash cost. |
| `buy_fx_rate` | REAL | USD/GBP exchange rate applied by Trading 212 at the time of the buy fill. |
| `buy_fees_gbp` | REAL | Total transaction costs (stamp duty, currency conversion charge, etc.) charged by Trading 212 on the buy, in GBP. |

### Sell side (filled when position is closed)

| Column | Type | Description |
|---|---|---|
| `sell_price` | REAL | Actual fill price in USD at the time of sale. |
| `sell_time` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when the sell order was placed. |
| `sell_order_id` | TEXT | Trading 212 order ID for the sell. |
| `stop_order_id` | TEXT | **(v20)** Order ID of the resting STOP-market sell placed at buy time. This is the current architecture: the loss side rests at the broker (zero latency, and — proven on 2026-07-31 — independent of whether *our* price feed is working), while take-profit and time-stop are polled. Cleared (NULL) when cancelled/replaced by the breakeven ratchet. |
| `tp_order_id` | TEXT | **(legacy, pre-v20)** Order ID of the resting take-profit LIMIT sell. The v20 inversion replaced this with `stop_order_id`; only trades opened before v20 still carry it, and they keep the old handling until they close. |
| `ratchet_armed` | INTEGER | **(v20.1)** 1 once the breakeven ratchet has fired (stop moved up to roughly break-even after +`RATCHET_TRIGGER_PCT`). Persisted on the row rather than in memory so a restart mid-position cannot re-arm and cancel a good stop. |
| `exit_reason` | TEXT | Why the position was closed: `"take_profit"`, `"stop_loss"`, `"time_stop"`, `"eod_flatten"` (forced close before the bell — day-trading systems never hold overnight), or `"afterhours_flatten"` (v21, the extended-session equivalent at 19:45 ET). |
| `sell_net_gbp` | REAL | GBP amount credited back to the account from this sale, net of FX conversion and fees, as reported by Trading 212. |
| `sell_fx_rate` | REAL | USD/GBP exchange rate applied by Trading 212 at the time of the sell fill. |
| `sell_fees_gbp` | REAL | Total transaction costs charged by Trading 212 on the sell, in GBP. |

### P&L (computed on close)

| Column | Type | Description |
|---|---|---|
| `profit_loss` | REAL | Realised profit or loss in GBP. Computed as `abs(sell_net_gbp) - abs(buy_net_gbp)` when both GBP values are available, so broker wallet-impact sign conventions cannot corrupt P&L or the daily kill switch. Falls back to `(sell_price - buy_price) × quantity` in USD terms for historical rows that pre-date GBP fill capture. |
| `profit_loss_pct` | REAL | P&L as a percentage of the cost basis. Computed as `profit_loss / abs(buy_net_gbp) × 100` when GBP data is available, otherwise `(sell_price - buy_price) / buy_price × 100`. |

### Excursion / MFE-MAE (v21.10)

**Maximum Favourable Excursion** and **Maximum Adverse Excursion** — in plain
terms, *how far up did this position go before I sold, and how far down did it
dip*. Both are percentages relative to `buy_price`.

**Pure observability: no exit decision reads these columns.** They exist to
make the trailing-stop-versus-flat-time-stop question answerable from our own
record, instead of from a simulation capped by yfinance's ~30-day 1-minute bar
retention (that simulation gave +0.51%/trade with a 95% confidence interval of
[−0.38, +1.30] over 8 trades — too wide to act on).

Written by `monitor/position_monitor.py::_record_excursion` via
`update_trade_excursion()`, which widens the band in SQL with
`GREATEST`/`LEAST`, so it stays correct across restarts and out-of-order
writes. An in-process cache means a DB write happens only on a genuinely new
extreme, not on every 5-second poll.

| Column | Type | Description |
|---|---|---|
| `max_favorable_pct` | REAL | Best unrealised gain the position reached while open, in % from `buy_price`. NULL for trades that closed before v21.10 (2026-07-31). |
| `max_adverse_pct` | REAL | Worst unrealised loss the position reached while open, in % from `buy_price`. |

**v21.11 correction — read older rows with care.** Until v21.11 these columns
recorded only prices the *polling loop* observed, and a broker-side resting
stop fills without the poller involved. NVT (trade 24, 2026-07-31) closed at
−2.56% while carrying `max_adverse_pct = +0.75%` — an impossible row, because
the last polled quote was frozen and the real −2.29% fill was never fed in.
v21.11 folds the realised exit price into the band on every close path
(`_record_exit_excursion`), so from that point MAE is bounded by the actual
outcome. Any row where `max_adverse_pct > profit_loss_pct` predates the fix.

### `signal_price` (v21.16) — what the entry would have cost without the wait

| Column | Type | Description |
|---|---|---|
| `signal_price` | REAL | Price observed at the **first** evaluation of the signal that produced this trade. NULL for every pre-v21.16 row, and for any entry whose first look produced no usable quote. |

`buy_price − signal_price` is the cost of waiting for momentum confirmation.
Before this column that cost was only estimable from `sentiment_scores` forward
returns, which sample at 5/15/60/120 min and therefore cannot see the path in
between — and that sampling gap is precisely why the buy-at-signal simulation
reports a 33% stop-out rate against a live 48%. One real row settles per trade
what four samples can only approximate.

Recorded at the signal's first evaluation and carried through the re-eval queue,
**not** read from `confirmation.current_price` at entry time — the latter would
report a wait cost of exactly zero on every trade. Values that are non-finite or
≤ 0 store as NULL rather than 0.0: the graduated pre-market hand-off constructs
a synthetic `PriceConfirmation` with `current_price=0.0`, and a stored 0.0 makes
every downstream wait-cost query divide by zero.

Pure observability — no entry, exit or sizing decision reads it.

### Entry provenance (v21.16) — HOW was this trade made?

| Column | Type | Description |
|---|---|---|
| `entry_reason` | TEXT | The decision path that authorised the entry: `momentum_confirmed` (the momentum floor passed on its own merits), `momentum_skipped` (the floor **would have rejected** and `SKIP_MOMENTUM_CATALYSTS` let it through), `premarket_gap` (the gap-and-go path). NULL pre-v21.16. |
| `entry_momentum_pct` | REAL | The actual momentum reading the decision was made on. A `momentum_skipped` row is below `MIN_PRICE_MOVE_PCT` by definition. |
| `entry_delay_seconds` | INTEGER | Seconds from the signal's first evaluation to the fill. `0` = confirmed on first look; a large value is the re-eval queue. |

`signal_price` records what waiting **cost**; these record **which path produced
the trade**, and those are different questions. A trade that confirmed on its
first look because momentum was already present and one that confirmed because
the floor was skipped both show a wait cost of ~zero — so price alone cannot
separate the v21.16 treatment group from the control. Without these columns the
change is unfalsifiable after the fact.

`entry_momentum_pct` exists so the outcome can be **regressed against the
reading** rather than only bucketed by it — the open question is where between
"flat" and "clearly moving" the edge actually lives, and a boolean cannot answer
that. `premarket_gap` is labelled separately on purpose: the overnight move
already happened, which is what put the candidate on the watchlist, so pooling
those rows into either momentum bucket would corrupt the comparison.

Read them on Grafana panels *"Entry Path: did entering at the signal work?"*
(25) and *"Entry Path per trade"* (26). Pure observability throughout — no
entry, exit or sizing decision reads any of them.

---

## `portfolio_snapshots`

Periodic snapshots of account value, used to plot the portfolio value chart in Grafana.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `total_value` | REAL | Total account value in GBP at snapshot time (cash + open position market value). |
| `cash` | REAL | Free cash available in GBP at snapshot time. |
| `snapshot_at` | TEXT | ISO 8601 timestamp (London time, BST/GMT) of when the snapshot was taken. |

---

## `sentiment_scores` (v14)

**The eval-loop dataset.** One row per article+ticker for EVERY Claude
classification — positive, neutral, and negative — written at scoring time by
`news/fetcher.py`. The nightly job (`analysis/forward_returns.py`, 22:30 UTC)
fills in what the market actually did afterwards, making classifier precision
and prompt changes measurable (see `docs/algorithm.md` §9 for the queries).

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `article_id` | TEXT | Benzinga article ID. |
| `ticker` | TEXT | Trading 212 instrument code. |
| `headline` | TEXT | Article headline. |
| `sentiment` | TEXT | `"positive"`, `"neutral"`, or `"negative"`. |
| `confidence` | REAL | Claude's confidence, 0.0–1.0. |
| `catalyst_type` | TEXT | One of the 14 catalyst classes (`news/fetcher.py::CATALYST_TYPES`). |
| `already_moved` | INTEGER | 1 if Claude judged the price move happened before publication (halt/recap articles). |
| `published_at` | TEXT | Article publication timestamp. |
| `scored_at` | TEXT | When Claude scored it (London time). |
| `fwd_return_5m` | REAL | % price change in the 5 minutes after publication. NULL until computed; stays NULL if price data unavailable. |
| `fwd_return_15m` | REAL | % price change in the 15 minutes after publication. |
| `fwd_return_60m` | REAL | % price change in the 60 minutes after publication. |
| `fwd_return_120m` | REAL | **(v21.3)** % price change 120 minutes after publication. Added because the 60-minute panel showed the tradeable-catalyst edge still *climbing* at 60 min — i.e. the old 60-minute time-stop was clipping the move mid-catalyst. This column is what sized the hold at 120 min (v21.8). |
| `fwd_return_eod` | REAL | **(v21.3)** % price change from publication to that session's close — the upper bound on what a same-day hold could have captured. |
| `catalyst_magnitude` | INTEGER | Claude's 1–5 size rating for the catalyst. Gated by `MIN_CATALYST_MAGNITUDE`. |
| `returns_computed_at` | TEXT | When the nightly job processed this row. NULL = pending. |

---

## `premarket_candidates` (v14)

The pre-market watchlist (`premarket/scanner.py`). News scored 08:00–09:30 ET
lands here; at the open each pending candidate is evaluated with the gap gate
plus full price confirmation. No orders are ever placed pre-market.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `article_id` | TEXT | Benzinga article ID. |
| `ticker` | TEXT | Trading 212 instrument code. |
| `headline` | TEXT | Article headline. |
| `catalyst_type` | TEXT | Claude's catalyst classification. |
| `confidence` | REAL | Claude's confidence, 0.0–1.0. |
| `published_at` | TEXT | Article publication timestamp. |
| `created_at` | TEXT | When the candidate was added (London time). |
| `status` | TEXT | `pending` → `traded` \| `rejected` \| `expired`. Candidates expire 30 min after the open or at end of day. |
| `eval_note` | TEXT | Why the candidate was rejected/expired (e.g. `"gap +0.4% < 1% — market doesn't believe the catalyst"`). |

---

## `heartbeat` (v14)

Job liveness, one row per scheduler job, upserted every cycle. Exists because
the 2026-06-11 crash loop ran 18 hours unnoticed. Grafana alerts when
`last_beat_at` goes stale:

```sql
SELECT EXTRACT(EPOCH FROM (NOW() - last_beat_at::timestamptz)) / 60 AS minutes_stale
FROM heartbeat WHERE job = 'news_cycle';
-- Alert when minutes_stale > 10
```

| Column | Type | Description |
|---|---|---|
| `job` | TEXT PK | Job name: `news_cycle`, `monitor`, `premarket_scan`, `forward_returns`. |
| `last_beat_at` | TEXT | ISO 8601 timestamp (London time) of the job's last successful start. |

---

## `classifier_calls` (v21.14)

One row per classifier API call, for **both** providers. This is the latency and
liveness dataset behind shadow mode.

**Failed calls are recorded as faithfully as successful ones** — the `ok = false`
rows *are* the outage record. The 2026-08-04 and 08-06 Claude blind spots (25 and
58 consecutive empty-classification cycles) left no trace on any monitoring
surface except journal lines; this table is what makes the next one queryable.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | |
| `provider` | TEXT | `claude` or `qwen`. |
| `model` | TEXT | Exact model id used. |
| `called_at` | TEXT | ISO 8601 (London). |
| `batch_size` | INTEGER | Articles sent. |
| `latency_ms` | INTEGER | Wall-clock round trip. **NULL when no HTTP call was made** (a suppressed or dropped batch) so those rows can't drag the percentiles. |
| `ok` | BOOLEAN | False for exceptions, missing tool call, bad JSON, empty batch, a suppressed cycle, or a dropped batch. |
| `scored_count` | INTEGER | Records the model returned, counted **pre-validation on both providers** (v21.14.1) — a batch of well-formed answers that all miss the taxonomy is a scored batch, not an outage, and must not look like a dead API. |
| `error_type` | TEXT | `empty_batch`, `no_tool_call`, `bad_json`, `truncated`, `no_tool_use`, `bad_shape`, `cooldown_suppressed`, `401_auth`, `403_*`, `429_rate_limit`, `api_status_*`, `dropped_backlog`, `client_unavailable`, or an exception class name. |
| `error_detail` | TEXT | First 500 chars. |
| `tokens_in` / `tokens_out` / `tokens_cached` | INTEGER | Cost + cache-hit tracking. |

```sql
-- Latency and liveness by provider. p95 matters more than the mean (the news
-- cycle is 60s and the mean hides the tail that would blow it).
SELECT provider,
       count(*)                                        AS calls,
       avg(ok::int)                                    AS success_rate,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM classifier_calls
WHERE called_at >= to_char(now() - interval '30 days', 'YYYY-MM-DD"T"HH24:MI:SS')
GROUP BY provider;
```

⚠️ **Judge liveness on the longest consecutive failure streak, not the success
rate.** 58 dead cycles in one unbroken run is a 98-minute blind spot but barely
moves a monthly average. `analysis/classifier_compare.py` reports both.

⚠️ **A missing row is not a passing row.** v21.14.1 closed three holes where a
provider's degraded periods were excluded from its own denominator, which is the
one failure mode that silently inverts the comparison:

| Hole | Was | Now |
|---|---|---|
| Claude in cooldown | no row at all | `cooldown_suppressed`, `latency_ms` NULL |
| Claude 401/403/429/5xx | no row at all (all five handlers returned early) | one row per failure |
| Qwen batch dropped for backlog | no row at all | `dropped_backlog`, written by the next background job |

Before this, a **total Claude outage rendered as `success_rate = 100%`,
`worst_failure_streak = 0`** — the rows describing the outage simply did not
exist, so the fallback decision would have been made on a dataset with Claude's
failures deleted. Treat any pre-v21.14.1 rows accordingly.

Note the unit difference when reading streaks: Claude retries an empty batch
within one cycle and each attempt is a real API call, so it writes up to
`_EMPTY_BATCH_ATTEMPTS` rows per cycle where Qwen writes one. A Claude streak of
116 and a Qwen streak of 58 can describe the same 58 dead cycles.

---

## `qwen_scores` (v21.14)

Shadow-mode classifications from Qwen-Flash. **Never consulted by any trading
decision** — Claude's `sentiment_scores` row is the only one that can open a
position.

`UNIQUE(article_id)` with `ON CONFLICT DO NOTHING`, so a retried or overlapping
batch cannot double-count and skew agreement statistics against Claude.

**Deliberately carries no forward-return columns.** A forward return is a
property of the ticker and timestamp, not of the model that classified the
article, so comparisons join `sentiment_scores` on `article_id` and reuse the
values already computed there. Two copies would drift.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | |
| `article_id` | TEXT UNIQUE | Join key to `sentiment_scores`. |
| `ticker` / `headline` | TEXT | Denormalised for standalone inspection. |
| `sentiment` | TEXT | `positive` / `neutral` / `negative`. |
| `confidence` | REAL | 0.0–1.0. |
| `catalyst_type` | TEXT | One of the 14 classes; invalid values are rejected, never clamped. |
| `already_moved` | INTEGER | 0/1. |
| `catalyst_magnitude` | INTEGER | 1–5. A record whose magnitude is out of range is rejected outright (v21.14.1 — matching the live path, which rejects the whole record rather than NULLing the field). |
| `model` | TEXT | e.g. `qwen-flash`. |
| `scored_at` | TEXT | ISO 8601 (London). |

**Prefer `python -m analysis.classifier_compare` over hand-written SQL here.**
Two things make the obvious query wrong, and both are easy to miss:

1. **`sentiment_scores` is one row per (article, TICKER)**; `qwen_scores` is one
   row per ARTICLE. A plain join fans out, so a 3-ticker article triple-weights
   its single classification in every mean. The tool uses
   `DISTINCT ON (s.article_id)`.
2. **Which ticker leg survives matters.** `forward_returns.py` measures per
   (article, ticker) row and yfinance often has no bars for one leg, so an
   arbitrary pick can keep an all-NULL leg and silence an article that *did*
   have a measured outcome. The tool orders by `(s.fwd_return_60m IS NULL)`
   first.

```sql
-- The deciding metric: forward returns of each model's OWN tradeable set.
-- Agreement is necessary but NOT sufficient — a model can agree 90% of the time
-- and still differ on exactly the calls TRADEABLE_CATALYSTS acts on.
--
-- NOTE: this illustrates the shape only. It omits the confidence and magnitude
-- gates, and `round(confidence * 10) >= MIN_SENTIMENT_CONFIDENCE` is NOT the
-- same as `confidence >= 0.7` — they disagree across the whole [0.65, 0.70)
-- band, which production trades. classifier_compare._would_trade() applies all
-- four live gates.
WITH one_row_per_article AS (
    SELECT DISTINCT ON (s.article_id)
           s.article_id, s.fwd_return_60m,
           s.catalyst_type AS c_cat, s.sentiment AS c_sent, s.already_moved AS c_moved,
           q.catalyst_type AS q_cat, q.sentiment AS q_sent, q.already_moved AS q_moved
    FROM sentiment_scores s
    JOIN qwen_scores q ON q.article_id = s.article_id
    ORDER BY s.article_id, (s.fwd_return_60m IS NULL), s.id
)
SELECT 'claude' AS model, count(*) AS n, avg(fwd_return_60m) AS mean_60m
FROM one_row_per_article
WHERE c_cat IN ('fda_approval','guidance_raise')
  AND c_sent = 'positive' AND c_moved = 0
UNION ALL
SELECT 'qwen', count(*), avg(fwd_return_60m)
FROM one_row_per_article
WHERE q_cat IN ('fda_approval','guidance_raise')
  AND q_sent = 'positive' AND q_moved = 0;
```

---

## `system_events` (v17)

Degradation and outage markers. The heartbeat catches a *dead* process, but the
2026-06-11–23 nine-session drought had green heartbeats the whole time — the
process was alive and silently degraded. This table records the events that make
a degraded-but-up system visible.

One row per `(event_type, event_day)`, de-duped atomically by a UNIQUE index +
`ON CONFLICT DO NOTHING` (safe under the 8-worker pre-market thread pool).

Written by `storage/database.py::record_system_event()`, which derives severity
automatically from `event_type`.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Auto-incrementing primary key. |
| `event_type` | TEXT | Machine-readable event key. Critical: `twelvedata_credits_exhausted`, `claude_billing_error`, `claude_auth_error`, `zero_trade_session`, `claude_truncated_batch` (v21.15 — our `max_tokens` cut the answer off; same operational state as a billing error and cannot self-heal without a code change). Warning: `claude_outage`, `claude_empty_batch` (v21.12), `twelvedata_prepost_unavailable` (v21.6), `finnhub_outage` (v21.10), `stale_quote_feed` (v21.10 — a provider serving a run of frozen quotes; fired for real on its first live day, 2026-07-31, four minutes before the NVT entry), `exit_stuck` (v19.2), `entry_slippage_high` (v21.13), `claude_cache_ineffective` (v21.16 — 25 consecutive Claude calls with zero cached tokens, i.e. `cache_control` is being silently ignored because the cached prefix fell below the model's 4096-token minimum. **Warning by design**: it is a cost regression, not a trading outage — nothing stops being scored and no trade is affected — so it must not page a human or dilute the critical set. Read it on the *Classifier Prompt Cache* panel). ⚠️ Severity comes from `_CRITICAL_EVENT_TYPES` in `storage/database.py`, and the documented Grafana alert queries `severity = 'critical'` — an event type omitted from that set is recorded but **never alerts**. |
| `severity` | TEXT | `"critical"` or `"warning"`. |
| `detail` | TEXT | Human-readable context (e.g. credits used at exhaustion, drought session count). |
| `created_at` | TIMESTAMPTZ | When the event was first recorded. |
| `event_day` | DATE | Calendar date the event belongs to (used in the UNIQUE constraint with `event_type`). |

Grafana alert query (fires on any critical event today):

```sql
SELECT event_type, detail, created_at FROM system_events
WHERE severity = 'critical'
  AND (created_at::timestamptz AT TIME ZONE 'Europe/London')::date =
      (NOW() AT TIME ZONE 'Europe/London')::date;
```
