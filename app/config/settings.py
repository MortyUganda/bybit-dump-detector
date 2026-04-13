"""
Central configuration via Pydantic Settings.
All values can be overridden via environment variables or .env file.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ──────────────────────────────────────────────────────
    env: str = "development"
    log_level: str = "INFO"
    service_name: str = "bot"

    # ── Telegram ─────────────────────────────────────────────────
    telegram_bot_token: str
    telegram_allowed_users: Optional[str] = None  # comma-separated IDs

    @computed_field
    @property
    def allowed_user_ids(self) -> list[int]:
        if not self.telegram_allowed_users:
            return []
        return [int(uid.strip()) for uid in self.telegram_allowed_users.split(",") if uid.strip()]

    # ── Bybit ─────────────────────────────────────────────────────
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_network: str = "mainnet"  # mainnet | testnet

    @computed_field
    @property
    def bybit_testnet(self) -> bool:
        return self.bybit_network == "testnet"

    # ── PostgreSQL ────────────────────────────────────────────────
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "dumpdetector"
    postgres_user: str = "dumpuser"
    postgres_password: str = ""

    @computed_field
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field
    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Redis ─────────────────────────────────────────────────────
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None

    @computed_field
    @property
    def redis_url(self) -> str:
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ── Universe Filters ─────────────────────────────────────────
    universe_min_24h_volume_usdt: float = 150_000_000.0
    universe_min_trades_per_hour: int = 50
    universe_min_listing_age_days: int = 14
    universe_exclude_symbols: str = "BTC,ETH,BNB,SOL,XRP,ADA,DOGE,AVAX,DOT,MATIC,LTC,LINK,UNI,ATOM,TRX"
    universe_refresh_interval: int = 300  # seconds

    @computed_field
    @property
    def excluded_base_assets(self) -> set[str]:
        return {s.strip().upper() for s in self.universe_exclude_symbols.split(",") if s.strip()}

    # ── Scoring ───────────────────────────────────────────────────
    score_alert_threshold: int = 50
    score_critical_threshold: int = 75

    # ── Alerts ───────────────────────────────────────────────────
    alert_cooldown_minutes: int = 60
    overvalued_top_n: int = 20

    # ── Feature Windows ──────────────────────────────────────────
    feature_window_short: int = 5    # minutes
    feature_window_medium: int = 15  # minutes
    feature_window_long: int = 60    # minutes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
