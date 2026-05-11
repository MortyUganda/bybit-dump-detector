"""Выгружает таблицы ml-short (ml_short_signals, ml_short_positions, ml_short_cooldowns) в CSV.

Использование:
    docker compose -p <env> -f docker/docker-compose.<env>.yml exec bot \\
        python -m scripts.export_ml_shorts_csv

Файлы создаются внутри контейнера в /app/exports/, копировать наружу:
    docker cp $(docker compose -p <env> -f docker/docker-compose.<env>.yml ps -q bot):/app/exports/. ./exports/
"""
from __future__ import annotations

import asyncio
import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings


EXPORT_DIR = Path("/app/exports")

# Таблицы ml-short и колонка для сортировки (нужна для воспроизводимого порядка).
TABLES: list[tuple[str, str]] = [
    ("ml_short_signals", "id"),
    ("ml_short_positions", "id"),
    ("ml_short_cooldowns", "symbol"),  # PK = symbol, не id
]


async def export_table(engine, table: str, order_by: str, out_path: Path) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(text(f"SELECT * FROM {table} ORDER BY {order_by}"))
        rows = result.fetchall()
        if not rows:
            print(f"  {table}: пусто")
            return 0
        cols = list(result.keys())

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt(v) for v in r])

    print(f"  {table}: {len(rows)} строк → {out_path.name}")
    return len(rows)


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    return v


async def main() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")

    print(f"Выгрузка ml-short в {EXPORT_DIR}/ (timestamp {ts})")
    total = 0
    for table, order_by in TABLES:
        try:
            total += await export_table(
                engine, table, order_by, EXPORT_DIR / f"{table}_{ts}.csv"
            )
        except Exception as exc:
            print(f"  {table}: ошибка — {exc}")

    print(f"\nГотово: {total} строк")
    print("Скопировать наружу:")
    print(
        "  docker cp $(docker compose -p <env> -f docker/docker-compose.<env>.yml "
        "ps -q bot):/app/exports/. ./exports/"
    )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
