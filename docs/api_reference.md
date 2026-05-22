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

Classifies the sentiment of each Benzinga article as `positive`, `neutral`, or `negative` from a US equity trader's perspective. Only `positive` articles proceed to price confirmation.

---

### API call

**Code:**
```python
client = anthropic.Anthropic()
msg = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=64,
    messages=[{"role": "user", "content": prompt}],
)
```

**Prompt template:**
```
You are a financial news sentiment classifier.
Classify the sentiment of this news article for US equity traders.
Respond with a JSON object only — no markdown, no explanation:
{"sentiment": "positive" | "neutral" | "negative", "confidence": 0.0-1.0}

Headline: <title>
Summary: <teaser>
```

**Sample response:**
```json
{"sentiment": "positive", "confidence": 0.88}
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
**Used by:** `market/finnhub_bars.py` → `market/price_check.py`, `monitor/position_monitor.py`

### Purpose

Provides real-time stock quotes (no delay). Used for:
- **Signal confirmation** — current price to measure momentum vs. the yfinance baseline
- **Position monitor** — current price every 60s to evaluate take-profit / stop-loss

---

### Endpoint: GET `/quote`

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

**Rate limit:** 60 requests/minute on the free tier.

---

## 4. yfinance (Yahoo Finance)

**Package:** `yfinance` (Python library, no API key required)
**Used by:** `market/price_check.py`
**Latency:** ~15 minutes delayed

yfinance is used only for **historical** data where the 15-minute delay is acceptable or intentional.

---

### Call 1: Market open check (`is_market_open`)

**Purpose:** Determine whether the US market is in regular trading hours. Fetches 1 minute of SPY data and checks if the last bar is less than 5 minutes old.

**Code:**
```python
yf.Ticker("SPY").history(period="1d", interval="1m")
```

---

### Call 2: Momentum baseline (`confirm_price_signal`)

**Purpose:** Fetch 1-minute intraday bars to get the price from ~15 minutes ago as the momentum baseline. The 15-minute delay is intentional — the most recent yfinance bar aligns with `MOMENTUM_WINDOW_MINUTES=15`.

**Code:**
```python
yf.Ticker("AAPL").history(period="1d", interval="1m")
```

**Sample response (DataFrame):**
```
                                 Open        High         Low       Close    Volume
Datetime
2026-05-21 09:30:00-04:00  291.000000  292.500000  290.800000  292.100000   985432
2026-05-21 09:31:00-04:00  292.100000  293.200000  291.900000  293.000000   754321
...
```

The last bar (`Close.iloc[-1]`) is used as `past_price` for the recent momentum calculation.

---

### Call 3: 20-day average volume (`confirm_price_signal`)

**Purpose:** Calculate the 20-day average daily volume to determine if today's volume is elevated (volume ratio ≥ 1.5× average = volume spike).

**Code:**
```python
yf.Ticker("AAPL").history(period="21d", interval="1d")
```

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
  "detail": "invalid quantity precision 4"
}
```

---

## Summary Table

| API | Endpoint / Call | Purpose | File |
|---|---|---|---|
| Benzinga (massive.com) | `GET /benzinga/v2/news` | Fetch recent US equity news | `news/fetcher.py` |
| Anthropic (Claude Haiku) | `messages.create` | Classify article sentiment | `news/fetcher.py` |
| Finnhub | `GET /quote` | Real-time current price | `market/finnhub_bars.py` |
| yfinance | SPY 1m history | Check if market is open | `market/price_check.py` |
| yfinance | `<ticker>` 1m history (1d) | Momentum baseline (~15 min ago) | `market/price_check.py` |
| yfinance | `<ticker>` 1d history (21d) | 20-day average volume | `market/price_check.py` |
| Trading 212 | `GET /equity/account/cash` | Portfolio value + cash | `trading/executor.py` |
| Trading 212 | `POST /equity/orders/market` | Place buy / sell order | `trading/executor.py` |
