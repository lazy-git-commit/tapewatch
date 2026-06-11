"""
config/settings.py
──────────────────
Loads all settings from environment variables (via .env).
Import this anywhere with: from config.settings import cfg
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

    # ── Trading Mode ──────────────────────────────────────────────────────────
    trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "demo"))

    # ── Strategy Settings ─────────────────────────────────────────────────────
    min_sentiment_confidence: int = field(
        default_factory=lambda: int(os.getenv("MIN_SENTIMENT_CONFIDENCE", "7"))
    )
    min_price_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MIN_PRICE_MOVE_PCT", "1.5"))
    )
    momentum_window_minutes: int = field(
        default_factory=lambda: int(os.getenv("MOMENTUM_WINDOW_MINUTES", "15"))
    )
    max_day_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAY_DROP_PCT", "3.0"))
    )
    # Minimum daily dollar volume (price × shares traded). Stocks below this
    # threshold are too illiquid — market sell orders can move price 10%+ vs
    # the trigger price (observed: GOAI −18.99% on stop-loss exit, $390k ADV).
    min_daily_dollar_volume: float = field(
        default_factory=lambda: float(os.getenv("MIN_DAILY_DOLLAR_VOLUME", "1000000"))
    )
    # Block trades during the first N minutes after open. Opening auction noise
    # (1-min observed: GOAI entire spike in 09:30 bar, bought at 09:32 into collapse).
    open_block_minutes: int = field(
        default_factory=lambda: int(os.getenv("OPEN_BLOCK_MINUTES", "5"))
    )
    # Reject signals where the stock has already moved more than this % in the last
    # 5 min. A +30%+ momentum reading means we are buying the top after a halt/spike —
    # the circuit-breaker halt article arrives AFTER the move. All Jun 8–11 losses
    # were on halt articles with day_move_pct > 20%.
    max_price_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_PRICE_MOVE_PCT", "15.0"))
    )
    # Reject signals where the volume ratio is above this ceiling. Extreme volume
    # (>20× average) on micro-caps is the hallmark of a circuit-breaker halt pattern,
    # not a genuine catalyst. All Jun 8–11 halt-article trades had vol_ratio > 30×.
    max_volume_ratio: float = field(
        default_factory=lambda: float(os.getenv("MAX_VOLUME_RATIO", "20.0"))
    )
    # Reject stocks trading below this price. Sub-$2 stocks have catastrophic
    # spread/slippage and all observed losses this week were on stocks < $5.
    min_stock_price: float = field(
        default_factory=lambda: float(os.getenv("MIN_STOCK_PRICE", "2.0"))
    )
    max_position_size_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE_PCT", "5.0"))
    )
    take_profit_pct: float = field(
        default_factory=lambda: float(os.getenv("TAKE_PROFIT_PCT", "5.0"))
    )
    stop_loss_pct: float = field(
        default_factory=lambda: float(os.getenv("STOP_LOSS_PCT", "2.0"))
    )
    time_stop_minutes: int = field(
        default_factory=lambda: int(os.getenv("TIME_STOP_MINUTES", "60"))
    )

    # ── News Settings ─────────────────────────────────────────────────────────
    news_poll_interval_minutes: int = field(
        default_factory=lambda: int(os.getenv("NEWS_POLL_INTERVAL_MINUTES", "5"))
    )
    blocklist: list[str] = field(
        default_factory=lambda: [
            t.strip() for t in os.getenv("BLOCKLIST", "").split(",") if t.strip()
        ]
    )

    # ── Storage ───────────────────────────────────────────────────────────────
    db_url: str = field(default_factory=lambda: os.getenv("DB_URL", "postgresql://<db-user>:<db-password>@localhost:5432/momentum_trader"))

    def validate(self) -> None:
        """Raise if any required key is missing."""
        missing = []
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

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"


cfg = Settings()
