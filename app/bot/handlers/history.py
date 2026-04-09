"""
/history — история закрытых авто-шортов из БД.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

PAGE_SIZE = 10


def history_keyboard(
    page: int,
    has_next: bool,
    filter_type: str = "all",
    period: str = "all",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    # Row 1: Filter by result
    filters = [
        ("Все", "all"),
        ("✅ Прибыльные", "wins"),
        ("❌ Убыточные", "losses"),
    ]
    filter_row = []
    for label, ftype in filters:
        marker = "→ " if ftype == filter_type else ""
        filter_row.append(InlineKeyboardButton(
            text=f"{marker}{label}",
            callback_data=f"history:{ftype}:{period}:0",
        ))
    rows.append(filter_row)

    # Row 2: Filter by period
    periods = [
        ("Сегодня", "today"),
        ("Неделя", "week"),
        ("Всё время", "all"),
    ]
    period_row = []
    for label, p in periods:
        marker = "→ " if p == period else ""
        period_row.append(InlineKeyboardButton(
            text=f"{marker}{label}",
            callback_data=f"history:{filter_type}:{p}:0",
        ))
    rows.append(period_row)

    # Row 3: Pagination (Назад / page indicator / Вперёд)
    has_prev = page > 0
    if has_prev or has_next:
        nav_row = []
        if has_prev:
            nav_row.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"history:{filter_type}:{period}:{page - 1}",
            ))
        nav_row.append(InlineKeyboardButton(
            text=f"📄 {page + 1}",
            callback_data="noop",
        ))
        if has_next:
            nav_row.append(InlineKeyboardButton(
                text="▶️ Вперёд",
                callback_data=f"history:{filter_type}:{period}:{page + 1}",
            ))
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _fetch_history(
    filter_type: str = "all",
    period: str = "all",
    page: int = 0,
) -> tuple[list, bool]:
    """Получить историю сделок из БД."""
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models.auto_short import AutoShort
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            query = select(AutoShort).where(AutoShort.status != "open")

            # Фильтр по периоду
            now = datetime.now(timezone.utc)
            if period == "today":
                query = query.where(
                    AutoShort.entry_ts >= now.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                )
            elif period == "week":
                query = query.where(
                    AutoShort.entry_ts >= now - timedelta(days=7)
                )

            # Фильтр по результату
            if filter_type == "wins":
                query = query.where(AutoShort.ml_label == 1)
            elif filter_type == "losses":
                query = query.where(AutoShort.ml_label == 0)

            query = query.order_by(AutoShort.entry_ts.desc())

            result = await session.execute(query)
            all_trades = result.scalars().all()

        start = page * PAGE_SIZE
        end = start + PAGE_SIZE
        page_trades = all_trades[start:end]
        has_next = end < len(all_trades)

        return page_trades, has_next

    except Exception as e:
        logger.error("History fetch failed", error=str(e))
        return [], False


def _format_history(trades: list, filter_type: str, period: str, page: int) -> str:
    if not trades:
        return (
            "📋 <b>История авто-шортов</b>\n\n"
            "<i>Нет закрытых сделок по выбранным фильтрам.</i>"
        )

    period_labels = {
        "today": "сегодня",
        "week": "за неделю",
        "all": "за всё время",
    }
    filter_labels = {
        "all": "все",
        "wins": "прибыльные",
        "losses": "убыточные",
    }

    lines = [
        f"📋 <b>История авто-шортов</b>\n"
        f"Фильтр: {filter_labels.get(filter_type, 'все')} | "
        f"Период: {period_labels.get(period, 'всё время')}\n"
    ]

    for trade in trades:
        pnl = trade.pnl_pct or 0
        pnl_em = "🟢" if pnl > 0 else "🔴"

        status_labels = {
            "tp_hit": "🎯 TP",
            "sl_hit": "🛑 SL",
            "trailing_sl": "📉 Trailing",
            "expired": "⏰ Истёк",
            "closed_manual": "✋ Вручную",
            "manual": "✋ Вручную",
        }
        status_label = status_labels.get(trade.status, trade.status)

        try:
            ts = trade.entry_ts.strftime("%d.%m %H:%M")
        except Exception:
            ts = "—"

        base = trade.symbol.replace("USDT", "")
        bybit_url = f"https://www.bybit.com/trade/usdt/{trade.symbol}"

        lines.append(
            f"{pnl_em} <a href=\"{bybit_url}\">{trade.symbol}</a> | "
            f"{status_label} | <b>{pnl:+.1f}%</b>\n"
            f"   Score: {trade.score:.0f} | "
            f"Вход: ${trade.entry_price:.6g} | {ts}"
        )

    return "\n\n".join(lines)


@router.message(Command("history"))
async def cmd_history(msg: Message) -> None:
    trades, has_next = await _fetch_history()
    text = _format_history(trades, "all", "all", 0)
    await msg.answer(
        text,
        reply_markup=history_keyboard(0, has_next, "all", "all"),
    )


@router.callback_query(F.data.startswith("history:"))
async def cb_history(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    parts = query.data.split(":")
    filter_type = parts[1]
    period = parts[2]
    page = int(parts[3])

    trades, has_next = await _fetch_history(filter_type, period, page)
    text = _format_history(trades, filter_type, period, page)

    try:
        await query.message.edit_text(
            text,
            reply_markup=history_keyboard(page, has_next, filter_type, period),
        )
    except Exception:
        pass