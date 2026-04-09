"""
Migration: Add ML enrichment columns to auto_shorts table.

Safe to run multiple times — uses IF NOT EXISTS pattern via
raw SQL (SQLAlchemy's create_all won't add columns to existing tables).

Run: python -m app.db.migrations.add_ml_columns
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
                   WHERE table_name='auto_shorts' AND column_name='btc_change_15m') THEN
        ALTER TABLE auto_shorts ADD COLUMN btc_change_15m FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='funding_rate_at_signal') THEN
        ALTER TABLE auto_shorts ADD COLUMN funding_rate_at_signal FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='oi_change_pct_at_signal') THEN
        ALTER TABLE auto_shorts ADD COLUMN oi_change_pct_at_signal FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='trend_strength_1h') THEN
        ALTER TABLE auto_shorts ADD COLUMN trend_strength_1h FLOAT;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("ML columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
