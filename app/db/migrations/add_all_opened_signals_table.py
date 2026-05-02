"""
Миграция: создание таблицы all_opened_signals.

Shadow-paper сделка по КАЖДОМУ risk-сигналу для ML золотого датасета.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.db.session import engine


SQL = """
CREATE TABLE IF NOT EXISTS all_opened_signals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL,
    signal_type VARCHAR(32) NOT NULL,

    -- shadow-paper специфичные поля
    would_have_opened BOOLEAN NOT NULL DEFAULT FALSE,
    actual_blocked_by VARCHAR(64) NULL,
    linked_auto_short_id INTEGER NULL,
    linked_canceled_signal_id INTEGER NULL,

    -- вход
    entry_price DOUBLE PRECISION NOT NULL,
    signal_price DOUBLE PRECISION NULL,
    entry_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- параметры шорта (всегда 10%/10%)
    leverage INTEGER DEFAULT 10,
    tp_pct DOUBLE PRECISION NOT NULL,
    sl_pct DOUBLE PRECISION NOT NULL,
    tp_price DOUBLE PRECISION NOT NULL,
    sl_price DOUBLE PRECISION NOT NULL,

    -- scoring
    score DOUBLE PRECISION NOT NULL,
    entry_score DOUBLE PRECISION NULL,
    triggered_count INTEGER NULL,

    -- результат
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    close_reason VARCHAR(20) NULL,
    exit_price DOUBLE PRECISION NULL,
    exit_ts TIMESTAMPTZ NULL,
    pnl_pct DOUBLE PRECISION NULL,
    ml_label INTEGER NULL,

    -- risk factors
    f_rsi_5m DOUBLE PRECISION NULL,
    f_large_sell_cluster DOUBLE PRECISION NULL,
    f_rsi DOUBLE PRECISION NULL,
    f_vwap_extension DOUBLE PRECISION NULL,
    f_volume_zscore DOUBLE PRECISION NULL,
    f_trade_imbalance DOUBLE PRECISION NULL,
    f_large_buy_cluster DOUBLE PRECISION NULL,
    f_price_acceleration DOUBLE PRECISION NULL,
    f_consecutive_greens DOUBLE PRECISION NULL,
    f_ob_bid_thinning DOUBLE PRECISION NULL,
    f_spread_expansion DOUBLE PRECISION NULL,
    f_momentum_loss DOUBLE PRECISION NULL,
    f_upper_wick DOUBLE PRECISION NULL,
    f_funding_rate DOUBLE PRECISION NULL,
    f_cvd_divergence DOUBLE PRECISION NULL,
    f_liquidation_cascade DOUBLE PRECISION NULL,

    -- market context
    volume_24h_usdt DOUBLE PRECISION NULL,
    price_change_5m DOUBLE PRECISION NULL,
    price_change_1h DOUBLE PRECISION NULL,
    spread_pct DOUBLE PRECISION NULL,
    bid_depth_change_5m DOUBLE PRECISION NULL,
    realized_vol_1h DOUBLE PRECISION NULL,

    -- ML enrichment
    btc_change_15m DOUBLE PRECISION NULL,
    funding_rate_at_signal DOUBLE PRECISION NULL,
    oi_change_pct_at_signal DOUBLE PRECISION NULL,
    trend_strength_1h DOUBLE PRECISION NULL
);

CREATE INDEX IF NOT EXISTS ix_all_opened_signals_symbol
    ON all_opened_signals(symbol);

CREATE INDEX IF NOT EXISTS ix_all_opened_signals_entry_ts
    ON all_opened_signals(entry_ts);

CREATE INDEX IF NOT EXISTS ix_all_opened_signals_would_have_opened
    ON all_opened_signals(would_have_opened);

CREATE INDEX IF NOT EXISTS ix_all_opened_signals_actual_blocked_by
    ON all_opened_signals(actual_blocked_by);
"""


async def main() -> None:
    async with engine.begin() as conn:
        for stmt in SQL.split(";"):
            stmt = stmt.strip()
            if stmt:
                await conn.execute(text(stmt))
    print("all_opened_signals table created")


if __name__ == "__main__":
    asyncio.run(main())
