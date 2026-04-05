"""
/overvalued — shows ranked list of currently overvalued coins.
Refreshed every 5 minutes by the analyzer service.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("overvalued"))
async def cmd_overvalued(msg: Message) -> None:
    # TODO: fetch latest overvalued snapshot from DB or Redis cache
    await msg.answer(
        "📊 <b>Overvalued Coins</b>\n\n"
        "<i>Ranking is computed every 5 minutes.\n"
        "First results appear after the analyzer warms up (~2 min).</i>\n\n"
        "Top coins are ranked by composite risk score.\n"
        "Higher score = more overheated = higher dump probability.\n\n"
        "<b>Example format when live:</b>\n"
        "1. COIN1USDT 🔴 Score: 82 | RSI: 87 | +18.3% VWAP\n"
        "2. COIN2USDT 🟠 Score: 67 | RSI: 74 | +9.1% VWAP\n"
        "...\n\n"
        "Use /coin SYMBOL for full diagnostics.",
    )
