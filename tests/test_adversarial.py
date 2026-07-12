"""
tests/test_adversarial.py — chaos / garbage-input suite (v19.3).

Contract under test: NOTHING an external service can send — malformed JSON,
wrong types, NaN, explicit nulls, mis-scaled values, poisoned fields — may
(a) raise out of a public function, or (b) produce a trade approval.
Garbage in → None / reject / skip out, and one bad record never takes down
its batch or cycle.

Attack surfaces: Finnhub /quote, Twelvedata /quote + time_series, Claude
tool-use output, the Benzinga article feed, T212 cash/sizing, and the pure
math helpers. Everything is mocked — no network.
"""
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _resp(status_code=200, json_body=None, json_raises=False):
    m = MagicMock()
    m.status_code = status_code
    m.ok = status_code < 400
    m.text = "adversarial"
    if json_raises:
        m.json.side_effect = ValueError("not json")
    else:
        m.json.return_value = json_body if json_body is not None else {}
    m.raise_for_status.return_value = None
    return m


# ── Finnhub /quote ─────────────────────────────────────────────────────────────

class TestFinnhubQuoteGarbage:
    """get_finnhub_quote must emit a normalized quote or None — never raw junk."""

    def _quote(self, body, **kw):
        from market.finnhub_bars import get_finnhub_quote
        with patch("market.finnhub_bars.requests.get", return_value=_resp(json_body=body, **kw)):
            return get_finnhub_quote("ACME", fast=True)

    def test_missing_c_is_none(self):
        assert self._quote({}) is None

    def test_null_c_is_none(self):
        assert self._quote({"c": None, "o": 10, "pc": 10}) is None

    def test_string_garbage_c_is_none(self):
        assert self._quote({"c": "abc"}) is None

    def test_negative_c_is_none(self):
        # A negative "price" passed the old `c == 0` check.
        assert self._quote({"c": -5.0, "o": 10, "pc": 10}) is None

    def test_nan_c_is_none(self):
        # NaN compares False against every gate threshold — the silent killer:
        # NaN < $5 penny gate is False, NaN < -3% dead-cat is False, etc.
        assert self._quote({"c": float("nan"), "o": 10, "pc": 10}) is None

    def test_numeric_string_c_is_coerced(self):
        q = self._quote({"c": "12.50", "o": "12.25", "pc": "12.10"})
        assert q == {"c": 12.50, "o": 12.25, "pc": 12.10, "t": None}

    def test_non_dict_payload_is_none(self):
        assert self._quote(["not", "a", "dict"]) is None

    def test_garbage_secondary_fields_degrade_not_fatal(self):
        q = self._quote({"c": 12.5, "o": "x", "pc": None, "t": "yesterday"})
        assert q is not None
        assert q["c"] == 12.5
        assert q["o"] == 0 and q["pc"] == 0   # 0 = "missing" to price_check
        assert q["t"] is None                 # bad timestamp: staleness fails open

    def test_http_401_is_none_single_attempt(self):
        from market.finnhub_bars import get_finnhub_quote
        with patch("market.finnhub_bars.requests.get",
                   return_value=_resp(status_code=401)) as mock_get:
            assert get_finnhub_quote("ACME") is None
            mock_get.assert_called_once()  # 4xx never retries

    def test_non_json_body_is_none(self):
        assert self._quote(None, json_raises=True) is None


# ── Twelvedata /quote + time_series ───────────────────────────────────────────

class _TDBucketReset:
    """Refill the credit meter + token bucket so gates never block a test."""

    def setup_method(self):
        import market.twelvedata_bars as td
        self._saved = (td._bucket_tokens, td._bucket_last_refill)
        td._bucket_tokens = 55.0
        import time as _t
        td._bucket_last_refill = _t.monotonic()

    def teardown_method(self):
        import market.twelvedata_bars as td
        td._bucket_tokens, td._bucket_last_refill = self._saved


