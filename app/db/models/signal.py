"""
signals table — fired alerts and their full context.

Retention: 30 days (purge via scheduled job)
Indexes: symbol + ts (most queries are "recent signals for symbol")
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models.base import Base


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # early_warning | overheated | reversal_risk | dump_started
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    # low | moderate | high | critical
    score: Mapped[float] = mapped_column(Float, nullable=False)
    triggered_count: Mapped[int] = mapped_column(Integer, default=0)
    # Top 3 factor names
    top_reasons: Mapped[str] = mapped_column(String(256), nullable=True)
    # Full factor breakdown as JSON
    factors_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Feature snapshot for audit
    features_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Price at signal time
    price_at_signal: Mapped[float] = mapped_column(Float, default=0.0)
    # Was alert actually sent to Telegram?
    alert_sent: Mapped[bool] = mapped_column(default=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
