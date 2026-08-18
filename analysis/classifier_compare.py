"""
Claude vs Qwen assessment, from live shadow-mode data.

Reads what shadow mode has collected in production — no API calls, no cost, no
side effects. Pure reporting over two tables:

    classifier_calls  one row per API call, BOTH providers  → latency, liveness
    qwen_scores       one row per article Qwen classified   → prediction quality
                      (joined to sentiment_scores on article_id, which already
                       carries the measured forward returns)

USAGE
    python -m analysis.classifier_compare
    python -m analysis.classifier_compare --days 14
    python -m analysis.classifier_compare --json report.json

THE THREE QUESTIONS, AND WHY EACH IS MEASURED THIS WAY
------------------------------------------------------
LATENCY — reported as p50 and p95, not mean. The news cycle is 60s and the mean
hides exactly the tail that would blow it. p95 is the number that decides
whether a provider is usable.

LIVENESS — reported as success rate AND the longest consecutive failure streak.
The streak is the more important figure: Claude's 2026-08-06 outage was 58
consecutive dead cycles, which is a catastrophic 98-minute blind spot, yet it
barely moves a whole-month success rate. A provider that fails 2% of calls at
random is fine; one that fails 2% in a single unbroken run is not.

PREDICTION — agreement is reported but is NOT the deciding metric. A model can
agree with Claude 90% of the time and still differ on precisely the
fda_approval/guidance_raise calls that TRADEABLE_CATALYSTS acts on. The
deciding metric is the forward return of each model's OWN tradeable set: a
cheaper model that picks a different but equally predictive set is a perfectly
good fallback, while one that agrees broadly and misses the tradeable calls is
not. The disagreement panel shows who was right when they differed.
"""

import argparse
import json
import logging
import statistics
import sys
from collections import Counter

from config.settings import cfg
from storage.database import get_conn

logger = logging.getLogger(__name__)

_TRADEABLE = tuple(sorted({c.strip() for c in cfg.tradeable_catalysts if c.strip()}))
_HORIZONS = ("5m", "60m", "120m", "eod")

# Below this, differences are noise. Stated explicitly so the report can refuse
# to draw a conclusion rather than inviting one from a handful of rows.
_MIN_CALLS_FOR_VERDICT = 200
_MIN_PAIRS_FOR_VERDICT = 300

# error_types caused by OUR request, not by the provider. Excluded from the
# liveness numbers (success rate, failure streak, latency) and reported
# separately, so a sizing bug on our side can never be read as the provider
# being unreliable — the question this tool exists to answer is whether the
# CHALLENGER can be trusted, and a truncated call says nothing about that.
#   truncated          — our max_tokens cut the answer off mid-serialisation
#   bad_shape          — a completion we could not use; still ours to bound
#   client_unavailable — our config (missing key, bad base URL, no `openai`)
#   dropped_backlog    — our bounded queue shed the batch, no call was made
_OUR_FAULT = frozenset({
    "truncated", "bad_shape", "client_unavailable", "dropped_backlog",
})


def _rows(sql: str, params=()) -> list[dict]:
    # get_conn() sets cursor_factory = RealDictCursor, so fetchall() already
    # yields dict-like rows. Re-zipping them against cur.description would
    # iterate each row's KEYS and map every column to its own name — silently
    # producing {'provider': 'provider', ...} for every row.
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _would_trade(sentiment, confidence, catalyst, already_moved, magnitude) -> bool:
    """
    Reproduce the four live trade gates from news/fetcher.py Step 3.

    This is the only comparison that maps to a real decision, so it has to
    match production exactly rather than approximately:

      * confidence is compared as `round(conf * 10) >= min_sentiment_confidence`,
        NOT `conf >= min/10`. With the deployed threshold of 7 those differ for
        every confidence in [0.65, 0.70) — the model says 0.68, production
        rounds to 7 and TRADES, a naive float compare says no.
      * the magnitude floor (Gate 4) is a real gate, not an optional extra;
        omitting it counts signals production would have dropped.
    """
    if (sentiment or "").lower() != "positive":
        return False
    if catalyst not in _TRADEABLE:
        return False
    if already_moved:
        return False
    try:
        if round(float(confidence or 0) * 10) < cfg.min_sentiment_confidence:
            return False
    except (TypeError, ValueError):
        return False
    # A NULL magnitude cannot clear a floor > 0. Live rows always carry one;
    # a shadow row can be NULL only if the model omitted it, which is a miss.
    if magnitude is None:
        return cfg.min_catalyst_magnitude <= 0
    try:
        return int(magnitude) >= cfg.min_catalyst_magnitude
    except (TypeError, ValueError):
        return False


