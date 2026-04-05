"""
overvalued_snapshots table — periodic rankings of risky coins.

Retention: 7 days
Indexes: created_at (for "latest snapshot"), symbol
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class OvervaluedSnapshot(Base):
    __tablename__ = "overvalued_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Snapshot batch ID — all rows in one snapshot share the same batch_id
    batch_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = most overvalued
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    price_change_24h_pct: Mapped[float] = mapped_column(Float, default=0.0)
    volume_24h_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    rsi: Mapped[float] = mapped_column(Float, default=50.0)
    vwap_extension_pct: Mapped[float] = mapped_column(Float, default=0.0)
    top_reasons: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Full features as JSON for diagnostics
    features_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
