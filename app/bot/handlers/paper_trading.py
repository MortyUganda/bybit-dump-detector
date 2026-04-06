"""
Paper trading handlers — открытие, выбор стратегии, статус сделки.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards.paper_trading import (
    alert_action_keyboard,
    strategy_keyboard,
    trade_status_keyboard,
)
from app.bot.paper_trading.strategies import STRATEGIES, calculate_levels
from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

# MVP: хранение сделок в памяти
PAPER_TRADES: dict[int, dict] = {}
_trade_counter = 0


def _next_trade_id() -> int:
    global _trade_counter
    _trade_counter += 1
    return _trade_counter


async def _get_current_price(symbol: str) -> float | None:
    # Сначала пробуем из Redis
    try:
        settings = get_settings()
        redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        raw = await redis.get(f"score:{symbol}")
        await redis.aclose()

        if raw:
            data = json.loads(raw)
            # Пробуем features_snapshot
            snapshot = data.get("features_snapshot") or {}
            price = snapshot.get("last_price")
            if price:
                return float(price)

            # Пробуем прямо в корне dict
            factors = data.get("factors") or []
            # fallback ниже
    except Exception:
        pass

    # Fallback: берём цену напрямую с Bybit REST
    try:
        import aiohttp
        url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
                items = data.get("result", {}).get("list", [])
                if items:
                    return float(items[0]["lastPrice"])
    except Exception:
        pass

    return None


# ── Пропустить сигнал ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("pt:skip:"))
async def cb_pt_skip(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer("⏭ Сигнал пропущен")
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(f"⏭ Сигнал по <b>{symbol}</b> пропущен.")


# ── Открыть позицию — выбор стратегии ────────────────────────────

@router.callback_query(F.data.startswith("pt:open:"))
async def cb_pt_open(query: CallbackQuery) -> None:
    symbol = query.data.split(":")[-1]
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"📈 <b>Открываем позицию по {symbol}</b>\n\n"
        f"Выберите стратегию:\n\n"
        f"🎯 <b>Консервативная</b> — небольшие цели, меньший риск\n"
        f"⚡ <b>Средняя</b> — стандартные уровни TP/SL\n"
        f"🚀 <b>Агрессивная</b> — большие цели, выше риск\n",
        reply_markup=strategy_keyboard(symbol),
    )


# ── Выбор стратегии — открытие сделки ────────────────────────────

@router.callback_query(F.data.startswith("pt:strategy:"))
async def cb_pt_strategy(query: CallbackQuery) -> None:
    await query.answer()

    parts = query.data.split(":")
    symbol = parts[2]
    strategy_key = parts[3]

    strategy = STRATEGIES.get(strategy_key)
    if not strategy:
        await query.answer("Неизвестная стратегия", show_alert=True)
        return

    entry_price = await _get_current_price(symbol)
    if not entry_price:
        await query.message.answer(
            f"⚠️ Не удалось получить цену для <b>{symbol}</b>. Попробуйте позже."
        )
        return

    levels = calculate_levels(entry_price, strategy)
    trade_id = _next_trade_id()
    user_id = query.from_user.id if query.from_user else 0

    PAPER_TRADES[trade_id] = {
        "id": trade_id,
        "user_id": user_id,
        "symbol": symbol,
        "strategy": strategy_key,
        "entry_price": entry_price,
        "entry_ts": datetime.now(timezone.utc).isoformat(),
        "status": "open",
        "tp1_price": levels["tp1_price"],
        "tp2_price": levels["tp2_price"],
        "tp3_price": levels["tp3_price"],
        "sl_price": levels["sl_price"],
        "tp1_pct": strategy.tp1_pct,
        "tp2_pct": strategy.tp2_pct,
        "tp3_pct": strategy.tp3_pct,
        "sl_pct": strategy.sl_pct,
        "pnl_pct": None,
        "exit_price": None,
        "exit_ts": None,
    }

    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"✅ <b>Paper сделка #{trade_id} открыта</b>\n\n"
        f"📌 <b>{symbol}</b> | {strategy.label}\n"
        f"💰 Вход: <b>${entry_price:.6g}</b>\n\n"
        f"🎯 <b>Уровни:</b>\n"
        f"  TP1: ${levels['tp1_price']:.6g} (+{strategy.tp1_pct}%)\n"
        f"  TP2: ${levels['tp2_price']:.6g} (+{strategy.tp2_pct}%)\n"
        f"  TP3: ${levels['tp3_price']:.6g} (+{strategy.tp3_pct}%)\n"
        f"  SL:  ${levels['sl_price']:.6g} (-{strategy.sl_pct}%)\n\n"
        f"<i>Бот будет следить за ценой и уведомит при достижении уровней.</i>",
        reply_markup=trade_status_keyboard(trade_id),
    )


# ── Статус сделки ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("pt:status:"))
async def cb_pt_status(query: CallbackQuery) -> None:
    await query.answer()
    trade_id = int(query.data.split(":")[-1])
    trade = PAPER_TRADES.get(trade_id)

    if not trade:
        await query.message.answer("❌ Сделка не найдена.")
        return

    current_price = await _get_current_price(trade["symbol"])
    if not current_price:
        await query.message.answer("⚠️ Не удалось получить текущую цену.")
        return

    pnl = (current_price - trade["entry_price"]) / trade["entry_price"] * 100
    pnl_em = "🟢" if pnl >= 0 else "🔴"

    await query.message.answer(
        f"📊 <b>Сделка #{trade_id} — {trade['symbol']}</b>\n\n"
        f"Статус: <b>{trade['status'].upper()}</b>\n"
        f"Вход: <b>${trade['entry_price']:.6g}</b>\n"
        f"Текущая цена: <b>${current_price:.6g}</b>\n"
        f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>\n\n"
        f"🎯 TP1: ${trade['tp1_price']:.6g} (+{trade['tp1_pct']}%)\n"
        f"🎯 TP2: ${trade['tp2_price']:.6g} (+{trade['tp2_pct']}%)\n"
        f"🎯 TP3: ${trade['tp3_price']:.6g} (+{trade['tp3_pct']}%)\n"
        f"🛑 SL:  ${trade['sl_price']:.6g} (-{trade['sl_pct']}%)\n",
        reply_markup=trade_status_keyboard(trade_id),
    )


# ── Закрыть сделку вручную ────────────────────────────────────────

@router.callback_query(F.data.startswith("pt:close:"))
async def cb_pt_close(query: CallbackQuery) -> None:
    await query.answer()
    trade_id = int(query.data.split(":")[-1])
    trade = PAPER_TRADES.get(trade_id)

    if not trade:
        await query.message.answer("❌ Сделка не найдена.")
        return

    if trade["status"] != "open":
        await query.message.answer(f"ℹ️ Сделка #{trade_id} уже закрыта.")
        return

    current_price = await _get_current_price(trade["symbol"])
    if not current_price:
        await query.message.answer("⚠️ Не удалось получить текущую цену.")
        return

    pnl = (current_price - trade["entry_price"]) / trade["entry_price"] * 100

    trade["status"] = "closed_manual"
    trade["exit_price"] = current_price
    trade["exit_ts"] = datetime.now(timezone.utc).isoformat()
    trade["pnl_pct"] = pnl

    pnl_em = "🟢" if pnl >= 0 else "🔴"

    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"❌ <b>Сделка #{trade_id} закрыта вручную</b>\n\n"
        f"📌 {trade['symbol']}\n"
        f"Вход: ${trade['entry_price']:.6g}\n"
        f"Выход: ${current_price:.6g}\n"
        f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>",
    )


# ── Список всех сделок ────────────────────────────────────────────

@router.message(Command("trades"))
async def cmd_trades(msg: Message) -> None:
    if not msg.from_user:
        await msg.answer("Не удалось определить пользователя.")
        return

    user_id = msg.from_user.id
    user_trades = [t for t in PAPER_TRADES.values() if t["user_id"] == user_id]

    if not user_trades:
        await msg.answer(
            "📋 <b>Ваши paper сделки</b>\n\n"
            "<i>Пока нет сделок. Откройте позицию при следующем сигнале.</i>"
        )
        return

    lines = ["📋 <b>Ваши paper сделки</b>\n"]
    for trade in sorted(user_trades, key=lambda x: -x["id"]):
        status_em = {
            "open": "🟡",
            "tp1": "🟢",
            "tp2": "🟢",
            "tp3": "🟢",
            "sl": "🔴",
            "closed_manual": "⚪",
        }.get(trade["status"], "❓")

        pnl_str = (
            f"{trade['pnl_pct']:+.2f}%"
            if trade.get("pnl_pct") is not None
            else "в процессе"
        )
        lines.append(
            f"{status_em} #{trade['id']} de>{trade['symbol']}</code> "
            f"| {trade['strategy']} | {pnl_str}"
        )

    await msg.answer("\n".join(lines))

