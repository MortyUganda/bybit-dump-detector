"""
Entry-point для ML-Short watcher сервиса.

Запускается как отдельный docker service:
  python -m app.services.ml_short_runner

Мониторит открытые paper-позиции (TP/SL/timeout).
Обработка сигналов происходит в analyzer через AlertManager.
"""
from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bybit.rest_client import BybitRestClient
from app.config import get_settings
from app.db.migrations.create_ml_short_tables import run_migration
from app.db.migrations.create_real_short_tables import run_migration as run_real_short_migration
from app.services.ml_short_watcher import MlShortWatcher
from app.services.real_short_service import RealShortService
from app.utils.logging import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


async def main() -> None:
    """Главная функция запуска ML-Short watcher."""
    logger.info("Запуск ML-Short watcher сервиса...")

    redis_client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
    )

    # Применить миграции
    try:
        await run_migration()
        await run_real_short_migration()
        logger.info("ML-Short: миграции применены")
    except Exception as exc:
        logger.warning(
            "ML-Short: миграции — возможно таблицы уже существуют",
            error=str(exc),
        )

    rest = BybitRestClient()
    await rest.start()

    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Real-shorts: закрывает реальные позиции зеркально к ml_short (close-хук).
    real_short = RealShortService(
        redis=redis_client,
        bot=bot,
        rest_client=rest,
    )

    watcher = MlShortWatcher(
        redis=redis_client,
        bot=bot,
        rest_client=rest,
        real_short_service=real_short,
    )
    await watcher.start()

    logger.info(
        "ML-Short watcher запущен — мониторинг позиций каждые 5с"
    )

    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        await watcher.stop()
        await rest.stop()
        await bot.session.close()
        await redis_client.aclose()
        logger.info("ML-Short watcher остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("ML-Short: завершение по сигналу")
