"""
Triple-barrier labelling — what would a REAL trade have done?

The problem this solves
-----------------------
`sentiment_scores.fwd_return_*` answers "where was the price N minutes later?".
That is not the same question as "would a trade with our stop have survived to
collect it", and the difference has cost us money twice:

  * v20 kept `fda_approval` because it drifts +1.42%/60m. It does — but the
    path gets there through drawdowns that trip a 2% stop, so the tradeable
    return is negative (v21.16).
  * v20 dropped `product_launch` on a 15-minute reading while its edge only
    appears at 120 minutes.

Both errors have the same shape: a fixed-horizon return is a snapshot, and a
trade is a path. Lopez de Prado's triple-barrier method labels each signal by
which of three barriers is touched FIRST — take-profit, stop-loss, or the time
limit — which is exactly the question our exit rules ask.

Conservative by construction
----------------------------
Three choices all bias the result DOWN, so a strategy that looks good here has
cleared a deliberately high bar:

  * When a single 1-minute bar's range spans both barriers we cannot know the
    intrabar order, so we assume the STOP filled. Real fills are worse than
    optimistic assumptions far more often than the reverse.
  * Entry is the close of the bar at or after the signal, not the signal price
    itself — we cannot fill at a price we only observed.
  * Every completed trade is charged `--cost-pct` (default 0.46pp: 0.31pp of
    Trading 212 FX conversion plus 0.15pp of typical entry slippage).

Regular hours only. Pre-market bars carry no usable volume from our data
source, so a barrier "touched" out there is not evidence a trade could have
filled (see the 2026-08-19 pre-market study).

Usage
-----
    python -m analysis.triple_barrier --signals signals.csv --out labels.json
    python -m analysis.triple_barrier --signals signals.csv --tp 5 --sl 2 --hold 120
    python -m analysis.triple_barrier --signals signals.csv --group catalyst_type

`--signals` is a CSV with at least `ticker,published_at`; any other columns are
carried through so results can be grouped by them. Produce it with:

    psql -tAF',' -c "COPY (SELECT ticker, published_at, catalyst_type,
      confidence, catalyst_magnitude FROM sentiment_scores
      WHERE sentiment='positive' AND already_moved=0) TO STDOUT WITH CSV HEADER"
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from collections import defaultdict
from datetime import timedelta

logger = logging.getLogger(__name__)

ET = "America/New_York"

# Outcome labels. The names are the exit reasons the live monitor uses, so a
# label here maps directly onto a row in the trades table.
TAKE_PROFIT = "take_profit"
STOP_LOSS = "stop_loss"
TIME_STOP = "time_stop"


def _lazy_imports():
    """Import the heavy analysis deps only when actually running."""
    import pandas as pd
    import yfinance as yf
    return pd, yf


_bars_cache: dict[tuple[str, str], object] = {}


def _yahoo_symbol(ticker: str) -> str:
    """T212 instrument code or exchange symbol -> Yahoo symbol."""
    s = ticker.replace("_US_EQ", "")
    return s.replace(".", "-")          # MOG.A -> MOG-A


def _get_bars(symbol: str, day, pd, yf):
    """Regular-hours 1-min bars for one ticker-day, cached per process."""
    key = (symbol, day.strftime("%Y-%m-%d"))
    if key in _bars_cache:
        return _bars_cache[key]
    try:
        df = yf.Ticker(symbol).history(
            start=day.strftime("%Y-%m-%d"),
            end=(day + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
            prepost=False,      # see module docstring
            auto_adjust=False,
        )
        if df is None or df.empty:
            _bars_cache[key] = None
            return None
        idx = df.index
        df.index = (idx.tz_localize("UTC").tz_convert(ET)
                    if idx.tz is None else idx.tz_convert(ET))
        _bars_cache[key] = df
        return df
    except Exception as exc:
        logger.warning("bar fetch failed %s %s: %s", symbol, key[1], exc)
        _bars_cache[key] = None
        return None


def label_one(bars, entry_ts, tp_pct: float, sl_pct: float,
              hold_minutes: int, cost_pct: float) -> dict | None:
    """
    Walk one signal forward bar by bar and return which barrier it hit first.

    Returns None when there is no tradeable bar at or after `entry_ts` (the
    signal fired outside regular hours, or the day has no data).
    """
    after = bars[bars.index >= entry_ts]
    if after.empty:
        return None

    entry_bar = after.iloc[0]
    entry_price = float(entry_bar["Close"])
    if not math.isfinite(entry_price) or entry_price <= 0:
        return None

    tp_price = entry_price * (1 + tp_pct / 100.0)
    sl_price = entry_price * (1 - sl_pct / 100.0)
    deadline = after.index[0] + timedelta(minutes=hold_minutes)

    window = after[after.index <= deadline]
    # Skip the entry bar itself: we entered at its close, so its high/low
    # already happened. Counting them would fabricate instant exits.
    for ts, bar in list(window.iterrows())[1:]:
        low, high = float(bar["Low"]), float(bar["High"])
        hit_stop = low <= sl_price
        hit_target = high >= tp_price
        if hit_stop:
            # Both barriers inside one bar -> assume the stop. We cannot see
            # intrabar order and the pessimistic read is the honest one.
            return _result(STOP_LOSS, entry_price, sl_price, entry_ts, ts, cost_pct)
        if hit_target:
            return _result(TAKE_PROFIT, entry_price, tp_price, entry_ts, ts, cost_pct)

    last = window.iloc[-1] if not window.empty else entry_bar
    exit_ts = window.index[-1] if not window.empty else after.index[0]
    return _result(TIME_STOP, entry_price, float(last["Close"]),
                   entry_ts, exit_ts, cost_pct)


def _result(reason, entry_price, exit_price, entry_ts, exit_ts, cost_pct) -> dict:
    gross = (exit_price - entry_price) / entry_price * 100.0
    return {
        "exit_reason": reason,
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "gross_pct": round(gross, 4),
        "net_pct": round(gross - cost_pct, 4),
        "label": 1 if reason == TAKE_PROFIT else (-1 if reason == STOP_LOSS else 0),
        "held_minutes": int((exit_ts - entry_ts).total_seconds() // 60),
    }


def label_signals(rows: list[dict], tp_pct: float, sl_pct: float,
                  hold_minutes: int, cost_pct: float,
                  progress: bool = True) -> list[dict]:
    """Label every signal. Rows need `ticker` and `published_at`."""
    pd, yf = _lazy_imports()
    out: list[dict] = []
    for i, row in enumerate(rows, 1):
        ticker = (row.get("ticker") or "").strip()
        pub_raw = (row.get("published_at") or "").strip()
        if not ticker or not pub_raw:
            continue
        try:
            pub = pd.Timestamp(pub_raw)
            pub = pub.tz_localize("UTC") if pub.tz is None else pub
            pub = pub.tz_convert(ET)
        except Exception:
            continue

        symbol = _yahoo_symbol(ticker)
        bars = _get_bars(symbol, pub.normalize().to_pydatetime(), pd, yf)
        if bars is None:
            continue
        res = label_one(bars, pub, tp_pct, sl_pct, hold_minutes, cost_pct)
        if res is None:
            continue
        rec = dict(row)
        rec.update(res)
        rec["symbol"] = symbol
        out.append(rec)
        if progress and i % 100 == 0:
            print(f"  ...{i}/{len(rows)} processed, {len(out)} labelled",
                  file=sys.stderr)
    return out


# ── Summarising ───────────────────────────────────────────────────────────────

def summarise(labelled: list[dict]) -> dict:
    """Aggregate stats for one group of labelled signals."""
    n = len(labelled)
    if not n:
        return {"n": 0}
    nets = [r["net_pct"] for r in labelled]
    wins = [r for r in labelled if r["net_pct"] > 0]
    mean = sum(nets) / n
    var = sum((x - mean) ** 2 for x in nets) / n if n > 1 else 0.0
    sd = var ** 0.5
    counts: dict[str, int] = defaultdict(int)
    for r in labelled:
        counts[r["exit_reason"]] += 1
    return {
        "n": n,
        "net_mean": round(mean, 3),
        "net_median": round(sorted(nets)[n // 2], 3),
        "win_rate": round(100.0 * len(wins) / n, 1),
        "sd": round(sd, 3),
        # t against zero. |t| > 2 is the usual "unlikely to be chance" bar.
        "t": round(mean / (sd / (n ** 0.5)), 2) if sd > 0 and n > 1 else 0.0,
        "stopped_pct": round(100.0 * counts[STOP_LOSS] / n, 1),
        "target_pct": round(100.0 * counts[TAKE_PROFIT] / n, 1),
        "timeout_pct": round(100.0 * counts[TIME_STOP] / n, 1),
        "avg_hold_min": round(sum(r["held_minutes"] for r in labelled) / n, 1),
    }


def _print_table(title: str, groups: dict[str, list[dict]]) -> None:
    print(f"\n{title}")
    print("=" * 118)
    print(f"{'group':<26}{'n':>5}{'net/trade':>11}{'median':>9}{'win%':>7}"
          f"{'t':>7}{'stopped%':>10}{'target%':>9}{'timeout%':>10}{'hold':>7}")
    print("-" * 118)
    rows = [(k, summarise(v)) for k, v in groups.items()]
    rows.sort(key=lambda kv: kv[1].get("net_mean", -99), reverse=True)
    for name, s in rows:
        if not s.get("n"):
            continue
        print(f"{name[:25]:<26}{s['n']:>5}{s['net_mean']:>10.3f}%{s['net_median']:>8.2f}%"
              f"{s['win_rate']:>6.0f}%{s['t']:>7.2f}{s['stopped_pct']:>9.0f}%"
              f"{s['target_pct']:>8.0f}%{s['timeout_pct']:>9.0f}%{s['avg_hold_min']:>7.0f}")


def _load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals", required=True, help="CSV: ticker,published_at,...")
    ap.add_argument("--tp", type=float, default=5.0, help="take-profit %% (default 5)")
    ap.add_argument("--sl", type=float, default=2.0, help="stop-loss %% (default 2)")
    ap.add_argument("--hold", type=int, default=120, help="time stop, minutes (default 120)")
    ap.add_argument("--cost-pct", type=float, default=0.46,
                    help="round-trip cost in pp (default 0.46)")
    ap.add_argument("--group", default="catalyst_type",
                    help="column to group results by (default catalyst_type)")
    ap.add_argument("--out", help="write labelled rows as JSON here")
    ap.add_argument("--limit", type=int, help="only label the first N signals")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    rows = _load_csv(args.signals)
    if args.limit:
        rows = rows[: args.limit]
    print(f"labelling {len(rows)} signals  "
          f"(TP +{args.tp}% / SL -{args.sl}% / hold {args.hold}m / cost {args.cost_pct}pp)",
          file=sys.stderr)

    labelled = label_signals(rows, args.tp, args.sl, args.hold, args.cost_pct)
    if not labelled:
        print("no signals could be labelled — check the date range is inside "
              "yfinance's ~30-day 1-minute window", file=sys.stderr)
        return 1

    overall = summarise(labelled)
    print(f"\nlabelled {len(labelled)} of {len(rows)} signals")
    print(f"OVERALL  net {overall['net_mean']:+.3f}%/trade   win {overall['win_rate']:.0f}%   "
          f"t={overall['t']:.2f}   stopped {overall['stopped_pct']:.0f}%   "
          f"target {overall['target_pct']:.0f}%   timeout {overall['timeout_pct']:.0f}%")

    if args.group and args.group in labelled[0]:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in labelled:
            groups[str(r.get(args.group) or "?")].append(r)
        _print_table(f"BY {args.group.upper()}", groups)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(labelled, fh, indent=1)
        print(f"\nwrote {len(labelled)} labelled rows to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
