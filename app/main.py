"""
Entry point for all services.
Usage:
  python -m app.main bot        — Start Telegram bot
  python -m app.main ingestion  — Start data ingestion service
  python -m app.main analyzer   — Start analyzer + scoring service
  python -m app.main all        — Start everything in one process (dev mode)
"""
from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.utils.logging import get_logger, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger(__name__)


async def run_bot() -> None:
    """Start the Telegram bot (polling mode)."""
    from app.bot.dispatcher import create_bot, create_dispatcher

    bot = create_bot()
    dp = create_dispatcher(settings.redis_url)

    logger.info("Starting Telegram bot")
    await dp.start_polling(bot)


async def run_ingestion() -> None:
    """Start market data ingestion from Bybit."""
    import redis.asyncio as aioredis
    from app.bybit.rest_client import BybitRestClient
    from app.bybit.universe import UniverseManager
    from app.services.ingestion import IngestionService

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    rest = BybitRestClient()
    await rest.start()

    universe = UniverseManager(rest)
    ingestion = IngestionService(rest, universe, redis_client)

    await ingestion.start()
    logger.info("Ingestion service running — Ctrl+C to stop")

    try:
        await universe.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await ingestion.stop()
        await rest.stop()
        await redis_client.aclose()


async def run_analyzer() -> None:
    """Start the analyzer + scoring + alert service."""
    import redis.asyncio as aioredis
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    from app.bybit.rest_client import BybitRestClient
    from app.bybit.universe import UniverseManager
    from app.services.alert_manager import AlertManager
    from app.services.analyzer import AnalyzerService
    from app.services.auto_short_service import AutoShortService
    from app.services.ingestion import IngestionService

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    rest = BybitRestClient()
    await rest.start()

    universe = UniverseManager(rest)
    ingestion = IngestionService(rest, universe, redis_client)
    await ingestion.start()

    # Создаём бот для отправки уведомлений из analyzer
    bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Создаём AutoShortService
    auto_short = AutoShortService(redis=redis_client, bot=bot)

    # Восстанавливаем активные сделки из БД после перезапуска
    await auto_short.restore_active_trades()  # ← добавить

    # Создаём AlertManager с AutoShortService
    alert_mgr = AlertManager(bot=bot, auto_short_service=auto_short)


    analyzer = AnalyzerService(
        ingestion=ingestion,
        redis=redis_client,
        alert_callback=alert_mgr.send_alert,
    )
    await analyzer.start()

    logger.info("Analyzer service running — Ctrl+C to stop")

    try:
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass
    finally:
        await analyzer.stop()
        await ingestion.stop()
        await rest.stop()
        await bot.session.close()
        await redis_client.aclose()


async def run_all() -> None:
    """Run all services in one process (development mode only)."""
    logger.warning("Running ALL services in single process — dev mode only!")
    await asyncio.gather(
        run_bot(),
        run_analyzer(),
    )


def main() -> None:
    service = sys.argv[1] if len(sys.argv) > 1 else "bot"

    handlers = {
        "bot": run_bot,
        "ingestion": run_ingestion,
        "analyzer": run_analyzer,
        "all": run_all,
    }

    handler = handlers.get(service)
    if not handler:
        print(f"Unknown service: {service}. Choose from: {list(handlers.keys())}")
        sys.exit(1)

    logger.info("Starting service", service=service, env=settings.env)

    try:
        asyncio.run(handler())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")


if __name__ == "__main__":
    main()