"""
/overvalued — показывает список переоценённых монет.
Обновляется каждые 5 минут сервисом анализа.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("overvalued"))
async def cmd_overvalued(msg: Message) -> None:
    # TODO: получить свежий снимок переоценённых монет из БД или Redis
    await msg.answer(
        "📊 <b>Переоценённые монеты</b>\n\n"
        "<i>Рейтинг пересчитывается каждые 5 минут.\n"
        "Первые результаты появятся после разогрева анализатора (~2 минуты).</i>\n\n"
        "Вверху списка — монеты с наибольшим совокупным риском.\n"
        "Чем выше score, тем сильнее перегрев и выше вероятность резкого слива.\n\n"
        "<b>Пример формата в онлайне:</b>\n"
        "1. COIN1USDT 🔴 Score: 82 | RSI: 87 | +18.3% к VWAP\n"
        "2. COIN2USDT 🟠 Score: 67 | RSI: 74 | +9.1% к VWAP\n"
        "...\n\n"
        "Для подробного разбора используйте /coin SYMBOL.",
    )