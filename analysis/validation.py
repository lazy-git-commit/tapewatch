"""
Validation guards — walk-forward evaluation and the deflated Sharpe ratio.

Why this module exists
----------------------
On 2026-08-18 this project concluded that `guidance_raise` earned +0.667% per
trade, and shipped a strategy change on it. On 2026-08-19 the same signals,
labelled by the path a real trade would have taken (`analysis.triple_barrier`),
came back at **-0.388%**. Nothing about the market changed in between. What
changed was the measurement:

  * the first number came from sampling the price at 5/15/60/120 minutes and
    inferring the stop-out rate (estimated 33%; the true rate was 48%);
  * it was the best of many variants examined, reported without any correction
    for how many had been tried.

Both errors are old and named. Sampling instead of walking the path is what the
triple-barrier method fixes. Reporting the winner of a search as though it were
a single hypothesis is what the deflated Sharpe ratio fixes. This module
supplies the second one, plus the walk-forward split that keeps a parameter
choice from being scored on the data that chose it.

Numpy only, by design: a live trading service should not grow scipy/sklearn so
an offline study can run. Everything here is small enough to be auditable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

# ── Normal distribution helpers (stdlib only) ─────────────────────────────────


def _norm_cdf(x: float) -> float:
    """P(Z <= x) for a standard normal."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float) -> float:
    """
    Inverse normal CDF (Acklam's rational approximation, |error| < 1.15e-9).

    Used only for the expected maximum of N draws in the deflated Sharpe ratio.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in (0, 1)")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


# ── Moments ───────────────────────────────────────────────────────────────────

def _moments(returns: Sequence[float]) -> tuple[float, float, float, float]:
    """(mean, stdev, skew, excess kurtosis) — population estimates."""
    n = len(returns)
    if n < 2:
        return 0.0, 0.0, 0.0, 0.0
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(m2)
    if sd <= 0:
        return mean, 0.0, 0.0, 0.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return mean, sd, m3 / sd ** 3, m4 / sd ** 4 - 3.0


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Per-trade Sharpe (mean / stdev). Not annualised — trades aren't calendar time."""
    mean, sd, _, _ = _moments(returns)
    return mean / sd if sd > 0 else 0.0


def expected_max_sharpe(n_trials: int, sharpe_variance: float = 1.0) -> float:
    """
    Expected maximum Sharpe from `n_trials` strategies that all truly have ZERO
    edge. This is the bar a search result has to clear to mean anything.

    Bailey & Lopez de Prado (2014), eq. for E[max]. The point it makes is
    blunt: try 50 variants of a worthless strategy and the best one will show a
    Sharpe near 2.3 standard errors above zero purely by luck.
    """
    if n_trials < 2:
        return 0.0
    gamma = 0.5772156649015329          # Euler-Mascheroni
    e = math.e
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * e))
    return math.sqrt(sharpe_variance) * ((1 - gamma) * z1 + gamma * z2)


def deflated_sharpe_ratio(returns: Sequence[float], n_trials: int) -> dict:
    """
    Probability that the observed Sharpe is real once you account for how many
    variants were tried, the sample length, and non-normal returns.

    `dsr` is a probability: 0.95 is the usual bar. Anything below ~0.5 means
    the result is more likely a product of the search than of an edge.

    Returns a dict so callers can report the components rather than a bare
    number — the components are what make the verdict legible.
    """
    n = len(returns)
    if n < 3:
        return {"n": n, "sharpe": 0.0, "dsr": 0.0, "verdict": "too few observations"}

    sr = sharpe_ratio(returns)
    _, _, skew, kurt = _moments(returns)
    sr0 = expected_max_sharpe(max(n_trials, 1))

    # Standard error of the Sharpe estimate, adjusted for skew and fat tails.
    denom = 1.0 - skew * sr + ((kurt) / 4.0) * sr ** 2
    if denom <= 0:
        return {"n": n, "sharpe": round(sr, 4), "sharpe0": round(sr0, 4),
                "dsr": 0.0, "verdict": "moment estimate unstable"}
    se = math.sqrt(denom / (n - 1))
    dsr = _norm_cdf((sr - sr0) / se) if se > 0 else 0.0

    if dsr >= 0.95:
        verdict = "survives multiple-testing correction"
    elif dsr >= 0.5:
        verdict = "weak — better than the search bar but not significant"
    else:
        verdict = "NOT distinguishable from the best of a random search"
    return {
        "n": n,
        "sharpe": round(sr, 4),
        "sharpe0_expected_from_search": round(sr0, 4),
        "skew": round(skew, 3),
        "excess_kurtosis": round(kurt, 3),
        "n_trials": n_trials,
        "dsr": round(dsr, 4),
        "verdict": verdict,
    }


