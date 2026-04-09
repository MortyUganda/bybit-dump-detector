"""
Migration: Add CVD divergence, liquidation cascade, and realized volatility
columns to auto_shorts table.

Safe to run multiple times — uses IF NOT EXISTS pattern via
raw SQL (SQLAlchemy's create_all won't add columns to existing tables).

Run: python -m app.db.migrations.add_cvd_liquidation_columns
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
                   WHERE table_name='auto_shorts' AND column_name='f_cvd_divergence') THEN
        ALTER TABLE auto_shorts ADD COLUMN f_cvd_divergence FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='f_liquidation_cascade') THEN
        ALTER TABLE auto_shorts ADD COLUMN f_liquidation_cascade FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='realized_vol_1h') THEN
        ALTER TABLE auto_shorts ADD COLUMN realized_vol_1h FLOAT;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("CVD/liquidation columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
