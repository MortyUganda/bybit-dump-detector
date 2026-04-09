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
    "rsi_5m":             "RSI 5m перекупленность",
    "vwap_extension":     "Отклонение от VWAP",
    "volume_zscore":      "Всплеск объёма",
    "trade_imbalance":    "Дисбаланс покупок",
    "large_buy_cluster":  "Кластер крупных покупок",
    "large_sell_cluster": "Кластер крупных продаж",
    "price_acceleration": "Ускорение цены",
    "consecutive_greens": "Зелёные свечи подряд",
    "ob_bid_thinning":    "Уход бидов",
    "spread_expansion":   "Расширение спреда",
    "momentum_loss":      "Потеря моментума",
    "upper_wick":         "Верхний хвост (rejection)",
    "funding_rate":       "Funding rate",
    "oi_spike":           "OI всплеск",
    "cvd_divergence":     "CVD дивергенция",
    "liquidation_cascade": "Ликвидационный каскад",
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


def _value_emoji(value: float, warn_threshold: float, crit_threshold: float, higher_is_worse: bool = True) -> str:
    """Return emoji indicator based on thresholds."""
    if higher_is_worse:
        if value >= crit_threshold:
            return "🔴"
        if value >= warn_threshold:
            return "⚠️"
        return "✅"
    else:
        if value <= crit_threshold:
            return "🔴"
        if value <= warn_threshold:
            return "⚠️"
        return "✅"


def _format_coin_card(symbol: str, data: dict) -> str:
    score = data.get("score", 0)
    level = data.get("level", "low")
    signal_type = data.get("signal_type")
    triggered_count = data.get("triggered_count", 0)
    factors = data.get("factors", [])

    level_em = _risk_emoji(level)
    signal_em = _signal_emoji(signal_type)
    signal_label = (signal_type or "нет").replace("_", " ").title()
    level_label = {
        "low": "НИЗКИЙ", "moderate": "УМЕРЕННЫЙ",
        "high": "ВЫСОКИЙ", "critical": "КРИТИЧЕСКИЙ",
    }.get(level, level.upper())

    bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

    # Заголовок
    lines = [
        f"🔍 <b><a href=\"{bybit_url}\">{symbol}</a> — Диагностика</b>\n",
    ]

    snapshot = data.get("features_snapshot") or {}
    price = snapshot.get("last_price")
    if price:
        lines.append(f"💰 Цена: <b>${price:.6g}</b>")

    lines.extend([
        f"📊 Risk Score: {level_em} <b>{score:.0f}/100</b> ({level_label})",
        f"{signal_em} Сигнал: <b>{signal_label}</b>",
        f"⚡ Факторов сработало: <b>{triggered_count}/17</b>\n",
    ])

    # ── Индикаторы ───────────────────────────────────────────────
    if snapshot:
        rsi_1m = snapshot.get("rsi_14_1m")
        rsi_5m = snapshot.get("rsi_14_5m")
        vwap = snapshot.get("vwap_extension_pct")
        volume_z = snapshot.get("volume_zscore_1m")

        lines.append("<b>📈 Индикаторы:</b>")
        indicator_parts = []
        if rsi_1m is not None:
            indicator_parts.append(f"RSI 1m: <b>{rsi_1m:.1f}</b>")
        if rsi_5m is not None:
            indicator_parts.append(f"RSI 5m: <b>{rsi_5m:.1f}</b>")
        if indicator_parts:
            lines.append(f"  {' | '.join(indicator_parts)}")
        if vwap is not None:
            vwap_em = _value_emoji(abs(vwap), 1.5, 3.0)
            lines.append(f"  VWAP отклонение: {vwap_em} <b>{vwap:+.1f}%</b>")
        if volume_z is not None:
            vol_em = _value_emoji(volume_z, 1.5, 2.5)
            lines.append(f"  Volume z-score: {vol_em} <b>{volume_z:.1f}σ</b>")

    # ── Продвинутые метрики ──────────────────────────────────────
    if snapshot:
        cvd_div = snapshot.get("cvd_divergence")
        liq_score = snapshot.get("liquidation_cascade_score")
        trend_ctx = snapshot.get("trend_context") or {}
        trend_strength = trend_ctx.get("trend_strength") if isinstance(trend_ctx, dict) else None
        funding = snapshot.get("funding_rate")
        oi_change = snapshot.get("oi_change_pct")
        vol_1h = snapshot.get("realized_vol_1h")

        has_advanced = any(v is not None for v in [
            cvd_div, liq_score, trend_strength, funding, oi_change, vol_1h,
        ])

        if has_advanced:
            lines.append("\n<b>🆕 Продвинутые:</b>")

            if cvd_div is not None:
                cvd_label = "медвежья ⚠️" if cvd_div < -0.2 else (
                    "бычья" if cvd_div > 0.2 else "нейтральная"
                )
                lines.append(f"  CVD дивергенция: <b>{cvd_div:.2f}</b> ({cvd_label})")

            if liq_score is not None:
                liq_em = _value_emoji(liq_score, 0.3, 0.6)
                liq_label = "⚠️ повышенный" if liq_score >= 0.3 else "спокойно"
                lines.append(
                    f"  Ликвидационный каскад: {liq_em} <b>{liq_score:.2f}</b> ({liq_label})"
                )

            if trend_strength is not None:
                if trend_strength > 0.3:
                    trend_label = "аптренд"
                    trend_em = "📈"
                elif trend_strength < -0.3:
                    trend_label = "даунтренд"
                    trend_em = "📉"
                else:
                    trend_label = "боковик"
                    trend_em = "➡️"
                lines.append(
                    f"  {trend_em} Тренд 1h: <b>{trend_strength:+.1f}</b> ({trend_label})"
                )

            if funding is not None:
                fund_em = _value_emoji(abs(funding), 0.03, 0.08)
                lines.append(f"  Funding rate: {fund_em} <b>{funding:.4f}%</b>")

            if oi_change is not None:
                lines.append(f"  OI изменение: <b>{oi_change:+.1f}%</b>")

            if vol_1h is not None:
                lines.append(f"  Волатильность 1h: <b>{vol_1h:.1f}%</b>")

    # ── Факторы с барами ─────────────────────────────────────────
    lines.append("\n<b>Факторы риска:</b>")

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

    # ── BTC контекст ─────────────────────────────────────────────
    btc_change = snapshot.get("btc_change_15m") if snapshot else None
    if btc_change is not None:
        btc_filter = abs(btc_change) > 1.0
        btc_label = "фильтр АКТИВЕН" if btc_filter else "фильтр неактивен"
        btc_em = "🚫" if btc_filter else "📌"
        lines.append(
            f"\n{btc_em} BTC: <b>{btc_change:+.1f}%</b> (15m) — {btc_label}"
        )

    return "\n".join(lines)


@router.message(Command("coin"))
async def cmd_coin(msg: Message) -> None:
    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer(
            "Укажи тикер монеты.\n\n"
            "Пример: <code>/coin DOGE</code>"
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