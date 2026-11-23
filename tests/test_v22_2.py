# Licensed to ParallaxTech Ltd under one or more contributor licence
# agreements. See the NOTICE file distributed with this work for additional
# information regarding copyright ownership.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
v22.2 — fixes from the 2026-11-23 review of 51 demo trades.

1. The classifier could not tell which company an article was about. It was
   sent only the headline and teaser, and its one verdict per article was then
   copied to every tagged ticker. Trade #61 bought AMZN on an Estée Lauder
   headline; #45 bought NVDA on a Super Micro article also tagged SMCI and WDC.
2. Benzinga's "<Company> Analysts Raise Their Forecasts Following ..." recaps
   reached the classifier and were sometimes scored as a tradeable guidance
   raise (MMM #46, WMT #82).
3. The $2M liquidity-floor experiment collected no trades in eleven weeks.
4. The zero-trade alert paged every day of every drought — 21 days, all quiet
   stretches between earnings seasons.

The attribution tests drive fetch_all_news() end to end with only the Claude
call faked, because the bug lived in the hand-off between scoring and fan-out:
a test of either half alone would have passed before the fix.
"""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent

# A slice of the real T212 catalogue, including the legacy codes that made a
# bare symbol unreliable (Cencora trades as COR but T212 calls it ABC_US_EQ).
_CATALOGUE = {
    "COR": ("ABC_US_EQ", "Cencora"),
    "AMZN": ("AMZN_US_EQ", "Amazon.com"),
    "SMCI": ("SMCI_US_EQ", "Super Micro Computer"),
    "NVDA": ("NVDA_US_EQ", "NVIDIA"),
    "WDC": ("WDC_US_EQ", "Western Digital"),
    "LW": ("LW_US_EQ", "Lamb Weston"),
}


@pytest.fixture
def catalogue(monkeypatch):
    import trading.executor as ex
    monkeypatch.setattr(ex, "_symbol_to_t212", {s: c for s, (c, _) in _CATALOGUE.items()})
    monkeypatch.setattr(ex, "_t212_to_symbol", {c: s for s, (c, _) in _CATALOGUE.items()})
    monkeypatch.setattr(ex, "_t212_to_name", {c: n for _, (c, n) in _CATALOGUE.items()})


@pytest.fixture(autouse=True)
def _fresh_session_state():
    import news.fetcher as f
    f._scored_articles["date"] = None
    f._scored_articles["ids"] = set()
    f._ticker_history["date"] = None
    f._ticker_history["tickers"] = {}
    yield


def _article(aid, title, tickers):
    return {"benzinga_id": aid, "title": title, "teaser": "t",
            "tickers": tickers,
            "published": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}


def _claude_msg(classifications):
    block = SimpleNamespace(type="tool_use", input={"classifications": classifications})
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def _positive(aid, **extra):
    rec = {"id": aid, "sentiment": "positive", "confidence": 0.9,
           "catalyst_type": "guidance_raise", "already_moved": False,
           "catalyst_magnitude": 3}
    rec.update(extra)
    return rec


@contextmanager
def _gates():
    from config.settings import cfg
    with patch.object(cfg, "min_sentiment_confidence", 7), \
         patch.object(cfg, "tradeable_catalysts", ["guidance_raise"]), \
         patch.object(cfg, "min_catalyst_magnitude", 2), \
         patch.object(cfg, "blocklist", []):
        yield


def _run(articles, classifications):
    """fetch_all_news with the real scorer; only the Claude call is faked."""
    import news.fetcher as f
    with _gates(), \
         patch("news.fetcher._fetch", return_value=articles), \
         patch("news.fetcher._claude") as claude, \
         patch("news.fetcher.save_sentiment_scores") as save:
        claude.messages.create.return_value = _claude_msg(classifications)
        items = f.fetch_all_news(seen_checker=lambda a, t: False)
    rows = [r for call in save.call_args_list for r in call.args[0]]
    user_msg = claude.messages.create.call_args.kwargs["messages"][0]["content"]
    return items, {r["ticker"]: r for r in rows}, user_msg


# ── 1. Ticker attribution ─────────────────────────────────────────────────────

class TestTaggedTickersReachTheClassifier:

    def test_prompt_names_every_tagged_company(self, catalogue):
        _, _, user_msg = _run([_article("a1", "Cencora Sees FY27 Adj EPS", ["COR", "AMZN"])],
                              [_positive("a1", subject_tickers=["COR"])])
        assert "Tagged: COR (Cencora), AMZN (Amazon.com)" in user_msg

    def test_legacy_t212_code_is_shown_as_its_exchange_symbol(self, catalogue):
        """ABC is what T212 calls Cencora; the model must see COR."""
        _, _, user_msg = _run([_article("a1", "Cencora Sees FY27 Adj EPS", ["COR"])],
                              [_positive("a1", subject_tickers=["COR"])])
        tagged = next(l for l in user_msg.splitlines() if "Tagged:" in l)
        assert "COR (Cencora)" in tagged and "ABC" not in tagged

    def test_symbol_alone_before_the_catalogue_is_built(self):
        """No instrument names yet (startup) — show the symbol, never 'None'."""
        _, _, user_msg = _run([_article("a1", "Acme Raises Guidance", ["ACME"])],
                              [_positive("a1", subject_tickers=["ACME"])])
        assert "Tagged: ACME" in user_msg and "None" not in user_msg

    def test_schema_requires_attribution_before_the_verdict(self):
        from news.fetcher import _CLASSIFY_TOOL
        item = _CLASSIFY_TOOL["input_schema"]["properties"]["classifications"]["items"]
        assert "subject_tickers" in item["required"]
        order = list(item["properties"])
        assert order.index("subject_tickers") < order.index("sentiment")

    def test_symbol_map_keeps_company_names(self, monkeypatch):
        import trading.executor as ex
        monkeypatch.setattr(ex, "_symbol_to_t212", {})
        monkeypatch.setattr(ex, "_t212_to_symbol", {})
        monkeypatch.setattr(ex, "_t212_to_name", {})
        resp = MagicMock(status_code=200)
        resp.json.return_value = [
            {"ticker": "ABC_US_EQ", "shortName": "COR", "name": "Cencora", "currencyCode": "USD"},
            {"ticker": "VOD_EQ", "shortName": "VOD", "name": "Vodafone", "currencyCode": "GBX"},
        ]
        with patch("trading.executor.requests.get", return_value=resp):
            assert ex.build_symbol_map(retries=1) is True
        assert ex.instrument_name("ABC_US_EQ") == "Cencora"
        assert ex.instrument_name("VOD_EQ") is None        # non-USD never mapped


class TestVerdictGoesOnlyToTheSubject:

    def test_untagged_company_news_buys_nothing(self, catalogue):
        """Trade #61: an Estée Lauder headline, tagged only AMZN."""
        items, rows, _ = _run(
            [_article("a1", "Estée Lauder Turns Up the Glamour With Strong Earnings, Higher Outlook", ["AMZN"])],
            [_positive("a1", subject_tickers=[])])
        assert items == []
        assert rows["AMZN_US_EQ"]["sentiment"] == "neutral"
        assert rows["AMZN_US_EQ"]["is_subject"] is False

    def test_multi_ticker_article_trades_only_its_subject(self, catalogue):
        """Trade #45: Super Micro guidance, tagged SMCI, NVDA and WDC."""
        items, rows, _ = _run(
            [_article("a1", "Super Micro Squeeze Watch: Revenue Guide Jumps", ["SMCI", "NVDA", "WDC"])],
            [_positive("a1", subject_tickers=["SMCI"])])
        assert [i.ticker for i in items] == ["SMCI_US_EQ"]
        assert rows["SMCI_US_EQ"]["sentiment"] == "positive"
        assert rows["SMCI_US_EQ"]["is_subject"] is True
        for leg in ("NVDA_US_EQ", "WDC_US_EQ"):
            assert rows[leg]["sentiment"] == "neutral"
            assert rows[leg]["is_subject"] is False

    def test_a_genuine_single_ticker_raise_still_trades(self, catalogue):
        """The fix must not cost the trades it exists to keep."""
        items, rows, _ = _run(
            [_article("a1", "Lamb Weston Raises FY2027 Adj EPS Guidance", ["LW"])],
            [_positive("a1", subject_tickers=["LW"])])
        assert [i.ticker for i in items] == ["LW_US_EQ"]
        assert rows["LW_US_EQ"]["sentiment"] == "positive"

    def test_missing_attribution_fails_closed(self, catalogue):
        items, rows, _ = _run(
            [_article("a1", "Lamb Weston Raises FY2027 Adj EPS Guidance", ["LW"])],
            [_positive("a1")])                                   # no subject_tickers
        assert items == []
        assert rows["LW_US_EQ"]["sentiment"] == "neutral"
        assert rows["LW_US_EQ"]["is_subject"] is None

    def test_malformed_attribution_fails_closed(self, catalogue):
        items, _, _ = _run(
            [_article("a1", "Lamb Weston Raises FY2027 Adj EPS Guidance", ["LW"])],
            [_positive("a1", subject_tickers="LW")])             # a string, not a list
        assert items == []

    def test_model_cannot_nominate_an_untagged_ticker(self, catalogue):
        items, _, _ = _run(
            [_article("a1", "Estée Lauder Raises Outlook", ["AMZN"])],
            [_positive("a1", subject_tickers=["EL"])])
        assert items == []

    def test_attribution_is_restricted_to_the_tags(self):
        """The scorer's contract: subject_tickers ⊆ the entry's own tags. The
        fan-out happens to be safe either way, but the shadow, the backtest and
        the Gate [subject] log all read this list as 'the tagged subjects'."""
        import news.fetcher as f
        entry = {"id": "a1", "headline": "Estée Lauder Raises Outlook", "teaser": "t",
                 "ticker": "AMZN_US_EQ",
                 "tagged": [{"symbol": "AMZN", "name": "Amazon.com", "t212": "AMZN_US_EQ"}]}
        with patch("news.fetcher._claude") as claude:
            claude.messages.create.return_value = _claude_msg(
                [_positive("a1", subject_tickers=["EL", "amzn", "AMZN"])])
            scores = f._batch_score_sentiment([entry])
        assert scores["a1"]["subject_tickers"] == ["AMZN"]    # EL dropped, de-duplicated

    @pytest.mark.parametrize("echo", ["cor", "COR (Cencora)", "ABC_US_EQ"])
    def test_echoed_forms_of_the_tag_are_recognised(self, catalogue, echo):
        """Case, the Tagged-line form, and the T212 code (it appears on the
        PRIOR ARTICLE(S) line) all resolve to the same ticker."""
        items, _, _ = _run(
            [_article("a1", "Cencora Raises FY27 Adj EPS Guidance", ["COR"])],
            [_positive("a1", subject_tickers=[echo])])
        assert [i.ticker for i in items] == ["ABC_US_EQ"]

    def test_non_subjects_get_no_same_day_context(self, catalogue):
        """Super Micro's guidance is not context for NVIDIA's next headline."""
        import news.fetcher as f
        _run([_article("a1", "Super Micro Raises Revenue Guide", ["SMCI", "NVDA"])],
             [_positive("a1", subject_tickers=["SMCI"])])
        assert f._prior_ticker_context("SMCI_US_EQ") is not None
        assert f._prior_ticker_context("NVDA_US_EQ") is None


class TestAttributionIsPersisted:

    def test_is_subject_reaches_sentiment_scores(self):
        from storage import database as db
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        @contextmanager
        def fake_conn():
            yield conn

        row = {"article_id": "a1", "ticker": "NVDA_US_EQ", "headline": "h",
               "sentiment": "neutral", "confidence": 0.9,
               "catalyst_type": "guidance_raise", "already_moved": False,
               "catalyst_magnitude": 3, "published_at": "", "is_subject": False}
        with patch.object(db, "get_conn", fake_conn):
            db.save_sentiment_scores([row])
        sql, params = cur.executemany.call_args.args
        columns = re.search(r"INSERT INTO sentiment_scores\s*\(([^)]*)\)", sql).group(1)
        assert "is_subject" in columns            # the column list, not just VALUES
        assert "%(is_subject)s" in sql
        assert params[0]["is_subject"] is False

    def test_rows_without_attribution_store_null(self):
        """Callers that never set the key (pre-v22.2 shape) store NULL."""
        from storage import database as db
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        @contextmanager
        def fake_conn():
            yield conn

        row = {"article_id": "a1", "ticker": "X", "headline": "h",
               "sentiment": "neutral", "confidence": 0.5, "catalyst_type": "other",
               "already_moved": False, "catalyst_magnitude": 1, "published_at": ""}
        with patch.object(db, "get_conn", fake_conn):
            db.save_sentiment_scores([row])
        assert cur.executemany.call_args.args[1][0]["is_subject"] is None

    def test_migration_adds_the_column(self):
        import inspect
        from storage import database as db
        assert "ADD COLUMN IF NOT EXISTS is_subject BOOLEAN" in inspect.getsource(db)


class TestShadowAttributionParity:
    """Qwen's row must store the verdict its ticker INHERITS, exactly as
    Claude's row for that ticker does — or every mis-tagged article would
    read as a disagreement between the two models."""

    def _run_shadow(self, subject):
        import news.shadow_classifier as sc
        call = MagicMock()
        call.function.arguments = json.dumps({"classifications": [
            {"id": "a1", "subject_tickers": subject, "sentiment": "positive",
             "confidence": 0.9, "catalyst_type": "guidance_raise",
             "already_moved": False, "catalyst_magnitude": 3}]})
        resp = MagicMock(usage=None)
        resp.choices = [MagicMock(message=MagicMock(tool_calls=[call]), finish_reason="stop")]
        client = MagicMock()
        client.chat.completions.create.return_value = resp
        entry = {"id": "a1", "ticker": "NVDA_US_EQ", "headline": "h",
                 "tagged": [{"symbol": "NVDA", "name": "NVIDIA", "t212": "NVDA_US_EQ"},
                            {"symbol": "SMCI", "name": "Super Micro", "t212": "SMCI_US_EQ"}]}
        with patch.object(sc, "_get_client", return_value=client), \
             patch("storage.database.save_qwen_scores", return_value=1) as save, \
             patch("storage.database.record_classifier_call"):
            sc._run([entry], "msg")
        return save.call_args.args[0][0]

    def test_non_subject_representative_is_stored_neutral(self):
        assert self._run_shadow(["SMCI"])["sentiment"] == "neutral"

    def test_subject_representative_keeps_its_verdict(self):
        assert self._run_shadow(["NVDA"])["sentiment"] == "positive"

    def test_comparison_pairs_attributed_rows_on_the_same_ticker(self):
        import inspect
        import analysis.classifier_compare as cc
        sql = inspect.getsource(cc.prediction)
        assert "q.ticker = s.ticker" in sql
        # ...but legacy rows (one copied verdict per article) may still borrow
        # a measured leg, as v21.14.1 established.
        assert "s.is_subject IS NULL" in sql
        assert "(s.fwd_return_60m IS NULL)" in sql


# ── 2. Analyst forecast recaps ────────────────────────────────────────────────

class TestAnalystForecastRecapsFiltered:

    RECAPS = [   # real headlines from sentiment_scores
        "3M Analysts Boost Their Forecasts Following Strong Q3 Earnings",
        "Walmart Analysts Raise Their Forecasts Following Strong Q3 Earnings",
        "Ambiq Micro Analysts Increase Their Forecasts After Strong Q2 Results",
        "Accenture Analysts Cut Their Forecasts After Q3 Results",
        "Aaon Analysts Slash Their Forecasts Following Q2 Earnings",
        "AEVEX Analysts Revise Their Forecasts Following Q3 Results",
        "Alcoa Earnings Are Imminent; These Most Accurate Analysts Revise Forecasts Ahead Of Earnings Call",
        "RTX Gears Up For Q3 Print; Here Are The Recent Forecast Changes From Wall Street's Most Accurate Analysts",
    ]
    COMPANY_NEWS = [   # must still reach the classifier
        "Lamb Weston Hldgs Raises FY2027 Adj EPS Guidance from $2.95-$3.25 to $3.05-$3.35 vs $3.01 Est",
        "Post-It Maker 3M Raises 2026 Profit Outlook Again",
        "Acme Raises FY26 Revenue Forecast To $1.2B",
        "Acme Boosts Full-Year Forecast After Strong Q3",
        "UnitedHealth Exec Sees Lower End Of Long-Term 13%-16% Growth Rate As Starting Point For 2027 EPS Forecast",
        "Acme Analyst Day: Company Raises Long-Term Targets",
    ]

    def _reaches_classifier(self, title):
        import news.fetcher as f
        with patch("news.fetcher._fetch", return_value=[_article("a1", title, ["ACME"])]), \
             patch("news.fetcher._batch_score_sentiment", return_value={}) as score:
            f.fetch_all_news(seen_checker=lambda a, t: False)
        return score.called

    @pytest.mark.parametrize("title", RECAPS)
    def test_recap_never_reaches_the_classifier(self, title):
        assert not self._reaches_classifier(title)

    @pytest.mark.parametrize("title", COMPANY_NEWS)
    def test_company_guidance_still_reaches_the_classifier(self, title):
        assert self._reaches_classifier(title)


# ── 3. Liquidity floor ────────────────────────────────────────────────────────

class TestLiquidityFloorRestored:

    def test_production_runs_the_shipped_default(self):
        src = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        values = re.findall(r"^\s*MIN_DAILY_DOLLAR_VOLUME=(\d+)\s*$", src, re.MULTILINE)
        assert values == ["5000000"]


# ── 4. Zero-trade alert: once per drought ─────────────────────────────────────

class TestZeroTradeAlertOncePerDrought:

    def _check(self, idle=3, alerted=False):
        import main
        from config.settings import cfg
        helper = (MagicMock(side_effect=alerted) if isinstance(alerted, Exception)
                  else MagicMock(return_value=alerted))
        with patch.object(cfg, "zero_trade_alert_sessions", 3), \
             patch("main.trading_days_since_last_trade", return_value=idle), \
             patch("main.drought_alerted_on_earlier_day", helper), \
             patch("main.record_system_event") as rec:
            main.check_zero_trade_drought()
        return [c.args[0] for c in rec.call_args_list]

    def test_first_day_of_a_drought_is_critical(self):
        assert self._check(alerted=False) == ["zero_trade_session"]

    def test_later_days_of_the_same_drought_are_a_warning(self):
        assert self._check(idle=9, alerted=True) == ["zero_trade_session_ongoing"]

    def test_an_unanswerable_check_stays_loud(self):
        assert self._check(alerted=RuntimeError("db down")) == ["zero_trade_session"]

    def test_below_the_threshold_records_nothing(self):
        assert self._check(idle=2) == []

    def test_severities(self):
        from storage.database import _CRITICAL_EVENT_TYPES
        assert "zero_trade_session" in _CRITICAL_EVENT_TYPES
        assert "zero_trade_session_ongoing" not in _CRITICAL_EVENT_TYPES

    def test_helper_is_scoped_to_the_current_drought(self):
        """An alert from an EARLIER drought — before the last entry — must not
        silence this one; and today's own alert must not downgrade itself."""
        from storage import database as db
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur

        @contextmanager
        def fake_conn():
            yield conn

        with patch.object(db, "get_conn", fake_conn), \
             patch.object(db, "_now_london", return_value="2026-09-16T21:30:00+01:00"):
            assert db.drought_alerted_on_earlier_day() is False
        sql, params = cur.execute.call_args.args
        assert "event_day < %s" in sql and params[0] == "2026-09-16"
        assert "event_day > COALESCE" in sql and "origin = 'strategy'" in sql
