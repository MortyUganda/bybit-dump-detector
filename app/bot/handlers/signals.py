"""
/signals — shows recent alerts from the signals table.
Supports pagination via inline buttons.
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
    # TODO: inject DB service
    # signals = await signal_service.get_recent(limit=PAGE_SIZE, offset=0)
    # For scaffold: show placeholder
    await msg.answer(
        "📡 <b>Recent Signals</b>\n\n"
        "<i>No signals yet — analysis is warming up.</i>\n\n"
        "Signals appear here when risk score ≥ 50 with ≥ 3 factors triggered.",
        reply_markup=signals_keyboard(page=0, has_next=False),
    )


@router.callback_query(F.data.startswith("signals:page:"))
async def cb_signals_page(query: CallbackQuery) -> None:
    await query.answer()  # сразу снимаем спиннер
    page = int(query.data.split(":")[-1])
    try:
        await query.message.edit_text(
            f"📡 <b>Recent Signals</b> (page {page + 1})\n\n<i>Loading...</i>",
            reply_markup=signals_keyboard(page=page, has_next=False),
        )
    except Exception:
        pass  # сообщение не изменилось — игнорируем TelegramBadRequest