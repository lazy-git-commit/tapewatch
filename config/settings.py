"""
config/settings.py
──────────────────
Loads all settings from environment variables (via .env).
Import this anywhere with: from config.settings import cfg

IMPORTANT — deployment contract:
  The production .env is written by .github/workflows/deploy.yml from GitHub
  Secrets + hardcoded strategy values. Any NEW setting added here MUST also be
  added to the "Write .env" step in deploy.yml (and to .env.example), otherwise
  the VM runs on defaults — or, for required keys, crash-loops at startup
  (this exact failure took the system down for 18h on 2026-06-11).
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    # ── API Keys ──────────────────────────────────────────────────────────────
    trading212_api_key: str = field(default_factory=lambda: os.getenv("TRADING212_API_KEY", ""))
    trading212_api_key_id: str = field(default_factory=lambda: os.getenv("TRADING212_API_KEY_ID", ""))
    trading212_demo_api_key: str = field(default_factory=lambda: os.getenv("TRADING212_DEMO_API_KEY", ""))
    trading212_demo_api_key_id: str = field(default_factory=lambda: os.getenv("TRADING212_DEMO_API_KEY_ID", ""))
    benzinga_api_key: str = field(default_factory=lambda: os.getenv("MASSIVE_BENZINGA_API_KEY", ""))
    finnhub_api_key: str = field(default_factory=lambda: os.getenv("FINNHUBIO_API_KEY", ""))
    twelvedata_api_key: str = field(default_factory=lambda: os.getenv("TWELVEDATA_API_KEY", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # Qwen (Alibaba Cloud Model Studio) — OPTIONAL, and deliberately absent from
    # validate()'s required list.
    #
    # v21.14: these ARE read on the live news path — news/shadow_classifier.py
    # sends every batch Claude sees to Qwen as well. They stay optional because
    # the shadow is strictly fire-and-forget: it runs on a background thread,
    # returns nothing, and cannot influence or delay a trading decision, so an
    # unset key degrades to "no shadow data" rather than to a startup
    # crash-loop. Assessed offline with `python -m analysis.classifier_compare`.
    qwen_api_key: str = field(default_factory=lambda: os.getenv("QWEN_API_KEY", ""))
    qwen_base_url: str = field(default_factory=lambda: os.getenv("QWEN_BASE_URL", ""))
    qwen_model: str = field(default_factory=lambda: os.getenv("QWEN_MODEL", "qwen-flash"))

    # ── Trading Mode ──────────────────────────────────────────────────────────
    trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "demo"))

    # ── Entry filters (signal confirmation) ───────────────────────────────────
    # Minimum Claude confidence (1–10 scale) required to act on a positive
    # signal. Enforced in news/fetcher.py — signals below this are downgraded
    # to neutral (they are still recorded in sentiment_scores for the eval loop).
    min_sentiment_confidence: int = field(
        default_factory=lambda: int(os.getenv("MIN_SENTIMENT_CONFIDENCE", "7"))
    )
    # Minimum catalyst magnitude (1–5) to trade. Filters noise signals that the
    # model scores positive but judges as small relative-to-market-cap impact.
    # Default 2: trades material catalysts (3+) and modest ones (2) but blocks
    # pure noise (1 = PT raise, reiteration, vague MOU, conference attendance).
    # Set to 3 to restrict to material+ only; set to 1 to disable the gate.
    min_catalyst_magnitude: int = field(
        default_factory=lambda: int(os.getenv("MIN_CATALYST_MAGNITUDE", "2"))
    )
    # ── Momentum confirmation (v15: VWAP-relative, size-neutral) ──────────────
    # The v14 fixed-% momentum floor was the strategy's binding constraint:
    # 1,077 of all-time rejections were `low_momentum`, and on 2026-06-15 every
    # real large-cap catalyst was rejected at near-zero 5-min change (DXCM
    # +0.14%, SNY +0.07%, LLY +0.01%) — because deep order books reprice
    # slowly. A single % threshold cannot serve both a $2 micro-cap and a
    # $1000 mega-cap.
    #
    # Research (PEAD literature + practitioner playbooks — citations in
    # docs/algorithm.md) converges on VWAP-relative position as the
    # size-NEUTRAL confirmation of "is this being accumulated?": a stock held
    # above its session VWAP on elevated relative volume is being bought by
    # institutions regardless of its raw % change; one fading below VWAP is
    # gap-and-crap regardless. So confirmation is now:
    #   (a) price >= VWAP × (1 − vwap_tolerance_pct)   [primary, size-neutral]
    #   (b) recent move >= min_price_move_pct           [noise floor only]
    #   (c) recent move <= max_price_move_pct           [post-halt ceiling]
    #   (d) RVOL in band                                [participation]
    #
    # require_vwap_confirmation toggles (a). When True, the momentum floor (b)
    # is lowered to a pure noise-rejection level since VWAP does the real work.
    require_vwap_confirmation: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_VWAP_CONFIRMATION", "true").lower() in ("1", "true", "yes")
    )
    # How far BELOW VWAP we still accept (small tolerance for a stock that just
    # reclaimed VWAP on the current bar). 0.1% ≈ touching the line.
    vwap_tolerance_pct: float = field(
        default_factory=lambda: float(os.getenv("VWAP_TOLERANCE_PCT", "0.1"))
    )
    # ── VWAP extension ceiling (v20) ──────────────────────────────────────────
    # Maximum entry distance ABOVE VWAP. Derived from the stop, not arbitrary:
    # the stop sits stop_loss_pct below entry, so if entry is further above
    # VWAP than the stop is wide, a routine mean-reversion to value — the
    # base case for any extended stock, even in an uptrend — hits the stop by
    # construction. Both 2026-07 losses had this geometry (LEVI +1.9% above
    # VWAP, CRCL +2.2%, stop 2%). Default 1.5 = stop 2.0 minus safety margin;
    # keep it BELOW stop_loss_pct. The reject is TRANSIENT: the re-eval queue
    # re-checks and enters on the first pullback into value instead.
    max_vwap_extension_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_VWAP_EXTENSION_PCT", "1.5"))
    )
    # ── Intraday exhaustion gate (v19.5) ──────────────────────────────────────
    # day_change_pct (vs YESTERDAY's close) and recent_move_pct (last ~5 min)
    # are both blind to the SHAPE of today's own session: a stock that gapped
    # down hard and clawed most of the way back looks identical, on both those
    # measures, to one calmly grinding to fresh highs. 2026-07-09: LEVI gapped
    # -7.8% at the open on an earnings beat ("sell the news"), then recovered
    # to +2.3% by the time we bought — within 15 cents of the exact high of
    # the day, three minutes before the actual peak — and faded the rest of
    # the session. Both thresholds must trip: the day's own low-to-high range
    # must be large enough to represent a real round trip (not noise), AND
    # price must already have recovered most of that range.
    require_exhaustion_check: bool = field(
        default_factory=lambda: os.getenv("REQUIRE_EXHAUSTION_CHECK", "true").lower() in ("1", "true", "yes")
    )
    exhaustion_min_range_pct: float = field(
        default_factory=lambda: float(os.getenv("EXHAUSTION_MIN_RANGE_PCT", "5.0"))
    )
    exhaustion_recovery_threshold: float = field(
        default_factory=lambda: float(os.getenv("EXHAUSTION_RECOVERY_THRESHOLD", "0.75"))
    )
    # Momentum noise floor. With VWAP confirmation on, this only rejects
    # dead-flat tape (the catalyst produced literally no move); VWAP handles
    # the "is it being accumulated" judgement. With VWAP confirmation OFF this
    # reverts to being the sole momentum gate, so it is set higher in that case
    # via .env. Default tuned for the VWAP-on regime.
    min_price_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MIN_PRICE_MOVE_PCT", "0.2"))
    )
    # Momentum ceiling: if the stock is up MORE than this % in the look-back
    # window, we are reading a post-halt spike — the move already happened
    # (halt articles publish AFTER the 30–120% pop). Buying here = buying the top.
    max_price_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_PRICE_MOVE_PCT", "15.0"))
    )
    # Day-move ceiling: reject if the stock is already up more than this %
    # vs the PREVIOUS CLOSE (gap included). A stock up 30%+ on the day has
    # already paid out its catalyst — late articles on it are recaps.
    # This closes the hole where a stock up 80% on the day but flat in the
    # last 5 minutes passed the 5-min momentum ceiling.
    #
    # v21.11: 25% → 10%, calibrated on all 24 closed trades (2026-08-01).
    # 25% was so loose it never bound on a real (non-microcap) trade — every
    # one of the 20 trades with usable prev-close data passed it. The entry
    # day-move vs realized P&L panel:
    #
    #   ceiling  kept  mean P&L   blocked                       blocked mean
    #     8%      14    -0.57%    MRVL,CRCL,TMO,APH,GRMN,NVT      -1.72%
    #    10%      17    -0.57%    CRCL,TMO,NVT                    -2.85%
    #    12%      18    -0.65%    CRCL,NVT                        -3.27%
    #    25%      20    -0.91%    (nothing)                            —
    #
    # 10% is the TIGHTEST ceiling that blocks only losers: it removes CRCL
    # (-3.97%), TMO (-2.03%) and NVT (-2.56%) and costs no winner. 8% also
    # cuts GRMN (+3.86%, entered at +9.99%), the second-best trade on record.
    # Mechanically this is a risk:reward statement, not a momentum one — with
    # a 2% stop and a 5% target, entering a stock already up 15% needs +21%
    # on the day to pay out while a routine pullback stops you.
    # n=20 is calibration, not proof; GRMN sits 0.01pp inside the boundary.
    max_day_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAY_MOVE_PCT", "10.0"))
    )
    # Maximum age of the quote backing an ENTRY decision (seconds).
    #
    # Distinct from price_check._QUOTE_MAX_AGE_SECONDS (20 min), which answers
    # "does any provider carry this instrument at all?" — a coverage question.
    # This answers "is this price safe to size and buy against right now?"
    #
    # Why (2026-07-31, NVT): both providers froze at the open. The quote that
    # confirmed the entry honestly reported itself ~3 minutes old and the
    # 20-minute coverage threshold waved it through. That price ($167.37) was
    # the 09:33 bar close; the real tape was ~$165.50 and falling. Every gate
    # — momentum +1.74%, RVOL, VWAP held — was computed from a 3-minute-old
    # photograph, and each produced a green light the live market would have
    # refused. The position was stopped out 42 seconds after the fill.
    # In the first minutes of a session a 3-minute lag is a 3% pricing error:
    # larger than the entire stop distance.
    max_entry_quote_age_seconds: int = field(
        default_factory=lambda: int(os.getenv("MAX_ENTRY_QUOTE_AGE_SECONDS", "90"))
    )
    # How many minutes back the momentum baseline looks. Bars are selected by
    # TIMESTAMP (not array index) so missing 1-min bars on thin stocks don't
    # silently stretch the window.
    momentum_lookback_minutes: int = field(
        default_factory=lambda: int(os.getenv("MOMENTUM_LOOKBACK_MINUTES", "5"))
    )
    # Dead-cat guard: reject if the stock is down more than this % vs the
    # previous close. Uses prev close (not today's open) so overnight gap-downs
    # are caught — a stock that gapped down 25% and is flat since open is still
    # a falling knife.
    max_day_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAY_DROP_PCT", "3.0"))
    )
    # Liquidity floor — AVERAGE daily dollar volume (20-day ADV × price).
    # NOTE: deliberately ADV-based, NOT today's volume. During a halt-spike,
    # today's dollar volume explodes, which would let the filter pass exactly
    # the stocks it exists to block. Exit slippage depends on the NORMAL book
    # depth, which ADV measures. (GOAI: $390k ADV → −18.99% fill on a −2% stop.)
    min_daily_dollar_volume: float = field(
        default_factory=lambda: float(os.getenv("MIN_DAILY_DOLLAR_VOLUME", "5000000"))
    )
    # RVOL floor/ceiling — relative volume, TIME-OF-DAY NORMALIZED:
    #   rvol = today's cumulative volume / (20-day ADV × expected fraction of
    #          a typical day's volume traded by this time of day)
    # Without normalization, "1.5× the full-day average" is nearly impossible
    # at 10:00 and trivial at 15:45. The ceiling catches halt patterns:
    # parabolic volume on micro-caps is the circuit-breaker signature.
    min_rvol: float = field(
        default_factory=lambda: float(os.getenv("MIN_RVOL", "1.5"))
    )
    max_rvol: float = field(
        default_factory=lambda: float(os.getenv("MAX_RVOL", "20.0"))
    )
    # Size-neutral RVOL floor bypass (v20.2): a stock with ADV$ at or above
    # this floor doesn't need anomalous RELATIVE volume to make a real move —
    # its normal book is already enormous in dollar terms, so a held VWAP
    # (institutions net buying, independent of raw % change) substitutes for
    # RVOL as evidence of participation. 2026-07-13 post-mortem: BMY (ADV$
    # $752M) drifted +2.1% all session on RVOL that never exceeded 0.3 and
    # was rejected low_volume on all 27 consecutive re-eval cycles despite
    # holding VWAP throughout — the size-neutral VWAP test (step 10) never
    # got a chance to run because the RVOL floor (step 9) rejects first.
    # $50M = 10x the illiquidity floor; a first-pass estimate, not yet
    # validated against multiple days of measured mega-cap data.
    rvol_bypass_min_adv_dollar: float = field(
        default_factory=lambda: float(os.getenv("RVOL_BYPASS_MIN_ADV_DOLLAR", "50000000"))
    )
    # Penny-stock floor. Sub-$5 names carry outsized spread (as % of price),
    # halt frequency, and manipulation risk. Every observed loss in the Jun
    # 8–11 week was on a sub-$5 stock. $5 is the classic institutional cutoff.
    min_stock_price: float = field(
        default_factory=lambda: float(os.getenv("MIN_STOCK_PRICE", "5.0"))
    )
    # Spread proxy ceiling: (high − low) / close of the latest completed 1-min
    # bar. We have no direct bid/ask feed, so the recent bar range serves as a
    # proxy for effective spread + microstructure noise. Momentum names print
    # wide bars naturally, so the default is permissive — it only rejects the
    # truly untradeable.
    max_spread_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_SPREAD_PCT", "3.0"))
    )
    # Block trades during the first N minutes after open (opening auction noise).
    open_block_minutes: int = field(
        default_factory=lambda: int(os.getenv("OPEN_BLOCK_MINUTES", "5"))
    )
    # Catalyst classes that are allowed to trade. Claude tags every article
    # with a catalyst_type; every class is recorded to sentiment_scores for
    # the eval loop, but only these trade.
    #
    # v20 (2026-07-10): pruned to the classes with MEASURED positive forward
    # returns. 60 days of the system's own eval loop (positive sentiment,
    # not-already-moved, confidence ≥ 0.7; avg fwd returns @5/15/60 min):
    #   fda_approval   n=86   −0.02 / +0.55 / +1.42   ← kept
    #   guidance_raise n=9    +0.38 / +1.15 / +2.97   ← kept
    #   contract_win   n=152  −0.42 / −0.84 / −1.34   ← removed
    #   ma_target      n=71   −0.08 / −0.07 / −0.37   ← removed (targets pin
    #                          to deal price instantly; nothing left to capture)
    #   earnings_beat  n=33   −0.74 / −1.13 / −1.60   ← removed (earnings news
    #                          at our latency gets SOLD — LEVI/CRCL losses)
    #   product_launch n=32   −0.93 / −1.13 / −2.05   ← removed
    #   short_squeeze  n=0    never produced a tradeable signal ← removed
    # The kept classes are binary regulatory/guidance surprises whose drift
    # BUILDS over 15–60 min — a shape our 1–3 min entry latency captures.
    # Re-enable a class only with fresh forward-return evidence.
    tradeable_catalysts: list[str] = field(
        default_factory=lambda: [
            c.strip().lower() for c in os.getenv(
                "TRADEABLE_CATALYSTS",
                "fda_approval,guidance_raise",
            ).split(",") if c.strip()
        ]
    )

    # ── Position sizing & portfolio risk ──────────────────────────────────────
    # Hard cap: max % of total portfolio value in a single position.
    max_position_size_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE_PCT", "5.0"))
    )
    # Risk-based sizing: size the position so that a stop-loss hit costs at
    # most this % of account equity:  position = equity × risk% / stop%.
    # With the current fixed 2% stop this caps positions at 12.5% of equity,
    # so max_position_size_pct (5%) usually binds first — but this formula
    # becomes the active constraint if stops are ever widened or made dynamic.
    risk_per_trade_pct: float = field(
        default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "0.25"))
    )
    # Liquidity participation cap: position dollar size ≤ this % of the
    # stock's average daily dollar volume. Keeps us small enough that our own
    # exit market order doesn't move the price (the GOAI lesson: our sell
    # alone pushed the fill 11.7% below trigger).
    max_adv_participation_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_ADV_PARTICIPATION_PCT", "0.5"))
    )
    # Max simultaneous open positions. Momentum signals cluster (one macro
    # headline → 4 correlated semis trades on Jun 3); this caps the blast radius.
    max_open_positions: int = field(
        default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "8"))
    )
    # Max new positions per calendar day.
    max_trades_per_day: int = field(
        default_factory=lambda: int(os.getenv("MAX_TRADES_PER_DAY", "10"))
    )
    # Daily kill switch: once today's REALIZED P&L is below
    # −max_daily_loss_pct % of portfolio value, no new positions are opened
    # until the next day. Open positions continue to be managed normally.
    # This is the single most important live-trading safety control.
    max_daily_loss_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_PCT", "2.0"))
    )

    # ── Exit management ───────────────────────────────────────────────────────
    take_profit_pct: float = field(
        default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "5.0"))
    )
    stop_loss_pct: float = field(
        default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "2.0"))
    )
    time_stop_minutes: int = field(
        default_factory=lambda: int(os.getenv("TIME_STOP_MINUTES", "60"))
    )
    # How long before a session's hard exit boundary to STOP opening new
    # positions. Decoupled from time_stop_minutes (2026-07-20): the forward-
    # return panel showed the guidance_raise/fda_approval edge still building at
    # 60 min, so the HOLD (time_stop_minutes) wants to grow — but coupling the
    # entry cutoff to it would collapse the entry window (a 120-min hold would
    # bar every entry after 14:00 ET). The cutoff only needs enough runway for a
    # trade to develop before the EOD flatten, not the full hold; a position
    # opened late simply gets flattened at EOD instead of running its time stop.
    # Defaults to time_stop_minutes so an unset deployment keeps today's exact
    # behavior; set ENTRY_CUTOFF_MINUTES explicitly to decouple them.
    entry_cutoff_minutes: int = field(
        default_factory=lambda: int(
            os.getenv("ENTRY_CUTOFF_MINUTES", os.getenv("TIME_STOP_MINUTES", "60"))
        )
    )
    # v20 exit inversion: the STOP rests at the broker (zero-latency loss
    # side); the TP is polled by the monitor. Disable only for brokers/modes
    # where stop orders are unavailable — the monitor then polls both sides.
    resting_stop_enabled: bool = field(
        default_factory=lambda: os.getenv("RESTING_STOP_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    # Breakeven ratchet: once a position is up ratchet_trigger_pct, the stop
    # moves to buy × (1 + ratchet_lock_pct/100), once. A trade that has paid
    # one full risk unit (trigger = stop distance by default) may mean-revert
    # but must not become a loser.
    ratchet_trigger_pct: float = field(
        default_factory=lambda: float(os.getenv("RATCHET_TRIGGER_PCT", "2.0"))
    )
    ratchet_lock_pct: float = field(
        default_factory=lambda: float(os.getenv("RATCHET_LOCK_PCT", "0.1"))
    )
    # How often the position monitor runs. 5s (v20; was 20s): with the stop
    # resting broker-side the poll no longer guards the loss side, but the
    # POLLED side (take-profit) still pays the full cadence as latency at
    # the exact moment price touches the target. The cycle early-outs when
    # no positions are open, so the idle cost is one local DB query.
    monitor_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("MONITOR_INTERVAL_SECONDS", "5"))
    )
    # Flatten ALL positions this many minutes before the close, regardless of
    # P&L. This is a day-trading system: holding a momentum spike overnight
    # exposes the account to uncapped gap risk that none of our stops can
    # protect against (stops don't work when the market is closed).
    eod_flatten_minutes: int = field(
        default_factory=lambda: int(os.getenv("EOD_FLATTEN_MINUTES", "10"))
    )
    # Bound stop-loss slippage: exit orders are placed as LIMIT orders at
    # (trigger price × (1 − this%)) instead of pure market orders. We accept
    # a possible unfilled order (retried next monitor cycle) in exchange for
    # never repeating GOAI's −18.99% market-sell fill on a −2% stop.
    sell_limit_slack_pct: float = field(
        default_factory=lambda: float(os.getenv("SELL_LIMIT_SLACK_PCT", "1.0"))
    )

    # ── Pre-market pipeline ───────────────────────────────────────────────────
    # Most genuine catalysts (earnings, FDA, M&A) publish 07:00–09:25 ET when
    # the RTH pipeline is asleep. The pre-market scanner scores that news and
    # builds a watchlist; candidates are evaluated AT THE OPEN with gap +
    # momentum confirmation. We deliberately do NOT pre-place orders — a
    # pre-placed order fills at the open auction price, i.e. buys the entire
    # gap with zero confirmation (the classic "gap-and-crap" trap).
    premarket_enabled: bool = field(
        default_factory=lambda: os.getenv("PREMARKET_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    # When the pre-market scanner starts collecting news (ET, HH:MM).
    # 07:00 captures the early catalyst block (earnings/FDA/M&A print from
    # ~07:00 ET — see scanner.py docstring). Scanning earlier only runs the
    # Benzinga+Claude watchlist build; it makes NO price-API calls, so it adds
    # no pre-open Twelvedata/Finnhub load — the concurrent price-confirm fan-out
    # happens only at the open via evaluate_premarket_candidates().
    premarket_scan_start_et: str = field(
        default_factory=lambda: os.getenv("PREMARKET_SCAN_START_ET", "07:00")
    )
    # Gap band for at-open evaluation of pre-market candidates:
    #   gap < min  → the market doesn't believe the catalyst; skip.
    #   gap > max  → the move is fully paid out; entering buys exhaustion.
    min_gap_pct: float = field(
        default_factory=lambda: float(os.getenv("MIN_GAP_PCT", "1.0"))
    )
    max_gap_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_GAP_PCT", "20.0"))
    )

    # ── 24/5 extended-hours trading (v21) ─────────────────────────────────────
    # T212 supports four sessions (ET): premarket 04:00–09:30, regular
    # 09:30–16:00, after-hours 16:00–20:00, overnight 20:00–04:00 (Blue Ocean).
    # Master switch: mirrors whether 24/5 is enabled on the T212 account. When
    # False the system is exactly its pre-v21 RTH-only self.
    extended_hours_enabled: bool = field(
        default_factory=lambda: os.getenv("EXTENDED_HOURS_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    # After-hours entries (16:00–20:00 ET). THE reason v21 exists: FDA
    # decisions and guidance raises overwhelmingly print right after the
    # close — a window the system used to sleep through entirely.
    afterhours_trading_enabled: bool = field(
        default_factory=lambda: os.getenv("AFTERHOURS_TRADING_ENABLED", "true").lower() in ("1", "true", "yes")
    )
    # Direct pre-market entries (04:00–09:30 ET). Default OFF: the existing
    # pre-market scanner + at-open gap-and-go evaluation is the deliberate
    # pre-market strategy (thin 5am books, wide spreads; the at-open eval
    # trades the same news with confirmation). Enable only with evidence.
    premarket_trading_enabled: bool = field(
        default_factory=lambda: os.getenv("PREMARKET_TRADING_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    # Extended-session liquidity floor (ADV$). Extended tape is thin; only
    # names whose NORMAL book is institutional-depth get considered. $50M
    # matches T212's own "most liquid NYSE/NASDAQ" eligibility universe and
    # the v20.2 RVOL-bypass tier.
    extended_min_adv_dollar: float = field(
        default_factory=lambda: float(os.getenv("EXTENDED_MIN_ADV_DOLLAR", "50000000"))
    )
    # Extended-session participation floor: dollars actually traded in the
    # session since its start (session volume × price). Replaces the RVOL
    # band, whose time-of-day curve is calibrated on RTH volume shape and is
    # meaningless at 17:00. An after-hours catalyst on a $50M+ ADV name that
    # can't print $500k of tape is not a tradeable reaction.
    extended_min_session_dollar_volume: float = field(
        default_factory=lambda: float(os.getenv("EXTENDED_MIN_SESSION_DOLLAR_VOLUME", "500000"))
    )
    # Tighter spread proxy ceiling for extended sessions (RTH default is 3.0).
    extended_max_spread_pct: float = field(
        default_factory=lambda: float(os.getenv("EXTENDED_MAX_SPREAD_PCT", "1.5"))
    )
    # Position-size multiplier for extended-session entries. Half size: the
    # loss side is POLLED after hours (T212 stop orders execute RTH-only and
    # the API accepts extendedHours on market orders only), so per-trade risk
    # is cut at the sizing stage instead.
    extended_size_factor: float = field(
        default_factory=lambda: float(os.getenv("EXTENDED_SIZE_FACTOR", "0.5"))
    )
    # Force-flatten all positions this many minutes before the after-hours
    # session ends (19:45 ET by default). Non-negotiable: the overnight
    # session runs on Blue Ocean, which neither Finnhub nor Twelvedata
    # carries — a position held past 20:00 would be completely unmonitorable.
    extended_flatten_buffer_minutes: int = field(
        default_factory=lambda: int(os.getenv("EXTENDED_FLATTEN_BUFFER_MINUTES", "15"))
    )

    # ── Observability ─────────────────────────────────────────────────────────
    # Alert (system_event + CRITICAL log) when this many consecutive NYSE
    # trading sessions pass with signals flowing but ZERO trades. This is the
    # silent-failure tripwire: the 2026-06-23 incident ran 9 sessions of green
    # heartbeats and healthy news scoring while a data-budget collapse quietly
    # prevented every trade. A multi-day drought CAN be legitimate (genuinely bad
    # tape), so this alerts — it does not stand the system down. Default 3.
    zero_trade_alert_sessions: int = field(
        default_factory=lambda: int(os.getenv("ZERO_TRADE_ALERT_SESSIONS", "3"))
    )

    # ── News Settings ─────────────────────────────────────────────────────────
    blocklist: list[str] = field(
        default_factory=lambda: [
            t.strip().upper() for t in os.getenv("BLOCKLIST", "").split(",") if t.strip()
        ]
    )

    # ── Storage ───────────────────────────────────────────────────────────────
    db_url: str = field(default_factory=lambda: os.getenv("DB_URL", "postgresql://<db-user>:<db-password>@localhost:5432/momentum_trader"))

    def validate(self) -> None:
        """Raise if any required key is missing."""
        missing = []
        errors = []
        if self.trading_mode.lower() not in ("demo", "live"):
            errors.append("TRADING_MODE must be either 'demo' or 'live'")
        if self.is_live:
            if not self.trading212_api_key:
                missing.append("TRADING212_API_KEY")
            if not self.trading212_api_key_id:
                missing.append("TRADING212_API_KEY_ID")
        else:
            if not self.trading212_demo_api_key:
                missing.append("TRADING212_DEMO_API_KEY")
            if not self.trading212_demo_api_key_id:
                missing.append("TRADING212_DEMO_API_KEY_ID")
        if not self.benzinga_api_key:
            missing.append("MASSIVE_BENZINGA_API_KEY")
        if not self.finnhub_api_key:
            missing.append("FINNHUBIO_API_KEY")
        if not self.twelvedata_api_key:
            missing.append("TWELVEDATA_API_KEY")
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in your keys."
            )
        numeric_checks = [
            ("MIN_SENTIMENT_CONFIDENCE", 1 <= self.min_sentiment_confidence <= 10),
            ("MIN_CATALYST_MAGNITUDE", 1 <= self.min_catalyst_magnitude <= 5),
            ("MIN_PRICE_MOVE_PCT", self.min_price_move_pct >= 0),
            ("MAX_PRICE_MOVE_PCT", self.max_price_move_pct > self.min_price_move_pct),
            ("MAX_DAY_MOVE_PCT", self.max_day_move_pct > 0),
            # A ceiling at or below the take-profit target is self-defeating:
            # anything that could pay out would already be rejected on entry.
            ("MAX_DAY_MOVE_PCT > TAKE_PROFIT_PCT",
             self.max_day_move_pct > self.take_profit_pct),
            ("MAX_ENTRY_QUOTE_AGE_SECONDS", self.max_entry_quote_age_seconds > 0),
            ("MOMENTUM_LOOKBACK_MINUTES", self.momentum_lookback_minutes > 0),
            ("MAX_DAY_DROP_PCT", self.max_day_drop_pct > 0),
            ("MIN_DAILY_DOLLAR_VOLUME", self.min_daily_dollar_volume >= 0),
            ("MIN_RVOL", self.min_rvol >= 0),
            ("MAX_RVOL", self.max_rvol > self.min_rvol),
            ("RVOL_BYPASS_MIN_ADV_DOLLAR >= MIN_DAILY_DOLLAR_VOLUME",
             self.rvol_bypass_min_adv_dollar >= self.min_daily_dollar_volume),
            ("MIN_STOCK_PRICE", self.min_stock_price >= 0),
            ("MAX_SPREAD_PCT", self.max_spread_pct > 0),
            ("OPEN_BLOCK_MINUTES", self.open_block_minutes >= 0),
            ("MAX_POSITION_SIZE_PCT", self.max_position_size_pct > 0),
            ("RISK_PER_TRADE_PCT", self.risk_per_trade_pct > 0),
            ("MAX_ADV_PARTICIPATION_PCT", self.max_adv_participation_pct > 0),
            ("MAX_OPEN_POSITIONS", self.max_open_positions > 0),
            ("MAX_TRADES_PER_DAY", self.max_trades_per_day > 0),
            ("MAX_DAILY_LOSS_PCT", self.max_daily_loss_pct > 0),
            ("TAKE_PROFIT_PCT", self.take_profit_pct > 0),
            ("STOP_LOSS_PCT", self.stop_loss_pct > 0),
            ("TAKE_PROFIT_PCT >= STOP_LOSS_PCT (min 1:1 R:R required)", self.take_profit_pct >= self.stop_loss_pct),
            ("TIME_STOP_MINUTES", self.time_stop_minutes > 0),
            ("ENTRY_CUTOFF_MINUTES", self.entry_cutoff_minutes > 0),
            ("MONITOR_INTERVAL_SECONDS", self.monitor_interval_seconds > 0),
            ("EOD_FLATTEN_MINUTES", self.eod_flatten_minutes >= 0),
            ("SELL_LIMIT_SLACK_PCT", self.sell_limit_slack_pct >= 0),
            ("MIN_GAP_PCT/MAX_GAP_PCT", self.max_gap_pct > self.min_gap_pct),
            ("ZERO_TRADE_ALERT_SESSIONS", self.zero_trade_alert_sessions > 0),
            ("EXHAUSTION_MIN_RANGE_PCT", self.exhaustion_min_range_pct >= 0),
            ("EXHAUSTION_RECOVERY_THRESHOLD", 0 < self.exhaustion_recovery_threshold <= 1),
            ("MAX_VWAP_EXTENSION_PCT", self.max_vwap_extension_pct > 0),
            ("MAX_VWAP_EXTENSION_PCT < STOP_LOSS_PCT (stop must sit at/above value)",
             self.max_vwap_extension_pct < self.stop_loss_pct),
            ("RATCHET_TRIGGER_PCT", self.ratchet_trigger_pct > 0),
            ("RATCHET_LOCK_PCT", 0 <= self.ratchet_lock_pct < self.ratchet_trigger_pct),
            ("EXTENDED_MIN_ADV_DOLLAR >= MIN_DAILY_DOLLAR_VOLUME",
             self.extended_min_adv_dollar >= self.min_daily_dollar_volume),
            ("EXTENDED_MIN_SESSION_DOLLAR_VOLUME", self.extended_min_session_dollar_volume >= 0),
            ("EXTENDED_MAX_SPREAD_PCT", self.extended_max_spread_pct > 0),
            ("EXTENDED_SIZE_FACTOR", 0 < self.extended_size_factor <= 1.0),
            # ≥5 min: the flatten must fire with enough runway for a market
            # sell + at least one monitor-cycle retry before the venue change.
            ("EXTENDED_FLATTEN_BUFFER_MINUTES", self.extended_flatten_buffer_minutes >= 5),
        ]
        errors.extend(name for name, ok in numeric_checks if not ok)
        if errors:
            raise ValueError("Invalid trading configuration: " + "; ".join(errors))

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"


cfg = Settings()
