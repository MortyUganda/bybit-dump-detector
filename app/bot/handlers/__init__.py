from app.bot.handlers.commands import router as commands_router
from app.bot.handlers.signals import router as signals_router
from app.bot.handlers.overvalued import router as overvalued_router
from app.bot.handlers.coin import router as coin_router
from app.bot.handlers.settings import router as settings_router
from app.bot.handlers.watchlist import router as watchlist_router

__all__ = [
    "commands_router",
    "signals_router",
    "overvalued_router",
    "coin_router",
    "settings_router",
    "watchlist_router",
]
