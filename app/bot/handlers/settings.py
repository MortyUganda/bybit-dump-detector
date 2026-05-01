"""
/settings — настройки уведомлений пользователя.
Хранение в Redis (hash per user).
"""
from __future__ import annotations

import json

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.services.runtime_config import get_runtime_strategy_config, patch_runtime_strategy_config
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

REDIS_USER_SETTINGS_PREFIX = "user_settings"

DEFAULT_SETTINGS = {
    "alerts_enabled": True,
    "min_score": 50,
    "notify_early_warning": False,
    "notify_overheated": True,
    "notify_reversal_risk": True,
    "notify_dump_started": True,
    "quiet_mode": False,
}


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def get_user_settings(user_id: int, redis: aioredis.Redis | None = None) -> dict:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        raw = await redis.hgetall(f"{REDIS_USER_SETTINGS_PREFIX}:{user_id}")
        if not raw:
            return DEFAULT_SETTINGS.copy()
        merged = DEFAULT_SETTINGS.copy()
        merged.update({k: json.loads(v) for k, v in raw.items()})
        return merged
    finally:
        if close_after:
            await redis.aclose()


async def set_user_setting(
    user_id: int, key: str, value, redis: aioredis.Redis | None = None
) -> None:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        await redis.hset(
            f"{REDIS_USER_SETTINGS_PREFIX}:{user_id}", key, json.dumps(value)
        )
    finally:
        if close_after:
            await redis.aclose()


async def save_all_settings(
    user_id: int, settings_dict: dict, redis: aioredis.Redis | None = None
) -> None:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        key = f"{REDIS_USER_SETTINGS_PREFIX}:{user_id}"
        mapping = {k: json.dumps(v) for k, v in settings_dict.items()}
        await redis.hset(key, mapping=mapping)
    finally:
        if close_after:
            await redis.aclose()


async def settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    s = await get_user_settings(user_id)
    builder = InlineKeyboardBuilder()

    # Уведомления вкл/выкл
    alerts_label = "🔔 Уведомления: ВКЛ ✅" if s["alerts_enabled"] else "🔕 Уведомления: ВЫКЛ ❌"
    builder.button(text=alerts_label, callback_data="settings:toggle:alerts_enabled")

    # Минимальный score
    score_options = [45, 50, 55, 60]
    for score in score_options:
        marker = "✅ " if s["min_score"] == score else ""
        builder.button(
            text=f"{marker}Score ≥{score}",
            callback_data=f"settings:score:{score}",
        )

    # Типы сигналов
    signal_types = [
        ("notify_early_warning", "⚠️ Раннее предупреждение"),
        ("notify_overheated", "🔥 Перегрев"),
        ("notify_reversal_risk", "⬇️ Риск разворота"),
        ("notify_dump_started", "💥 Слив начался"),
    ]
    for key, label in signal_types:
        marker = "✅" if s[key] else "❌"
        builder.button(
            text=f"{marker} {label}",
            callback_data=f"settings:toggle:{key}",
        )

    # Тихий режим
    quiet_label = "🌙 Тихий режим: ВКЛ ✅" if s["quiet_mode"] else "🌙 Тихий режим: ВЫКЛ ❌"
    builder.button(text=quiet_label, callback_data="settings:toggle:quiet_mode")

    # Сброс
    builder.button(text="🔄 Сбросить настройки", callback_data="settings:reset")

    builder.adjust(1, 4, 1, 1, 1, 1, 1, 1)
    return builder.as_markup()


async def _format_settings(user_id: int) -> str:
    s = await get_user_settings(user_id)

    alerts_em = "✅ ВКЛ" if s["alerts_enabled"] else "❌ ВЫКЛ"
    quiet_em = "🌙 ВКЛ" if s["quiet_mode"] else "💡 ВЫКЛ"

    signal_lines = ""
    signal_map = {
        "notify_early_warning": "⚠️ Раннее предупреждение",
        "notify_overheated": "🔥 Перегрев",
        "notify_reversal_risk": "⬇️ Риск разворота",
        "notify_dump_started": "💥 Слив начался",
    }
    for key, label in signal_map.items():
        em = "✅" if s[key] else "❌"
        signal_lines += f"  {em} {label}\n"

    return (
        f"⚙️ <b>Настройки уведомлений</b>\n\n"
        f"🔔 Уведомления: <b>{alerts_em}</b>\n"
        f"📊 Минимальный score: <b>{s['min_score']}</b>\n"
        f"🌙 Тихий режим: <b>{quiet_em}</b>\n\n"
        f"<b>Типы сигналов:</b>\n{signal_lines}\n"
        f"<i>Нажмите кнопку для изменения настройки</i>"
    )


@router.message(Command("settings"))
async def cmd_settings(msg: Message) -> None:
    if not msg.from_user:
        return
    user_id = msg.from_user.id
    await msg.answer(
        await _format_settings(user_id),
        reply_markup=await settings_keyboard(user_id),
    )


@router.callback_query(F.data.startswith("settings:toggle:"))
async def cb_settings_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    key = query.data.split(":")[-1]
    s = await get_user_settings(user_id)

    if key in s:
        s[key] = not s[key]
        await set_user_setting(user_id, key, s[key])
        logger.info("Setting toggled", user_id=user_id, key=key, value=s[key])

    try:
        await query.message.edit_text(
            await _format_settings(user_id),
            reply_markup=await settings_keyboard(user_id),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("settings:score:"))
