# API Reference

This document covers every external API used by the momentum trader, including endpoints, purpose, which files use them, and sample requests/responses.

---

## 1. Benzinga News API (via massive.com)

**Base URL:** `https://api.massive.com/benzinga/v2/news`
**Auth:** `Authorization: Bearer <MASSIVE_BENZINGA_API_KEY>` header
**Format:** JSON
**Used by:** `news/fetcher.py`

### Purpose

Fetches real-time US equity news articles. Benzinga tags each article with the stock tickers it mentions — no keyword matching is needed. Sentiment is **not** included; Claude Haiku classifies it separately.

---

### Endpoint: GET `/benzinga/v2/news`

**Request:**
```
GET https://api.massive.com/benzinga/v2/news
  ?published.gte=2026-05-21T19:55:00Z
  &limit=100
  &sort=published.desc
Authorization: Bearer <MASSIVE_BENZINGA_API_KEY>
```

**Query parameters:**

| Parameter | Description |
|---|---|
| `published.gte` | ISO 8601 UTC — fetch articles published after this time |
| `limit` | Max articles to return (up to 100) |
| `sort` | Sort order — `published.desc` for newest first |

**Sample response (JSON):**
```json
{
  "status": "OK",
  "request_id": "6c717517b18d4866a455669b56280c9b",
  "results": [
    {
      "benzinga_id": 52736856,
      "title": "Apple Reports Record iPhone Sales",
      "teaser": "Apple shares rose after the company reported...",
      "body": "Full article text...",
      "author": "John Smith",
      "published": "2026-05-21T19:58:00Z",
      "last_updated": "2026-05-21T19:58:30Z",
      "tickers": ["AAPL", "MSFT"],
      "channels": ["Tech", "Earnings"],
      "tags": ["earnings", "revenue"],
      "url": "https://www.benzinga.com/..."
    }
  ],
  "next_url": "..."
}
```

**Key fields used:**

| Field | Used for |
|---|---|
| `benzinga_id` | Deduplication — skip if already seen in `news_signals.article_id` |
| `title` | Headline passed to Claude for sentiment classification |
| `teaser` | Summary passed to Claude for sentiment classification |
| `tickers` | List of US stock symbols — each becomes a `NewsItem` |
| `published` | Stored as `published_at` in `news_signals` |

---

## 2. Anthropic API (Claude Haiku)

**Package:** `anthropic` (Python SDK)
**Model:** `claude-haiku-4-5-20251001`
**Auth:** `ANTHROPIC_API_KEY` environment variable (picked up automatically by the SDK)
**Used by:** `news/fetcher.py`

### Purpose

Acts as an expert day trader to identify news that will cause a stock to move up sharply within the next 5–15 minutes. Classifies each article as `positive`, `neutral`, or `negative`. Only `positive` articles proceed to price confirmation.

The prompt is domain-specific — it explicitly teaches Claude which news types drive intraday momentum (earnings beats, FDA approvals, M&A, contract wins, guidance raises) and which to ignore regardless of tone (analyst price target raises, "Maintains" ratings, sector commentary, conference attendance, LOI/MOU non-binding agreements, recap articles, large-cap routine coverage).

---

### API call

All eligible articles in a single news cycle are scored in **one batched call** — not one call per article. Articles are pre-filtered (must have tickers, not blocklisted, not already seen in DB, published within the last 60 seconds) before the call is made. `max_tokens` scales dynamically with batch size (~40 tokens per article).

**Code:**
```python
client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=max(512, len(articles) * 40 + 64),
    messages=[{"role": "user", "content": prompt}],
)
```

**Prompt structure:**
```
You are an expert day trader specialising in US equity momentum trading.
Your job is to identify news that will cause a stock to move UP sharply
within the next 15 minutes of market trading.

POSITIVE (high confidence 0.8–1.0) — genuine catalysts that move stocks NOW:
- Earnings beats: revenue or EPS above analyst estimates
- FDA approvals, drug trial success, regulatory green lights
- M&A: acquisition announcements, buyout offers, merger deals
- Major contract wins with concrete dollar values
- Guidance raises: company raises full-year revenue or earnings outlook
- Short squeeze signals: stock halted to the upside, unusual volume surge
- Surprise CEO/product announcements with material business impact

NEUTRAL (do not mark positive) — these almost never move a stock in 15 min:
- Analyst price target raises or reiterations with no new information
- 'Maintains Buy/Overweight' — the analyst already had this rating
- General market or sector commentary
- Conference attendance, awards, ESG reports
- Articles that summarise news published days ago

Articles:
[{"id": "...", "headline": "...", "teaser": "..."}, ...]
```

