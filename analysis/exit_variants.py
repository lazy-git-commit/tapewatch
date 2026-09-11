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
Exit-rule variants — would a DIFFERENT exit have kept more of what we won?

The question this answers
--------------------------
`analysis.triple_barrier` asks "did THIS exit rule survive to collect the
move?". This module asks a narrower follow-on: for the trades that survived,
did the fixed TP/time-stop cut them short?

It exists because a 2026-09-11 review of 21 recent trades, using 5-day
post-entry closing prices (coarse — see Limitations below), found a real
split by exit reason:

  * stop_loss exits were mostly justified — the price rarely recovered to
    breakeven afterward.
  * time_stop exits left money on the table in 5 of 8 cases (FSS: exited
    +0.73%, five-day peak +11.60%; SBRA, SCHW, MDT, PLD similar).
  * the one take_profit case that ran further (GRMN: hit +5%, peaked +10.16%
    five days later) suggests the fixed target itself may be too conservative
    on a genuinely strong mover.

The live system's breakeven ratchet (`RATCHET_TRIGGER_PCT`) already reacts to
a winning trade, but it is SINGLE-SHOT: it arms once at +2%, locks a stop near
breakeven, and never moves again no matter how far price continues. A trade
that clears +2% keeps its downside protection but gains no further upside
mechanism — it just rides the fixed clock to `time_stop`.

