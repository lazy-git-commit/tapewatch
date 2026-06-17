# Database Schema

PostgreSQL database: `momentum_trader`

---

## `news_signals`

One row per news article-ticker pair that the system evaluated. An article mentioning three tickers produces three rows.

Articles pass through three pre-filters before reaching Claude or the DB:
1. **Freshness** — must be published within 60 seconds of the fetch time.
2. **Crypto filter** — tickers prefixed `X:` (e.g. `X:BTCUSD`) are stripped; not equities.
3. **Roundup filter** — articles tagging more than 3 tickers are skipped; these are market digests, not single-stock catalysts.

Only articles that pass all three filters, are classified `"positive"` by Claude, and proceed through the price check are saved here. Articles dropped by a pre-filter or classified neutral/negative are silently discarded before any DB write.

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
| `tp_order_id` | TEXT | Order ID of the resting take-profit LIMIT sell placed at buy time. Cleared (NULL) if cancelled before a stop/time-stop exit. The monitor checks this order's status each cycle to detect take-profit fills. |
| `exit_reason` | TEXT | Why the position was closed: `"take_profit"`, `"stop_loss"`, `"time_stop"`, or `"eod_flatten"` (forced close before the bell — day-trading systems never hold overnight). |
| `sell_net_gbp` | REAL | GBP amount credited back to the account from this sale, net of FX conversion and fees, as reported by Trading 212. |
| `sell_fx_rate` | REAL | USD/GBP exchange rate applied by Trading 212 at the time of the sell fill. |
| `sell_fees_gbp` | REAL | Total transaction costs charged by Trading 212 on the sell, in GBP. |

### P&L (computed on close)

| Column | Type | Description |
|---|---|---|
| `profit_loss` | REAL | Realised profit or loss in GBP. Computed as `abs(sell_net_gbp) - abs(buy_net_gbp)` when both GBP values are available, so broker wallet-impact sign conventions cannot corrupt P&L or the daily kill switch. Falls back to `(sell_price - buy_price) × quantity` in USD terms for historical rows that pre-date GBP fill capture. |
| `profit_loss_pct` | REAL | P&L as a percentage of the cost basis. Computed as `profit_loss / abs(buy_net_gbp) × 100` when GBP data is available, otherwise `(sell_price - buy_price) / buy_price × 100`. |

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
