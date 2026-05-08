"""
SQLAlchemy модели для ml_short paper-trading сервиса.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class MlShortSignal(Base):
    """Все сигналы которые увидел ml_short (даже отфильтрованные)."""

    __tablename__ = "ml_short_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    signal_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    # ML decision
    ml_proba: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ml_decision: Mapped[str] = mapped_column(Text, nullable=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # кросс-ссылка на auto_short
    auto_short_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    auto_short_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # snapshot фич для оффлайн-анализа
    features_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_ml_short_signals_ts", signal_ts.desc()),
        Index("idx_ml_short_signals_symbol", "symbol"),
        Index("idx_ml_short_signals_decision", "ml_decision"),
    )


class MlShortPosition(Base):
    """Открытые/закрытые paper-позиции ml_short."""

    __tablename__ = "ml_short_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric, nullable=False)
    ml_proba: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    score: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    tp_pct: Mapped[float] = mapped_column(Numeric, nullable=False, default=10.0)
    sl_pct: Mapped[float] = mapped_column(Numeric, nullable=False, default=10.0)
    timeout_hours: Mapped[float] = mapped_column(Numeric, nullable=False, default=24)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_ml_short_positions_status", "status"),
        Index("idx_ml_short_positions_symbol", "symbol"),
        Index("idx_ml_short_positions_entry_ts", entry_ts.desc()),
    )


class MlShortCooldown(Base):
    """Cooldown после убытка для ml_short."""

    __tablename__ = "ml_short_cooldowns"

    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    loss_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_loss_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
