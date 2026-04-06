"""
aiogram Dispatcher factory.
Registers all routers (handlers) and middleware.
"""
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
from app.bot.handlers.watchlist import router as watchlist_router
from app.bot.handlers.nav import router as nav_router

from app.bot.handlers import (
    commands_router,
    signals_router,
    overvalued_router,
    coin_router,
    settings_router,
    watchlist_router,
    nav_router,
)
from app.bot.middleware import AccessMiddleware
from app.config import get_settings

settings = get_settings()


def create_bot() -> Bot:
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

def create_dispatcher(redis_url: str) -> Dispatcher:
    storage = RedisStorage.from_url(redis_url)
    dp = Dispatcher(storage=storage)

    # Middleware — access control
    dp.message.middleware(AccessMiddleware(allowed_ids=settings.allowed_user_ids))
    dp.callback_query.middleware(AccessMiddleware(allowed_ids=settings.allowed_user_ids))

    # Register routers
    dp.include_router(commands_router)
    dp.include_router(nav_router)        # nav:* callback handlers
    dp.include_router(signals_router)
    dp.include_router(overvalued_router)
    dp.include_router(coin_router)
    dp.include_router(settings_router)
    dp.include_router(watchlist_router)
    
    return dp