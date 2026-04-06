"""
Paper trade — имитация сделки без реальных денег.
"""
from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.models.base import Base


class PaperTrade(Base):
    __tablename__ = "paper_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Монета и сигнал
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Вход
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc)
    )

    # Стратегия
    strategy: Mapped[str] = mapped_column(String(16), nullable=False)  # "10", "30", "50"
    tp1_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp2_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp3_pct: Mapped[float] = mapped_column(Float, nullable=False)
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False)

    # Цены TP/SL
    tp1_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp2_price: Mapped[float] = mapped_column(Float, nullable=False)
    tp3_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)

    # Результат
    status: Mapped[str] = mapped_column(String(16), default="open")
    # open / tp1 / tp2 / tp3 / sl / closed_manual
    exit_price: Mapped[float] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=True)

    # Пользователь
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)