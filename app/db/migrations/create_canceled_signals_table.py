"""Создаёт таблицу canceled_signals если её нет.

Таблица не создавалась автоматически SQLAlchemy на части окружений.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings


CREATE_SQL = """
CREATE TABLE IF NOT EXISTS canceled_signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL,
    signal_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
    cancel_reason VARCHAR(32) NOT NULL,
    signal_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    signal_price DOUBLE PRECISION NOT NULL,
    final_price DOUBLE PRECISION NOT NULL,
    price_change_pct DOUBLE PRECISION NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    final_score DOUBLE PRECISION NOT NULL,
    min_score_at_entry DOUBLE PRECISION NOT NULL,
    entry_mode_candidate VARCHAR(32) NOT NULL DEFAULT 'direct',
    triggered_count INTEGER,
    entry_delay_sec INTEGER,
    monitor_attempts INTEGER,
    monitor_interval_sec INTEGER,
    stabilization_threshold_pct DOUBLE PRECISION,
    max_rise_pct DOUBLE PRECISION,
    max_entry_drop_pct DOUBLE PRECISION,
    f_rsi DOUBLE PRECISION,
    f_vwap_extension DOUBLE PRECISION,
    f_volume_zscore DOUBLE PRECISION,
    f_trade_imbalance DOUBLE PRECISION,
    f_large_buy_cluster DOUBLE PRECISION,
    f_price_acceleration DOUBLE PRECISION,
    f_consecutive_greens DOUBLE PRECISION,
    f_ob_bid_thinning DOUBLE PRECISION,
    f_spread_expansion DOUBLE PRECISION,
    f_momentum_loss DOUBLE PRECISION,
    f_upper_wick DOUBLE PRECISION,
    f_funding_rate DOUBLE PRECISION,
    f_rsi_5m DOUBLE PRECISION,
    f_large_sell_cluster DOUBLE PRECISION,
    f_cvd_divergence DOUBLE PRECISION,
    f_liquidation_cascade DOUBLE PRECISION,
    realized_vol_1h DOUBLE PRECISION,
    volume_24h_usdt DOUBLE PRECISION,
    price_change_5m DOUBLE PRECISION,
    price_change_1h DOUBLE PRECISION,
    spread_pct DOUBLE PRECISION,
    bid_depth_change_5m DOUBLE PRECISION,
    btc_change_15m DOUBLE PRECISION,
    funding_rate_at_signal DOUBLE PRECISION,
    oi_change_pct_at_signal DOUBLE PRECISION,
    trend_strength_1h DOUBLE PRECISION,
    ob_snapshot JSONB,
    ob_bid_volume_top10 DOUBLE PRECISION,
    ob_ask_volume_top10 DOUBLE PRECISION,
    ob_imbalance_top10 DOUBLE PRECISION,
    ob_spread_bps DOUBLE PRECISION,
    ob_bid_wall_price DOUBLE PRECISION,
    ob_bid_wall_size DOUBLE PRECISION,
    ob_ask_wall_price DOUBLE PRECISION,
    ob_ask_wall_size DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS ix_canceled_signals_symbol ON canceled_signals (symbol);
CREATE INDEX IF NOT EXISTS ix_canceled_signals_cancel_reason ON canceled_signals (cancel_reason);
CREATE INDEX IF NOT EXISTS ix_canceled_signals_signal_ts ON canceled_signals (signal_ts);
"""


async def run_migration() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        for stmt in CREATE_SQL.strip().split(";"):
            s = stmt.strip()
            if s:
                await conn.execute(text(s))
    await engine.dispose()
    print("OK: canceled_signals ready")


if __name__ == "__main__":
    asyncio.run(run_migration())