# ── Walk-forward ──────────────────────────────────────────────────────────────

@dataclass
class WalkForwardResult:
    splits: list[dict] = field(default_factory=list)
    oos_returns: list[float] = field(default_factory=list)

    def summary(self, n_trials: int = 1) -> dict:
        n = len(self.oos_returns)
        if not n:
            return {"n": 0, "verdict": "no out-of-sample trades"}
        mean, sd, _, _ = _moments(self.oos_returns)
        out = {
            "splits": len(self.splits),
            "oos_trades": n,
            "oos_mean_pct": round(mean, 4),
            "oos_win_rate": round(100.0 * sum(1 for r in self.oos_returns if r > 0) / n, 1),
            "oos_t": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0,
        }
        out.update(deflated_sharpe_ratio(self.oos_returns, n_trials))
        return out


def walk_forward(
    observations: Sequence[dict],
    param_grid: Sequence[dict],
    evaluate: Callable[[Sequence[dict], dict], list[float]],
    n_splits: int = 4,
    embargo: int = 0,
) -> WalkForwardResult:
    """
    Anchored walk-forward: choose parameters on everything seen so far, then
    score them on the next block only — the block the choice could not see.

    `observations` must be ordered in time. `evaluate(obs, params)` returns the
    per-trade returns those params would have produced on `obs`.

    `embargo` drops that many observations immediately after each training
    block before the test block starts. Our labels overlap in time (a 120-min
    hold spans later signals), and without the gap a trade's outcome can sit in
    both training and test — the leak purged cross-validation exists to close.

    Deliberately anchored rather than rolling: with ~1,200 observations over
    three weeks, a rolling window would leave training sets too small to choose
    anything stable, and the instability would look like a result.
    """
    n = len(observations)
    result = WalkForwardResult()
    if n < (n_splits + 1) * 10 or not param_grid:
        return result

    block = n // (n_splits + 1)
    for i in range(1, n_splits + 1):
        train_end = block * i
        test_start = min(train_end + embargo, n)
        test_end = min(block * (i + 1), n)
        train, test = observations[:train_end], observations[test_start:test_end]
        if len(train) < 10 or len(test) < 5:
            continue

        best_params, best_score = None, float("-inf")
        for params in param_grid:
            rets = evaluate(train, params)
            if len(rets) < 5:
                continue
            mean, sd, _, _ = _moments(rets)
            # Zero variance is not "unrankable" — a constant return is a
            # perfectly good result and scoring it -inf made every parameter
            # tie at the bottom, so nothing was selected and the split was
            # silently skipped. Rank on the mean in that case.
            score = mean / sd if sd > 0 else mean
            if score > best_score:
                best_params, best_score = params, score

        if best_params is None:
            continue
        oos = evaluate(test, best_params)
        result.oos_returns.extend(oos)
        oos_mean = sum(oos) / len(oos) if oos else 0.0
        result.splits.append({
            "split": i,
            "train_n": len(train),
            "test_n": len(test),
            "chosen": best_params,
            "in_sample_score": round(best_score, 4),
            "oos_trades": len(oos),
            "oos_mean_pct": round(oos_mean, 4),
        })
    return result
