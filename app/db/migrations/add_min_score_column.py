"""
Migration: Add min_score_at_entry column to auto_shorts table.
Stores the runtime min_score_to_enter value at the moment a short was opened.

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_min_score_column
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
                   WHERE table_name='auto_shorts' AND column_name='min_score_at_entry') THEN
        ALTER TABLE auto_shorts ADD COLUMN min_score_at_entry FLOAT;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("min_score_at_entry column migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
