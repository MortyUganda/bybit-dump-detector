"""
Migration: Add paper trading cost columns to auto_shorts table.

New columns: raw_pnl_pct, fee_pct, slippage_pct, funding_pct

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_paper_trading_costs
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
                   WHERE table_name='auto_shorts' AND column_name='raw_pnl_pct') THEN
        ALTER TABLE auto_shorts ADD COLUMN raw_pnl_pct FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='fee_pct') THEN
        ALTER TABLE auto_shorts ADD COLUMN fee_pct FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='slippage_pct') THEN
        ALTER TABLE auto_shorts ADD COLUMN slippage_pct FLOAT;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_name='auto_shorts' AND column_name='funding_pct') THEN
        ALTER TABLE auto_shorts ADD COLUMN funding_pct FLOAT;
    END IF;
END $$;
"""


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("Paper trading cost columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
