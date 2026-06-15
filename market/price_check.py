"""
market/price_check.py
──────────────────────
Fetches live price data and checks whether a news signal is confirmed by actual
price movement and elevated volume. This is the last gate before money moves.

Price sources:
  - Current price       — Finnhub REST quote (real-time, <1s latency, retried)
  - Previous close      — Finnhub quote `pc` field (cross-checked w/ Twelvedata)
  - Momentum baseline   — Twelvedata 1-min bars, selected by TIMESTAMP
  - Volume stats        — Twelvedata 1-day bars (20-day ADV)

A signal is confirmed when ALL of the following hold (in evaluation order —
cheapest checks first so we fail fast and spend fewer API credits):

  1. opening_block   — at least cfg.open_block_minutes after the open. The
                       opening auction produces violent noise (GOAI: entire
                       spike was in the 09:30 bar; bought 09:32 into collapse).
  2. penny_stock     — price >= cfg.min_stock_price. Sub-$5 names carry
                       outsized spread %, halt frequency, and manipulation
                       risk; every Jun 8–11 loss was sub-$5.
  3. wide_spread     — latest 1-min bar range (high−low)/close must be under
                       cfg.max_spread_pct. Bar-range proxy for effective
                       spread (no bid/ask feed available on our data plan).
  4. dead_cat        — not down more than cfg.max_day_drop_pct vs the PREVIOUS
                       CLOSE. Prev close (not today's open) so overnight
                       gap-downs are caught: a stock that gapped −25% and is
                       flat since open is still a falling knife.
  5. extended_move   — not UP more than cfg.max_day_move_pct vs prev close.
                       A stock already up 30%+ on the day has paid out its
                       catalyst; late articles on it are recaps. Closes the
                       hole where a stock up 80% on the day but flat in the
                       last 5 min passed the 5-min momentum ceiling.
  6. illiquid        — 20-day ADV × price >= cfg.min_daily_dollar_volume.
                       ADV-based on purpose: spike-day volume would let halt
                       patterns through (GOAI: $390k ADV → −18.99% stop fill).
  7. low_momentum    — up at least cfg.min_price_move_pct over the momentum
                       look-back window. v15: this is now only a DEAD-TAPE
                       noise floor (default 0.2%); the real accumulation
                       judgement is VWAP (step 10), which is size-neutral.
  8. high_momentum   — but NOT up more than cfg.max_price_move_pct in that
                       window — that is a post-halt spike, not an entry.
                       Runs before VWAP so spikes don't waste a VWAP credit.
  9. low_volume /    — RVOL (time-of-day normalized relative volume) within
     high_volume       [cfg.min_rvol, cfg.max_rvol]. See _expected_volume_
                       fraction() for why raw volume ratios are meaningless
                       without time normalization.
 10. below_vwap      — price must hold at/above session VWAP (cfg.
                       require_vwap_confirmation). SIZE-NEUTRAL accumulation
                       test: a deep-book large-cap reprices <1% in 5 min but
                       holds above VWAP when institutions buy; a fading
                       gap-up sits below VWAP regardless of % change. This is
                       the v15 fix for the fixed-% momentum floor rejecting
                       every real large-cap catalyst. Runs LAST — it spends an
                       extra Twelvedata credit, so all cheaper gates go first.
"""

import logging
import requests
import pandas as pd
import pandas_market_calendars as mcal
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
import pytz
from config.settings import cfg
from market.finnhub_bars import get_finnhub_quote
from market.twelvedata_bars import (
    get_momentum_baseline, get_volume_stats, get_twelvedata_quote, get_session_vwap,
)

_NYSE = mcal.get_calendar("NYSE")

logger = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")
_MARKET_OPEN = (9, 30)   # 09:30 ET
_MARKET_CLOSE = (16, 0)  # 16:00 ET