class TestTwelvedataQuoteGarbage(_TDBucketReset):

    def _quote(self, body):
        from market.twelvedata_bars import get_twelvedata_quote
        with patch("market.twelvedata_bars.requests.get", return_value=_resp(json_body=body)):
            return get_twelvedata_quote("ACME", fast=True)

    def test_garbage_close_is_none(self):
        assert self._quote({"close": "abc"}) is None

    def test_null_close_is_none(self):
        assert self._quote({"close": None}) is None

    def test_nan_close_is_none(self):
        assert self._quote({"close": "NaN"}) is None

    def test_garbage_timestamp_keeps_quote(self):
        # Regression: a bad timestamp used to raise inside normalisation and
        # discard an otherwise perfectly good quote.
        q = self._quote({"close": "10.5", "open": "10.0",
                         "previous_close": "10.2", "timestamp": "garbage"})
        assert q is not None and q["c"] == 10.5 and q["t"] is None

    def test_garbage_secondary_fields_degrade(self):
        q = self._quote({"close": "10.5", "open": None,
                         "previous_close": "n/a", "average_volume": "lots"})
        assert q is not None
        assert q["o"] == 10.5 and q["pc"] is None and q["av"] is None


class TestTimeSeriesGarbage(_TDBucketReset):

    def test_values_as_dict_is_none(self):
        from market.twelvedata_bars import _get_time_series
        body = {"values": {"datetime": "2026-07-07"}}  # dict, not list
        with patch("market.twelvedata_bars.requests.get", return_value=_resp(json_body=body)):
            assert _get_time_series("ACME", "1min", 10, fast=True) is None

    def test_session_analysis_survives_dict_values(self):
        from market.twelvedata_bars import get_session_analysis
        body = {"values": "garbage-string"}
        with patch("market.twelvedata_bars.requests.get", return_value=_resp(json_body=body)):
            assert get_session_analysis("ACME", fast=True) is None

    def test_daily_stats_survive_scalar_bars(self):
        import market.twelvedata_bars as td
        td._daily_stats_cache.clear()
        with patch.object(td, "_get_time_series",
                          return_value=["junk", None, 42]):
            assert td.get_daily_stats("ACME") is None

    def test_session_analysis_survives_scalar_bars(self):
        import market.twelvedata_bars as td
        result = "sentinel"
        with patch.object(td, "_get_time_series",
                          return_value=["junk", None, 42]):
            try:
                result = td.get_session_analysis("ACME", fast=True)
            except Exception as exc:  # noqa: BLE001 — the assertion IS "no raise"
                pytest.fail(f"get_session_analysis raised on scalar bars: {exc!r}")
        assert result is None  # no usable bars → fail closed

    def test_session_bars_skip_malformed_rows(self):
        import market.twelvedata_bars as td
        import pytz
        et = pytz.timezone("America/New_York")
        from datetime import datetime, timedelta
        now_et = datetime.now(et)
        fresh = (now_et - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:00")
        older = (now_et - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:00")
        bars = [
            {"datetime": fresh, "high": "10.2", "low": "10.0",
             "close": "10.1", "volume": "3000"},
            {"datetime": older, "high": "oops", "low": "9.9",
             "close": "10.0", "volume": "2000"},        # bad high → skipped
            {"datetime": "not-a-date", "high": "1", "low": "1",
             "close": "1", "volume": "1"},               # bad date → skipped
            "not-even-a-dict-would-be-nice",             # raises per-bar → skipped
        ]
        with patch.object(td, "_get_time_series", return_value=bars):
            sa = td.get_session_analysis("ACME")
        assert sa is not None
        assert sa.session_volume == 3000 and sa.last_price == 10.1
        assert sa.vwap is not None
        assert sa.session_low == 10.0 and sa.session_high == 10.2  # clean bar only


# ── Price confirmation end-to-end with hostile inputs ─────────────────────────

class TestConfirmSignalGarbage:
    """Whatever reaches confirm_price_signal, it must return None or a
    rejection — never raise, never approve on garbage."""

    def _confirm(self, quote):
        import market.price_check as pc
        with patch.object(pc, "get_quote_with_fallback", return_value=quote), \
             patch.object(pc, "get_session_analysis", return_value=None), \
             patch.object(pc, "get_daily_stats", return_value=None):
            return pc.confirm_price_signal("ACME_US_EQ")

    def test_minimal_quote_no_bars_fails_closed(self):
        conf = self._confirm({"c": 10.5})
        assert conf is None or not conf.is_confirmed

    def test_zero_open_and_pc_no_division_crash(self):
        conf = self._confirm({"c": 10.5, "o": 0, "pc": 0})
        assert conf is None or not conf.is_confirmed

    def test_string_fields_fail_closed_not_raise(self):
        # Normalisation upstream should prevent this shape, but the outer
        # net must hold anyway (defense in depth).
        conf = self._confirm({"c": 10.5, "o": "abc", "pc": {}})
        assert conf is None or not conf.is_confirmed


# ── Position sizing ────────────────────────────────────────────────────────────

class TestQuantitySizingGarbage:

    def _size(self, price, cash=None):
        from trading.executor import calculate_quantity
        cash = cash if cash is not None else {"total": 5000.0, "free": 5000.0}
        with patch("trading.executor._get", return_value=cash), \
             patch("trading.executor.get_gbp_usd_rate", return_value=1.25):
            return calculate_quantity("ACME_US_EQ", price)

    def test_zero_price_refused(self):
        qty, reason = self._size(0.0)
        assert qty is None and "invalid price" in reason

    def test_negative_price_refused(self):
        qty, reason = self._size(-3.0)
        assert qty is None

    def test_nan_price_refused(self):
        qty, reason = self._size(float("nan"))
        assert qty is None

    def test_garbage_price_refused(self):
        qty, reason = self._size("abc")
        assert qty is None

    def test_malformed_cash_payload_refused(self):
        qty, reason = self._size(10.0, cash={"total": "abc", "free": 100})
        assert qty is None and "malformed" in reason

    def test_nan_cash_refused(self):
        qty, reason = self._size(10.0, cash={"total": float("nan"), "free": 100})
        assert qty is None


# ── Claude classification output ──────────────────────────────────────────────

def _claude_msg(classifications):
    block = SimpleNamespace(type="tool_use",
                            input={"classifications": classifications})
    return SimpleNamespace(content=[block])


class TestClaudeRecordGarbage:
    """One malformed record skips that record; the rest of the batch survives.
    Out-of-range values fail closed (skipped), never clamped into a trade."""

    def setup_method(self):
        import news.fetcher as f
        f._claude_cooldown = None

    teardown_method = setup_method

    def _score(self, classifications, n_articles=1):
        import news.fetcher as f
        articles = [{"id": str(i), "headline": "h", "teaser": "t"}
                    for i in range(n_articles)]
        with patch("news.fetcher._claude") as mock_claude:
            mock_claude.messages.create.return_value = _claude_msg(classifications)
            return f._batch_score_sentiment(articles)

    def test_one_bad_record_does_not_kill_batch(self):
        results = self._score([
            {"id": "0", "sentiment": "positive", "confidence": 0.9,
             "catalyst_type": "fda_approval", "already_moved": False,
             "catalyst_magnitude": 4},
            {"id": "1", "confidence": "high"},          # garbage type
            {"id": "2", "catalyst_magnitude": "big"},   # garbage type
            "just-a-string",                            # not even a dict
            {"id": "3", "confidence": 7},               # mis-scaled 0-10 answer
            {"id": "4", "confidence": float("nan")},    # NaN
            {"id": "5", "catalyst_magnitude": 99},      # out of range
        ], n_articles=6)
        assert set(results.keys()) == {"0"}
        assert results["0"]["confidence"] == 0.9

    def test_boundary_values_kept(self):
        results = self._score([
            {"id": "0", "confidence": 1.0, "catalyst_magnitude": 5},
            {"id": "1", "confidence": 0.0, "catalyst_magnitude": 1},
        ], n_articles=2)
        assert set(results.keys()) == {"0", "1"}

    def test_classifications_as_string_is_empty(self):
        assert self._score("garbage") == {}

    def test_classifications_as_dict_is_empty(self):
        assert self._score({"id": "0"}) == {}


# ── Benzinga article feed ──────────────────────────────────────────────────────

class TestFetcherGarbageArticles:
    """One poisoned article must never kill the cycle for every article."""

    def setup_method(self):
        import news.fetcher as f
        f._scored_articles["date"] = None
        f._scored_articles["ids"] = set()

    def _fetch_with(self, articles):
        import news.fetcher as f
        with patch("news.fetcher._fetch", return_value=articles), \
             patch("news.fetcher._batch_score_sentiment", return_value={}) as mock_score:
            items = f.fetch_all_news(seen_checker=lambda a, t: False)
        return items, mock_score

    def test_hostile_feed_survives(self):
        items, _ = self._fetch_with([
            None,                                        # null in article array
            42,                                          # scalar in article array
            {},                                          # empty article
            {"tickers": "AAPL", "title": None},          # tickers as bare string
            {"tickers": [123, None, ""], "title": None}, # non-string tickers
            {"tickers": ["AAPL"], "title": None,         # explicit null title,
             "published": None, "teaser": None,          # published, teaser, body
             "body": None, "benzinga_id": "ok-1"},
        ])
        assert items == []  # nothing tradeable, and — critically — no crash

    def test_bare_string_tickers_do_not_become_char_tickers(self):
        # "AAPL" iterated as a string yields A/A/P/L — and "A" is a real NYSE
        # ticker. The article must contribute NO tickers at all.
        _, mock_score = self._fetch_with([
            {"tickers": "AAPL", "title": "Big news", "benzinga_id": "x1"},
        ])
        mock_score.assert_not_called()  # no eligible articles → no Claude call

    def test_null_title_article_scores_with_empty_headline(self):
        _, mock_score = self._fetch_with([
            {"tickers": ["AAPL"], "title": None, "teaser": None, "body": None,
             "benzinga_id": "x2"},
        ])
        mock_score.assert_called_once()
        payload = mock_score.call_args.args[0]
        assert payload[0]["headline"] == "" and payload[0]["teaser"] == ""


# ── Symbol + math edges ───────────────────────────────────────────────────────

class TestSymbolEdges:

    def test_t212_to_symbol_handles_falsy(self):
        from trading.executor import t212_to_symbol
        assert t212_to_symbol("") == ""
        assert t212_to_symbol(None) == ""

    def test_clean_symbol_whitespace(self):
        from trading.executor import clean_benzinga_symbol
        assert clean_benzinga_symbol("  aapl  ") == "AAPL"


class TestMathEdges:

    def test_volume_fraction_negative_minutes_no_crash(self):
        from market.price_check import _expected_volume_fraction
        f = _expected_volume_fraction(-30.0)
        assert 0.0 < f <= 1.0

    def test_rvol_negative_minutes_no_crash(self):
        from market.price_check import compute_rvol
        r = compute_rvol(100, 1000, -30.0)
        assert r >= 0.0

    def test_rvol_zero_and_negative_adv(self):
        from market.price_check import compute_rvol
        assert compute_rvol(100, 0, 30) == 0.0
        assert compute_rvol(100, -5, 30) == 0.0

    def test_quote_staleness_poisoned_timestamps(self):
        from market.price_check import _quote_is_stale
        # Garbage/absent timestamps must fail OPEN (quote kept); only positive
        # evidence of staleness rejects.
        assert _quote_is_stale("X", {"t": "abc"}, "F") is False
        assert _quote_is_stale("X", {"t": 0}, "F") is False
        assert _quote_is_stale("X", {}, "F") is False
        # A timestamp absurdly far in the past IS positive evidence.
        assert _quote_is_stale("X", {"t": 1}, "F") is True
