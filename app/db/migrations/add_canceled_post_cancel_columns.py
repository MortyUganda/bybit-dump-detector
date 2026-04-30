"""
Миграция: добавляет post-cancel price tracking + synthetic PnL в canceled_signals.

Безопасна для повторного запуска (IF NOT EXISTS).

Запуск: python -m app.db.migrations.add_canceled_post_cancel_columns
"""
import asyncio

from app.db.session import engine
from app.utils.logging import get_logger, setup_logging

setup_logging("INFO")
logger = get_logger(__name__)

_COLUMNS = [
    ("price_15m", "DOUBLE PRECISION"),
    ("price_30m", "DOUBLE PRECISION"),
    ("price_60m", "DOUBLE PRECISION"),
    ("price_15m_ts", "TIMESTAMPTZ"),
    ("price_30m_ts", "TIMESTAMPTZ"),
    ("price_60m_ts", "TIMESTAMPTZ"),
    ("price_min_60m", "DOUBLE PRECISION"),
    ("price_max_60m", "DOUBLE PRECISION"),
    ("synthetic_pnl_pct", "DOUBLE PRECISION"),
    ("would_hit_tp", "BOOLEAN"),
    ("would_hit_sl", "BOOLEAN"),
    ("synthetic_close_reason", "VARCHAR(32)"),
    ("time_to_tp_sec", "INTEGER"),
    ("time_to_sl_sec", "INTEGER"),
]

_TABLE = "canceled_signals"


def _build_sql() -> str:
    blocks: list[str] = []
    for col_name, col_type in _COLUMNS:
        blocks.append(
            f"    IF NOT EXISTS (SELECT 1 FROM information_schema.columns\n"
            f"                   WHERE table_name='{_TABLE}' AND column_name='{col_name}') THEN\n"
            f"        ALTER TABLE {_TABLE} ADD COLUMN {col_name} {col_type};\n"
            f"    END IF;"
        )
    body = "\n\n".join(blocks)
    return f"DO $$\nBEGIN\n{body}\nEND $$;"


ADD_COLUMNS_SQL = _build_sql()


async def run_migration() -> None:
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(ADD_COLUMNS_SQL))
    logger.info("Post-cancel price tracking columns migration completed")


if __name__ == "__main__":
    asyncio.run(run_migration())
