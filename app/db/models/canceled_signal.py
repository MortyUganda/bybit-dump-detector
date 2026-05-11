from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class CanceledSignal(Base):
    __tablename__ = "canceled_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    cancel_reason: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    signal_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    decision_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    signal_price: Mapped[float] = mapped_column(Float, nullable=False)
    final_price: Mapped[float] = mapped_column(Float, nullable=False)
    price_change_pct: Mapped[float] = mapped_column(Float, nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)
    min_score_at_entry: Mapped[float] = mapped_column(Float, nullable=False)

    entry_mode_candidate: Mapped[str] = mapped_column(String(32), nullable=False, default="direct")
    triggered_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    entry_delay_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monitor_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monitor_interval_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stabilization_threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_rise_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_entry_drop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

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
    f_rsi_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_large_sell_cluster: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_cvd_divergence: Mapped[float | None] = mapped_column(Float, nullable=True)
    f_liquidation_cascade: Mapped[float | None] = mapped_column(Float, nullable=True)

    realized_vol_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_change_1h: Mapped[float | None] = mapped_column(Float, nullable=True)
    spread_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_change_5m: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_change_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    funding_rate_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change_pct_at_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    trend_strength_1h: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Order book snapshot при сигнале (для ML) ───────────────────
    ob_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ob_bid_volume_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_volume_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_imbalance_top10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_spread_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_bid_wall_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_bid_wall_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_wall_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    ob_ask_wall_size: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Z-score нормализация по символу ───────────────────────────
    spread_pct_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_depth_change_5m_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_vol_1h_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_24h_usdt_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    oi_change_pct_z: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Режимные BTC-фичи ────────────────────────────────────────
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

    # ── Adverse move (движение цены против позиции за время delay) ──
    adverse_move_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── Post-cancel price tracking (для synthetic PnL и ML) ────────
    price_15m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_30m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_15m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_30m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    price_60m_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Мин/макс цены в окне 60 мин — чтобы не терять касание TP/SL между точками
    price_min_60m: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max_60m: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Synthetic PnL (что было бы, если бы вошли)
    synthetic_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    would_hit_tp: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    would_hit_sl: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    synthetic_close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    time_to_tp_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    time_to_sl_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)