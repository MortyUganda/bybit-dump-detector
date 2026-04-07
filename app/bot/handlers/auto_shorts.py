"""
/auto_shorts — активные шорты
/stats — статистика по всем авто-шортам
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
from app.services.auto_short_service import ACTIVE_SHORTS
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


def auto_shorts_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="auto_shorts:refresh")
    builder.button(text="📊 Статистика", callback_data="auto_shorts:stats")
    builder.adjust(2)
    return builder.as_markup()


def stats_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="auto_shorts:stats")
    builder.button(text="🤖 Активные", callback_data="auto_shorts:refresh")
    builder.adjust(2)
    return builder.as_markup()


async def _get_stats() -> dict:
    """Получить статистику из БД."""
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models.auto_short import AutoShort
        from sqlalchemy import select, func

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AutoShort))
            all_trades = result.scalars().all()

        if not all_trades:
            return {}

        closed = [t for t in all_trades if t.status != "open"]
        wins = [t for t in closed if t.ml_label == 1]
        losses = [t for t in closed if t.ml_label == 0]
        open_trades = [t for t in all_trades if t.status == "open"]

        pnls = [t.pnl_pct for t in closed if t.pnl_pct is not None]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        best = max(closed, key=lambda t: t.pnl_pct or 0, default=None)
        worst = min(closed, key=lambda t: t.pnl_pct or 0, default=None)

        by_status = {}
        for t in closed:
            by_status[t.status] = by_status.get(t.status, 0) + 1

        win_rate = len(wins) / len(closed) * 100 if closed else 0

        return {
            "total": len(all_trades),
            "open": len(open_trades),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "best": best,
            "worst": worst,
            "by_status": by_status,
        }

    except Exception as e:
        logger.error("Stats fetch failed", error=str(e))
        return {}


def _format_active_shorts() -> str:
    if not ACTIVE_SHORTS:
        return (
            "🤖 <b>Авто-шорты</b>\n\n"
            "<i>Нет активных сделок.</i>\n\n"
            "Сделки открываются автоматически при score ≥ 45."
        )

    lines = [f"🤖 <b>Авто-шорты</b> ({len(ACTIVE_SHORTS)} активных)\n"]

    for trade_id, trade in sorted(ACTIVE_SHORTS.items(), reverse=True):
        entry = trade["entry_price"]
        symbol = trade["symbol"]
        base = symbol.replace("USDT", "")
        bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

        entry_ts = trade.get("entry_ts")
        if isinstance(entry_ts, datetime):
            elapsed = (datetime.now(timezone.utc) - entry_ts).total_seconds()
            elapsed_str = f"{int(elapsed // 60)}м"
        else:
            elapsed_str = "—"

        lines.append(
            f"📌 #{trade_id} <a href=\"{bybit_url}\">{symbol}</a>\n"
            f"   💰 Вход: <b>${entry:.6g}</b> | ⏱ {elapsed_str}\n"
            f"   🎯 TP: ${trade['tp_price']:.6g} | 🛑 SL: ${trade['sl_price']:.6g}"
        )

    return "\n\n".join(lines)


async def _format_stats() -> str:
    stats = await _get_stats()

    if not stats:
        return (
            "📊 <b>Статистика авто-шортов</b>\n\n"
            "<i>Данных пока нет.</i>\n\n"
            "Статистика появится после первых закрытых сделок."
        )

    win_rate = stats["win_rate"]
    win_em = "🟢" if win_rate >= 60 else "🟡" if win_rate >= 45 else "🔴"
    avg_pnl = stats["avg_pnl"]
    avg_em = "🟢" if avg_pnl > 0 else "🔴"

    by_status = stats["by_status"]
    status_lines = ""
    status_labels = {
        "tp_hit": "🎯 TP hit",
        "sl_hit": "🛑 SL hit",
        "expired": "⏰ Истекли",
        "closed_manual": "✋ Вручную",
    }
    for status, count in by_status.items():
        label = status_labels.get(status, status)
        status_lines += f"  {label}: {count}\n"

    best = stats.get("best")
    worst = stats.get("worst")
    best_str = f"{best.symbol} <b>{best.pnl_pct:+.1f}%</b>" if best and best.pnl_pct else "—"
    worst_str = f"{worst.symbol} <b>{worst.pnl_pct:+.1f}%</b>" if worst and worst.pnl_pct else "—"

    return (
        f"📊 <b>Статистика авто-шортов</b>\n\n"
        f"📈 Всего сделок: <b>{stats['total']}</b>\n"
        f"🟡 Открытых: <b>{stats['open']}</b>\n"
        f"✅ Закрытых: <b>{stats['closed']}</b>\n\n"
        f"{win_em} Win rate: <b>{win_rate:.1f}%</b> "
        f"({stats['wins']}W / {stats['losses']}L)\n"
        f"{avg_em} Средний P&L: <b>{avg_pnl:+.2f}%</b>\n\n"
        f"🏆 Лучшая: {best_str}\n"
        f"💀 Худшая: {worst_str}\n\n"
        f"<b>По типу закрытия:</b>\n{status_lines}"
    )


# ── Команды ───────────────────────────────────────────────────────

@router.message(Command("auto_shorts"))
async def cmd_auto_shorts(msg: Message) -> None:
    text = _format_active_shorts()
    await msg.answer(text, reply_markup=auto_shorts_keyboard())


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    text = await _format_stats()
    await msg.answer(text, reply_markup=stats_keyboard())


# ── Callbacks ─────────────────────────────────────────────────────

@router.callback_query(F.data == "auto_shorts:refresh")
async def cb_auto_shorts_refresh(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю...")
    except Exception:
        pass
    text = _format_active_shorts()
    try:
        await query.message.edit_text(
            text,
            reply_markup=auto_shorts_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "auto_shorts:stats")
async def cb_auto_shorts_stats(query: CallbackQuery) -> None:
    try:
        await query.answer("📊 Загружаю статистику...")
    except Exception:
        pass
    text = await _format_stats()
    try:
        await query.message.edit_text(
            text,
            reply_markup=stats_keyboard(),
        )
    except Exception:
        pass