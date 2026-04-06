"""
Обработчик nav:* callback — главное меню (кнопки /start).
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bot.keyboards import main_menu_keyboard

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
    await query.message.answer(
        "⭐ <b>Ваш список отслеживания</b>\n\n"
        "<i>Пусто. Добавьте монеты командой /add SYMBOL</i>",
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
    symbol = query.data.split(":")[-1]
    await query.answer(f"✅ {symbol} добавлена в список отслеживания")


@router.callback_query(F.data.startswith("coin:refresh:"))
async def cb_coin_refresh(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer("🔄 Обновляю данные...")
    await query.message.answer(
        f"🔍 <b>{symbol}</b> — данные обновлены (анализатор разогревается)"
    )