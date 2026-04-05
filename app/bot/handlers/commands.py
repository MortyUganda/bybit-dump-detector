"""
/start, /help, /status handlers.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import main_menu_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

HELP_TEXT = """
<b>🔍 Bybit Dump Detector</b>

Monitors speculative coins on Bybit for overheating and dump risk.

<b>Commands:</b>
/signals — recent risk alerts
/overvalued — top overvalued coins right now
/coin SYMBOL — full diagnostics for one coin
/watchlist — your personal watchlist
/add SYMBOL — add coin to watchlist
/remove SYMBOL — remove from watchlist
/settings — configure your alert preferences
/status — bot health and universe size
/help — this message

<b>Risk Levels:</b>
🟢 LOW (0–24) — no concern
🟡 MODERATE (25–49) — keep an eye
🟠 HIGH (50–74) — elevated dump risk
🔴 CRITICAL (75–100) — strong reversal signal

<b>Signal Types:</b>
⚠️ Early Warning — early signs of overheating
🔥 Overheated — RSI + volume + VWAP all elevated
⬇️ Reversal Risk — momentum stalling, wick rejection
💥 Dump Started — price dropping + OB collapsing

<i>Signals are informational only. Not financial advice.</i>
"""


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    name = msg.from_user.first_name if msg.from_user else "Trader"
    await msg.answer(
        f"👋 Welcome, <b>{name}</b>!\n\n{HELP_TEXT}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    # TODO: inject universe manager and show live stats
    await msg.answer(
        "⚙️ <b>Bot Status</b>\n\n"
        "✅ Ingestion: running\n"
        "✅ Analyzer: running\n"
        "📊 Universe: <i>refreshing...</i>\n"
        "🕐 Uptime: <i>available after deployment</i>"
    )