def _pct(values: list[float], q: float):
    if not values:
        return None
    s = sorted(values)
    idx = min(int(round(q * (len(s) - 1))), len(s) - 1)
    return s[idx]


def latency_and_liveness(days: int) -> dict:
    calls = _rows(
        """SELECT provider, model, called_at, latency_ms, ok, error_type,
                  batch_size, scored_count, tokens_in, tokens_out, tokens_cached
           FROM classifier_calls
           WHERE called_at >= to_char(now() - (%s || ' days')::interval,
                                      'YYYY-MM-DD"T"HH24:MI:SS')
           ORDER BY called_at""",
        (days,),
    )
    out = {}
    for provider in ("claude", "qwen"):
        allrows = [c for c in calls if c["provider"] == provider]
        if not allrows:
            out[provider] = {"calls": 0}
            continue
        # Liveness answers "can this provider be relied on?", so it must be
        # computed over the provider's OWN failures. Rows in _OUR_FAULT are
        # caused by our request parameters, not by the provider — counting them
        # charges our config bugs to the candidate's reliability record and
        # inflates worst_failure_streak, the figure this tool tells the reader
        # to weigh above everything else. That is precisely the accounting
        # error v21.14.1 removed on the Qwen side; leaving it on the Claude
        # side would just point the same bias the other way. They stay visible
        # in `our_fault` so a sizing bug is never hidden, only re-attributed.
        ours = [c for c in allrows if c["error_type"] in _OUR_FAULT]
        mine = [c for c in allrows if c["error_type"] not in _OUR_FAULT]
        if not mine:
            out[provider] = {"calls": 0, "our_fault": len(ours)}
            continue
        lat = [c["latency_ms"] for c in mine if c["latency_ms"] is not None]
        ok = [c for c in mine if c["ok"]]
        errors: dict[str, int] = {}
        streak = worst_streak = 0
        for c in mine:
            if c["ok"]:
                streak = 0
            else:
                streak += 1
                worst_streak = max(worst_streak, streak)
                errors[c["error_type"] or "unknown"] = \
                    errors.get(c["error_type"] or "unknown", 0) + 1
        toks = lambda k: sum(c[k] or 0 for c in mine)     # noqa: E731
        out[provider] = {
            "calls": len(mine),
            "model": mine[-1]["model"],
            "success_rate": len(ok) / len(mine),
            "worst_failure_streak": worst_streak,
            # Not a provider failure — see _OUR_FAULT. Surfaced so it is
            # re-attributed rather than hidden.
            "our_fault": len(ours),
            "our_fault_errors": dict(sorted(
                Counter(c["error_type"] for c in ours).items(),
                key=lambda kv: -kv[1],
            )),
            "errors": dict(sorted(errors.items(), key=lambda kv: -kv[1])),
            "latency_ms": {
                "p50": _pct(lat, 0.50), "p95": _pct(lat, 0.95),
                "max": max(lat) if lat else None,
                "mean": round(statistics.fmean(lat)) if lat else None,
            },
            "articles_scored": sum(c["scored_count"] or 0 for c in mine),
            "tokens": {"in": toks("tokens_in"), "out": toks("tokens_out"),
                       "cached": toks("tokens_cached")},
        }
    return out


