"""
/signals — показывает последние сигналы из таблицы сигналов.
Поддерживает постраничный просмотр через inline-кнопки.
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.formatters import format_signal_list
from app.bot.keyboards import signals_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

PAGE_SIZE = 5


@router.message(Command("signals"))
async def cmd_signals(msg: Message) -> None:
    # TODO: подключить сервис БД
    # signals = await signal_service.get_recent(limit=PAGE_SIZE, offset=0)
    # Пока показываем заглушку
    await msg.answer(
        "📡 <b>Последние сигналы</b>\n\n"
        "<i>Пока сигналов нет — анализ ещё разогревается.</i>\n\n"
        "Сигналы появятся здесь, когда риск ≥ 50 и срабатывает ≥ 3 фактора.",
        reply_markup=signals_keyboard(page=0, has_next=False),
    )


@router.callback_query(F.data.startswith("signals:page:"))
async def cb_signals_page(query: CallbackQuery) -> None:
    await query.answer()  # сразу снимаем спиннер
    page = int(query.data.split(":")[-1])
    try:
        await query.message.edit_text(
            f"📡 <b>Последние сигналы</b> (страница {page + 1})\n\n<i>Загружаю...</i>",
            reply_markup=signals_keyboard(page=page, has_next=False),
        )
    except Exception:
        pass  # сообщение не изменилось — игнорируем TelegramBadRequest