# ── Intraday volume curve ─────────────────────────────────────────────────────
# Equity volume is U-shaped: heavy at the open, dead at lunch, heavy at the
# close. These anchor points give the typical cumulative fraction of a full
# day's volume traded by each time of day (ET). Derived from the well-known
# U-curve; linearly interpolated between anchors.
#
# Why it matters: "today's volume >= 1.5× the 20-day FULL-DAY average" is
# nearly impossible at 10:00 (only ~16% of a normal day has traded) and
# trivially true at 15:45. Normalizing by this curve makes the RVOL floor and
# ceiling mean the same thing all session long.
_VOLUME_CURVE: list[tuple[float, float]] = [
    # (minutes since 09:30 open, cumulative fraction of typical daily volume)
    (0,    0.00),
    (5,    0.05),
    (15,   0.10),
    (30,   0.16),
    (60,   0.25),
    (90,   0.32),
    (150,  0.42),
    (210,  0.50),
    (270,  0.59),
    (330,  0.71),
    (360,  0.80),
    (380,  0.88),
    (390,  1.00),
]


def _expected_volume_fraction(minutes_since_open: float) -> float:
    """
    Return the typical cumulative fraction of a day's volume traded by
    `minutes_since_open` (linear interpolation over _VOLUME_CURVE).

    Floored at 0.04 so RVOL doesn't divide by a near-zero denominator in the
    first minutes after the open (which would make every stock look like 50×).
    """
    m = max(0.0, min(390.0, minutes_since_open))
    for (m0, f0), (m1, f1) in zip(_VOLUME_CURVE, _VOLUME_CURVE[1:]):
        if m0 <= m <= m1:
            # Linear interpolation between the two anchors
            frac = f0 + (f1 - f0) * ((m - m0) / (m1 - m0)) if m1 > m0 else f0
            return max(0.04, frac)
    return 1.0


def compute_rvol(today_volume: int, avg_daily_volume: int, minutes_since_open: float) -> float:
    """
    Time-of-day normalized relative volume:
      rvol = today's cumulative volume
             / (20-day ADV × expected fraction traded by this time of day)

    rvol == 1.0 means "trading exactly like a normal day so far".
    rvol >= cfg.min_rvol confirms genuine participation behind the move;
    rvol >  cfg.max_rvol is the parabolic halt-pattern signature.
    """
    if avg_daily_volume <= 0:
        return 0.0
    expected = avg_daily_volume * _expected_volume_fraction(minutes_since_open)
    return today_volume / expected if expected > 0 else 0.0


def next_market_open() -> datetime:
    """
    Return the next NYSE market open as a UTC-aware datetime.
    If called during market hours, returns today's open (already passed).
    Skips weekends.
    """
    now_et = datetime.now(_ET)
    candidate = now_et.replace(hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0)

    if candidate > now_et and now_et.weekday() < 5:
        return candidate.astimezone(timezone.utc)

    candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)


def _to_yf_ticker(t212_ticker: str) -> str:
    """Strip Trading 212 suffix to get a Finnhub/Twelvedata-compatible ticker."""
    return t212_ticker.split("_")[0]


def get_quote_with_fallback(symbol: str) -> dict | None:
    """
    Real-time quote with a two-source fallback chain:
      1. Finnhub /quote  — primary (fastest, generous rate limit)
      2. Twelvedata /quote — fallback when Finnhub has no coverage

    Finnhub's free tier silently omits many small caps and recent IPOs
    (observed 2026-06-15: CUPR/ELAN/WBD/INBX/SAIL all returned no Finnhub
    quote — exactly the small-cap catalysts this strategy targets — while
    Twelvedata carried every one). Returning None from BOTH means the symbol
    is genuinely unpriceable and the signal can't be evaluated.

    Both sources return the same normalised keys (c/o/pc), so callers don't
    need to know which one answered.
    """
    quote = get_finnhub_quote(symbol)
    if quote is not None:
        return quote
    logger.info("Quote [%s]: no Finnhub coverage — trying Twelvedata fallback", symbol)
    return get_twelvedata_quote(symbol)


def minutes_until_close() -> float | None:
    """
    Minutes until today's actual market close (handles early-close days via
    the exchange calendar). Returns None outside a trading session or if the
    calendar lookup fails. Used by the position monitor's EOD flatten.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return None
        close_utc = sched.iloc[0]["market_close"]
        delta = (close_utc - now_utc).total_seconds() / 60
        return delta if delta > 0 else None
    except Exception as exc:
        logger.warning("minutes_until_close: calendar lookup failed: %s", exc)
        return None


def is_too_late_to_buy() -> bool:
    """
    Return True if we are within TIME_STOP_MINUTES of today's market close.
    Uses the calendar's actual close time so early-close days are handled correctly.
    Returns False outside of market hours — is_market_open() handles that.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        close_utc = sched.iloc[0]["market_close"]
        minutes_to_close = (close_utc - now_utc).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.time_stop_minutes
    except Exception:
        now_et = datetime.now(_ET)
        close_et = now_et.replace(hour=_MARKET_CLOSE[0], minute=_MARKET_CLOSE[1], second=0, microsecond=0)
        minutes_to_close = (close_et - now_et).total_seconds() / 60
        return 0 < minutes_to_close <= cfg.time_stop_minutes


