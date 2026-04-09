"""
AutoShort — автоматическая paper шорт-сделка.
Открывается автоматически при score >= 45.
Все метрики сохраняются для обучения ИИ.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AutoShort(Base):
    __tablename__ = "auto_shorts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # ── Идентификация ─────────────────────────────────────────────
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # ── Вход ──────────────────────────────────────────────────────
    signal_price: Mapped[float] = mapped_column(Float, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_change_at_entry: Mapped[float] = mapped_column(Float, nullable=True)
    entry_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    entry_delay_sec: Mapped[int] = mapped_column(Integer, default=90, nullable=True)

    # ── Параметры шорта ───────────────────────────────────────────
    leverage: Mapped[int] = mapped_column(Integer, default=10, nullable=True)
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False)
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)


    # ── Результат ─────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(16), default="open")
    exit_price: Mapped[float] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_pct: Mapped[float] = mapped_column(Float, nullable=True)

    # ── Цены через N минут после входа (для ИИ) ───────────────────
    price_15m: Mapped[float] = mapped_column(Float, nullable=True)
    price_30m: Mapped[float] = mapped_column(Float, nullable=True)
    price_60m: Mapped[float] = mapped_column(Float, nullable=True)
    price_15m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    price_30m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    price_60m_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # ml_label: 1 = прибыль, 0 = убыток
    ml_label: Mapped[int] = mapped_column(Integer, nullable=True)

    # ── Метрики движка в момент сигнала (для ИИ) ─────────────────
    score: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_count: Mapped[int] = mapped_column(Integer, nullable=False)

    f_rsi_5m: Mapped[float] = mapped_column(Float, nullable=True)        # новый
    f_large_sell_cluster: Mapped[float] = mapped_column(Float, nullable=True)  # новый
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

    # ── Рыночный контекст ─────────────────────────────────────────
    volume_24h_usdt: Mapped[float] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float] = mapped_column(Float, nullable=True)
    bid_depth_change_5m: Mapped[float] = mapped_column(Float, nullable=True)

    # ── ML enrichment columns ────────────────────────────────────
    btc_change_15m: Mapped[float] = mapped_column(Float, nullable=True)
    funding_rate_at_signal: Mapped[float] = mapped_column(Float, nullable=True)
    oi_change_pct_at_signal: Mapped[float] = mapped_column(Float, nullable=True)
    trend_strength_1h: Mapped[float] = mapped_column(Float, nullable=True)

    # ── CVD / Liquidation / Volatility columns ────────────────────
    f_cvd_divergence: Mapped[float] = mapped_column(Float, nullable=True)
    f_liquidation_cascade: Mapped[float] = mapped_column(Float, nullable=True)
    realized_vol_1h: Mapped[float] = mapped_column(Float, nullable=True)