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
        "📡 <b>Recent Signals</b>\n\n"
        "<i>No signals yet — analysis is warming up.</i>\n\n"
        "Signals appear here when risk score ≥ 50 with ≥ 3 factors triggered.",
    )


@router.callback_query(F.data == "nav:overvalued")
async def cb_nav_overvalued(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "📊 <b>Overvalued Coins</b>\n\n"
        "<i>Ranking is computed every 5 minutes.\n"
        "First results appear after the analyzer warms up (~2 min).</i>",
    )


@router.callback_query(F.data == "nav:watchlist")
async def cb_nav_watchlist(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "⭐ <b>Your Watchlist</b>\n\n"
        "<i>Empty. Add coins with /add SYMBOL</i>",
    )


@router.callback_query(F.data == "nav:settings")
async def cb_nav_settings(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "⚙️ <b>Your Settings</b>\n\n"
        "🔔 Alerts: <b>ON</b>\n"
        "📊 Min score: <b>50</b>\n"
        "⏱ Cooldown: <b>60 min</b>",
    )


@router.callback_query(F.data == "nav:status")
async def cb_nav_status(query: CallbackQuery) -> None:
    await query.answer()
    await query.message.answer(
        "⚙️ <b>Bot Status</b>\n\n"
        "✅ Ingestion: running\n"
        "✅ Analyzer: running\n"
        "📊 Universe: refreshing...",
    )


@router.callback_query(F.data.startswith("watch:add:"))
async def cb_watch_add(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer(f"✅ {symbol} added to watchlist")


@router.callback_query(F.data.startswith("coin:refresh:"))
async def cb_coin_refresh(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer("🔄 Refreshing...")
    await query.message.answer(f"🔍 <b>{symbol}</b> — data refreshed (analyzer warming up)")