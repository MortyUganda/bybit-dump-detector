"""
Migration: Add retrospective price columns to canceled_signals table.

Tracks price at 15m/30m/60m after cancellation for ML analysis.

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_canceled_price_columns
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

ADD_COLUMNS_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_15m') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_15m DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_30m') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_30m DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_60m') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_60m DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_15m_ts') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_15m_ts TIMESTAMPTZ;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_30m_ts') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_30m_ts TIMESTAMPTZ;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='canceled_signals' AND column_name='price_60m_ts') THEN
        ALTER TABLE canceled_signals ADD COLUMN price_60m_ts TIMESTAMPTZ;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("canceled_signals price columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
