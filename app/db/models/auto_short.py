"""
AutoShort — автоматическая paper short-сделка.
Открывается автоматически при сигнале.
Все метрики сохраняются для дальнейшего анализа и обучения модели.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AutoShort(Base):
    __tablename__ = "auto_shorts"

    # ── Идентификация ─────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── Вход ──────────────────────────────────────────────────────
    signal_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_change_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    entry_delay_sec: Mapped[int | None] = mapped_column(Integer, default=90, nullable=True)

    # ── Параметры шорта ───────────────────────────────────────────
    leverage: Mapped[int | None] = mapped_column(Integer, default=10, nullable=True)
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False)
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)

    # ── Метрики score ─────────────────────────────────────────────
    score: Mapped[float] = mapped_column(Float, nullable=False)
    entry_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_score_at_entry: Mapped[float | None] = mapped_column(Float, nullable=True)
    triggered_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Результат ─────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(16), default="open")
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ml_label: 1 = прибыль, 0 = убыток
    ml_label: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── Цены через N минут после входа (для ИИ) ───────────────────
    price_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_15m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_30m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_60m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Метрики движка в момент сигнала (для ИИ) ──────────────────
    f_rsi_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_large_sell_cluster: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_vwap_extension: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_volume_zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_trade_imbalance: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_large_buy_cluster: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_price_acceleration: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_consecutive_greens: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_ob_bid_thinning: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_spread_expansion: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_momentum_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_upper_wick: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_funding_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Рыночный контекст ─────────────────────────────────────────
    volume_24h_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── ML enrichment columns ─────────────────────────────────────
    btc_change_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change_pct_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_strength_1h: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── CVD / Liquidation / Volatility ────────────────────────────
    f_cvd_divergence: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_liquidation_cascade: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_vol_1h: Mapped[float | None] = mapped_column(Float, nullable=True)