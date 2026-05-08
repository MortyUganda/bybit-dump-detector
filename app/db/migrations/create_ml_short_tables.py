"""
Миграция: создание таблиц ml_short_signals, ml_short_positions, ml_short_cooldowns.
Используются сервисом ml_short для paper-trading на основе ML-фильтрации.
"""
from __future__ import annotations

import asyncio

from app.db.session import engine
from app.utils.logging import get_logger

logger = get_logger(__name__)

SQL = """
-- Все сигналы которые увидел ml_short (даже отфильтрованные)
CREATE TABLE IF NOT EXISTS ml_short_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_ts TIMESTAMPTZ NOT NULL,
    symbol TEXT NOT NULL,
    signal_price NUMERIC,
    score NUMERIC,
    -- ML decision
    ml_proba NUMERIC,
    ml_decision TEXT NOT NULL,
    blocked_reason TEXT,
    -- кросс-ссылка на auto_short
    auto_short_decision TEXT,
    auto_short_position_id BIGINT,
    -- snapshot фич для оффлайн-анализа
    features_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_short_signals_ts
    ON ml_short_signals(signal_ts DESC);
CREATE INDEX IF NOT EXISTS idx_ml_short_signals_symbol
    ON ml_short_signals(symbol);
CREATE INDEX IF NOT EXISTS idx_ml_short_signals_decision
    ON ml_short_signals(ml_decision);

-- Открытые/закрытые paper-позиции ml_short
CREATE TABLE IF NOT EXISTS ml_short_positions (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT REFERENCES ml_short_signals(id),
    symbol TEXT NOT NULL,
    entry_ts TIMESTAMPTZ NOT NULL,
    entry_price NUMERIC NOT NULL,
    ml_proba NUMERIC,
    score NUMERIC,
    tp_pct NUMERIC NOT NULL DEFAULT 10.0,
    sl_pct NUMERIC NOT NULL DEFAULT 10.0,
    timeout_hours NUMERIC NOT NULL DEFAULT 24,
    status TEXT NOT NULL DEFAULT 'open',
    exit_ts TIMESTAMPTZ,
    exit_price NUMERIC,
    pnl_pct NUMERIC,
    close_reason TEXT,
    is_paper BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_short_positions_status
    ON ml_short_positions(status);
CREATE INDEX IF NOT EXISTS idx_ml_short_positions_symbol
    ON ml_short_positions(symbol);
CREATE INDEX IF NOT EXISTS idx_ml_short_positions_entry_ts
    ON ml_short_positions(entry_ts DESC);

-- Cooldown после убытка
CREATE TABLE IF NOT EXISTS ml_short_cooldowns (
    symbol TEXT PRIMARY KEY,
    loss_count INTEGER NOT NULL DEFAULT 0,
    last_loss_ts TIMESTAMPTZ,
    cooldown_until TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def run_migration() -> None:
    """Выполнить миграцию: создание таблиц ml_short."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(SQL)
    logger.info("Миграция ml_short таблиц выполнена успешно")


if __name__ == "__main__":
    asyncio.run(run_migration())