async def cb_settings_score(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    score = int(query.data.split(":")[-1])
    await set_user_setting(user_id, "min_score", score)

    logger.info("Min score changed", user_id=user_id, score=score)

    try:
        await query.message.edit_text(
            await _format_settings(user_id),
            reply_markup=await settings_keyboard(user_id),
        )
    except Exception:
        pass


@router.callback_query(F.data == "settings:reset")
async def cb_settings_reset(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Настройки сброшены")
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    await save_all_settings(user_id, DEFAULT_SETTINGS)

    try:
        await query.message.edit_text(
            await _format_settings(user_id),
            reply_markup=await settings_keyboard(user_id),
        )
    except Exception:
        pass


# ── BTC trend filter submenu ────────────────────────────────────


async def _btc_filter_keyboard() -> InlineKeyboardMarkup:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    enabled = cfg.get("btc_filter_enabled", True)
    threshold_15m = float(cfg.get("btc_filter_change_15m_threshold", 0.5))
    threshold_1h = float(cfg.get("btc_filter_change_1h_threshold", 1.0))
    mode = cfg.get("btc_filter_mode", "any")

    builder = InlineKeyboardBuilder()

    # Toggle on/off
    en_label = "🟢 BTC фильтр: ВКЛ" if enabled else "🔴 BTC фильтр: ВЫКЛ"
    builder.button(text=en_label, callback_data="btcf:toggle")

    # 15m threshold options
    for val in [0.3, 0.5, 0.7, 1.0, 1.5, 2.0]:
        marker = "✅ " if abs(threshold_15m - val) < 0.01 else ""
        builder.button(text=f"{marker}{val}%", callback_data=f"btcf:t15:{val}")

    # 1h threshold options
    for val in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        marker = "✅ " if abs(threshold_1h - val) < 0.01 else ""
        builder.button(text=f"{marker}{val}%", callback_data=f"btcf:t1h:{val}")

    # Mode toggle
    mode_label = "🔀 Режим: any (хотя бы один)" if mode == "any" else "🔀 Режим: both (оба)"
    builder.button(text=mode_label, callback_data="btcf:mode")

    # Back
    builder.button(text="⬅️ Назад", callback_data="btcf:back")

    builder.adjust(1, 6, 6, 1, 1)
    return builder.as_markup()


async def _format_btc_filter() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    enabled = cfg.get("btc_filter_enabled", True)
    threshold_15m = float(cfg.get("btc_filter_change_15m_threshold", 0.5))
    threshold_1h = float(cfg.get("btc_filter_change_1h_threshold", 1.0))
    mode = cfg.get("btc_filter_mode", "any")

    en_em = "🟢 ВКЛ" if enabled else "🔴 ВЫКЛ"
    mode_text = "any (хотя бы один порог)" if mode == "any" else "both (оба порога)"

    return (
        f"📊 <b>BTC trend filter — настройки</b>\n\n"
        f"Статус: <b>{en_em}</b>\n"
        f"⚙️ Порог 15m: <b>{threshold_15m}%</b>\n"
        f"⚙️ Порог 1h: <b>{threshold_1h}%</b>\n"
        f"🔀 Режим: <b>{mode_text}</b>\n\n"
        f"<i>Если BTC растёт выше порога — шорт блокируется.\n"
        f"any = хотя бы один порог пробит → блок\n"
        f"both = оба порога пробиты → блок</i>"
    )


async def show_btc_filter_menu(query: CallbackQuery) -> None:
    try:
        await query.message.edit_text(
            await _format_btc_filter(),
            reply_markup=await _btc_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "btcf:toggle")
async def cb_btcf_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_val = not cfg.get("btc_filter_enabled", True)
        await patch_runtime_strategy_config(redis, {"btc_filter_enabled": new_val})
        logger.info("BTC filter toggled", user_id=query.from_user.id if query.from_user else None, value=new_val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc_filter(),
            reply_markup=await _btc_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("btcf:t15:"))
async def cb_btcf_threshold_15m(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    val = float(query.data.split(":")[-1])
    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"btc_filter_change_15m_threshold": val})
        logger.info("BTC filter 15m threshold changed", user_id=query.from_user.id if query.from_user else None, value=val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc_filter(),
            reply_markup=await _btc_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("btcf:t1h:"))
async def cb_btcf_threshold_1h(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    val = float(query.data.split(":")[-1])
    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"btc_filter_change_1h_threshold": val})
        logger.info("BTC filter 1h threshold changed", user_id=query.from_user.id if query.from_user else None, value=val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc_filter(),
            reply_markup=await _btc_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "btcf:mode")
async def cb_btcf_mode(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        current_mode = cfg.get("btc_filter_mode", "any")
        new_mode = "both" if current_mode == "any" else "any"
        await patch_runtime_strategy_config(redis, {"btc_filter_mode": new_mode})
        logger.info("BTC filter mode changed", user_id=query.from_user.id if query.from_user else None, value=new_mode)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc_filter(),
            reply_markup=await _btc_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "btcf:back")
async def cb_btcf_back(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    from aiogram.types import InlineKeyboardButton

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Глобальные настройки авто-шорта", callback_data="settings:strategy")],
        [InlineKeyboardButton(text="🔔 Настройки уведомлений", callback_data="settings:notifications")],
        [InlineKeyboardButton(text="📊 BTC trend filter", callback_data="settings:btc_filter")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="settings:back_main")],
    ])
    try:
        await query.message.edit_text(
            "🔧 <b>Настройки</b>\n\nВыберите раздел:",
            reply_markup=kb,
        )
    except Exception:
        pass