**Sample response:**
```json
[
  {"id": "52736856", "sentiment": "positive", "confidence": 0.92},
  {"id": "52736901", "sentiment": "neutral",  "confidence": 0.10}
]
```

**Output mapping:**

| Field | Description |
|---|---|
| `sentiment` | `"positive"` → proceeds to price check; others are dropped |
| `confidence` | 0.0–1.0, multiplied by 10 and rounded to store as `news_signals.confidence` (1–10) |

---

## 3. Finnhub REST API

**Base URL:** `https://finnhub.io/api/v1`
**Auth:** `token=<FINNHUBIO_API_KEY>` query parameter
**Used by:** `market/price_check.py`, `market/finnhub_bars.py`

### Purpose

Provides two things:
- **Market status** — fallback NYSE open/closed check. The primary check is `pandas_market_calendars` (local, no network). Finnhub is only called if the calendar check raises an exception.
- **Real-time stock quotes** — used for signal confirmation and position monitoring.

**Rate limit:** 60 requests/minute on the free tier.

---

### Endpoint: GET `/stock/market-status`

**Purpose:** Fallback market open check. `pandas_market_calendars` is tried first; this endpoint is only reached if the calendar library raises an exception. Correctly handles NYSE holidays and early closes.

**Request:**
```
GET https://finnhub.io/api/v1/stock/market-status?exchange=US&token=<FINNHUBIO_API_KEY>
```

**Sample response (market open):**
```json
{"exchange": "US", "holiday": null, "isOpen": true, "session": "market", "t": 1716321600, "timezone": "America/New_York"}
```

**Sample response (market closed / holiday):**
```json
{"exchange": "US", "holiday": "Memorial Day", "isOpen": false, "session": null, "t": 1716321600, "timezone": "America/New_York"}
```

| Field | Description |
|---|---|
| `isOpen` | `true` when the market is in regular trading hours |
| `holiday` | Holiday name if closed for a holiday, otherwise `null` |

---

### Endpoint: GET `/quote`

**Purpose:** Real-time current price. Used for signal confirmation (current price vs. yfinance baseline) and position monitoring.

**Request:**
```
GET https://finnhub.io/api/v1/quote?symbol=AAPL&token=<FINNHUBIO_API_KEY>
```

**Sample response (JSON):**
```json
{
  "c": 192.53,
  "d": 1.23,
  "dp": 0.64,
  "h": 193.10,
  "l": 190.85,
  "o": 191.30,
  "pc": 191.30,
  "t": 1716321600
}
```

**Key fields used:**

| Field | Description |
|---|---|
| `c` | Current price (real-time) — used as `current_price` |
| `o` | Today's open price — used for `day_move_pct` and as fallback baseline |

---

## 4. Twelvedata API

**Base URL:** `https://api.twelvedata.com`
**Auth:** `apikey=<TWELVEDATA_API_KEY>` query parameter
**Plan:** Basic (800 credits/day — 1 credit per symbol per call)
**Used by:** `market/twelvedata_bars.py`

### Purpose

Provides near-real-time 1-minute intraday bars for the momentum baseline, and 1-day bars for volume statistics. Replaces yfinance, which served stale bars on high-volume days (VECO root cause: bar from 09:56 returned at 11:42, giving a false +1.20% momentum signal).

**Credit budget:** At ~50 price checks/day (2 calls per check: 1-min + 1-day), usage is ~100 credits/day — well within the 800/day Basic limit.

---

### Endpoint: GET `/time_series` (1-min bars — momentum baseline)

**Purpose:** Fetch the last 10 one-minute bars. `values[0]` is the most recent completed bar; `values[5]` is ~5 minutes ago and is used as the momentum baseline.

**Staleness guard:** If `values[0].datetime` is more than 10 minutes old, the data is rejected entirely. This prevents the VECO failure mode where stale bars caused a false positive signal.

**Request:**
```
GET https://api.twelvedata.com/time_series
  ?symbol=AAPL
  &interval=1min
  &outputsize=10
  &apikey=<TWELVEDATA_API_KEY>
```

**Sample response (JSON):**
```json
{
  "meta": {"symbol": "AAPL", "interval": "1min", "currency": "USD"},
  "values": [
    {"datetime": "2026-06-11 15:45:00", "open": "192.10", "high": "192.50", "low": "192.00", "close": "192.40", "volume": "125430"},
    {"datetime": "2026-06-11 15:44:00", "open": "191.90", "high": "192.15", "low": "191.85", "close": "192.10", "volume": "98210"},
    ...
  ],
  "status": "ok"
}
```

