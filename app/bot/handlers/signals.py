"""
/signals — показывает историю сигналов с ценой и временем.
Хранение в памяти процесса (MVP).
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.keyboards import signals_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

PAGE_SIZE = 5

# MVP: хранение сигналов в памяти
# list of dict: symbol, signal_type, score, price, ts
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
    SIGNALS_HISTORY.append({
        "symbol": symbol,
        "signal_type": signal_type,
        "score": score,
        "price": price,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


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

    # От новых к старым
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

        # Цена
        price_str = f"${s['price']:.6g}" if s.get("price") else "N/A"

        lines.append(
            f"{em} de>{s['symbol']}</code> — {signal_label}\n"
            f"   📊 Score: <b>{s['score']:.0f}</b> | 💰 {price_str} | 🕐 {time_str}"
        )

    text = "\n\n".join(lines)
    return text, has_next


def signals_history_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Навигация
    nav = []
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
        text="🔄 Обновить",
        callback_data=f"signals:page:{page}",
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
    await query.answer()
    page = int(query.data.split(":")[-1])
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
    await query.answer("🗑 История очищена")
    SIGNALS_HISTORY.clear()
    try:
        await query.message.edit_text(
            "📡 <b>История сигналов</b>\n\n"
            "<i>История очищена.</i>",
            reply_markup=None,
        )
    except Exception:
        pass