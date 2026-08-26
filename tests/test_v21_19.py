"""
v21.19 — one event, one trade; and a retry path that survives a rate limit.

Both defects fired together on 2026-08-26 and killed every signal of the day.

Bath & Body Works published THREE guidance articles before the open. The
scanner stores one candidate per ARTICLE, nothing collapsed them by ticker, so
all three approved and all three fired a buy for LB_US_EQ within seconds. Each
buy hit a quantity-precision mismatch and immediately retried — six order
requests in a couple of seconds. T212 rate-limited us, and because the
precision retry (unlike the initial placement) had no retry of its own, every
one died instantly:

    BUY failed for LB_US_EQ after precision retry: HTTP 429 TooManyRequests

Those were the first 429s in nine days. Had the orders instead SUCCEEDED we
would have opened three positions in one stock, because the 24-hour ticker
cooldown only engages once a trade is recorded in the database — which these
were racing.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestPremarketTickerDedupe:
    """One corporate event routinely produces several articles."""

    @staticmethod
    def _cand(cid, ticker, conf=0.8, mag=3):
        from datetime import datetime
        import pytz
        now = datetime.now(pytz.timezone("Europe/London")).isoformat()
        return {"id": cid, "ticker": ticker, "confidence": conf,
                "catalyst_magnitude": mag, "created_at": now}

    def _live(self, cands, minutes_open=5.0):
        import premarket.scanner as sc
        with patch.object(sc, "update_premarket_candidate") as upd:
            live, graduated = sc._live_candidates(cands, minutes_open)
        return live, graduated, upd

    def test_three_articles_for_one_ticker_become_one_candidate(self):
        # The exact 2026-08-26 shape.
        cands = [self._cand(584, "LB_US_EQ", conf=0.75),
                 self._cand(585, "LB_US_EQ", conf=0.85),
                 self._cand(586, "LB_US_EQ", conf=0.90)]
        live, _, _ = self._live(cands)
        assert len(live) == 1, "one event must produce one tradeable candidate"

    def test_the_highest_confidence_article_is_the_one_kept(self):
        # Dropping the newest/best article would be its own bug — LB's three
        # were 0.75, 0.85 and 0.90 in publication order.
        cands = [self._cand(584, "LB_US_EQ", conf=0.75),
                 self._cand(585, "LB_US_EQ", conf=0.85),
                 self._cand(586, "LB_US_EQ", conf=0.90)]
        live, _, _ = self._live(cands)
        assert live[0]["id"] == 586

    def test_confidence_beats_recency(self):
        # Deliberately inverted: the OLDEST row has the highest confidence, so
        # a ranking that silently fell back to "newest id wins" would pick the
        # wrong article and this test would catch it. (The real LB rows all
        # shared a magnitude AND ordered confidence with id, which would let
        # that bug hide.)
        cands = [self._cand(100, "ACME_US_EQ", conf=0.95, mag=3),
                 self._cand(200, "ACME_US_EQ", conf=0.70, mag=3),
                 self._cand(300, "ACME_US_EQ", conf=0.60, mag=3)]
        live, _, _ = self._live(cands)
        assert live[0]["id"] == 100, "highest confidence must win, not newest"

    def test_magnitude_breaks_a_confidence_tie(self):
        cands = [self._cand(1, "ACME_US_EQ", conf=0.8, mag=2),
                 self._cand(2, "ACME_US_EQ", conf=0.8, mag=5)]
        live, _, _ = self._live(cands)
        assert live[0]["id"] == 2

    def test_the_losers_are_retired_not_left_pending(self):
        # A duplicate left pending would be re-evaluated every cycle for the
        # rest of the window, spending a quote credit each time.
        cands = [self._cand(584, "LB_US_EQ", conf=0.75),
                 self._cand(585, "LB_US_EQ", conf=0.90)]
        _, _, upd = self._live(cands)
        retired = {c.args[0]: c.args[1:] for c in upd.call_args_list}
        assert 584 in retired
        assert retired[584][0] == "rejected"
        assert "duplicate" in retired[584][1].lower()
        assert 585 not in retired, "the keeper must stay pending"

    def test_different_tickers_are_never_collapsed(self):
        cands = [self._cand(1, "LB_US_EQ"), self._cand(2, "KSS_US_EQ"),
                 self._cand(3, "ANF_US_EQ")]
        live, _, _ = self._live(cands)
        assert {c["ticker"] for c in live} == {"LB_US_EQ", "KSS_US_EQ", "ANF_US_EQ"}

    def test_dedupe_runs_before_the_price_check_not_after(self):
        # Deduping after the parallel confirm phase would still spend a quote
        # credit per duplicate — 594 candidates covered only 550 ticker-days.
        import inspect
        import premarket.scanner as sc
        src = inspect.getsource(sc._live_candidates)
        assert "_dedupe_by_ticker" in src, (
            "dedupe must happen in the sequential no-I/O pre-pass"
        )

    def test_a_single_candidate_is_untouched(self):
        cands = [self._cand(1, "ACME_US_EQ")]
        live, _, upd = self._live(cands)
        assert len(live) == 1
        upd.assert_not_called()


class TestOrderRetrySurvivesRateLimit:
    """The precision retry is the attempt MOST likely to meet a throttle."""

    @staticmethod
    def _rate_limited():
        from trading.executor import T212HTTPError
        return T212HTTPError(429, '{"errorMessage":"too many requests"}')

    @staticmethod
    def _precision_error():
        from trading.executor import T212HTTPError
        return T212HTTPError(
            400,
            '{"type":"/api-errors/quantity-precision-mismatch",'
            '"detail":"invalid quantity precision 2"}')

    def _patched(self, post):
        import trading.executor as ex
        from config.settings import cfg
        return (patch.object(ex, "_post", post),
                patch.object(ex, "calculate_quantity", return_value=(3.456, None)),
                patch.object(ex.time, "sleep"),
                patch.multiple(cfg, entry_limit_enabled=True,
                               entry_limit_slack_pct=0.4, stop_loss_pct=2.0,
                               extended_size_factor=0.5))

    def test_the_exact_LB_failure_now_recovers(self):
        # precision mismatch -> retry -> 429 -> retry again -> filled.
        import trading.executor as ex
        post = MagicMock(side_effect=[
            self._precision_error(), self._rate_limited(), {"id": "ok"}])
        a, b, c, d = self._patched(post)
        with a, b, c, d, patch.object(ex, "_fetch_fill",
                                      return_value={"fillPrice": 100.2}):
            res = ex.buy("LB_US_EQ", 100.0)
        assert res.success is True, "a 429 on the precision retry must not be terminal"
        assert post.call_count == 3

    def test_a_rate_limit_on_the_FIRST_attempt_still_recovers(self):
        import trading.executor as ex
        post = MagicMock(side_effect=[self._rate_limited(), {"id": "ok"}])
        a, b, c, d = self._patched(post)
        with a, b, c, d, patch.object(ex, "_fetch_fill",
                                      return_value={"fillPrice": 100.2}):
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is True and post.call_count == 2

    def test_a_persistent_rate_limit_still_fails_cleanly(self):
        # Retrying forever would be worse than failing — the signal goes back
        # to the queue either way.
        import trading.executor as ex
        post = MagicMock(side_effect=[
            self._precision_error(), self._rate_limited(), self._rate_limited()])
        a, b, c, d = self._patched(post)
        with a, b, c, d:
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is False
        assert "429" in (res.error or "")

    def test_a_non_retryable_error_is_not_retried(self):
        # 401/403 will not fix themselves; burning a second order request on
        # them is how you reach a rate limit in the first place.
        import trading.executor as ex
        from trading.executor import T212HTTPError
        post = MagicMock(side_effect=T212HTTPError(401, "unauthorized"))
        a, b, c, d = self._patched(post)
        with a, b, c, d:
            res = ex.buy("ACME_US_EQ", 100.0)
        assert res.success is False
        assert post.call_count == 1

    def test_both_order_sites_share_one_retry_implementation(self):
        # The bug was an ASYMMETRY: the first POST retried, the precision retry
        # did not. Two copies is how that returns.
        import inspect
        import trading.executor as ex
        src = inspect.getsource(ex.buy)
        assert src.count("_post_order_with_retry") == 2
        assert "_post(endpoint, payload)" not in src, (
            "a bare _post inside buy() bypasses the shared retry"
        )
