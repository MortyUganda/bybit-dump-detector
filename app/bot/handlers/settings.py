"""
/settings — view and modify user alert preferences.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("settings"))
async def cmd_settings(msg: Message) -> None:
    await msg.answer(
        "⚙️ <b>Your Settings</b>\n\n"
        "🔔 Alerts: <b>ON</b>\n"
        "📊 Min score to alert: <b>50</b>\n"
        "⏱ Cooldown: <b>60 min</b>\n\n"
        "<b>Signal types:</b>\n"
        "⚠️ Early Warning: OFF\n"
        "🔥 Overheated: ON\n"
        "⬇️ Reversal Risk: ON\n"
        "💥 Dump Started: ON\n\n"
        "<i>Settings customization via inline buttons — coming in Week 3.</i>"
    )