| Field | Used for |
|---|---|
| `values[0].close` | `current_bar_price` — most recent completed 1-min close |
| `values[5].close` | `past_price` — ~5 minute momentum baseline |
| `values[0].datetime` | Staleness check — reject if >10 min old |

---

### Endpoint: GET `/time_series` (1-day bars — volume stats)

**Purpose:** Fetch 21 daily bars. `values[0]` is today's partial bar; `values[1..20]` are the prior 20 complete trading days used to compute the 20-day average daily volume.

**Request:**
```
GET https://api.twelvedata.com/time_series
  ?symbol=AAPL
  &interval=1day
  &outputsize=21
  &apikey=<TWELVEDATA_API_KEY>
```

**Key fields used:**

| Field | Used for |
|---|---|
| `values[0].volume` | Today's intraday volume (partial — underestimates early in session) |
| `values[0].close` | Today's close for daily dollar volume calculation |
| `values[1..20]` average volume | 20-day ADV for volume ratio |

**Daily dollar volume** = `today_close × today_volume`. Used for the illiquidity filter: stocks with DDV < `MIN_DAILY_DOLLAR_VOLUME` ($1M default) are rejected. This guards against GOAI-style catastrophic slippage on market sell orders through thin order books.

---

### Retry policy

Both calls use the same retry logic in `market/twelvedata_bars.py`:
- 3 attempts
- Exponential backoff: 1.5s, 3s, 6s
- Retries on `Timeout`, `ConnectionError`, HTTP 5xx, and HTTP 429 (rate limit)
- Does NOT retry HTTP 4xx (bad symbol, auth failure — won't self-heal)

---

## 5. Trading 212 API

**Base URLs:**
- Demo: `https://demo.trading212.com/api/v0`
- Live: `https://live.trading212.com/api/v0`

**Auth:** HTTP Basic Authentication with Base64-encoded credentials.

```python
import base64
credentials = f"{API_KEY_ID}:{API_KEY}"
encoded = base64.b64encode(credentials.encode()).decode()
headers = {"Authorization": f"Basic {encoded}"}
```

**Used by:** `trading/executor.py`

The correct base URL and credentials are selected automatically based on `TRADING_MODE` in `.env`:
- `TRADING_MODE=demo` → `demo.trading212.com` + `TRADING212_DEMO_API_KEY_ID` / `TRADING212_DEMO_API_KEY`
- `TRADING_MODE=live` → `live.trading212.com` + `TRADING212_API_KEY_ID` / `TRADING212_API_KEY`

---

### Endpoint: GET `/equity/metadata/instruments`

**Purpose:** Fetch T212's full instrument catalogue at startup to build a `shortName → ticker` lookup map. Required because T212 keeps the original SPAC/IPO ticker code even after a company changes its exchange symbol (e.g. after a reverse merger, `SUNE` on the exchange lives as `JCS_US_EQ` in T212). Without this map, ~16% of small-cap US tickers 404 when the system appends `_US_EQ` to the Benzinga symbol.

Called once at startup by `build_symbol_map()` in `trading/executor.py`. The result is stored in the module-level `_symbol_to_t212` dict; `resolve_t212_ticker(symbol)` uses it for all subsequent symbol lookups.

**Request:**
```
GET https://demo.trading212.com/api/v0/equity/metadata/instruments
Authorization: Basic <base64(KEY_ID:KEY)>
```

**Sample response (JSON array):**
```json
[
  {
    "addedOn": "2020-01-02T08:00:00.000+02:00",
    "currencyCode": "USD",
    "isin": "US4592001014",
    "maxOpenQuantity": 10000.0,
    "minTradeQuantity": 0.1,
    "name": "International Business Machines Corp",
    "shortName": "IBM",
    "ticker": "IBM_US_EQ",
    "type": "STOCK"
  },
  {
    "addedOn": "2021-11-15T08:00:00.000+02:00",
    "currencyCode": "USD",
    "isin": "US47759T1007",
    "maxOpenQuantity": 10000.0,
    "minTradeQuantity": 1.0,
    "name": "JCS Enterprises Inc (formerly SUNation Energy)",
    "shortName": "JCS",
    "ticker": "JCS_US_EQ",
    "type": "STOCK"
  }
]
```

**Key fields used:**

| Field | Description |
|---|---|
| `shortName` | Current exchange symbol (e.g. `"JCS"`) — used as map key |
| `ticker` | T212 internal instrument code (e.g. `"JCS_US_EQ"`) — used as map value |
| `currencyCode` | Filtered to `"USD"` only — ignores non-US instruments |

**Fallback:** If the endpoint is unavailable at startup (network error, rate limit), `build_symbol_map()` logs a warning and `resolve_t212_ticker()` falls back to `<symbol>_US_EQ` for all tickers.

---

### Endpoint: GET `/equity/account/cash`

**Purpose:** Fetch account total value and available cash for position sizing (live mode only; demo mode uses `DEMO_PORTFOLIO_VALUE` from `.env`).

**Request:**
```
GET https://live.trading212.com/api/v0/equity/account/cash
Authorization: Basic <base64(KEY_ID:KEY)>
```

**Sample response (JSON):**
```json
{
  "blocked": 0.0,
  "free": 482.50,
  "invested": 17.50,
  "pieCash": 0.0,
  "ppl": 0.35,
  "result": 0.35,
  "total": 500.00
}
```

| Field | Description |
|---|---|
| `free` | Cash available to invest |
| `invested` | Amount currently in open positions |
| `total` | Total account value (`free` + `invested`) |

---

### Endpoint: POST `/equity/orders/market`

**Purpose:** Place a market order. Positive quantity = buy; negative quantity = sell.

**Buy request:**
```
POST https://demo.trading212.com/api/v0/equity/orders/market
Authorization: Basic <base64(KEY_ID:KEY)>
Content-Type: application/json

{
  "ticker": "AAPL_US_EQ",
  "quantity": 0.0873
}
```

**Sell request** (negative quantity):
```json
{
  "ticker": "AAPL_US_EQ",
  "quantity": -0.0873
}
```

**Sample response (JSON):**
```json
{
  "id": 1234567890,
  "type": "MARKET",
  "ticker": "AAPL_US_EQ",
  "quantity": 0.0873,
  "filledQuantity": 0.0873,
  "filledPrice": 292.15,
  "status": "FILLED",
  "dateCreated": "2026-05-21T19:22:10.000Z",
  "dateModified": "2026-05-21T19:22:10.500Z"
}
```

**Error response (quantity precision mismatch):**
```json
{
  "type": "/api-errors/quantity-precision-mismatch",
  "title": "Error while placing the order",
  "status": 400,
  "detail": "invalid quantity precision 2"
}
```

The `detail` field contains the maximum allowed decimal places for that instrument. `trading/executor.py` parses this value from the error and retries the order once with the quantity rounded to the allowed precision. For example, `164.9305` → `164.93` when precision 2 is required.

Other notable error types:

| Error type | HTTP | Meaning |
|---|---|---|
| `/api-errors/quantity-precision-mismatch` | 400 | Quantity has more decimal places than the instrument allows — auto-retried once |
| `/api-errors/instrument-disabled` | 400 | Instrument is suspended or not tradeable — order fails permanently |
| `/api-errors/entity-not-found` | 404 | Ticker not in T212 universe — order fails permanently |
| `TooManyRequests` | 429 | Rate limit hit — order fails; next cycle will retry the full pipeline |

---

## Summary Table

| API | Endpoint / Call | Purpose | File |
|---|---|---|---|
| Benzinga (massive.com) | `GET /benzinga/v2/news` | Fetch recent US equity news | `news/fetcher.py` |
| Anthropic (Claude Haiku) | `messages.create` (batched) | Classify article sentiment | `news/fetcher.py` |
| Finnhub | `GET /stock/market-status` | Fallback NYSE open/closed check (primary is `pandas_market_calendars`) | `market/price_check.py` |
| Finnhub | `GET /quote` | Real-time current price (retried, exp backoff) | `market/finnhub_bars.py` |
| Twelvedata | `GET /time_series?interval=1min` | 1-min momentum baseline (~5 min ago, staleness guard) | `market/twelvedata_bars.py` |
| Twelvedata | `GET /time_series?interval=1day` | 20-day ADV + daily dollar volume (liquidity filter) | `market/twelvedata_bars.py` |
| Trading 212 | `GET /equity/metadata/instruments` | Build shortName→ticker map at startup (handles SPAC/rename mismatches) | `trading/executor.py` |
| Trading 212 | `GET /equity/account/cash` | Portfolio value + cash for position sizing | `trading/executor.py` |
| Trading 212 | `POST /equity/orders/market` | Place buy / sell order | `trading/executor.py` |
