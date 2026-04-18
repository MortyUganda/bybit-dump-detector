"""
/strategy — глобальная runtime-конфигурация стратегии авто-шорта через Telegram.
Только для админов (защищается AccessMiddleware).
"""
from __future__ import annotations

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.services.runtime_config import (
    get_runtime_strategy_config,
    patch_runtime_strategy_config,
    reset_runtime_strategy_config,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


def _format_signal_toggle(signal_type: str, enabled: bool) -> str:
    labels = {
        "early_warning": "⚠️ early_warning",
        "overheated": "🔥 overheated",
        "reversal_risk": "⬇️ reversal_risk",
        "dump_started": "💥 dump_started",
    }
    marker = "✅" if enabled else "❌"
    return f"{marker} {labels.get(signal_type, signal_type)}"


async def strategy_keyboard() -> InlineKeyboardMarkup:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    builder = InlineKeyboardBuilder()

    enabled_label = "🤖 Авто-шорт: ВКЛ ✅" if cfg["enabled"] else "🤖 Авто-шорт: ВЫКЛ ❌"
    builder.button(text=enabled_label, callback_data="strategy:toggle:enabled")

    for signal_type in ["early_warning", "overheated", "reversal_risk", "dump_started"]:
        is_enabled = signal_type in cfg.get("allowed_signal_types", [])
        builder.button(
            text=_format_signal_toggle(signal_type, is_enabled),
            callback_data=f"strategy:signal:{signal_type}",
        )

    for score in [40, 45, 50, 55]:
        marker = "✅ " if cfg["min_score_to_enter"] == score else ""
        builder.button(
            text=f"{marker}Entry score ≥{score}",
            callback_data=f"strategy:min_score:{score}",
        )

    for delay in [15, 30, 60, 90]:
        marker = "✅ " if cfg["entry_delay_sec"] == delay else ""
        builder.button(
            text=f"{marker}Delay {delay}s",
            callback_data=f"strategy:delay:{delay}",
        )

    for value in [10, 15, 20, 25]:
        marker = "✅ " if int(cfg["target_pnl_pct"]) == value else ""
        builder.button(
            text=f"{marker}TP {value}%",
            callback_data=f"strategy:tp:{value}",
        )

    for value in [5, 10, 12, 15]:
        marker = "✅ " if int(cfg["target_sl_pct"]) == value else ""
        builder.button(
            text=f"{marker}SL {value}%",
            callback_data=f"strategy:sl:{value}",
        )

    # ── Reversal Risk section ────────────────────────────────
    rev_enabled = cfg.get("reversal_enabled", True)
    rev_label = "🔄 Риск разворота: ВКЛ ✅" if rev_enabled else "🔄 Риск разворота: ВЫКЛ ❌"
    builder.button(text=rev_label, callback_data="strategy:reversal:toggle")

    for thr in [3, 4, 5]:
        marker = "✅ " if cfg.get("reversal_warning_threshold", 4) == thr else ""
        builder.button(
            text=f"{marker}⚠️ Порог {thr}",
            callback_data=f"strategy:reversal:warn:{thr}",
        )

    for thr in [6, 7, 8]:
        marker = "✅ " if cfg.get("reversal_critical_threshold", 7) == thr else ""
        builder.button(
            text=f"{marker}🔴 Порог {thr}",
            callback_data=f"strategy:reversal:crit:{thr}",
        )

    action_labels = {
        "notify_only": "Уведомление",
        "tighten_trailing": "Trailing",
        "auto_close": "Авто-закрытие",
    }
    cur_action = cfg.get("reversal_action", "tighten_trailing")
    for action_key, action_label in action_labels.items():
        marker = "✅ " if cur_action == action_key else ""
        builder.button(
            text=f"{marker}{action_label}",
            callback_data=f"strategy:reversal:action:{action_key}",
        )

    pnl_labels = {"profit_only": "В плюсе", "always": "Всегда"}
    cur_pnl = cfg.get("reversal_pnl_filter", "always")
    for pnl_key, pnl_label in pnl_labels.items():
        marker = "✅ " if cur_pnl == pnl_key else ""
        builder.button(
            text=f"{marker}PnL: {pnl_label}",
            callback_data=f"strategy:reversal:pnl:{pnl_key}",
        )

    builder.button(text="🔄 Сбросить стратегию", callback_data="strategy:reset")

    # Row layout: enabled(1), signals(2,2), scores(2,2), delays(2,2), TP(2,2), SL(2,2),
    # rev_toggle(1), rev_warn(3), rev_crit(3), rev_action(3), rev_pnl(2), reset(1)
    builder.adjust(1, 2, 2, 2, 2, 2, 2, 1, 3, 3, 3, 2, 1)
    return builder.as_markup()


async def _format_strategy_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    allowed = cfg.get("allowed_signal_types", [])
    allowed_str = ", ".join(allowed) if allowed else "ничего"

    # Reversal config
    rev_enabled = "ВКЛ" if cfg.get("reversal_enabled", True) else "ВЫКЛ"
    rev_action_labels = {
        "notify_only": "Уведомление",
        "tighten_trailing": "Подтянуть trailing",
        "auto_close": "Авто-закрытие",
    }
    rev_pnl_labels = {"profit_only": "В плюсе", "always": "Всегда"}
    rev_action = rev_action_labels.get(cfg.get("reversal_action", "tighten_trailing"), "?")
    rev_pnl = rev_pnl_labels.get(cfg.get("reversal_pnl_filter", "always"), "?")

    return (
        f"🎛 <b>Глобальная стратегия авто-шорта</b>\n\n"
        f"🤖 Enabled: <b>{'YES' if cfg['enabled'] else 'NO'}</b>\n"
        f"📡 Signal types: <b>{allowed_str}</b>\n"
        f"📊 Min entry score: <b>{cfg['min_score_to_enter']}</b>\n"
        f"⏱ Entry delay: <b>{cfg['entry_delay_sec']}s</b>\n"
        f"🎯 TP: <b>{cfg['target_pnl_pct']}%</b>\n"
        f"🛑 SL: <b>{cfg['target_sl_pct']}%</b>\n"
        f"⚡ Leverage: <b>{cfg['leverage']}x</b>\n"
        f"📈 Max rise: <b>{cfg['max_rise_pct']}%</b>\n"
        f"📉 Max entry drop: <b>{cfg['max_entry_drop_pct']}%</b>\n"
        f"📌 Stabilization threshold: <b>{cfg['stabilization_threshold_pct']}%</b>\n\n"
        f"🔄 <b>Риск разворота:</b> {rev_enabled}\n"
        f"⚠️ Порог предупреждения: <b>{cfg.get('reversal_warning_threshold', 4)}</b>\n"
        f"🔴 Порог критический: <b>{cfg.get('reversal_critical_threshold', 7)}</b>\n"
        f"⚡ Действие: <b>{rev_action}</b>\n"
        f"💰 PnL фильтр: <b>{rev_pnl}</b>\n\n"
        f"<i>Изменения применяются на лету через Redis</i>"
    )


@router.message(Command("strategy"))
async def cmd_strategy(msg: Message) -> None:
    await msg.answer(
        await _format_strategy_text(),
        reply_markup=await strategy_keyboard(),
    )


@router.callback_query(F.data == "strategy:toggle:enabled")
async def cb_toggle_enabled(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_value = not cfg["enabled"]
        await patch_runtime_strategy_config(redis, {"enabled": new_value})
        logger.info("Strategy enabled toggled", value=new_value, user_id=query.from_user.id if query.from_user else None)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:signal:"))
async def cb_toggle_signal(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    signal_type = query.data.split(":")[-1]
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        allowed = set(cfg.get("allowed_signal_types", []))
        if signal_type in allowed:
            allowed.remove(signal_type)
        else:
            allowed.add(signal_type)

        await patch_runtime_strategy_config(
            redis,
            {"allowed_signal_types": sorted(allowed)},
        )
        logger.info(
            "Strategy signal type toggled",
            signal_type=signal_type,
            enabled=signal_type in allowed,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:min_score:"))
async def cb_min_score(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = int(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"min_score_to_enter": value})
        logger.info("Strategy min_score_to_enter updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:delay:"))
async def cb_delay(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = int(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"entry_delay_sec": value})
        logger.info("Strategy entry_delay_sec updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:tp:"))
async def cb_tp(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"target_pnl_pct": value})
        logger.info("Strategy target_pnl_pct updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:sl:"))
async def cb_sl(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"target_sl_pct": value})
        logger.info("Strategy target_sl_pct updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "strategy:reversal:toggle")
async def cb_reversal_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_value = not cfg.get("reversal_enabled", True)
        await patch_runtime_strategy_config(redis, {"reversal_enabled": new_value})
        logger.info("Reversal enabled toggled", value=new_value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:reversal:warn:"))
async def cb_reversal_warn(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = int(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_warning_threshold": value})
        logger.info("Reversal warning threshold updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:reversal:crit:"))
async def cb_reversal_crit(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = int(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_critical_threshold": value})
        logger.info("Reversal critical threshold updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:reversal:action:"))
async def cb_reversal_action(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = query.data.split(":")[-1]

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_action": value})
        logger.info("Reversal action updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:reversal:pnl:"))
async def cb_reversal_pnl(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = query.data.split(":")[-1]

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"reversal_pnl_filter": value})
        logger.info("Reversal PnL filter updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "strategy:reset")
async def cb_strategy_reset(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Стратегия сброшена")
    except Exception:
        pass

    redis = await _get_redis()
    try:
        await reset_runtime_strategy_config(redis)
        logger.info("Strategy reset", user_id=query.from_user.id if query.from_user else None)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass