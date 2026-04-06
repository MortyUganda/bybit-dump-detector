"""
/overvalued — показывает список переоценённых монет из Redis.
Данные обновляются каждые 5 минут сервисом анализа.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.formatters import format_overvalued_list
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

REDIS_OVERVALUED_KEY = "overvalued:latest"


async def get_redis() -> aioredis.Redis:
    from app.config import get_settings
    settings = get_settings()
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


def overvalued_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔄 Обновить",
        callback_data="overvalued:refresh",
    )
    builder.adjust(1)
    return builder.as_markup()


async def _fetch_and_format() -> tuple[str, bool]:
    """
    Читает данные из Redis и возвращает (текст, успех).
    """
    try:
        redis = await get_redis()
        raw = await redis.get(REDIS_OVERVALUED_KEY)
        await redis.aclose()
    except Exception as e:
        logger.error("Redis read failed", error=str(e))
        return (
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Ошибка чтения данных. Попробуйте позже.</i>",
            False,
        )

    if not raw:
        return (
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Данные ещё не готовы — анализатор разогревается (~2 минуты).\n"
            "Попробуйте ещё раз через минуту.</i>",
            False,
        )

    try:
        items = json.loads(raw)
    except Exception:
        return (
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Ошибка обработки данных. Попробуйте позже.</i>",
            False,
        )

    return format_overvalued_list(items), True


@router.message(Command("overvalued"))
async def cmd_overvalued(msg: Message) -> None:
    text, success = await _fetch_and_format()
    await msg.answer(
        text,
        reply_markup=overvalued_keyboard() if success else None,
    )


@router.callback_query(F.data == "overvalued:refresh")
async def cb_overvalued_refresh(query: CallbackQuery) -> None:
    await query.answer("🔄 Обновляю...")
    text, success = await _fetch_and_format()
    try:
        await query.message.edit_text(
            text,
            reply_markup=overvalued_keyboard() if success else None,
        )
    except Exception:
        # Если текст не изменился — просто игнорируем
        pass