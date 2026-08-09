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

from config.settings import cfg
from storage.database import get_conn

logger = logging.getLogger(__name__)

_TRADEABLE = tuple(sorted({c.strip() for c in cfg.tradeable_catalysts if c.strip()}))
_HORIZONS = ("5m", "60m", "120m", "eod")

# Below this, differences are noise. Stated explicitly so the report can refuse
# to draw a conclusion rather than inviting one from a handful of rows.
_MIN_CALLS_FOR_VERDICT = 200
_MIN_PAIRS_FOR_VERDICT = 300


def _rows(sql: str, params=()) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


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
        mine = [c for c in calls if c["provider"] == provider]
        if not mine:
            out[provider] = {"calls": 0}
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
    """
    placeholders = ",".join(["%s"] * len(_TRADEABLE))
    pairs = _rows(
        f"""SELECT s.article_id, s.ticker, s.headline,
                   s.sentiment       AS c_sent,  q.sentiment       AS q_sent,
                   s.catalyst_type   AS c_cat,   q.catalyst_type   AS q_cat,
                   s.already_moved   AS c_moved, q.already_moved   AS q_moved,
                   s.confidence      AS c_conf,  q.confidence      AS q_conf,
                   s.fwd_return_5m, s.fwd_return_60m,
                   s.fwd_return_120m, s.fwd_return_eod
            FROM sentiment_scores s
            JOIN qwen_scores q ON q.article_id = s.article_id
            WHERE s.scored_at >= to_char(now() - (%s || ' days')::interval,
                                         'YYYY-MM-DD"T"HH24:MI:SS')""",
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

        # "Would this have been traded?" — the only comparison that maps to a
        # real decision. Mirrors the live gates in news/fetcher.py.
        c_trade = (p["c_cat"] in _TRADEABLE and p["c_sent"] == "positive"
                   and not p["c_moved"] and (p["c_conf"] or 0) >= cfg.min_sentiment_confidence / 10)
        q_trade = (p["q_cat"] in _TRADEABLE and p["q_sent"] == "positive"
                   and not p["q_moved"] and (p["q_conf"] or 0) >= cfg.min_sentiment_confidence / 10)
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

    print("\nCOST (tokens over the window)")
    for prov in ("claude", "qwen"):
        d = ll.get(prov) or {}
        if not d.get("calls"):
            continue
        t = d["tokens"]
        hit = f"{t['cached'] / t['in']:.0%}" if t["in"] else "—"
        print(f"  {prov:9} in={t['in']:>10,}  out={t['out']:>9,}  "
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
