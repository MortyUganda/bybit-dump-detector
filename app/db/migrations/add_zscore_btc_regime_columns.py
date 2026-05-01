"""
Migration: Add z-score normalization columns and BTC regime features
to auto_shorts and canceled_signals tables.

Z-score (per-symbol, window 200):
  - spread_pct_z, bid_depth_change_5m_z
  - realized_vol_1h_z, volume_24h_usdt_z
  - oi_change_pct_z

BTC regime features:
  - btc_change_1h, btc_change_4h, btc_change_24h
  - btc_adx_1h, btc_atr_pct_1h
  - recent_wr_20

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_zscore_btc_regime_columns
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

NEW_COLUMNS = [
    # Z-score columns
    "spread_pct_z",
    "bid_depth_change_5m_z",
    "realized_vol_1h_z",
    "volume_24h_usdt_z",
    "oi_change_pct_z",
    # BTC regime columns
    "btc_change_1h",
    "btc_change_4h",
    "btc_change_24h",
    "btc_adx_1h",
    "btc_atr_pct_1h",
    "recent_wr_20",
]

TABLES = ["auto_shorts", "canceled_signals"]


def _build_sql() -> str:
    blocks = []
    for table in TABLES:
        for col in NEW_COLUMNS:
            blocks.append(
                f"    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
                f"                   WHERE table_name='{table}' AND column_name='{col}') THEN\n"
                f"        ALTER TABLE {table} ADD COLUMN {col} FLOAT;\n"
                f"    END IF;"
            )
    body = "\n\n".join(blocks)
    return f"DO $$\nBEGIN\n{body}\nEND $$;"


ADD_COLUMNS_SQL = _build_sql()


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("Z-score + BTC regime columns migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
