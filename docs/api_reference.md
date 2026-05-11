# API Reference

This document covers every external API used by the momentum trader, including endpoints, purpose, which files use them, and sample requests/responses.

---

## 1. Benzinga News API

**Base URL:** `https://api.benzinga.com/api/v2/news`  
**Auth:** API key passed as `token` query parameter  
**Format:** XML  
**Used by:** `news/fetcher.py`

### Purpose
Fetches market news articles to detect trading signals. Two types of queries are made:
- **WIIM** (Why Is It Moving) — articles confirming a stock is already moving with a known catalyst. Highest-confidence signals.
- **General news** — broad market news used as an early signal before WIIM is published.

Tickers are extracted directly from the `<stocks>` XML tags — no keyword matching is needed.

---

### Endpoint: GET `/api/v2/news`

#### WIIM fetch (news/fetcher.py `fetch_wiim`)

**Request:**
```
GET https://api.benzinga.com/api/v2/news
  ?token=<BENZINGA_API_KEY>
  &channels=WIIM
  &pageSize=100
  &displayOutput=abstract
```

**Sample response (XML):**
```xml
<result>
  <item>
    <id>12345678</id>
    <title>Why Is Apple Stock Up Today?</title>
    <teaser>Apple shares are rising after strong iPhone sales data...</teaser>
    <created>Wed, 07 May 2026 14:32:00 -0400</created>
    <stocks>
      <item>
        <name>AAPL</name>
        <sector>Equity</sector>
      </item>
    </stocks>
    <channels>
      <item><name>WIIM</name></item>
      <item><name>Tech</name></item>
    </channels>
  </item>
</result>
```

---

#### General news fetch (news/fetcher.py `fetch_news`)

**Request:**
```
GET https://api.benzinga.com/api/v2/news
  ?token=<BENZINGA_API_KEY>
  &pageSize=100
  &displayOutput=abstract
```

**Sample response (XML):** Same structure as above, but articles are not filtered to the WIIM channel. The `<channels>` tag will not contain `WIIM`.

---

## 2. yfinance (Yahoo Finance)

**Package:** `yfinance` (Python library, no API key required)  
**Used by:** `market/price_check.py`

yfinance wraps the Yahoo Finance API internally. All calls are made via the `yf.Ticker` object.

---

### Call 1: Market open check (`is_market_open`)

**Purpose:** Determine whether the US stock market is currently in regular trading hours. Fetches 1 minute of SPY intraday data and checks if the last bar is less than 5 minutes old.

**Code:**
```python
yf.Ticker("SPY").history(period="1d", interval="1m")
```

**Sample response (DataFrame):**
```
                                 Open        High         Low       Close    Volume
Datetime
2026-05-07 09:30:00-04:00  580.010010  580.280029  579.730042  580.059998  4823100
2026-05-07 09:31:00-04:00  580.059998  580.340027  579.950012  580.200012  1234500
...
```

---

### Call 2: Intraday price data (`confirm_price_signal`)

**Purpose:** Fetch 5-minute intraday bars for a ticker to calculate recent price momentum (price change over the last 30 minutes) and today's total volume.

**Code:**
```python
yf.Ticker("AAPL").history(period="1d", interval="5m")
```

**Sample response (DataFrame):**
```
                                 Open        High         Low       Close    Volume
Datetime
2026-05-07 09:30:00-04:00  291.000000  292.500000  290.800000  292.100000   985432
2026-05-07 09:35:00-04:00  292.100000  293.200000  291.900000  293.000000   754321
...
```

---

### Call 3: Historical daily data (`confirm_price_signal`)

**Purpose:** Fetch 21 days of daily bars to calculate the 20-day average volume. Used to determine whether today's volume is elevated relative to normal.

**Code:**
```python
yf.Ticker("AAPL").history(period="21d", interval="1d")
```

**Sample response (DataFrame):**
```
                  Open        High         Low       Close      Volume
Date
2026-04-10  280.000000  284.500000  279.200000  283.800000  62345678
2026-04-11  283.800000  286.100000  282.500000  285.200000  58123456
...
```

---

### Call 4: Current price lookup (`get_current_price`)

**Purpose:** Fast price lookup for the position monitor. Fetches 1-minute bars and returns the latest close price. Called every 60 seconds for each open trade.

**Code:**
```python
yf.Ticker("AAPL").history(period="1d", interval="1m")
```

**Sample response:** Same structure as Call 1 above.

---

## 3. Trading 212 API

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

The correct base URL is selected automatically based on `TRADING_MODE` in `.env`:
- `TRADING_MODE=demo` → `demo.trading212.com` using `TRADING212_DEMO_API_KEY_ID` + `TRADING212_DEMO_API_KEY`
- `TRADING_MODE=live` → `live.trading212.com` using `TRADING212_API_KEY_ID` + `TRADING212_API_KEY`

---

### Endpoint: GET `/equity/account/cash`

**Purpose:** Fetch the account's total value and available cash. Used in live mode only to calculate position size. In demo mode, `DEMO_PORTFOLIO_VALUE` from `.env` is used instead (no API call made).

**Used by:** `get_portfolio_value()`, `get_available_cash()` in `trading/executor.py`

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

**Purpose:** Place a market order. Used for both buying (positive quantity) and selling (negative quantity).

**Used by:** `buy()` and `sell()` in `trading/executor.py`

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
  "dateCreated": "2026-05-07T19:22:10.000Z",
  "dateModified": "2026-05-07T19:22:10.500Z"
}
```

**Error response (quantity precision):**
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

| API | Endpoint | Purpose | File |
|---|---|---|---|
| Benzinga | `GET /api/v2/news?channels=WIIM` | Fetch WIIM signals | `news/fetcher.py` |
| Benzinga | `GET /api/v2/news` | Fetch general news | `news/fetcher.py` |
| yfinance | `SPY` 1m history | Check if market is open | `market/price_check.py` |
| yfinance | `<ticker>` 5m history | Recent momentum + volume | `market/price_check.py` |
| yfinance | `<ticker>` 1d history (21d) | 20-day average volume | `market/price_check.py` |
| yfinance | `<ticker>` 1m history | Current price for monitor | `market/price_check.py` |
| Trading 212 | `GET /equity/account/cash` | Portfolio value + cash | `trading/executor.py` |
| Trading 212 | `POST /equity/orders/market` | Place buy order | `trading/executor.py` |
| Trading 212 | `POST /equity/orders/market` | Place sell order (negative qty) | `trading/executor.py` |
