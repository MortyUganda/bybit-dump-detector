"""
symbols table — master list of tracked instruments.

Retention: permanent (updated on each universe refresh)
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Approximate listing date from Bybit instrument info
    listing_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Latest 24h stats (updated every universe refresh)
    volume_24h_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    last_price: Mapped[float] = mapped_column(Float, default=0.0)
    price_change_24h_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
