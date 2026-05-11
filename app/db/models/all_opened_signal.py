"""
AllOpenedSignal -- shadow-paper сделка по КАЖДОМУ risk-сигналу.

Записывается независимо от того, открыл ли бот реальный auto_short,
отменил ли по фильтрам или заблокировал.  TP/SL всегда 10%/10% на марже.
Цель: золотой датасет для ML без bias текущих фильтров.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Boolean
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AllOpenedSignal(Base):
    __tablename__ = "all_opened_signals"

    # -- Идентификация --
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # -- Shadow-paper специфичные поля --
    would_have_opened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    actual_blocked_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linked_auto_short_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    linked_canceled_signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Вход --
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    signal_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # -- Параметры шорта (всегда 10%/10%) --
    leverage: Mapped[int | None] = mapped_column(Integer, default=10, nullable=True)
    tp_pct: Mapped[float] = mapped_column(Float, nullable=False)
    sl_pct: Mapped[float] = mapped_column(Float, nullable=False)
    tp_price: Mapped[float] = mapped_column(Float, nullable=False)
    sl_price: Mapped[float] = mapped_column(Float, nullable=False)

    # -- Scoring --
    score: Mapped[float] = mapped_column(Float, nullable=False)
    entry_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    triggered_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Результат --
    status: Mapped[str] = mapped_column(String(16), default="open")
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ml_label: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Risk factors --
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
    f_cvd_divergence: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_liquidation_cascade: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Market context --
    volume_24h_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_vol_1h: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- ML enrichment --
    btc_change_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change_pct_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_strength_1h: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Adverse move (движение цены против позиции за время delay) --
    adverse_move_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Order book snapshot при входе (для ML) --
    ob_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ob_bid_volume_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_volume_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_imbalance_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_bid_wall_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_bid_wall_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_wall_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_wall_size: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Z-score нормализация по символу --
    spread_pct_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_change_5m_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_vol_1h_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h_usdt_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change_pct_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Режимные BTC-фичи --
    btc_change_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_change_4h: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_change_24h: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_adx_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_atr_pct_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    recent_wr_20: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Taker buy ratio (агрессия покупателей) ──────────────────
    taker_buy_ratio_60s: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_buy_ratio_5s: Mapped[float | None] = mapped_column(Float, nullable=True)
    taker_buy_ratio_delta: Mapped[float | None] = mapped_column(Float, nullable=True)
