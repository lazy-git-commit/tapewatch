# Database Schema

PostgreSQL database: `momentum_trader`

---

## `news_signals`

One row per news article-ticker pair that the system evaluated. An article mentioning three tickers produces three rows.

Only articles classified as `"positive"` by Claude AND published within 60 seconds of the fetch time are saved here. Articles that fail the freshness filter or are classified neutral/negative are silently dropped before any DB write.

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
| `rejection_code` | TEXT | Short keyword for the rejection reason: `low_momentum`, `low_volume`, `dead_cat`, `no_price_data`, or `buy_failed`. NULL if the signal led to a trade (`acted_on = 1`). |
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
| `exit_reason` | TEXT | Why the position was closed: `"take_profit"`, `"stop_loss"`, or `"time_stop"`. |
| `sell_net_gbp` | REAL | GBP amount credited back to the account from this sale, net of FX conversion and fees, as reported by Trading 212. |
| `sell_fx_rate` | REAL | USD/GBP exchange rate applied by Trading 212 at the time of the sell fill. |
| `sell_fees_gbp` | REAL | Total transaction costs charged by Trading 212 on the sell, in GBP. |

### P&L (computed on close)

| Column | Type | Description |
|---|---|---|
| `profit_loss` | REAL | Realised profit or loss in GBP. Computed as `sell_net_gbp - buy_net_gbp` when both GBP values are available (accurate). Falls back to `(sell_price - buy_price) × quantity` in USD terms for historical rows that pre-date GBP fill capture. |
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
