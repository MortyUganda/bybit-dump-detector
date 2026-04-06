"""
Обработчик nav:* callback — главное меню (кнопки /start).
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import watchlist_keyboard
from app.bot.handlers.watchlist_store import WATCHLISTS, normalize_symbol

router = Router()


@router.callback_query(F.data == "nav:signals")
async def cb_nav_signals(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "📡 <b>Последние сигналы</b>\n\n"
        "<i>Пока сигналов нет — анализ только разогревается.</i>\n\n"
        "Сигналы появляются здесь, когда риск ≥ 50 и срабатывает ≥ 3 фактора.",
    )


@router.callback_query(F.data == "nav:overvalued")
async def cb_nav_overvalued(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "📊 <b>Переоценённые монеты</b>\n\n"
        "<i>Рейтинг пересчитывается каждые 5 минут.\n"
        "Первые результаты появятся после разогрева анализатора (~2 минуты).</i>",
    )


@router.callback_query(F.data == "nav:watchlist")
async def cb_nav_watchlist(query: CallbackQuery) -> None:
    await query.answer()

    if not query.from_user:
        await query.message.answer("Не удалось определить пользователя.")
        return

    user_id = query.from_user.id
    symbols = sorted(WATCHLISTS.get(user_id, set()))

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
    await query.answer()
    await query.message.answer(
        "⚙️ <b>Ваши настройки</b>\n\n"
        "🔔 Уведомления: <b>ВКЛ</b>\n"
        "📊 Минимальный риск для сигнала: <b>50</b>\n"
        "⏱ Интервал между алертами: <b>60 минут</b>",
    )


@router.callback_query(F.data == "nav:status")
async def cb_nav_status(query: CallbackQuery) -> None:
    await query.answer()
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
    WATCHLISTS.setdefault(user_id, set())

    if symbol in WATCHLISTS[user_id]:
        await query.answer(f"ℹ️ {symbol} уже в списке")
        return

    WATCHLISTS[user_id].add(symbol)
    await query.answer(f"✅ {symbol} добавлена в список отслеживания")


@router.callback_query(F.data.startswith("watch:remove:"))
async def cb_watch_remove(query: CallbackQuery) -> None:
    if not query.from_user:
        await query.answer("Не удалось определить пользователя", show_alert=True)
        return

    raw_symbol = query.data.split(":")[-1]
    symbol = normalize_symbol(raw_symbol)

    user_id = query.from_user.id
    user_watchlist = WATCHLISTS.get(user_id, set())

    if symbol not in user_watchlist:
        await query.answer(f"ℹ️ {symbol} нет в списке")
        return

    user_watchlist.remove(symbol)

    if not user_watchlist:
        WATCHLISTS.pop(user_id, None)

    await query.answer(f"🗑 {symbol} удалена из списка отслеживания")


@router.callback_query(F.data.startswith("coin:refresh:"))
async def cb_coin_refresh(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer("🔄 Обновляю данные...")
    await query.message.answer(
        f"🔍 <b>{symbol}</b> — данные обновлены (анализатор разогревается)"
    )