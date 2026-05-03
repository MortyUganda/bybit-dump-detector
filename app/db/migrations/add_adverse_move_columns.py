"""
Migration: Add adverse_move_pct column to canceled_signals and all_opened_signals tables.

Tracks the adverse price movement (%) during entry delay for analysis.

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_adverse_move_columns
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
                   WHERE table_name='canceled_signals' AND column_name='adverse_move_pct') THEN
        ALTER TABLE canceled_signals ADD COLUMN adverse_move_pct DOUBLE PRECISION;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='all_opened_signals' AND column_name='adverse_move_pct') THEN
        ALTER TABLE all_opened_signals ADD COLUMN adverse_move_pct DOUBLE PRECISION;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("adverse_move_pct migration completed successfully (canceled_signals + all_opened_signals)")


if __name__ == "__main__":
    asyncio.run(run_migration())