def is_market_open() -> bool:
    """
    Check whether the NYSE is currently open using pandas_market_calendars
    as the authoritative local source (handles holidays, early closes, weekends).

    Falls back to a Finnhub API check if the calendar check fails for any reason.
    """
    try:
        now_utc = pd.Timestamp.now("UTC")
        today = now_utc.strftime("%Y-%m-%d")
        sched = _NYSE.schedule(today, today)
        if sched.empty:
            return False
        market_open = sched.iloc[0]["market_open"]
        market_close = sched.iloc[0]["market_close"]
        return bool(market_open <= now_utc < market_close)
    except Exception as exc:
        logger.warning("Calendar open check failed: %s — falling back to Finnhub", exc)

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/stock/market-status",
            params={"exchange": "US", "token": cfg.finnhub_api_key},
            timeout=5,
        )
        resp.raise_for_status()
        return bool(resp.json().get("isOpen", False))
    except Exception as exc:
        logger.warning("Finnhub market-status fallback also failed: %s — assuming closed", exc)
        return False


@dataclass
class PriceConfirmation:
    ticker: str                     # T212 instrument code, e.g. AAPL_US_EQ
    symbol: str                     # exchange symbol, e.g. AAPL
    current_price: float
    open_price: float
    prev_close: float | None        # previous session close (gap baseline)
    day_move_pct: float             # vs today's open (informational/logging)
    day_change_pct: float | None    # vs PREVIOUS CLOSE — used by filters
    recent_move_pct: float          # vs momentum baseline (~5 min ago)
    current_volume: int
    avg_volume: int
    rvol: float                     # time-of-day normalized relative volume
    avg_dollar_volume: float | None # 20-day ADV × price (liquidity metric)
    spread_proxy_pct: float | None  # latest bar (high−low)/close, %
    is_confirmed: bool
    reason: str
    # approved | opening_block | penny_stock | wide_spread | dead_cat |
    # extended_move | illiquid | low_momentum | high_momentum |
    # low_volume | high_volume | below_vwap | no_price_data
    reason_code: str
    vwap: float | None = None       # session VWAP at confirmation (v15)


def _reject(base: dict, code: str, reason: str) -> "PriceConfirmation":
    """Build a rejected PriceConfirmation and log it uniformly."""
    logger.info(
        "Price check [%s]: recent=%+.2f%% day_chg=%s rvol=%.1f adv$=%s — rejected: %s (%s)",
        base["symbol"], base.get("recent_move_pct", 0.0),
        f"{base['day_change_pct']:+.2f}%" if base.get("day_change_pct") is not None else "n/a",
        base.get("rvol", 0.0),
        f"${base['avg_dollar_volume']:,.0f}" if base.get("avg_dollar_volume") else "n/a",
        code, reason,
    )
    return PriceConfirmation(**base, is_confirmed=False, reason=reason, reason_code=code)


