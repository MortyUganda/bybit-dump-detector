"""
/signals — показывает историю сигналов с ценой и временем.
Хранение в памяти процесса (MVP).
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

# MVP: хранение сигналов в памяти
SIGNALS_HISTORY: list[dict] = []


def add_signal(
    symbol: str,
    signal_type: str,
    score: float,
    price: float | None,
) -> None:
    """
    Вызывается из AlertManager при каждом новом сигнале.
    Добавляет запись в историю.
    """
    # Не добавляем дубликаты — если сигнал по этой монете уже есть, обновляем
    for existing in SIGNALS_HISTORY:
        if existing["symbol"] == symbol and existing["signal_type"] == signal_type:
            existing["score"] = score
            existing["price"] = price
            existing["ts"] = datetime.now(timezone.utc).isoformat()
            return

    SIGNALS_HISTORY.append({
        "symbol": symbol,
        "signal_type": signal_type,
        "score": score,
        "price": price,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


async def _refresh_scores() -> None:
    """
    Обновить score для всех сигналов из Redis.
    Удалить те у которых score упал ниже SCORE_MIN_THRESHOLD.
    """
    try:
        settings = get_settings()
        redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )

        to_remove = []
        for signal in SIGNALS_HISTORY:
            raw = await redis.get(f"score:{signal['symbol']}")
            if raw:
                data = json.loads(raw)
                current_score = data.get("score", 0)
                signal["current_score"] = current_score

                # Если score упал ниже порога — помечаем на удаление
                if current_score < SCORE_MIN_THRESHOLD:
                    to_remove.append(signal)
            else:
                signal["current_score"] = signal["score"]

        # Удаляем просевшие сигналы
        for s in to_remove:
            SIGNALS_HISTORY.remove(s)
            logger.info(
                "Signal removed — score below threshold",
                symbol=s["symbol"],
                score=s.get("current_score", 0),
            )

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


def _format_signals_page(page: int) -> tuple[str, bool]:
    """
    Возвращает (текст страницы, есть_ли_следующая).
    Сигналы показываются от новых к старым.
    """
    if not SIGNALS_HISTORY:
        return (
            "📡 <b>История сигналов</b>\n\n"
            "<i>Пока сигналов нет — анализ ещё разогревается.</i>\n\n"
            "Сигналы появятся здесь, когда риск ≥ 50 и срабатывает ≥ 2 фактора.",
            False,
        )

    reversed_signals = list(reversed(SIGNALS_HISTORY))
    total = len(reversed_signals)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_signals = reversed_signals[start:end]
    has_next = end < total

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
        base = s["symbol"].replace("USDT", "")
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
    text, has_next = _format_signals_page(page=0)
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
    text, has_next = _format_signals_page(page=page)
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

    text, has_next = _format_signals_page(page=page)
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
    SIGNALS_HISTORY.clear()
    try:
        await query.message.edit_text(
            "📡 <b>История сигналов</b>\n\n"
            "<i>История очищена.</i>",
            reply_markup=None,
        )
    except Exception:
        pass