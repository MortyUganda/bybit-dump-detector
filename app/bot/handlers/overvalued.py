"""
/overvalued — показывает список переоценённых монет из Redis.
Данные обновляются каждые 5 минут сервисом анализа.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

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


@router.message(Command("overvalued"))
async def cmd_overvalued(msg: Message) -> None:
    try:
        redis = await get_redis()
        raw = await redis.get(REDIS_OVERVALUED_KEY)
        await redis.aclose()
    except Exception as e:
        logger.error("Redis read failed", error=str(e))
        await msg.answer(
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Ошибка чтения данных. Попробуйте позже.</i>"
        )
        return

    if not raw:
        await msg.answer(
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Данные ещё не готовы — анализатор разогревается (~2 минуты).\n"
            "Попробуйте ещё раз через минуту.</i>"
        )
        return

    try:
        items = json.loads(raw)
    except Exception:
        await msg.answer(
            "📊 <b>Переоценённые монеты</b>\n\n"
            "<i>Ошибка обработки данных. Попробуйте позже.</i>"
        )
        return

    text = format_overvalued_list(items)
    await msg.answer(text)