from app.bot.handlers.commands import router as commands_router
from app.bot.handlers.signals import router as signals_router
from app.bot.handlers.overvalued import router as overvalued_router
from app.bot.handlers.coin import router as coin_router
from app.bot.handlers.settings import router as settings_router
from app.bot.handlers.watchlist import router as watchlist_router
from app.bot.handlers.nav import router as nav_router
from app.bot.handlers.strategy import router as strategy_router
from app.bot.handlers.auto_shorts import router as auto_shorts_router

__all__ = [
    "commands_router",
    "signals_router",
    "overvalued_router",
    "coin_router",
    "settings_router",
    "watchlist_router",
    "nav_router",
    "strategy_router",
    "auto_shorts_router",
]