def prediction(days: int) -> dict:
    """
    Join the two models on article_id and compare.

    Forward returns come from sentiment_scores only — they are a property of the
    ticker and timestamp, not of the model that classified the article, so both
    models are scored against the identical outcome data.

    The join is deliberately INNER. Shadow dispatch happens before the Claude
    cooldown check, so during a Claude outage Qwen writes `qwen_scores` rows for
    articles that have no `sentiment_scores` row at all — there is no Claude
    verdict to compare them against, and (because articles age out of the 3-min
    freshness window long before a 30-min billing cooldown lifts) there never
    will be. Excluding them from the PREDICTION panel is correct: a comparison
    needs both answers. Those cycles are not lost — they are what the LIVENESS
    panel measures, and that is where a Claude outage is supposed to show up.
    """
    # DISTINCT ON (article_id) is load-bearing, not tidiness.
    #
    # sentiment_scores holds one row per (article, TICKER) — the same Claude
    # classification fanned out across up to 3 tagged tickers — while
    # qwen_scores is UNIQUE per ARTICLE. A plain join therefore emits one pair
    # per ticker, so a 3-ticker article would contribute its single
    # classification THREE times and triple-weight itself in every agreement
    # percentage and forward-return mean below. Both models classify per
    # article, so the comparison must be per article too.
    #
    # Which ticker's row is kept matters, so it is NOT chosen alphabetically.
    # forward_returns.py measures per (article, ticker) ROW, and yfinance
    # regularly has no bars for one leg of a multi-ticker article — so an
    # alphabetical pick can land on a leg whose returns are all NULL and
    # silence an article that DID have a measured outcome. Rows with a measured
    # 60m return sort first; s.id breaks the remaining tie deterministically so
    # repeated runs give identical numbers.
    pairs = _rows(
        """SELECT DISTINCT ON (s.article_id)
                  s.article_id, s.ticker, s.headline,
                  s.sentiment       AS c_sent,  q.sentiment       AS q_sent,
                  s.catalyst_type   AS c_cat,   q.catalyst_type   AS q_cat,
                  s.already_moved   AS c_moved, q.already_moved   AS q_moved,
                  s.confidence      AS c_conf,  q.confidence      AS q_conf,
                  s.catalyst_magnitude AS c_mag, q.catalyst_magnitude AS q_mag,
                  s.fwd_return_5m, s.fwd_return_60m,
                  s.fwd_return_120m, s.fwd_return_eod
           FROM sentiment_scores s
           JOIN qwen_scores q ON q.article_id = s.article_id
           WHERE s.scored_at >= to_char(now() - (%s || ' days')::interval,
                                        'YYYY-MM-DD"T"HH24:MI:SS')
           ORDER BY s.article_id, (s.fwd_return_60m IS NULL), s.id""",
        (days,),
    )
    if not pairs:
        return {"pairs": 0}

    agree = {"catalyst_type": 0, "sentiment": 0, "already_moved": 0}
    would_trade = {"claude": 0, "qwen": 0, "both": 0}
    fwd = {"claude": {h: [] for h in _HORIZONS},
           "qwen": {h: [] for h in _HORIZONS},
           "claude_only": {h: [] for h in _HORIZONS},
           "qwen_only": {h: [] for h in _HORIZONS}}
    disagreements: dict[tuple, int] = {}

    for p in pairs:
        if p["c_cat"] == p["q_cat"]:
            agree["catalyst_type"] += 1
        else:
            key = (p["c_cat"], p["q_cat"])
            disagreements[key] = disagreements.get(key, 0) + 1
        if (p["c_sent"] or "").lower() == (p["q_sent"] or "").lower():
            agree["sentiment"] += 1
        if bool(p["c_moved"]) == bool(p["q_moved"]):
            agree["already_moved"] += 1

        c_trade = _would_trade(p["c_sent"], p["c_conf"], p["c_cat"],
                               p["c_moved"], p["c_mag"])
        q_trade = _would_trade(p["q_sent"], p["q_conf"], p["q_cat"],
                               p["q_moved"], p["q_mag"])
        would_trade["claude"] += c_trade
        would_trade["qwen"] += q_trade
        would_trade["both"] += (c_trade and q_trade)

        for h in _HORIZONS:
            v = p[f"fwd_return_{h}"]
            if v is None:
                continue
            v = float(v)
            if c_trade:
                fwd["claude"][h].append(v)
            if q_trade:
                fwd["qwen"][h].append(v)
            if c_trade and not q_trade:
                fwd["claude_only"][h].append(v)
            if q_trade and not c_trade:
                fwd["qwen_only"][h].append(v)

    n = len(pairs)
    return {
        "pairs": n,
        "agreement": {k: v / n for k, v in agree.items()},
        "would_trade": would_trade,
        "top_disagreements": sorted(disagreements.items(),
                                    key=lambda kv: -kv[1])[:10],
        "fwd": {k: {h: {"n": len(v),
                        "mean": statistics.fmean(v) if v else None}
                    for h, v in hs.items()}
                for k, hs in fwd.items()},
    }


