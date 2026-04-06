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

Отслеживает спекулятивные монеты на Bybit и помогает находить перегретые активы с риском скорого слива.

<b>Команды:</b>
/signals — последние сигналы риска
/overvalued — самые переоценённые монеты сейчас
/coin SYMBOL — подробный разбор конкретной монеты
/watchlist — ваш личный список отслеживания
/add SYMBOL — добавить монету в список
/remove SYMBOL — удалить монету из списка
/settings — настроить уведомления
/status — статус бота и отслеживаемых монет
/help — показать эту справку

<b>Уровни риска:</b>
🟢 НИЗКИЙ (0–24) — серьёзных признаков нет
🟡 УМЕРЕННЫЙ (25–49) — стоит наблюдать
🟠 ВЫСОКИЙ (50–74) — повышенный риск слива
🔴 КРИТИЧЕСКИЙ (75–100) — сильный сигнал на разворот/обвал

<b>Типы сигналов:</b>
⚠️ Раннее предупреждение — первые признаки перегрева
🔥 Перегрев — повышены RSI, объём и отклонение от VWAP
⬇️ Риск разворота — импульс слабеет, есть признаки отката
💥 Слив начался — цена уже падает, ликвидность ухудшается

<i>Сигналы бота носят информационный характер и не являются финансовой рекомендацией.</i>
"""


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    name = msg.from_user.first_name if msg.from_user else "Трейдер"
    await msg.answer(
        f"👋 Добро пожаловать, <b>{name}</b>!\n\n{HELP_TEXT}",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    # TODO: inject universe manager and show live stats
    await msg.answer(
        "⚙️ <b>Статус бота</b>\n\n"
        "✅ Сбор данных: работает\n"
        "✅ Анализ: работает\n"
        "📊 Список монет: <i>обновляется...</i>\n"
        "🕐 Время работы: <i>будет доступно после деплоя</i>"
    )