"""
Migration: Add order book snapshot columns to auto_shorts and canceled_signals.

Saves top-10 bids/asks structure + aggregated OB metrics at entry moment for ML training.

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_ob_snapshot_columns
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

# Columns to add to both tables
_OB_COLUMNS = [
    ("ob_snapshot", "JSONB"),
    ("ob_bid_volume_top10", "DOUBLE PRECISION"),
    ("ob_ask_volume_top10", "DOUBLE PRECISION"),
    ("ob_imbalance_top10", "DOUBLE PRECISION"),
    ("ob_spread_bps", "DOUBLE PRECISION"),
    ("ob_bid_wall_price", "DOUBLE PRECISION"),
    ("ob_bid_wall_size", "DOUBLE PRECISION"),
    ("ob_ask_wall_price", "DOUBLE PRECISION"),
    ("ob_ask_wall_size", "DOUBLE PRECISION"),
]

_TABLES = ["auto_shorts", "canceled_signals"]


def _build_sql() -> str:
    blocks: list[str] = []
    for table in _TABLES:
        for col_name, col_type in _OB_COLUMNS:
            blocks.append(
                f"    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
                f"                   WHERE table_name='{table}' AND column_name='{col_name}') THEN\n"
                f"        ALTER TABLE {table} ADD COLUMN {col_name} {col_type};\n"
                f"    END IF;"
            )
    body = "\n\n".join(blocks)
    return f"DO $$\nBEGIN\n{body}\nEND $$;"


ADD_COLUMNS_SQL = _build_sql()


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("OB snapshot columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
