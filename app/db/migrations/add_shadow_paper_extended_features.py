"""
Migration: Add OB snapshot, z-score normalization and BTC regime features
to all_opened_signals table — parity with auto_shorts / canceled_signals.

New columns (20):
  OB (9): ob_snapshot (JSONB), ob_bid_volume_top10, ob_ask_volume_top10,
          ob_imbalance_top10, ob_spread_bps,
          ob_bid_wall_price, ob_bid_wall_size, ob_ask_wall_price, ob_ask_wall_size
  Z-score (5): spread_pct_z, bid_depth_change_5m_z, realized_vol_1h_z,
               volume_24h_usdt_z, oi_change_pct_z
  BTC regime (5): btc_change_1h, btc_change_4h, btc_change_24h,
                  btc_adx_1h, btc_atr_pct_1h
  Context (1): recent_wr_20

Safe to run multiple times — uses IF NOT EXISTS pattern.

Run: python -m app.db.migrations.add_shadow_paper_extended_features
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

FLOAT_COLUMNS = [
    # OB numeric features
    "ob_bid_volume_top10",
    "ob_ask_volume_top10",
    "ob_imbalance_top10",
    "ob_spread_bps",
    "ob_bid_wall_price",
    "ob_bid_wall_size",
    "ob_ask_wall_price",
    "ob_ask_wall_size",
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

TABLE = "all_opened_signals"


def _build_sql() -> str:
    blocks = []
    # ob_snapshot is JSONB, not FLOAT
    blocks.append(
        f"    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
        f"                   WHERE table_name='{TABLE}' AND column_name='ob_snapshot') THEN\n"
        f"        ALTER TABLE {TABLE} ADD COLUMN ob_snapshot JSONB;\n"
        f"    END IF;"
    )
    for col in FLOAT_COLUMNS:
        blocks.append(
            f"    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
            f"                   WHERE table_name='{TABLE}' AND column_name='{col}') THEN\n"
            f"        ALTER TABLE {TABLE} ADD COLUMN {col} FLOAT;\n"
            f"    END IF;"
        )
    body = "\n\n".join(blocks)
    return f"DO $$\nBEGIN\n{body}\nEND $$;"


ADD_COLUMNS_SQL = _build_sql()


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("Shadow-paper extended features migration completed successfully")


if __name__ == "__main__":
    asyncio.run(run_migration())
