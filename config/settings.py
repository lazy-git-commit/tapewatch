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
    finlight_api_key: str = field(default_factory=lambda: os.getenv("FINLIGHTME_API_KEY", ""))

    # ── Trading Mode ──────────────────────────────────────────────────────────
    trading_mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "demo"))

    # ── Strategy Settings ─────────────────────────────────────────────────────
    min_sentiment_confidence: int = field(
        default_factory=lambda: int(os.getenv("MIN_SENTIMENT_CONFIDENCE", "7"))
    )
    min_price_move_pct: float = field(
        default_factory=lambda: float(os.getenv("MIN_PRICE_MOVE_PCT", "0.5"))
    )
    momentum_window_minutes: int = field(
        default_factory=lambda: int(os.getenv("MOMENTUM_WINDOW_MINUTES", "30"))
    )
    max_day_drop_pct: float = field(
        default_factory=lambda: float(os.getenv("MAX_DAY_DROP_PCT", "3.0"))
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
        if not self.finlight_api_key:
            missing.append("FINLIGHTME_API_KEY")
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in your keys."
            )

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"


cfg = Settings()
