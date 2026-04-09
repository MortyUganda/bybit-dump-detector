"""
/watchlist, /add SYMBOL, /remove SYMBOL handlers.
Watchlist data is persisted in Redis.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import watchlist_keyboard
from app.bot.handlers.watchlist_store import (
    add_to_watchlist,
    get_watchlist,
    normalize_symbol,
    remove_from_watchlist,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("watchlist"))
async def cmd_watchlist(msg: Message) -> None:
    if not msg.from_user:
        await msg.answer("Не удалось определить пользователя.")
        return

    user_id = msg.from_user.id
    symbols = sorted(await get_watchlist(user_id))

    if not symbols:
        await msg.answer(
            "⭐ <b>Ваш список отслеживания</b>\n\n"
            "<i>Пусто. Добавьте монету командой /add SYMBOL</i>\n\n"
            "Монеты из списка отслеживания получают приоритетные сигналы."
        )
        return

    text = "⭐ <b>Ваш список отслеживания</b>\n\n"
    text += "\n".join(f"• <b>{symbol}</b>" for symbol in symbols)
    text += "\n\nНажмите кнопку ниже, чтобы удалить монету из списка."

    await msg.answer(
        text,
        reply_markup=watchlist_keyboard(symbols),
    )


@router.message(Command("add"))
async def cmd_add(msg: Message) -> None:
    if not msg.from_user:
        await msg.answer("Не удалось определить пользователя.")
        return

    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer(
            "Использование: <code>/add SYMBOL</code>\n"
            "Пример: <code>/add DOGE</code>"
        )
        return

    symbol = normalize_symbol(args[1])
    user_id = msg.from_user.id

    current = await get_watchlist(user_id)

    if symbol in current:
        await msg.answer(f"ℹ️ <b>{symbol}</b> уже есть в вашем списке отслеживания.")
        return

    await add_to_watchlist(user_id, symbol)

    await msg.answer(
        f"✅ <b>{symbol}</b> добавлена в список отслеживания.\n"
        f"Вы будете получать по ней приоритетные сигналы."
    )


@router.message(Command("remove"))
async def cmd_remove(msg: Message) -> None:
    if not msg.from_user:
        await msg.answer("Не удалось определить пользователя.")
        return

    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer(
            "Использование: <code>/remove SYMBOL</code>\n"
            "Пример: <code>/remove DOGE</code>"
        )
        return

    symbol = normalize_symbol(args[1])
    user_id = msg.from_user.id

    current = await get_watchlist(user_id)

    if symbol not in current:
        await msg.answer(f"ℹ️ <b>{symbol}</b> нет в вашем списке отслеживания.")
        return

    await remove_from_watchlist(user_id, symbol)

    await msg.answer(f"🗑 <b>{symbol}</b> удалена из списка отслеживания.")
