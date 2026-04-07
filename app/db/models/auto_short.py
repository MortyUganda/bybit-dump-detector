"""
AutoShort — автоматическая paper шорт-сделка.
Открывается автоматически при score >= 45.
Все метрики сохраняются для обучения ИИ.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, Integer, String, DateTime, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AutoShort(Base):
    __tablename__ = "auto_shorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── Идентификация ─────────────────────────────────────────────
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── Вход ──────────────────────────────────────────────────────
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    entry_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # ── Уровни шорта (цена падает = прибыль) ─────────────────────
    tp_pct: Mapped[float] = mapped_column(Float, default=20.0)   # TP -20% от входа
    sl_pct: Mapped[float] = mapped_column(Float, default=10.0)   # SL +10% от входа
    tp_price: Mapped[float] = mapped_column(Float, nullable=False)  # entry * (1 - 0.20)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)  # entry * (1 + 0.10)

    # ── Результат ─────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(16), default="open")
    # open / tp_hit / sl_hit / closed_manual / expired
    exit_price: Mapped[float] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=True)
    # Для шорта: pnl = (entry - exit) / entry * 100
    # Положительный = прибыль (цена упала)
    # Отрицательный = убыток (цена выросла)

    # ── Цены через N минут после входа (для ИИ) ───────────────────
    price_15m: Mapped[float] = mapped_column(Float, nullable=True)
    price_30m: Mapped[float] = mapped_column(Float, nullable=True)
    price_60m: Mapped[float] = mapped_column(Float, nullable=True)
    price_15m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    price_30m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    price_60m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # Исход для обучения (заполняется после закрытия)
    # 1 = шорт был прибыльным (цена упала), 0 = убыток
    ml_label: Mapped[int] = mapped_column(Integer, nullable=True)

    # ── Метрики движка в момент входа (для ИИ) ───────────────────
    score: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Факторы движка
    f_rsi: Mapped[float] = mapped_column(Float, nullable=True)
    f_vwap_extension: Mapped[float] = mapped_column(Float, nullable=True)
    f_volume_zscore: Mapped[float] = mapped_column(Float, nullable=True)
    f_trade_imbalance: Mapped[float] = mapped_column(Float, nullable=True)
    f_large_buy_cluster: Mapped[float] = mapped_column(Float, nullable=True)
    f_price_acceleration: Mapped[float] = mapped_column(Float, nullable=True)
    f_consecutive_greens: Mapped[float] = mapped_column(Float, nullable=True)
    f_ob_bid_thinning: Mapped[float] = mapped_column(Float, nullable=True)
    f_spread_expansion: Mapped[float] = mapped_column(Float, nullable=True)
    f_momentum_loss: Mapped[float] = mapped_column(Float, nullable=True)
    f_upper_wick: Mapped[float] = mapped_column(Float, nullable=True)
    f_funding_rate: Mapped[float] = mapped_column(Float, nullable=True)

    # Рыночный контекст в момент входа
    volume_24h_usdt: Mapped[float] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float] = mapped_column(Float, nullable=True)
    bid_depth_change_5m: Mapped[float] = mapped_column(Float, nullable=True)