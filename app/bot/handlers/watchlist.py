"""
/watchlist, /add SYMBOL, /remove SYMBOL handlers.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("watchlist"))
async def cmd_watchlist(msg: Message) -> None:
    # TODO: fetch user's watchlist from DB
    await msg.answer(
        "⭐ <b>Your Watchlist</b>\n\n"
        "<i>Empty. Add coins with /add SYMBOL</i>\n\n"
        "Watchlist coins get priority alerts regardless of global threshold."
    )


@router.message(Command("add"))
async def cmd_add(msg: Message) -> None:
    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer("Usage: <code>/add SYMBOL</code>\nExample: <code>/add DOGEUSDT</code>")
        return
    symbol = args[1].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    # TODO: validate symbol in universe + insert to DB
    await msg.answer(f"✅ <b>{symbol}</b> added to watchlist.\nYou'll receive priority alerts.")


@router.message(Command("remove"))
async def cmd_remove(msg: Message) -> None:
    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer("Usage: <code>/remove SYMBOL</code>")
        return
    symbol = args[1].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    # TODO: remove from DB
    await msg.answer(f"🗑 <b>{symbol}</b> removed from watchlist.")