Three variants, each targeting one specific pattern above
-----------------------------------------------------------
  trailing        No fixed take-profit. The ordinary stop-loss protects the
                  trade until price clears `--trail-trigger-pct` (defaults to
                  the live ratchet's own 2.0), at which point the stop starts
                  trailing `--trail-pct` behind the running peak instead of
                  freezing. Targets FSS/SBRA/MDT: a time_stop cut them while
                  they were still climbing.

  partial         The fixed TP is unchanged for `--partial-fraction` of the
                  position; sell that fraction there, exactly as today. The
                  remainder immediately begins trailing from the TP price
                  under the same trail mechanism as `trailing`. Before the TP
                  is reached, behaviour is IDENTICAL to the baseline — this
                  isolates "what if we only let PART of the position run"
                  from every other change. Targets GRMN.

  momentum_extend Stop-loss and take-profit are unchanged. Only the fixed
                  120-minute clock changes: at the deadline, if the position
                  has made a new high within the last `--extend-lookback`
                  minutes (still trending, not just holding), the deadline
                  moves `--extend-minutes` further out, up to
                  `--max-extensions` times. A deadline reached while flat or
                  falling exits on schedule, unchanged from today. Targets
                  PLD/SCHW: a plateau shortly before a later move.

This is Step 1 only — evidence-gathering, not a proposal
-----------------------------------------------------------
Running this script does not tell you whether to change the live exit rules.
It produces per-trade returns for each variant, on the SAME entries the
system actually took, walked minute-by-minute exactly as
`analysis.triple_barrier` does. The next step is mandatory, not optional:

  1. Feed each variant's returns through `analysis.validation.walk_forward`
     so a parameter (trail_pct, partial_fraction, extend_minutes...) is never
     scored on the data that chose it.
  2. Correct with `analysis.validation.deflated_sharpe_ratio` for the number
     of variants AND parameter combinations tried. Three exit rules times a
     handful of parameters each is easily 20-50 "trials" — on 2026-08-18 this
     project shipped a change on the best of a search without that correction
     and had to retract it the next day once the search size was accounted
     for.

Do not read a good-looking OVERALL number from this script's output as a
reason to change `monitor/position_monitor.py`. It is a reason to run step 2.

Limitations (same spirit as triple_barrier.py — state them, don't hide them)
------------------------------------------------------------------------------
  * Same entries, different exit only. This does not model whether a
    different exit rule would change which signals get taken in the first
    place (a wider profit potential could in principle justify accepting
    signals the current gates reject) — that is a separate, larger question.
  * yfinance's ~30-day 1-minute retention window, same as triple_barrier.py.
  * Same-bar barrier ambiguity is resolved pessimistically throughout: a stop
    (fixed or trailing) checked against a bar's LOW takes priority over any
    favourable event the same bar's HIGH might suggest.
  * `partial`'s second leg is assumed to fill exactly at its modelled trail
    price — the same optimism this project's cost model exists to guard
    against for a single exit, doubled here across two legs. Read `partial`'s
    numbers as an upper bound more than the other two variants.
  * `momentum_extend`'s "still trending" test is a bar making a new high
    within its own lookback window — a deliberately simple proxy. A better
    one is step-2-or-later work, not needed to test the basic hypothesis.

Usage
-----
    python -m analysis.exit_variants --signals signals.csv
    python -m analysis.exit_variants --signals signals.csv --variant trailing --trail-pct 1.5
    python -m analysis.exit_variants --signals signals.csv --variant all --out variants.json

`--signals` uses the identical CSV contract as `analysis.triple_barrier`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import timedelta

from analysis.triple_barrier import (
    ET,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_STOP,
    _get_bars,
    _lazy_imports,
    _load_csv,
    _print_table,
    _result,
    _yahoo_symbol,
    label_one,
    summarise,
)

TRAIL_STOP = "trail_stop"

_DEFAULT_TRAIL_TRIGGER_PCT = 2.0   # matches the live RATCHET_TRIGGER_PCT default
_DEFAULT_TRAIL_PCT = 1.5
_DEFAULT_PARTIAL_FRACTION = 0.5
_DEFAULT_EXTEND_MINUTES = 60
_DEFAULT_EXTEND_LOOKBACK_MINUTES = 15
_DEFAULT_MAX_EXTENSIONS = 2


# ── Shared entry setup ──────────────────────────────────────────────────────

def _entry_context(bars, entry_ts, hold_minutes):
    """
    Locate the entry bar and the post-entry walk window. Shared by every
    variant so the "which bar do we enter on, which bars do we walk" logic
    exists exactly once — mirrors `label_one`'s own setup so a baseline and a
    variant run on the identical entry price and window.

    Returns None exactly when `label_one` would (no tradeable bar at/after
    `entry_ts`), so callers use the same "signal could not be labelled" path.
    """
    after = bars[bars.index >= entry_ts]
    if after.empty:
        return None
    entry_bar = after.iloc[0]
    entry_price = float(entry_bar["Close"])
    if not math.isfinite(entry_price) or entry_price <= 0:
        return None
    entry_time = after.index[0]
    deadline = entry_time + timedelta(minutes=hold_minutes)
    window = after[after.index <= deadline]
    # Skip the entry bar: we entered at its close, so its high/low already
    # happened (same reasoning as triple_barrier.label_one).
    walk = list(window.iterrows())[1:]
    return entry_price, entry_time, window, walk


def _time_stop_fallback(entry_price, entry_time, window, cost_pct) -> dict:
    """What every variant falls back to when neither barrier fires before the
    (possibly extended) deadline. Shared so the fallback shape can't drift
    between variants — see the v21.14.1 lesson on duplicated formulas."""
    last = window.iloc[-1] if not window.empty else None
    exit_ts = window.index[-1] if not window.empty else entry_time
    exit_price = float(last["Close"]) if last is not None else entry_price
    return _result(TIME_STOP, entry_price, exit_price, entry_time, exit_ts, cost_pct)


# ── Variant: continuous trailing stop ───────────────────────────────────────

def label_trailing(bars, entry_ts, sl_pct: float, hold_minutes: int,
                    cost_pct: float, trigger_pct: float, trail_pct: float) -> dict | None:
    """
    No fixed take-profit. The fixed stop-loss protects the trade until price
    clears `trigger_pct`; from that point the stop trails `trail_pct` behind
    the running peak, updated every bar, instead of freezing at one level.

    Same-bar conservatism, twice over: (1) a bar whose low breaches the still
    -active fixed stop is checked BEFORE that bar's high is allowed to arm the
    trail — a bar cannot simultaneously stop us out on the low and promote us
    on the high; (2) the bar that arms the trail is immediately checked against
    the trail stop it just set, using that SAME bar's low — if price spiked
    and fully reverted within one bar, that counts as stopped, not as a clean
    arm.
    """
    ctx = _entry_context(bars, entry_ts, hold_minutes)
    if ctx is None:
        return None
    entry_price, entry_time, window, walk = ctx

    sl_price = entry_price * (1 - sl_pct / 100.0)
    trigger_price = entry_price * (1 + trigger_pct / 100.0)
    armed = False
    peak = entry_price
    trail_stop = None

    for ts, bar in walk:
        low, high = float(bar["Low"]), float(bar["High"])
        if not armed:
            if low <= sl_price:
                return _result(STOP_LOSS, entry_price, sl_price, entry_time, ts, cost_pct)
            if high >= trigger_price:
                armed = True
                peak = high
                trail_stop = peak * (1 - trail_pct / 100.0)
                if low <= trail_stop:
                    return _result(TRAIL_STOP, entry_price, trail_stop, entry_time, ts, cost_pct)
            continue
        if low <= trail_stop:
            return _result(TRAIL_STOP, entry_price, trail_stop, entry_time, ts, cost_pct)
        if high > peak:
            peak = high
            new_trail_stop = peak * (1 - trail_pct / 100.0)
            if low <= new_trail_stop:
                # Same bar both raised the peak AND its low would breach the
                # newly-tightened stop. We cannot see whether the high or the
                # low printed first, so — same discipline as arming — assume
                # the stop filled rather than assuming the peak was safely
                # banked before the drop.
                return _result(TRAIL_STOP, entry_price, new_trail_stop, entry_time, ts, cost_pct)
            trail_stop = new_trail_stop

    return _time_stop_fallback(entry_price, entry_time, window, cost_pct)


# ── Variant: partial profit-taking ──────────────────────────────────────────

def label_partial(bars, entry_ts, tp_pct: float, sl_pct: float, hold_minutes: int,
                   cost_pct: float, partial_fraction: float, trail_pct: float) -> dict | None:
    """
    Identical to the baseline until (and unless) the fixed TP is reached: the
    stop-loss is checked the same way, on the same schedule. Once the TP
    fires, `partial_fraction` of the position is realised there — exactly the
    baseline's take_profit fill — and the remainder immediately begins
    trailing from the TP price, using the same mechanism as `trailing`.

    Cost note: `cost_pct` is a PERCENTAGE of position value (FX conversion +
    slippage), so charging it once against the size-weighted blended return
    is equivalent to charging each leg its own cost against its own size —
    the algebra collapses because the fractions sum to 1. This is NOT a
    simplification that under-charges the trade; see the module docstring for
    the actual simplification this variant makes (the second leg's fill).
    """
    ctx = _entry_context(bars, entry_ts, hold_minutes)
    if ctx is None:
        return None
    entry_price, entry_time, window, walk = ctx

    sl_price = entry_price * (1 - sl_pct / 100.0)
    tp_price = entry_price * (1 + tp_pct / 100.0)

    for i, (ts, bar) in enumerate(walk):
        low, high = float(bar["Low"]), float(bar["High"])
        if low <= sl_price:
            # Stopped before any partial was taken — both legs exit together,
            # identical to the baseline.
            return _result(STOP_LOSS, entry_price, sl_price, entry_time, ts, cost_pct)
        if high >= tp_price:
            return _partial_result(entry_price, tp_price, entry_time, ts, window,
                                    walk[i + 1:], trail_pct, partial_fraction, cost_pct)

    return _time_stop_fallback(entry_price, entry_time, window, cost_pct)


def _partial_result(entry_price, tp_price, entry_time, tp_ts, window, remaining_walk,
                     trail_pct, fraction, cost_pct) -> dict:
    """The remainder's own mini-walk, trailing from the TP price as its
    initial peak. Falls back to time_stop at the last available bar (or
    immediately at the TP price, if the TP bar was itself the last bar in the
    window) exactly as the full-position walk would."""
    peak = tp_price
    trail_stop = peak * (1 - trail_pct / 100.0)
    leg2_price, leg2_ts, leg2_reason = tp_price, tp_ts, TIME_STOP

    for ts, bar in remaining_walk:
        low, high = float(bar["Low"]), float(bar["High"])
        if low <= trail_stop:
            leg2_price, leg2_ts, leg2_reason = trail_stop, ts, TRAIL_STOP
            break
        if high > peak:
            peak = high
            new_trail_stop = peak * (1 - trail_pct / 100.0)
            if low <= new_trail_stop:
                # Same conservatism as label_trailing — see its comment.
                leg2_price, leg2_ts, leg2_reason = new_trail_stop, ts, TRAIL_STOP
                break
            trail_stop = new_trail_stop
    else:
        if remaining_walk:
            last_ts, last_bar = remaining_walk[-1]
            leg2_price, leg2_ts, leg2_reason = float(last_bar["Close"]), last_ts, TIME_STOP

    leg1_gross = (tp_price - entry_price) / entry_price * 100.0
    leg2_gross = (leg2_price - entry_price) / entry_price * 100.0
    blended_gross = fraction * leg1_gross + (1 - fraction) * leg2_gross
    return {
        "exit_reason": f"partial_tp+{leg2_reason}",
        "entry_price": round(entry_price, 4),
        "exit_price": round(leg2_price, 4),
        "leg1_exit_pct": round(leg1_gross, 4),
        "leg2_exit_pct": round(leg2_gross, 4),
        "leg2_exit_reason": leg2_reason,
        "partial_fraction": fraction,
        "gross_pct": round(blended_gross, 4),
        "net_pct": round(blended_gross - cost_pct, 4),
        "label": 1 if blended_gross > 0 else (-1 if blended_gross < 0 else 0),
        "held_minutes": int((leg2_ts - entry_time).total_seconds() // 60),
    }


# ── Variant: momentum-conditional time-stop extension ───────────────────────

def label_momentum_extend(bars, entry_ts, tp_pct: float, sl_pct: float, hold_minutes: int,
                          cost_pct: float, extend_minutes: int,
                          extend_lookback_minutes: int, max_extensions: int) -> dict | None:
    """
    Stop-loss and take-profit are checked every bar, unchanged, for the ENTIRE
    possible window (including any extension) — a variant that changes when we
    give up must never also change what protects the trade while we wait.

    At the original deadline (and at each extended deadline after), the
    position gets one more `extend_minutes` ONLY if the current bar's high is
    at or above the highest high of the preceding `extend_lookback_minutes` —
    i.e. it is making a new local high right at the point we would otherwise
    cut it, not merely still above water. A deadline reached while flat or
    retreating exits immediately, identical to the baseline.
    """
    after = bars[bars.index >= entry_ts]
    if after.empty:
        return None
    entry_bar = after.iloc[0]
    entry_price = float(entry_bar["Close"])
    if not math.isfinite(entry_price) or entry_price <= 0:
        return None
    entry_time = after.index[0]

    sl_price = entry_price * (1 - sl_pct / 100.0)
    tp_price = entry_price * (1 + tp_pct / 100.0)
    current_deadline = entry_time + timedelta(minutes=hold_minutes)
    max_deadline = entry_time + timedelta(minutes=hold_minutes + max_extensions * extend_minutes)

    full_window = after[after.index <= max_deadline]
    walk = list(full_window.iterrows())[1:]
    extensions_used = 0

    for ts, bar in walk:
        low, high = float(bar["Low"]), float(bar["High"])
        if low <= sl_price:
            r = _result(STOP_LOSS, entry_price, sl_price, entry_time, ts, cost_pct)
            r["extensions_used"] = extensions_used
            return r
        if high >= tp_price:
            r = _result(TAKE_PROFIT, entry_price, tp_price, entry_time, ts, cost_pct)
            r["extensions_used"] = extensions_used
            return r
        if ts >= current_deadline:
            if extensions_used < max_extensions:
                lookback_start = ts - timedelta(minutes=extend_lookback_minutes)
                # Strictly BEFORE ts: comparing this bar's high against a
                # window that includes itself makes "new high" nearly always
                # true (the bar always ties its own candidacy for the max).
                # Strict `>`, not `>=`: a tied high is a flat tape, not a
                # trend, and a flat tape is exactly the case that should exit
                # on schedule.
                recent = full_window[(full_window.index > lookback_start) & (full_window.index < ts)]
                still_trending = not recent.empty and high > float(recent["High"].max())
                if still_trending:
                    extensions_used += 1
                    current_deadline = ts + timedelta(minutes=extend_minutes)
                    continue
            r = _result(TIME_STOP, entry_price, float(bar["Close"]), entry_time, ts, cost_pct)
            r["extensions_used"] = extensions_used
            return r

    # Ran off the end of the fetched window (max_deadline extends past what
    # the day's bars cover) without any deadline check resolving — exit at
    # the last bar seen, same fallback shape as every other variant.
    r = _time_stop_fallback(entry_price, entry_time, full_window, cost_pct)
    r["extensions_used"] = extensions_used
    return r


# ── Dispatch + reporting ─────────────────────────────────────────────────────

_VARIANTS = ("baseline", "trailing", "partial", "momentum_extend")


def _label_one_variant(variant: str, bars, entry_ts, args) -> dict | None:
    if variant == "baseline":
        return label_one(bars, entry_ts, args.tp, args.sl, args.hold, args.cost_pct)
    if variant == "trailing":
        return label_trailing(bars, entry_ts, args.sl, args.hold, args.cost_pct,
                              args.trail_trigger_pct, args.trail_pct)
    if variant == "partial":
        return label_partial(bars, entry_ts, args.tp, args.sl, args.hold, args.cost_pct,
                             args.partial_fraction, args.trail_pct)
    if variant == "momentum_extend":
        return label_momentum_extend(bars, entry_ts, args.tp, args.sl, args.hold, args.cost_pct,
                                     args.extend_minutes, args.extend_lookback, args.max_extensions)
    raise ValueError(f"unknown variant: {variant}")


def label_signals_variants(rows: list[dict], variants: list[str], args,
                           progress: bool = True) -> dict[str, list[dict]]:
    """
    Label every signal under every requested variant, fetching each
    ticker-day's bars ONCE and reusing them across variants (the cache lives
    in `analysis.triple_barrier._get_bars`, imported directly rather than
    duplicated).
    """
    pd, yf = _lazy_imports()
    out: dict[str, list[dict]] = {v: [] for v in variants}
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

        for variant in variants:
            res = _label_one_variant(variant, bars, pub, args)
            if res is None:
                continue
            rec = dict(row)
            rec.update(res)
            rec["symbol"] = symbol
            out[variant].append(rec)

        if progress and i % 100 == 0:
            print(f"  ...{i}/{len(rows)} processed", file=sys.stderr)
    return out


def _print_comparison(results: dict[str, list[dict]]) -> None:
    print("\nOVERALL — baseline vs each variant, SAME entries")
    print("=" * 100)
    print(f"{'variant':<18}{'n':>5}{'net/trade':>11}{'win%':>7}{'t':>7}"
          f"{'stopped%':>10}{'target%':>9}{'timeout%':>10}{'trail%':>8}")
    print("-" * 100)
    for variant in _VARIANTS:
        rows = results.get(variant) or []
        s = summarise(rows)
        if not s.get("n"):
            print(f"{variant:<18}    0  (no labelled signals)")
            continue
        counts: dict[str, int] = defaultdict(int)
        for r in rows:
            counts[r["exit_reason"]] += 1
        n = s["n"]
        trail_pct = round(100.0 * sum(v for k, v in counts.items() if TRAIL_STOP in k) / n, 1)
        print(f"{variant:<18}{s['n']:>5}{s['net_mean']:>10.3f}%{s['win_rate']:>6.0f}%"
              f"{s['t']:>7.2f}{s['stopped_pct']:>9.0f}%{s['target_pct']:>8.0f}%"
              f"{s['timeout_pct']:>9.0f}%{trail_pct:>7.0f}%")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals", required=True, help="CSV: ticker,published_at,...")
    ap.add_argument("--variant", default="all", choices=(*_VARIANTS[1:], "all"),
                    help="which variant(s) to run against the baseline (default all)")
    ap.add_argument("--tp", type=float, default=5.0, help="baseline/partial take-profit %% (default 5)")
    ap.add_argument("--sl", type=float, default=2.0, help="stop-loss %% (default 2)")
    ap.add_argument("--hold", type=int, default=120, help="time stop, minutes (default 120)")
    ap.add_argument("--cost-pct", type=float, default=0.46, help="round-trip cost in pp (default 0.46)")
    ap.add_argument("--trail-trigger-pct", type=float, default=_DEFAULT_TRAIL_TRIGGER_PCT,
                    help=f"trailing: %% gain before the stop starts trailing (default {_DEFAULT_TRAIL_TRIGGER_PCT}, matches RATCHET_TRIGGER_PCT)")
    ap.add_argument("--trail-pct", type=float, default=_DEFAULT_TRAIL_PCT,
                    help=f"trailing/partial: distance held below the running peak once armed (default {_DEFAULT_TRAIL_PCT})")
    ap.add_argument("--partial-fraction", type=float, default=_DEFAULT_PARTIAL_FRACTION,
                    help=f"partial: fraction sold at the fixed TP (default {_DEFAULT_PARTIAL_FRACTION})")
    ap.add_argument("--extend-minutes", type=int, default=_DEFAULT_EXTEND_MINUTES,
                    help=f"momentum_extend: minutes added per extension (default {_DEFAULT_EXTEND_MINUTES})")
    ap.add_argument("--extend-lookback", type=int, default=_DEFAULT_EXTEND_LOOKBACK_MINUTES,
                    help=f"momentum_extend: minutes to look back for a new high (default {_DEFAULT_EXTEND_LOOKBACK_MINUTES})")
    ap.add_argument("--max-extensions", type=int, default=_DEFAULT_MAX_EXTENSIONS,
                    help=f"momentum_extend: maximum number of extensions (default {_DEFAULT_MAX_EXTENSIONS})")
    ap.add_argument("--group", default="catalyst_type", help="column to group results by (default catalyst_type)")
    ap.add_argument("--out", help="write ALL variants' labelled rows as JSON here")
    ap.add_argument("--limit", type=int, help="only label the first N signals")
    args = ap.parse_args(argv)

    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    rows = _load_csv(args.signals)
    if args.limit:
        rows = rows[: args.limit]
    variants = list(_VARIANTS[1:]) if args.variant == "all" else [args.variant]
    print(f"labelling {len(rows)} signals against variant(s): {', '.join(variants)}  "
          f"(baseline always included for comparison)", file=sys.stderr)

    results = label_signals_variants(rows, ["baseline"] + variants, args)
    if not results.get("baseline"):
        print("no signals could be labelled — check the date range is inside "
              "yfinance's ~30-day 1-minute window", file=sys.stderr)
        return 1

    _print_comparison(results)

    if args.group:
        for variant in ["baseline"] + variants:
            labelled = results[variant]
            if labelled and args.group in labelled[0]:
                groups: dict[str, list[dict]] = defaultdict(list)
                for r in labelled:
                    groups[str(r.get(args.group) or "?")].append(r)
                _print_table(f"{variant.upper()} — BY {args.group.upper()}", groups)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1)
        print(f"\nwrote {sum(len(v) for v in results.values())} labelled rows "
              f"across {len(results)} variant(s) to {args.out}", file=sys.stderr)

    print("\nThis is Step 1 evidence only. Before proposing any live change, run "
          "each variant's returns through analysis.validation.walk_forward and "
          "deflated_sharpe_ratio — see the module docstring.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
