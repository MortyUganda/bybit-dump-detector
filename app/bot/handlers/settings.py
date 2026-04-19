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
        return {k: json.loads(v) for k, v in raw.items()}
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

    # Reversal Risk submenu
    builder.button(text="⬇️ Reversal Risk настройки", callback_data="settings:reversal_risk")

    # Сброс
    builder.button(text="🔄 Сбросить настройки", callback_data="settings:reset")

    builder.adjust(1, 4, 1, 1, 1, 1, 1, 1, 1)
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


# ── Reversal Risk submenu ────────────────────────────────────────


async def _reversal_risk_keyboard() -> InlineKeyboardMarkup:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    enabled = cfg.get("reversal_risk_enabled", True)
    warning_th = int(cfg.get("reversal_risk_warning_threshold", 4))
    critical_th = int(cfg.get("reversal_risk_critical_threshold", 7))
    action = cfg.get("reversal_risk_action", "tighten_trailing")

    builder = InlineKeyboardBuilder()

    # Toggle enabled
    en_label = "✅ Включено" if enabled else "❌ Выключено"
    builder.button(text=en_label, callback_data="rr:toggle")

    # Warning threshold
    for val in [3, 4, 5, 6]:
        marker = "✅ " if warning_th == val else ""
        builder.button(text=f"{marker}W≥{val}", callback_data=f"rr:warn:{val}")

    # Critical threshold
    for val in [5, 6, 7, 8]:
        marker = "✅ " if critical_th == val else ""
        builder.button(text=f"{marker}C≥{val}", callback_data=f"rr:crit:{val}")

    # Action
    actions = [
        ("notify_only", "🔔 Только уведомление"),
        ("tighten_trailing", "🔧 Ужесточить trailing"),
        ("auto_close", "🚪 Закрыть сделку"),
    ]
    for act_val, act_label in actions:
        marker = "✅ " if action == act_val else ""
        builder.button(text=f"{marker}{act_label}", callback_data=f"rr:action:{act_val}")

    # Back
    builder.button(text="◀️ Назад", callback_data="rr:back")

    builder.adjust(1, 4, 4, 3, 1)
    return builder.as_markup()


async def _format_reversal_risk() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    enabled = cfg.get("reversal_risk_enabled", True)
    warning_th = int(cfg.get("reversal_risk_warning_threshold", 4))
    critical_th = int(cfg.get("reversal_risk_critical_threshold", 7))
    action = cfg.get("reversal_risk_action", "tighten_trailing")

    en_em = "✅ ВКЛ" if enabled else "❌ ВЫКЛ"
    action_map = {
        "tighten_trailing": "🔧 Ужесточить trailing",
        "auto_close": "🚪 Закрыть сделку",
    }
    action_text = action_map.get(action, "🔔 Только уведомление")

    critical_desc = {
        "tighten_trailing": "ужесточение trailing stop",
        "auto_close": "автозакрытие позиции",
    }
    critical_action = critical_desc.get(action, "только уведомление")

    return (
        f"⬇️ <b>Reversal Risk — настройки</b>\n\n"
        f"Статус: <b>{en_em}</b>\n"
        f"🟡 Warning порог: <b>≥{warning_th}</b>\n"
        f"🔴 Critical порог: <b>≥{critical_th}</b>\n"
        f"Действие: <b>{action_text}</b>\n\n"
        f"<i>Warning — уведомление в Telegram\n"
        f"Critical — уведомление + {critical_action}</i>"
    )


@router.callback_query(F.data == "settings:reversal_risk")
async def cb_reversal_risk_menu(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.message.edit_text(
            await _format_reversal_risk(),
            reply_markup=await _reversal_risk_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "rr:toggle")
async def cb_rr_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_val = not cfg.get("reversal_risk_enabled", True)
        await patch_runtime_strategy_config(redis, {"reversal_risk_enabled": new_val})
        logger.info("Reversal risk toggled", user_id=query.from_user.id if query.from_user else None, value=new_val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_reversal_risk(),
            reply_markup=await _reversal_risk_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("rr:warn:"))
async def cb_rr_warning(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    val = int(query.data.split(":")[-1])
    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_risk_warning_threshold": val})
        logger.info("Reversal risk warning threshold changed", user_id=query.from_user.id if query.from_user else None, value=val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_reversal_risk(),
            reply_markup=await _reversal_risk_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("rr:crit:"))
async def cb_rr_critical(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    val = int(query.data.split(":")[-1])
    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_risk_critical_threshold": val})
        logger.info("Reversal risk critical threshold changed", user_id=query.from_user.id if query.from_user else None, value=val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_reversal_risk(),
            reply_markup=await _reversal_risk_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("rr:action:"))
async def cb_rr_action(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    val = query.data.split(":")[-1]
    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_risk_action": val})
        logger.info("Reversal risk action changed", user_id=query.from_user.id if query.from_user else None, value=val)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_reversal_risk(),
            reply_markup=await _reversal_risk_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "rr:back")
async def cb_rr_back(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    if not query.from_user:
        return

    user_id = query.from_user.id
    try:
        await query.message.edit_text(
            await _format_settings(user_id),
            reply_markup=await settings_keyboard(user_id),
        )
    except Exception:
        pass
