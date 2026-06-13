"""
Миграция: таблицы Real-shorts (реальная торговля Bybit).

real_short_positions — реальные позиции (зеркало ml_short, ссылка ml_signal_id).
real_short_orders    — лог всех реальных ордеров (вход / SL / закрытие).

Стиль повторяет create_ml_short_tables.py: отдельные DDL-стейтменты,
exec_driver_sql по одному (asyncpg не любит multi-statement).
"""
from __future__ import annotations

import asyncio

from app.db.session import engine
from app.utils.logging import get_logger

logger = get_logger(__name__)

STATEMENTS: list[str] = [
    # Реальные позиции
    """
    CREATE TABLE IF NOT EXISTS real_short_positions (
        id BIGSERIAL PRIMARY KEY,
        ml_signal_id BIGINT,
        ml_position_id BIGINT,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL DEFAULT 'Sell',
        entry_ts TIMESTAMPTZ NOT NULL,
        entry_price NUMERIC NOT NULL,
        qty NUMERIC NOT NULL,
        leverage NUMERIC NOT NULL DEFAULT 10,
        margin_usdt NUMERIC,
        notional_usdt NUMERIC,
        tp_pct NUMERIC NOT NULL DEFAULT 1.0,
        sl_pct NUMERIC NOT NULL DEFAULT 1.0,
        tp_price NUMERIC,
        sl_price NUMERIC,
        testnet BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'open',
        exit_ts TIMESTAMPTZ,
        exit_price NUMERIC,
        pnl_pct NUMERIC,
        pnl_usdt NUMERIC,
        close_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_real_short_positions_status ON real_short_positions(status)",
    "CREATE INDEX IF NOT EXISTS idx_real_short_positions_symbol ON real_short_positions(symbol)",
    "CREATE INDEX IF NOT EXISTS idx_real_short_positions_entry_ts ON real_short_positions(entry_ts DESC)",
    # Идемпотентность: один ml_signal_id → максимум одна реальная позиция.
    # Частичный уникальный индекс (NULL не блокируем — ручные/без-сигнальные кейсы).
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_real_short_positions_ml_signal "
    "ON real_short_positions(ml_signal_id) WHERE ml_signal_id IS NOT NULL",
    # Лог ордеров
    """
    CREATE TABLE IF NOT EXISTS real_short_orders (
        id BIGSERIAL PRIMARY KEY,
        position_id BIGINT,
        symbol TEXT NOT NULL,
        order_type TEXT NOT NULL,
        side TEXT NOT NULL,
        qty NUMERIC,
        price NUMERIC,
        reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
        order_id TEXT,
        order_link_id TEXT,
        status TEXT NOT NULL DEFAULT 'submitted',
        error TEXT,
        raw_response JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_real_short_orders_position ON real_short_orders(position_id)",
    "CREATE INDEX IF NOT EXISTS idx_real_short_orders_symbol ON real_short_orders(symbol)",
    # Вью для сверки бумажного (ml_short) и реального PnL по signal_id
    """
    CREATE OR REPLACE VIEW real_vs_paper_pnl AS
    SELECT
        r.id              AS real_position_id,
        r.ml_signal_id,
        r.ml_position_id,
        r.symbol,
        r.entry_price     AS real_entry_price,
        r.exit_price      AS real_exit_price,
        r.pnl_pct         AS real_pnl_pct,
        r.pnl_usdt        AS real_pnl_usdt,
        r.close_reason    AS real_close_reason,
        r.testnet,
        m.entry_price     AS paper_entry_price,
        m.exit_price      AS paper_exit_price,
        m.pnl_pct         AS paper_pnl_pct,
        m.close_reason    AS paper_close_reason
    FROM real_short_positions r
    LEFT JOIN ml_short_positions m ON m.id = r.ml_position_id
    """,
]


async def run_migration() -> None:
    """Выполнить миграцию: создание таблиц Real-shorts."""
    async with engine.begin() as conn:
        for stmt in STATEMENTS:
            sql = stmt.strip()
            if not sql:
                continue
            await conn.exec_driver_sql(sql)
    logger.info("Миграция real_short таблиц выполнена успешно")


if __name__ == "__main__":
    asyncio.run(run_migration())
