"""
/coin SYMBOL — полная диагностика монеты из Redis.
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


def coin_keyboard(symbol: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔄 Обновить",
        callback_data=f"coin:detail:{symbol}",
    )
    builder.button(
        text="⭐ В watchlist",
        callback_data=f"watch:add:{symbol}",
    )
    builder.button(
        text="📈 Bybit фьючерсы",
        url=f"https://www.bybit.com/trade/usdt/{symbol}",
    )
    builder.adjust(2, 1)
    return builder.as_markup()


def _bar(normalized: float, width: int = 5) -> str:
    """Визуальный бар от 0 до 1."""
    filled = round(normalized * width)
    return "▓" * filled + "░" * (width - filled)


def _risk_emoji(level: str) -> str:
    return {
        "low": "🟢",
        "moderate": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }.get(level, "⚪")


def _signal_emoji(signal_type: str) -> str:
    return {
        "early_warning": "⚠️",
        "overheated": "🔥",
        "reversal_risk": "⬇️",
        "dump_started": "💥",
    }.get(signal_type or "", "📊")


FACTOR_LABELS = {
    "rsi":                "RSI перекупленность",
    "vwap_extension":     "Отклонение от VWAP",
    "volume_zscore":      "Всплеск объёма",
    "trade_imbalance":    "Дисбаланс покупок",
    "large_buy_cluster":  "Кластер крупных покупок",
    "price_acceleration": "Ускорение цены",
    "consecutive_greens": "Зелёные свечи подряд",
    "ob_bid_thinning":    "Уход бидов",
    "spread_expansion":   "Расширение спреда",
    "momentum_loss":      "Потеря моментума",
    "upper_wick":         "Верхний хвост (rejection)",
    "funding_rate":       "Funding rate",
}


async def _fetch_coin_data(symbol: str) -> dict | None:
    """Получить данные монеты из Redis."""
    try:
        settings = get_settings()
        redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        raw = await redis.get(f"score:{symbol}")
        await redis.aclose()

        if not raw:
            return None

        return json.loads(raw)

    except Exception as e:
        logger.error("Redis fetch failed", symbol=symbol, error=str(e))
        return None


def _format_coin_card(symbol: str, data: dict) -> str:
    score = data.get("score", 0)
    level = data.get("level", "low")
    signal_type = data.get("signal_type")
    triggered_count = data.get("triggered_count", 0)
    factors = data.get("factors", [])

    level_em = _risk_emoji(level)
    signal_em = _signal_emoji(signal_type)
    signal_label = (signal_type or "нет").replace("_", " ").title()

    base = symbol.replace("USDT", "")
    bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

    # Заголовок
    lines = [
        f"🔍 <b><a href=\"{bybit_url}\">{symbol}</a> — Диагностика</b>\n",
        f"{level_em} <b>Risk Score: {score:.0f}/100</b> ({level.upper()})",
        f"{signal_em} Сигнал: <b>{signal_label}</b>",
        f"⚡ Факторов сработало: <b>{triggered_count}/12</b>\n",
    ]

    # Факторы с барами
    lines.append("<b>Факторы риска:</b>")

    sorted_factors = sorted(factors, key=lambda x: -x.get("contribution", 0))

    for f in sorted_factors:
        name = f.get("name", "")
        label = FACTOR_LABELS.get(name, name)
        normalized = f.get("normalized", 0)
        contribution = f.get("contribution", 0)
        raw_value = f.get("raw_value", 0)

        bar = _bar(normalized)
        triggered_mark = "🔴" if normalized >= 0.5 else "⚪"

        lines.append(
            f"{triggered_mark} {bar} <b>{label}</b>\n"
            f"         {contribution:.1f}pts | raw: {raw_value:.3g}"
        )

    # Снимок признаков если есть
    snapshot = data.get("features_snapshot") or {}
    if snapshot:
        price = snapshot.get("last_price")
        rsi = snapshot.get("rsi_14_1m")
        vwap = snapshot.get("vwap_extension_pct")
        volume_z = snapshot.get("volume_zscore_1m")
        imbalance = snapshot.get("trade_imbalance_5m")
        spread = snapshot.get("spread_pct")
        momentum = snapshot.get("momentum_loss_signal")
        greens = snapshot.get("consecutive_green_candles")

        lines.append("\n<b>Ключевые метрики:</b>")
        if price:
            lines.append(f"  💰 Цена: <b>${price:.6g}</b>")
        if rsi is not None:
            lines.append(f"  📊 RSI (1m): <b>{rsi:.1f}</b>")
        if vwap is not None:
            lines.append(f"  📈 VWAP ext: <b>{vwap:+.2f}%</b>")
        if volume_z is not None:
            lines.append(f"  📦 Volume z: <b>{volume_z:.2f}σ</b>")
        if imbalance is not None:
            lines.append(f"  ⚖️ Buy imbalance: <b>{imbalance:.2f}</b>")
        if spread is not None:
            lines.append(f"  ↔️ Спред: <b>{spread:.3f}%</b>")
        if greens is not None:
            lines.append(f"  🟢 Зелёных свечей: <b>{int(greens)}</b>")
        if momentum is not None:
            lines.append(f"  ⬇️ Моментум потерян: <b>{'Да ⚠️' if momentum else 'Нет'}</b>")

    return "\n".join(lines)


@router.message(Command("coin"))
async def cmd_coin(msg: Message) -> None:
    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer(
            "Укажи тикер монеты.\n\n"
            "Пример: de>/coin DOGE</code>"
        )
        return

    symbol = args[1].upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    data = await _fetch_coin_data(symbol)

    if not data:
        await msg.answer(
            f"🔍 <b>{symbol}</b>\n\n"
            f"<i>Данных нет. Возможные причины:</i>\n"
            f"• Монета не входит в universe (~115 монет)\n"
            f"• Анализатор ещё не обработал эту монету\n"
            f"• Данные устарели (TTL 5 минут)\n\n"
            f"Попробуй одну из монет из /overvalued"
        )
        return

    text = _format_coin_card(symbol, data)
    await msg.answer(text, reply_markup=coin_keyboard(symbol))


@router.callback_query(F.data.startswith("coin:detail:"))
async def cb_coin_detail(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю...")
    except Exception:
        pass

    symbol = query.data.split(":")[-1]
    data = await _fetch_coin_data(symbol)

    if not data:
        try:
            await query.message.edit_text(
                f"🔍 <b>{symbol}</b>\n\n<i>Данные недоступны.</i>",
                reply_markup=coin_keyboard(symbol),
            )
        except Exception:
            pass
        return

    text = _format_coin_card(symbol, data)
    try:
        await query.message.edit_text(text, reply_markup=coin_keyboard(symbol))
    except Exception:
        pass

@router.callback_query(F.data.startswith("alert:detail:"))
async def cb_alert_detail(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    symbol = query.data.split(":")[-1]
    data = await _fetch_coin_data(symbol)

    if not data:
        await query.message.answer(
            f"🔍 <b>{symbol}</b>\n\n<i>Данные недоступны.</i>"
        )
        return

    text = _format_coin_card(symbol, data)
    await query.message.answer(text, reply_markup=coin_keyboard(symbol))