"""
SymbolStatsUpdater — фоновая задача для пересчёта per-symbol статистики.

Раз в SYMBOL_STATS_REFRESH_SEC (default 60) считает из auto_shorts:
  - recent_wr_20: WR последних 20 закрытых трейдов символа
  - recent_wr_5: WR последних 5 закрытых трейдов символа
  - trades_count_24h: кол-во трейдов за последние 24h
  - avg_pnl_5: средний pnl_pct последних 5 закрытых трейдов

Результат — Redis hash ml_features:symbol_stats:{symbol}, TTL 600s.
Inference (auto_short_service, ml_short_service) читает sync с дефолтами.

Запускается ТОЛЬКО в analyzer. ml_short только читает Redis.
"""
from __future__ import annotations

import asyncio
import os
import time

import redis.asyncio as aioredis

from app.utils.logging import get_logger

logger = get_logger(__name__)

REDIS_KEY_PREFIX = "ml_features:symbol_stats:"
REDIS_TTL = 600  # 10 мин — если updater упал, фичи протухают

# Интервал обновления (env SYMBOL_STATS_REFRESH_SEC, default 60)
REFRESH_SEC = int(os.environ.get("SYMBOL_STATS_REFRESH_SEC", "60"))

# SQL для расчёта per-symbol статистики
# CTE: для каждого символа — последние 20 закрытых трейдов с pnl_pct
_STATS_SQL = """
WITH ranked AS (
    SELECT
        symbol,
        pnl_pct,
        entry_ts,
        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY entry_ts DESC) AS rn
    FROM auto_shorts
    WHERE status = 'closed'
      AND close_reason IN ('tp_hit', 'sl_hit')
      AND pnl_pct IS NOT NULL
),
last_24h AS (
    SELECT symbol, COUNT(*) AS cnt_24h
    FROM auto_shorts
    WHERE status = 'closed'
      AND close_reason IN ('tp_hit', 'sl_hit')
      AND entry_ts >= NOW() - INTERVAL '24 hours'
    GROUP BY symbol
)
SELECT
    r.symbol,
    -- WR последних 20
    COUNT(CASE WHEN r.rn <= 20 AND r.pnl_pct > 0 THEN 1 END)::float
        / NULLIF(COUNT(CASE WHEN r.rn <= 20 THEN 1 END), 0) AS wr_20,
    -- WR последних 5
    COUNT(CASE WHEN r.rn <= 5 AND r.pnl_pct > 0 THEN 1 END)::float
        / NULLIF(COUNT(CASE WHEN r.rn <= 5 THEN 1 END), 0) AS wr_5,
    -- Avg PnL последних 5
    AVG(CASE WHEN r.rn <= 5 THEN r.pnl_pct END) AS avg_pnl_5,
    -- Кол-во за 24h
    COALESCE(h.cnt_24h, 0) AS trades_count_24h
FROM ranked r
LEFT JOIN last_24h h ON r.symbol = h.symbol
GROUP BY r.symbol, h.cnt_24h
"""


class SymbolStatsUpdater:
    """Фоновый пересчёт per-symbol ML-статистики → Redis."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis
        self._log_counter = 0

    async def recalculate_all(self) -> int:
        """Пересчитать статистику для всех символов, записать в Redis.

        Returns: количество обновлённых символов.
        """
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        t0 = time.monotonic()
        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(text(_STATS_SQL))
                rows = result.fetchall()
        except Exception as exc:
            logger.error("Ошибка SQL при расчёте symbol stats", error=str(exc))
            return 0

        pipe = self._redis.pipeline()
        for row in rows:
            symbol, wr_20, wr_5, avg_pnl_5, cnt_24h = row
            key = f"{REDIS_KEY_PREFIX}{symbol}"
            mapping = {
                "recent_wr_20": str(round(wr_20, 4)) if wr_20 is not None else "0.5",
                "recent_wr_5": str(round(wr_5, 4)) if wr_5 is not None else "0.5",
                "avg_pnl_5": str(round(avg_pnl_5, 4)) if avg_pnl_5 is not None else "0.0",
                "trades_count_24h": str(int(cnt_24h)),
                "updated_at": str(int(time.time())),
            }
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, REDIS_TTL)

        await pipe.execute()

        elapsed_ms = (time.monotonic() - t0) * 1000
        # Логируем раз в 5 минут (каждые ~5 итераций при 60s refresh)
        self._log_counter += 1
        if self._log_counter % 5 == 1 or self._log_counter == 1:
            logger.info(
                "Обновлено symbol stats",
                symbols=len(rows),
                elapsed_ms=round(elapsed_ms, 1),
            )

        return len(rows)

    async def run_loop(self) -> None:
        """Бесконечный цикл: первый запуск сразу, потом каждые REFRESH_SEC."""
        logger.info(
            "SymbolStatsUpdater запущен",
            refresh_sec=REFRESH_SEC,
            redis_ttl=REDIS_TTL,
        )
        while True:
            try:
                await self.recalculate_all()
            except Exception as exc:
                logger.error("SymbolStatsUpdater iteration failed", error=str(exc))
            await asyncio.sleep(REFRESH_SEC)
