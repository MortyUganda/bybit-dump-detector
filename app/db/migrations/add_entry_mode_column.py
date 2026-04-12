"""
Migration: Add entry_mode column to auto_shorts table.
Stores how the trade was opened: direct vs after_monitor.

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_entry_mode_column
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
                   WHERE table_name='auto_shorts' AND column_name='entry_mode') THEN
        ALTER TABLE auto_shorts ADD COLUMN entry_mode VARCHAR(32);
    END IF;

    UPDATE auto_shorts
    SET entry_mode = 'direct'
    WHERE entry_mode IS NULL;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("entry_mode column migration completed successfully")


if __name__ == "__main__": 
    asyncio.run(run_migration())