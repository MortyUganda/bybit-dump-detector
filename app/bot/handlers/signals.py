"""
/signals — показывает историю сигналов с ценой и временем.
Хранение в Redis (LPUSH/LTRIM, cap 500).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

PAGE_SIZE = 5
SCORE_MIN_THRESHOLD = 35  # ниже этого — удалять из истории

REDIS_SIGNALS_KEY = "signals_history"
MAX_SIGNALS = 500


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def add_signal(
    symbol: str,
    signal_type: str,
    score: float,
    price: float | None,
    redis: aioredis.Redis | None = None,
) -> None:
    """
    Вызывается из AlertManager при каждом новом сигнале.
    Добавляет запись в Redis-список.
    """
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        # Проверяем дубликаты — если сигнал по этой монете уже есть, обновляем
        raw_all = await redis.lrange(REDIS_SIGNALS_KEY, 0, -1)
        for i, raw in enumerate(raw_all):
            existing = json.loads(raw)
            if existing["symbol"] == symbol and existing["signal_type"] == signal_type:
                existing["score"] = score
                existing["price"] = price
                existing["ts"] = datetime.now(timezone.utc).isoformat()
                await redis.lset(REDIS_SIGNALS_KEY, i, json.dumps(existing, default=str))
                return

        entry = {
            "symbol": symbol,
            "signal_type": signal_type,
            "score": score,
            "price": price,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await redis.lpush(REDIS_SIGNALS_KEY, json.dumps(entry, default=str))
        await redis.ltrim(REDIS_SIGNALS_KEY, 0, MAX_SIGNALS - 1)
    finally:
        if close_after:
            await redis.aclose()


async def get_signals(redis: aioredis.Redis, limit: int = 50) -> list[dict]:
    raw = await redis.lrange(REDIS_SIGNALS_KEY, 0, limit - 1)
    return [json.loads(r) for r in raw]


async def _refresh_scores() -> None:
    """
    Обновить score для всех сигналов из Redis.
    Удалить те у которых score упал ниже SCORE_MIN_THRESHOLD.
    """
    try:
        redis = await _get_redis()

        raw_all = await redis.lrange(REDIS_SIGNALS_KEY, 0, -1)
        signals = [json.loads(r) for r in raw_all]

        to_remove_indices: list[int] = []
        for i, signal in enumerate(signals):
            raw = await redis.get(f"score:{signal['symbol']}")
            if raw:
                data = json.loads(raw)
                current_score = data.get("score", 0)
                signal["current_score"] = current_score

                if current_score < SCORE_MIN_THRESHOLD:
                    to_remove_indices.append(i)
                    logger.info(
                        "Signal removed — score below threshold",
                        symbol=signal["symbol"],
                        score=current_score,
                    )
                else:
                    await redis.lset(
                        REDIS_SIGNALS_KEY, i, json.dumps(signal, default=str)
                    )
            else:
                signal["current_score"] = signal["score"]

        # Удаляем просевшие сигналы (от конца к началу чтобы индексы не сдвигались)
        for idx in reversed(to_remove_indices):
            sentinel = "__REMOVE__"
            await redis.lset(REDIS_SIGNALS_KEY, idx, sentinel)
        if to_remove_indices:
            await redis.lrem(REDIS_SIGNALS_KEY, 0, "__REMOVE__")

        await redis.aclose()

    except Exception as e:
        logger.warning("Score refresh failed", error=str(e))


def _signal_type_emoji(signal_type: str) -> str:
    return {
        "early_warning": "⚠️",
        "overheated": "🔥",
        "reversal_risk": "⬇️",
        "dump_started": "💥",
    }.get(signal_type, "📊")


async def _format_signals_page(page: int) -> tuple[str, bool]:
    """
    Возвращает (текст страницы, есть_ли_следующая).
    Сигналы показываются от новых к старым (LPUSH = newest first).
    """
    try:
        redis = await _get_redis()
        total = await redis.llen(REDIS_SIGNALS_KEY)
        if total == 0:
            await redis.aclose()
            return (
                "📡 <b>История сигналов</b>\n\n"
                "<i>Пока сигналов нет — анализ ещё разогревается.</i>\n\n"
                "Сигналы появятся здесь, когда риск ≥ 50 и срабатывает ≥ 2 фактора.",
                False,
            )

        start = page * PAGE_SIZE
        end = start + PAGE_SIZE - 1
        raw_page = await redis.lrange(REDIS_SIGNALS_KEY, start, end)
        await redis.aclose()
    except Exception:
        return (
            "📡 <b>История сигналов</b>\n\n"
            "<i>Ошибка загрузки сигналов.</i>",
            False,
        )

    page_signals = [json.loads(r) for r in raw_page]
    has_next = (start + PAGE_SIZE) < total

    lines = [f"📡 <b>История сигналов</b> ({total} всего)\n"]

    for s in page_signals:
        em = _signal_type_emoji(s["signal_type"])
        signal_label = s["signal_type"].replace("_", " ").title()

        # Время
        try:
            ts = datetime.fromisoformat(s["ts"])
            time_str = ts.strftime("%d.%m %H:%M")
        except Exception:
            time_str = "—"

        # Цена при сигнале
        price_str = f"${s['price']:.6g}" if s.get("price") else "N/A"

        # Текущий score (если обновлялся)
        current_score = s.get("current_score", s["score"])
        score_change = current_score - s["score"]
        if score_change > 0:
            score_str = f"<b>{current_score:.0f}</b> 🔺{score_change:+.0f}"
        elif score_change < 0:
            score_str = f"<b>{current_score:.0f}</b> 🔻{score_change:+.0f}"
        else:
            score_str = f"<b>{s['score']:.0f}</b>"

        # Ссылка на Bybit
        bybit_url = f"https://www.bybit.com/trade/usdt/{s['symbol']}"

        lines.append(
            f"{em} <a href=\"{bybit_url}\">{s['symbol']}</a> — {signal_label}\n"
            f"   📊 Score: {score_str} | 💰 {price_str} | 🕐 {time_str}"
        )

    text = "\n\n".join(lines)
    return text, has_next


def signals_history_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if page > 0:
        builder.button(
            text="◀ Назад",
            callback_data=f"signals:page:{page - 1}",
        )
    if has_next:
        builder.button(
            text="Вперёд ▶",
            callback_data=f"signals:page:{page + 1}",
        )

    builder.button(
        text="🔄 Обновить score",
        callback_data=f"signals:refresh:{page}",
    )
    builder.button(
        text="🗑 Очистить историю",
        callback_data="signals:clear",
    )

    builder.adjust(2, 1, 1)
    return builder.as_markup()


@router.message(Command("signals"))
async def cmd_signals(msg: Message) -> None:
    text, has_next = await _format_signals_page(page=0)
    await msg.answer(
        text,
        reply_markup=signals_history_keyboard(page=0, has_next=has_next),
    )


@router.callback_query(F.data.startswith("signals:page:"))
async def cb_signals_page(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    page = int(query.data.split(":")[-1])
    text, has_next = await _format_signals_page(page=page)
    try:
        await query.message.edit_text(
            text,
            reply_markup=signals_history_keyboard(page=page, has_next=has_next),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("signals:refresh:"))
async def cb_signals_refresh(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю score...")
    except Exception:
        pass

    page = int(query.data.split(":")[-1])

    # Обновляем score из Redis и удаляем просевшие
    await _refresh_scores()

    text, has_next = await _format_signals_page(page=page)
    try:
        await query.message.edit_text(
            text,
            reply_markup=signals_history_keyboard(page=page, has_next=has_next),
        )
    except Exception:
        pass


@router.callback_query(F.data == "signals:clear")
async def cb_signals_clear(query: CallbackQuery) -> None:
    try:
        await query.answer("🗑 История очищена")
    except Exception:
        pass

    try:
        redis = await _get_redis()
        await redis.delete(REDIS_SIGNALS_KEY)
        await redis.aclose()
    except Exception:
        pass

    try:
        await query.message.edit_text(
            "📡 <b>История сигналов</b>\n\n"
            "<i>История очищена.</i>",
            reply_markup=None,
        )
    except Exception:
        pass
