"""
Migration: Add taker_buy_ratio_60s / _5s / _delta columns
to auto_shorts, canceled_signals и all_opened_signals.

Идемпотентна (IF NOT EXISTS).

Run: python -m app.db.migrations.add_taker_buy_ratio_columns
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)


def _build_sql(table: str) -> str:
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='{table}' AND column_name='taker_buy_ratio_60s') THEN
        ALTER TABLE {table} ADD COLUMN taker_buy_ratio_60s DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='{table}' AND column_name='taker_buy_ratio_5s') THEN
        ALTER TABLE {table} ADD COLUMN taker_buy_ratio_5s DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='{table}' AND column_name='taker_buy_ratio_delta') THEN
        ALTER TABLE {table} ADD COLUMN taker_buy_ratio_delta DOUBLE PRECISION;
    END IF;
END $$;
"""


TABLES = ("auto_shorts", "canceled_signals", "all_opened_signals")


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(_build_sql(table)))
            logger.info("taker_buy_ratio columns added", table=table)
    logger.info("taker_buy_ratio columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