def confirm_price_signal(t212_ticker: str) -> PriceConfirmation | None:
    """
    Check whether a ticker is experiencing active, tradeable upward momentum
    that corroborates a bullish news signal.

    Returns None only when a hard data failure makes it impossible to evaluate
    the signal (Finnhub down, Twelvedata down). Confirmed/rejected signals
    are returned as PriceConfirmation with is_confirmed set accordingly.
    """
    symbol = _to_yf_ticker(t212_ticker)

    try:
        # ── Current price + previous close (Finnhub → Twelvedata fallback) ──
        quote = get_quote_with_fallback(symbol)
        if quote is None:
            logger.warning(
                "Price check [%s]: no quote from Finnhub or Twelvedata — cannot evaluate signal",
                symbol,
            )
            return None

        current_price = float(quote["c"])
        open_price = float(quote["o"]) if quote.get("o") else current_price
        if open_price == 0:
            open_price = current_price
        # `pc` = previous close (from whichever quote source answered —
        # Finnhub or the Twelvedata fallback both populate this key). The
        # baseline for gap/day-change math: using today's open instead
        # silently ignores overnight gaps in both directions (the dead-cat hole).
        quote_prev_close = float(quote.get("pc") or 0) or None
        day_move_pct = ((current_price - open_price) / open_price) * 100

        # ── Time since open ───────────────────────────────────────────────────
        now_et = datetime.now(_ET)
        market_open_et = now_et.replace(
            hour=_MARKET_OPEN[0], minute=_MARKET_OPEN[1], second=0, microsecond=0
        )
        minutes_since_open = (now_et - market_open_et).total_seconds() / 60

        # Template for every PriceConfirmation built before later data arrives.
        base = dict(
            ticker=t212_ticker, symbol=symbol,
            current_price=current_price, open_price=open_price,
            prev_close=quote_prev_close,
            day_move_pct=day_move_pct, day_change_pct=None,
            recent_move_pct=0.0, current_volume=0, avg_volume=0,
            rvol=0.0, avg_dollar_volume=None, spread_proxy_pct=None,
        )

        # ── 1. Opening block ─────────────────────────────────────────────────
        # Costs nothing to check; rejects before any Twelvedata credit is spent.
        if minutes_since_open < cfg.open_block_minutes:
            return _reject(
                base, "opening_block",
                f"Opening auction block: {minutes_since_open:.1f} min since open "
                f"(block lasts {cfg.open_block_minutes} min to avoid auction noise)",
            )

        # ── 2. Penny stock floor ─────────────────────────────────────────────
        if current_price < cfg.min_stock_price:
            return _reject(
                base, "penny_stock",
                f"Penny stock filter: price ${current_price:.4f} "
                f"< ${cfg.min_stock_price:.2f} minimum — spread/halt/manipulation risk",
            )

        # ── Momentum baseline + spread proxy (Twelvedata 1-min bars) ─────────
        past_price, current_bar_price, spread_proxy_pct = get_momentum_baseline(symbol)
        base["spread_proxy_pct"] = spread_proxy_pct

        if past_price is None:
            # Early-session fallback: Twelvedata may not have enough bars in
            # the first ~15 min; the official open price is a fair baseline.
            if minutes_since_open < 15 and open_price and open_price > 0:
                past_price = open_price
                logger.info(
                    "Price check [%s]: Twelvedata unavailable — using Finnhub open=%.4f as baseline",
                    symbol, open_price,
                )
            else:
                logger.warning(
                    "Price check [%s]: Twelvedata momentum baseline unavailable and "
                    "not in open window — cannot evaluate",
                    symbol,
                )
                return None

        recent_move_pct = ((current_price - past_price) / past_price) * 100 if past_price else 0.0
        base["recent_move_pct"] = recent_move_pct

        # ── 3. Spread proxy ──────────────────────────────────────────────────
        # No bid/ask feed on our data plan, so the latest 1-min bar's range is
        # the proxy. Permissive default — only the truly untradeable get cut.
        if spread_proxy_pct is not None and spread_proxy_pct > cfg.max_spread_pct:
            return _reject(
                base, "wide_spread",
                f"Spread proxy {spread_proxy_pct:.2f}% (last bar range) exceeds "
                f"{cfg.max_spread_pct}% — effective spread would eat the edge",
            )

        # ── Volume stats + prev close (Twelvedata 1-day bars) ────────────────
        today_volume, avg_daily_volume, avg_dollar_volume, td_prev_close = get_volume_stats(symbol)

        # Prefer Finnhub's prev close (real-time source); Twelvedata's daily
        # bar is the backup when the quote lacks `pc`.
        prev_close = quote_prev_close or td_prev_close
        base["prev_close"] = prev_close
        day_change_pct = (
            ((current_price - prev_close) / prev_close) * 100 if prev_close else None
        )
        base["day_change_pct"] = day_change_pct

        # FAIL-CLOSED on missing volume data. Without it we have neither the
        # liquidity gate (avg_dollar_volume) nor the participation gate (RVOL) —
        # the two checks that keep us out of untradeable / unconfirmed names.
        # Trading on momentum + VWAP alone here would be the worst kind of
        # silent fail-open: a Twelvedata volume outage would relax risk exactly
        # when data is least reliable. Quant standard: no confirmation = no
        # trade. Returning None (not a reject) means "couldn't evaluate" — the
        # signal is parked in main.py's retry queue and re-tried next cycle.
        if today_volume is None or avg_daily_volume is None or avg_dollar_volume is None:
            logger.warning(
                "Price check [%s]: volume/liquidity data unavailable — cannot run "
                "liquidity or RVOL gates; deferring (will retry)",
                symbol,
            )
            return None

        current_volume = today_volume
        rvol = compute_rvol(current_volume, avg_daily_volume or 0, minutes_since_open)

        base.update(
            current_volume=current_volume,
            avg_volume=avg_daily_volume or 0,
            rvol=rvol,
            avg_dollar_volume=avg_dollar_volume,
        )

        # ── 4. Dead-cat guard (vs prev close) ────────────────────────────────
        # If prev close is unavailable from both sources, fall back to the
        # open-based day move rather than silently disabling the guard —
        # it misses overnight gaps but still catches intraday knives.
        drop_metric = day_change_pct if day_change_pct is not None else day_move_pct
        if drop_metric < -cfg.max_day_drop_pct:
            baseline_name = "prev close" if day_change_pct is not None else "today's open (prev close unavailable)"
            return _reject(
                base, "dead_cat",
                f"Dead-cat guard: {drop_metric:.2f}% vs {baseline_name} "
                f"(max allowed drop −{cfg.max_day_drop_pct}%) — bullish news on a "
                f"falling knife is a bounce, not a trend",
            )

        # ── 5. Extended-move ceiling (vs prev close, gap included) ───────────
        if day_change_pct is not None and day_change_pct > cfg.max_day_move_pct:
            return _reject(
                base, "extended_move",
                f"Extended-move ceiling: {day_change_pct:+.2f}% vs prev close exceeds "
                f"+{cfg.max_day_move_pct}% — catalyst already paid out, entries here "
                f"buy exhaustion",
            )

        # ── 6. Liquidity floor (ADV-based) ───────────────────────────────────
        if avg_dollar_volume is not None and avg_dollar_volume < cfg.min_daily_dollar_volume:
            return _reject(
                base, "illiquid",
                f"Liquidity filter: avg daily dollar volume ${avg_dollar_volume:,.0f} "
                f"< ${cfg.min_daily_dollar_volume:,.0f} — normal book too thin for a "
                f"clean exit",
            )

        # ── 7. Momentum noise floor ──────────────────────────────────────────
        # With VWAP confirmation on (step 9), this only rejects dead-flat tape —
        # a catalyst that produced literally no move. The "is it being
        # accumulated?" judgement is VWAP's job, not a fixed % threshold's.
        if recent_move_pct < cfg.min_price_move_pct:
            return _reject(
                base, "low_momentum",
                f"Dead tape: {recent_move_pct:+.2f}% over last "
                f"~{cfg.momentum_lookback_minutes} min (need +{cfg.min_price_move_pct}% to "
                f"confirm the catalyst moved the stock at all)",
            )

        # ── 8. Momentum ceiling ──────────────────────────────────────────────
        # Runs BEFORE the VWAP call so a post-halt spike (which is also far
        # ABOVE VWAP and would pass step 9) is rejected without spending the
        # extra Twelvedata credit that get_session_vwap costs.
        if recent_move_pct > cfg.max_price_move_pct:
            return _reject(
                base, "high_momentum",
                f"Momentum ceiling: {recent_move_pct:+.2f}% in "
                f"~{cfg.momentum_lookback_minutes} min exceeds +{cfg.max_price_move_pct}% "
                f"— post-halt spike, not a live catalyst",
            )

        # ── 9. RVOL band ─────────────────────────────────────────────────────
        # Skip when we have no average volume to normalize against (RVOL would
        # be meaningless either way) — the liquidity filter above still applies.
        if avg_daily_volume and avg_daily_volume > 0:
            if rvol < cfg.min_rvol:
                return _reject(
                    base, "low_volume",
                    f"RVOL {rvol:.2f} below {cfg.min_rvol} — price move lacks real "
                    f"participation (time-normalized vs 20-day avg)",
                )
            if rvol > cfg.max_rvol:
                return _reject(
                    base, "high_volume",
                    f"RVOL {rvol:.1f} above {cfg.max_rvol} — parabolic volume is the "
                    f"halt-pattern signature, not a tradeable catalyst",
                )

        # ── 10. VWAP confirmation (size-neutral accumulation test) ───────────
        # LAST gate, because get_session_vwap() spends an extra Twelvedata
        # credit (a full-session bar pull) — every cheaper gate runs first so
        # this is only reached by signals that have already passed everything
        # else. Price held at/above session VWAP = institutions are net buyers,
        # independent of the raw % change. This is what lets a deep-book
        # large-cap catalyst through (it sits above VWAP even at +0.2%) while
        # rejecting a fading gap-up (below VWAP regardless of % change).
        # Research basis: PEAD literature + VWAP-reclaim practitioner playbooks
        # (citations in docs/algorithm.md).
        if cfg.require_vwap_confirmation:
            vwap, vwap_last = get_session_vwap(symbol)
            if vwap is not None and vwap > 0:
                base["vwap"] = vwap
                # Accept at/above VWAP minus a small tolerance (handles a fresh
                # reclaim on the current bar).
                vwap_floor = vwap * (1 - cfg.vwap_tolerance_pct / 100)
                if current_price < vwap_floor:
                    return _reject(
                        base, "below_vwap",
                        f"Below VWAP: price ${current_price:.4f} < VWAP ${vwap:.4f} "
                        f"(tol {cfg.vwap_tolerance_pct}%) — being distributed, not "
                        f"accumulated; gap-and-crap risk",
                    )
            else:
                # VWAP unavailable (no volume yet / data gap). Don't hard-fail:
                # the momentum floor + RVOL already passed. Logged so it's
                # visible when we confirm without VWAP.
                logger.info(
                    "Price check [%s]: VWAP unavailable — confirming on momentum + RVOL only",
                    symbol,
                )

        # ── All conditions met — signal confirmed ─────────────────────────────
        adv_str = f" | adv$={avg_dollar_volume:,.0f}" if avg_dollar_volume else ""
        reason = (
            f"+{recent_move_pct:.2f}% in ~{cfg.momentum_lookback_minutes} min "
            f"| RVOL {rvol:.1f} "
            f"| day {day_change_pct:+.2f}% vs prev close"
            f"{adv_str}"
        ) if day_change_pct is not None else (
            f"+{recent_move_pct:.2f}% | RVOL {rvol:.1f}{adv_str}"
        )
        logger.info(
            "Price check [%s]: recent=%+.2f%% day_chg=%s rvol=%.1f adv$=%s — APPROVED",
            symbol, recent_move_pct,
            f"{day_change_pct:+.2f}%" if day_change_pct is not None else "n/a",
            rvol,
            f"${avg_dollar_volume:,.0f}" if avg_dollar_volume else "n/a",
        )
        return PriceConfirmation(**base, is_confirmed=True, reason=reason, reason_code="approved")

    except Exception as exc:
        logger.error("Price check failed for %s: %s", symbol, exc, exc_info=True)
        return None


def get_current_price(t212_ticker: str) -> float | None:
    """
    Fast lookup of the latest price for the open-position monitor.
    Primary: Finnhub REST quote (real-time, retried).
    Fallback: most recent Twelvedata 1-min bar close.
    Returns None if both are unavailable — callers must handle this explicitly.
    """
    symbol = _to_yf_ticker(t212_ticker)
    quote = get_quote_with_fallback(symbol)
    if quote is not None:
        price = float(quote["c"])
        logger.debug("get_current_price [%s]: %.4f", symbol, price)
        return price
    # Twelvedata fallback: use most recent 1-min bar close
    try:
        _past, current_bar_price, _spread = get_momentum_baseline(symbol)
        if current_bar_price is not None:
            logger.warning(
                "get_current_price [%s]: Finnhub unavailable — using Twelvedata bar close %.4f",
                symbol, current_bar_price,
            )
            return current_bar_price
    except Exception as exc:
        logger.error("get_current_price [%s]: Twelvedata fallback also failed: %s", symbol, exc)
    logger.error(
        "get_current_price [%s]: both Finnhub and Twelvedata unavailable — returning None",
        symbol,
    )
    return None
