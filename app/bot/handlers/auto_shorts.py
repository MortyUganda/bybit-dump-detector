"""
/auto_shorts — показывает список автоматических paper шортов.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.services.auto_short_service import ACTIVE_SHORTS
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("auto_shorts"))
async def cmd_auto_shorts(msg: Message) -> None:
    if not ACTIVE_SHORTS:
        await msg.answer(
            "🤖 <b>Авто-шорты</b>\n\n"
            "<i>Нет активных сделок.</i>\n\n"
            "Сделки открываются автоматически при score ≥ 45."
        )
        return

    lines = [f"🤖 <b>Авто-шорты</b> ({len(ACTIVE_SHORTS)} активных)\n"]

    for trade_id, trade in sorted(ACTIVE_SHORTS.items(), reverse=True):
        from app.services.auto_short_service import AutoShortService
        lines.append(
            f"📌 #{trade_id} de>{trade['symbol']}</code>\n"
            f"   Вход: ${trade['entry_price']:.6g}\n"
            f"   TP: ${trade['tp_price']:.6g} | SL: ${trade['sl_price']:.6g}"
        )

    await msg.answer("\n\n".join(lines))
