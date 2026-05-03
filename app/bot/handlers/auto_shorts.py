"""
/auto_shorts — активные шорты
/stats — статистика по всем авто-шортам
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bybit.rest_client import BybitRestClient
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


import redis.asyncio as aioredis
from app.config import get_settings
from app.services.auto_short_service import AutoShortService

async def _get_current_price(symbol: str) -> float | None:
    try:
        settings = get_settings()
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)

        service = AutoShortService(redis=redis)
        # используем единый источник истины
        price = await service._get_price(symbol)  # внутренний метод, но наш код
        return float(price) if price is not None else None

    except Exception as e:
        logger.error("Failed to fetch current price", symbol=symbol, error=str(e))
        return None
    

def _calc_short_pnl_pct(
    entry_price: float,
    current_price: float,
    leverage: int | float | None,
) -> float:
    lev = float(leverage or 1)
    raw_move_pct = ((entry_price - current_price) / entry_price) * 100
    return raw_move_pct * lev


def auto_shorts_keyboard(trade_ids: list[int] | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    builder.button(text="🔄 Обновить", callback_data="auto_shorts:refresh")
    builder.button(text="📊 Статистика", callback_data="auto_shorts:stats")

    if trade_ids:
        for trade_id in trade_ids:
            builder.button(
                text=f"✋ Закрыть #{trade_id}",
                callback_data=f"auto_shorts:close:{trade_id}",
            )

    if trade_ids:
        builder.adjust(2, *([1] * len(trade_ids)))
    else:
        builder.adjust(2)

    return builder.as_markup()



def stats_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="auto_shorts:stats")
    builder.button(text="🤖 Активные", callback_data="auto_shorts:refresh")
    builder.adjust(2)
    return builder.as_markup()


async def _get_stats() -> dict:
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models.auto_short import AutoShort
        from sqlalchemy import select

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


async def _format_active_shorts() -> tuple[str, list[int]]:
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models.auto_short import AutoShort
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(AutoShort).where(AutoShort.status == "open")
            )
            trades = result.scalars().all()

    except Exception as e:
        logger.error("Failed to fetch active shorts", error=str(e))
        return "❌ Ошибка загрузки данных.", []

    if not trades:
        return (
            "🤖 <b>Авто-шорты</b>\n\n"
            "<i>Нет активных сделок.</i>\n\n"
            "Сделки открываются автоматически при score ≥ 45.",
            [],
        )

    lines = [f"🤖 <b>Авто-шорты</b> ({len(trades)} активных)\n"]
    trade_ids: list[int] = []

    for trade in sorted(trades, key=lambda t: -t.id):
        symbol = trade.symbol
        bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
        now = datetime.now(timezone.utc)
        elapsed_min = int((now - trade.entry_ts).total_seconds() / 60)

        current_price = await _get_current_price(symbol)
        if current_price is not None:
            current_pnl = _calc_short_pnl_pct(
                entry_price=float(trade.entry_price),
                current_price=float(current_price),
                leverage=trade.leverage,
            )
            pnl_emoji = "🟢" if current_pnl > 0 else "🔴" if current_pnl < 0 else "⚪"
            current_price_line = f"   💹 Сейчас: <b>${current_price:.6g}</b>\n"
            pnl_line = f"   {pnl_emoji} PnL now: <b>{current_pnl:+.2f}%</b>\n"
        else:
            current_price_line = "   💹 Сейчас: <b>н/д</b>\n"
            pnl_line = "   ⚪ PnL now: <b>н/д</b>\n"

        lines.append(
            f"📌 #{trade.id} <a href=\"{bybit_url}\">{symbol}</a>\n"
            f"   💰 Вход: <b>${trade.entry_price:.6g}</b> | ⏱ {elapsed_min}м\n"
            f"{current_price_line}"
            f"{pnl_line}"
            f"   🎯 TP: ${trade.tp_price:.6g} | 🛑 SL: ${trade.sl_price:.6g}\n"
            f"   📊 Score: {trade.score:.0f} | ⚙️ x{trade.leverage or 1}"
        )
        trade_ids.append(trade.id)

    return "\n\n".join(lines), trade_ids


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
        "trailing_sl": "📉 Trailing SL",
        "expired": "⏰ Истекли",
        "closed_manual": "✋ Вручную",
        "manual": "✋ Вручную",
    }
    for status, count in by_status.items():
        label = status_labels.get(status, status)
        status_lines += f"  {label}: {count}\n"

    best = stats.get("best")
    worst = stats.get("worst")
    best_str = (
        f"{best.symbol} <b>{best.pnl_pct:+.1f}%</b>"
        if best and best.pnl_pct is not None
        else "—"
    )
    worst_str = (
        f"{worst.symbol} <b>{worst.pnl_pct:+.1f}%</b>"
        if worst and worst.pnl_pct is not None
        else "—"
    )

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


async def _manual_close_trade(trade_id: int) -> tuple[bool, str]:
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        from app.services.auto_short_service import AutoShortService

        settings = get_settings()
        redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        service = AutoShortService(redis=redis)
        result = await service.close_trade_manually(trade_id)

        if not result:
            return False, "Сделка не найдена или уже закрыта."

        return True, result

    except Exception as e:
        logger.error("Manual close failed", trade_id=trade_id, error=str(e))
        return False, "Ошибка при ручном закрытии сделки."

@router.message(Command("auto_shorts"))
async def cmd_auto_shorts(msg: Message) -> None:
    text, trade_ids = await _format_active_shorts()
    await msg.answer(text, reply_markup=auto_shorts_keyboard(trade_ids))


@router.message(Command("stats"))
async def cmd_stats(msg: Message) -> None:
    text = await _format_stats()
    await msg.answer(text, reply_markup=stats_keyboard())


@router.message(F.text == "🤖 Авто-шорты")
async def auto_shorts_from_reply_keyboard(msg: Message) -> None:
    text, trade_ids = await _format_active_shorts()
    await msg.answer(text, reply_markup=auto_shorts_keyboard(trade_ids))


@router.callback_query(F.data == "auto_shorts:refresh")
async def cb_auto_shorts_refresh(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю...")
    except Exception:
        pass

    text, trade_ids = await _format_active_shorts()
    try:
        await query.message.edit_text(
            text,
            reply_markup=auto_shorts_keyboard(trade_ids),
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


@router.callback_query(F.data.startswith("auto_shorts:close:"))
async def cb_auto_shorts_close(query: CallbackQuery) -> None:
    try:
        await query.answer("✋ Закрываю сделку...")
    except Exception:
        pass

    try:
        trade_id = int(query.data.split(":")[-1])
    except Exception:
        await query.message.answer("❌ Некорректный trade_id.")
        return

    ok, result_text = await _manual_close_trade(trade_id)
    await query.message.answer(result_text)

    if ok:
        text, trade_ids = await _format_active_shorts()
        try:
            await query.message.answer(
                text,
                reply_markup=auto_shorts_keyboard(trade_ids),
            )
        except Exception:
            pass