def _fmt_fwd(block: dict) -> str:
    parts = []
    for h in _HORIZONS:
        d = block[h]
        parts.append(f"{h}={d['mean']:+.2f}%(n={d['n']})" if d["mean"] is not None
                     else f"{h}=—")
    return "  ".join(parts)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="Claude vs Qwen, from shadow data")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    ll = latency_and_liveness(args.days)
    pred = prediction(args.days)

    print(f"\n{'=' * 72}")
    print(f"CLASSIFIER ASSESSMENT — last {args.days} days")
    print("=" * 72)

    print("\nLATENCY & LIVENESS")
    print(f"  {'provider':9} {'calls':>7} {'ok%':>7} {'p50':>8} {'p95':>8} "
          f"{'max':>8} {'worst streak':>13}")
    for prov in ("claude", "qwen"):
        d = ll.get(prov) or {}
        if not d.get("calls"):
            print(f"  {prov:9} {'—':>7}  (no data yet)")
            continue
        lat = d["latency_ms"]
        f = lambda v: f"{v:,}ms" if v is not None else "—"   # noqa: E731
        print(f"  {prov:9} {d['calls']:>7,} {d['success_rate']:>6.1%} "
              f"{f(lat['p50']):>8} {f(lat['p95']):>8} {f(lat['max']):>8} "
              f"{d['worst_failure_streak']:>13,}")
    for prov in ("claude", "qwen"):
        errs = (ll.get(prov) or {}).get("errors") or {}
        if errs:
            print(f"    {prov} failures: " +
                  ", ".join(f"{k}×{v}" for k, v in errs.items()))
    for prov in ("claude", "qwen"):
        ours = (ll.get(prov) or {}).get("our_fault_errors") or {}
        if ours:
            print(f"    {prov} OUR-FAULT calls (excluded from ok% / streak "
                  f"above — our request, not the provider): " +
                  ", ".join(f"{k}×{v}" for k, v in ours.items()))

    print("\nCOST (tokens over the window)")
    for prov in ("claude", "qwen"):
        d = ll.get(prov) or {}
        if not d.get("calls"):
            continue
        t = d["tokens"]
        # tokens_in means different things per provider, so the denominator
        # must too. Anthropic's usage.input_tokens is the UNCACHED remainder
        # (cache reads are reported separately), while OpenAI's prompt_tokens
        # is the TOTAL prompt with cached_tokens as a subset of it. Dividing
        # cached/in uniformly produced hit rates above 100% for Claude — the
        # ~2.3k-token rubric over a few hundred uncached tokens reads as 575%.
        total_in = t["in"] + t["cached"] if prov == "claude" else t["in"]
        hit = f"{t['cached'] / total_in:.0%}" if total_in else "—"
        print(f"  {prov:9} prompt={total_in:>10,}  out={t['out']:>9,}  "
              f"cached={t['cached']:>10,} ({hit} hit)")

    print("\nPREDICTION")
    if not pred.get("pairs"):
        print("  no overlapping classifications yet")
    else:
        a, wt = pred["agreement"], pred["would_trade"]
        print(f"  {pred['pairs']:,} articles classified by both")
        print(f"    catalyst_type agreement : {a['catalyst_type']:.1%}")
        print(f"    sentiment agreement     : {a['sentiment']:.1%}")
        print(f"    already_moved agreement : {a['already_moved']:.1%}")
        print(f"\n  Would have traded:  claude={wt['claude']}  "
              f"qwen={wt['qwen']}  both={wt['both']}")
        if pred["top_disagreements"]:
            print("\n  Top catalyst disagreements (claude → qwen):")
            for (c, q), k in pred["top_disagreements"]:
                print(f"    {str(c):20} → {str(q):20} ×{k}")
        print("\n  Forward returns of each model's tradeable set "
              "— THE DECIDING METRIC:")
        for k in ("claude", "qwen"):
            print(f"    {k:12} {_fmt_fwd(pred['fwd'][k])}")
        print("\n  Where they disagreed (who was right):")
        for k in ("claude_only", "qwen_only"):
            print(f"    {k:12} {_fmt_fwd(pred['fwd'][k])}")

    # Refuse to imply a verdict the sample cannot support.
    print("\nREADINESS")
    calls = min((ll.get(p) or {}).get("calls", 0) for p in ("claude", "qwen"))
    pairs = pred.get("pairs", 0)
    ready = calls >= _MIN_CALLS_FOR_VERDICT and pairs >= _MIN_PAIRS_FOR_VERDICT
    print(f"  calls/provider {calls:,}/{_MIN_CALLS_FOR_VERDICT:,}   "
          f"paired articles {pairs:,}/{_MIN_PAIRS_FOR_VERDICT:,}")
    print("  → " + ("ENOUGH DATA to judge." if ready else
                    "NOT ENOUGH DATA yet — keep collecting."))
    print("  Judge on the forward-return line, not on agreement: a cheaper model")
    print("  that picks a DIFFERENT but equally predictive set is a fine")
    print("  fallback; one that agrees broadly but misses the tradeable calls")
    print("  is not. And weigh worst-failure-streak above success rate — a")
    print("  98-minute unbroken outage barely moves a monthly average.\n")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"latency_liveness": ll, "prediction": pred},
                      fh, indent=2, default=str)
        print(f"  Wrote {args.json}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
