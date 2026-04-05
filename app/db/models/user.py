from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, func, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class UserSettings(Base):
    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    alerts_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    min_score_to_alert: Mapped[float] = mapped_column(Float, default=50.0)
    alert_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)

    notify_early_warning: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_overheated: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_reversal_risk: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_dump_started: Mapped[bool] = mapped_column(Boolean, default=True)

    language: Mapped[str] = mapped_column(String(8), default="en")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (
        UniqueConstraint("telegram_user_id", "symbol", name="uq_watchlists_user_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )