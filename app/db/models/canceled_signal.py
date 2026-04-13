from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String
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