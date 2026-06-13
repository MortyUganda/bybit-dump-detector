"""
SQLAlchemy модели для Real-shorts (реальная торговля Bybit).

real_short_positions — реальная позиция, зеркало ml_short_positions
(ссылка через ml_signal_id / ml_position_id для сверки бумажного и реального PnL).
real_short_orders — лог всех реальных ордеров (вход, SL, закрытие).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Numeric,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class RealShortPosition(Base):
    """Реальная Bybit-позиция, открытая зеркально к ml_short."""

    __tablename__ = "real_short_positions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Связь с ml_short (для сверки бумажного и реального PnL)
    ml_signal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ml_position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False, default="Sell")

    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric, nullable=False)
    qty: Mapped[float] = mapped_column(Numeric, nullable=False)
    leverage: Mapped[float] = mapped_column(Numeric, nullable=False, default=10)
    margin_usdt: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    notional_usdt: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    tp_pct: Mapped[float] = mapped_column(Numeric, nullable=False, default=1.0)
    sl_pct: Mapped[float] = mapped_column(Numeric, nullable=False, default=1.0)
    tp_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    sl_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)

    testnet: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pnl_usdt: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

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
        Index("idx_real_short_positions_status", "status"),
        Index("idx_real_short_positions_symbol", "symbol"),
        Index("idx_real_short_positions_entry_ts", entry_ts.desc()),
        # Идемпотентность: один ml_signal_id → максимум одна реальная позиция
        Index("uq_real_short_positions_ml_signal", "ml_signal_id", unique=True),
    )


class RealShortOrder(Base):
    """Лог реальных ордеров Bybit (вход / SL / закрытие)."""

    __tablename__ = "real_short_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)

    order_type: Mapped[str] = mapped_column(Text, nullable=False)  # entry|sl|close
    side: Mapped[str] = mapped_column(Text, nullable=False)
    qty: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_link_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="submitted")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_real_short_orders_position", "position_id"),
        Index("idx_real_short_orders_symbol", "symbol"),
    )
