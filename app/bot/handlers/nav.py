"""
Обработчик nav:* callback — главное меню (кнопки /start).
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.handlers.watchlist_store import (
    add_to_watchlist,
    get_watchlist,
    normalize_symbol,
    remove_from_watchlist,
)
from app.bot.keyboards import watchlist_keyboard

router = Router()


@router.callback_query(F.data == "nav:signals")
async def cb_nav_signals(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    from app.bot.handlers.signals import _format_signals_page, signals_history_keyboard
    text, has_next = await _format_signals_page(page=0)
    await query.message.answer(
        text,
        reply_markup=signals_history_keyboard(page=0, has_next=has_next),
    )


@router.callback_query(F.data == "nav:overvalued")
async def cb_nav_overvalued(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    from app.bot.handlers.overvalued import _fetch_and_format, overvalued_keyboard

    text, success = await _fetch_and_format()
    await query.message.answer(
        text,
        reply_markup=overvalued_keyboard() if success else None,
    )


@router.callback_query(F.data == "nav:watchlist")
async def cb_nav_watchlist(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        await query.message.answer("Не удалось определить пользователя.")
        return

    user_id = query.from_user.id
    symbols = sorted(await get_watchlist(user_id))

    if not symbols:
        await query.message.answer(
            "⭐ <b>Ваш список отслеживания</b>\n\n"
            "<i>Пусто. Добавьте монеты командой /add SYMBOL</i>",
        )
        return

    text = "⭐ <b>Ваш список отслеживания</b>\n\n"
    text += "\n".join(f"• <b>{symbol}</b>" for symbol in symbols)
    text += "\n\nНажмите кнопку ниже, чтобы удалить монету из списка."

    await query.message.answer(
        text,
        reply_markup=watchlist_keyboard(symbols),
    )


@router.callback_query(F.data == "nav:settings")
async def cb_nav_settings(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    await query.message.answer(
        "⚙️ <b>Ваши настройки</b>\n\n"
        "🔔 Уведомления: <b>ВКЛ</b>\n"
        "📊 Минимальный риск для сигнала: <b>50</b>\n"
        "⏱ Интервал между алертами: <b>60 минут</b>",
    )


@router.callback_query(F.data == "nav:status")
async def cb_nav_status(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    await query.message.answer(
        "⚙️ <b>Статус бота</b>\n\n"
        "✅ Сбор данных: работает\n"
        "✅ Анализ: работает\n"
        "📊 Список монет: обновляется...",
    )


@router.callback_query(F.data.startswith("watch:add:"))
async def cb_watch_add(query: CallbackQuery) -> None:
    if not query.from_user:
        await query.answer("Не удалось определить пользователя", show_alert=True)
        return

    raw_symbol = query.data.split(":")[-1]
    symbol = normalize_symbol(raw_symbol)

    user_id = query.from_user.id
    current = await get_watchlist(user_id)

    if symbol in current:
        await query.answer(f"ℹ️ {symbol} уже в списке")
        return

    await add_to_watchlist(user_id, symbol)
    await query.answer(f"✅ {symbol} добавлена в список отслеживания")


@router.callback_query(F.data.startswith("watch:remove:"))
async def cb_watch_remove(query: CallbackQuery) -> None:
    if not query.from_user:
        await query.answer("Не удалось определить пользователя", show_alert=True)
        return

    raw_symbol = query.data.split(":")[-1]
    symbol = normalize_symbol(raw_symbol)

    user_id = query.from_user.id
    current = await get_watchlist(user_id)

    if symbol not in current:
        await query.answer(f"ℹ️ {symbol} нет в списке")
        return

    await remove_from_watchlist(user_id, symbol)
    await query.answer(f"🗑 {symbol} удалена из списка отслеживания")


@router.callback_query(F.data.startswith("coin:refresh:"))
async def cb_coin_refresh(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer("🔄 Обновляю данные...")
    await query.message.answer(
        f"🔍 <b>{symbol}</b> — данные обновлены (анализатор разогревается)"
    )
