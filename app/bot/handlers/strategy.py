"""
/strategy — глобальная runtime-конфигурация стратегии авто-шорта через Telegram.
Только для админов (защищается AccessMiddleware).
"""
from __future__ import annotations

import pickle
from datetime import datetime, timezone
from pathlib import Path

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import text

from app.config import get_settings
from app.db.session import AsyncSessionLocal
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

    shadow_label = (
        "👻 Shadow trades: ВКЛ ✅"
        if cfg.get("shadow_trades_enabled", True)
        else "👻 Shadow trades: ВЫКЛ ❌"
    )
    builder.button(text=shadow_label, callback_data="strategy:toggle:shadow")

    for signal_type in ["early_warning", "overheated", "reversal_risk", "dump_started"]:
        is_enabled = signal_type in cfg.get("allowed_signal_types", [])
        builder.button(
            text=_format_signal_toggle(signal_type, is_enabled),
            callback_data=f"strategy:signal:{signal_type}",
        )

    for score in [45, 50, 55, 60]:
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

    for value in [0.1, 0.2, 0.3, 0.5]:
        marker = "✅ " if abs(cfg.get("adverse_move_threshold_pct", 0.2) - value) < 0.01 else ""
        builder.button(
            text=f"{marker}Adverse {value}%",
            callback_data=f"strategy:adverse:{value}",
        )

    ml_enabled = cfg.get("ml_decision_enabled", True)
    ml_label = "🤖 ML фильтр: ВКЛ ✅" if ml_enabled else "🤖 ML фильтр: ВЫКЛ ❌"
    builder.button(text=ml_label, callback_data="strategy:ml_menu")

    btc24_enabled = cfg.get("btc_24h_filter_enabled", True)
    btc24_label = "📈 BTC 24h фильтр: ВКЛ ✅" if btc24_enabled else "📈 BTC 24h фильтр: ВЫКЛ ❌"
    builder.button(text=btc24_label, callback_data="strategy:btc24_menu")

    builder.button(text="🔄 Сбросить стратегию", callback_data="strategy:reset")

    builder.adjust(1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1)
    return builder.as_markup()


async def _format_strategy_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    allowed = cfg.get("allowed_signal_types", [])
    allowed_str = ", ".join(allowed) if allowed else "ничего"

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
        f"📌 Stabilization threshold: <b>{cfg['stabilization_threshold_pct']}%</b>\n"
        f"🚫 Adverse move threshold: <b>{cfg.get('adverse_move_threshold_pct', 0.2)}%</b>\n"
        f"👻 Shadow trades: <b>{'YES' if cfg.get('shadow_trades_enabled', True) else 'NO'}</b>\n"
        f"🤖 ML фильтр: <b>{'ВКЛ' if cfg.get('ml_decision_enabled', True) else 'ВЫКЛ'}</b> "
        f"(порог {cfg.get('ml_decision_threshold', 0.50):.2f})\n"
        f"📈 BTC 24h фильтр: <b>{'ВКЛ' if cfg.get('btc_24h_filter_enabled', True) else 'ВЫКЛ'}</b>\n\n"
        f"<i>Изменения применяются на лету через Redis</i>"
    )


@router.message(Command("strategy"))
async def cmd_strategy(msg: Message) -> None:
    await msg.answer(
        await _format_strategy_text(),
        reply_markup=await strategy_keyboard(),
    )


@router.callback_query(F.data == "strategy:toggle:shadow")
async def cb_toggle_shadow(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_value = not cfg.get("shadow_trades_enabled", True)
        await patch_runtime_strategy_config(redis, {"shadow_trades_enabled": new_value})
        logger.info(
            "Strategy shadow_trades_enabled toggled",
            value=new_value,
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


@router.callback_query(F.data.startswith("strategy:adverse:"))
async def cb_adverse(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"adverse_move_threshold_pct": value})
        logger.info("Strategy adverse_move_threshold_pct updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_strategy_text(),
            reply_markup=await strategy_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:ml_threshold:"))
async def cb_ml_threshold(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"ml_decision_threshold": value})
        logger.info("Strategy ml_decision_threshold updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_ml_filter_text(),
            reply_markup=await _ml_filter_keyboard(),
        )
    except Exception:
        pass


# ── ML filter submenu ─────────────────────────────────────────────

ML_MODEL_PATH = Path("ml_model/lgbm_scorer.pkl")


async def _ml_filter_keyboard() -> InlineKeyboardMarkup:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    builder = InlineKeyboardBuilder()

    enabled = cfg.get("ml_decision_enabled", True)
    toggle_label = "🤖 ML фильтр: ВКЛ ✅" if enabled else "🤖 ML фильтр: ВЫКЛ ❌"
    builder.button(text=toggle_label, callback_data="strategy:ml_toggle")

    for value in [0.50, 0.55, 0.60, 0.65, 0.70]:
        marker = "✅ " if abs(cfg.get("ml_decision_threshold", 0.50) - value) < 0.01 else ""
        builder.button(
            text=f"{marker}Порог {value:.2f}",
            callback_data=f"strategy:ml_threshold:{value}",
        )

    builder.button(text="← Назад", callback_data="strategy:back")
    builder.adjust(1, 3, 2, 1)
    return builder.as_markup()


def _format_model_info() -> str:
    """Информация о файле ML-модели."""
    if not ML_MODEL_PATH.exists():
        return "⚠️ Модель не найдена (fail-open)"

    try:
        stat = ML_MODEL_PATH.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        age = now - mtime
        hours = int(age.total_seconds() // 3600)
        minutes = int((age.total_seconds() % 3600) // 60)
        if hours > 0:
            age_str = f"{hours}ч назад"
        else:
            age_str = f"{minutes}м назад"

        size_kb = stat.st_size / 1024
        if size_kb >= 1024:
            size_str = f"{size_kb / 1024:.1f} MB"
        else:
            size_str = f"{size_kb:.1f} KB"

        lines = [
            f"  Обучена: {mtime.strftime('%Y-%m-%d %H:%M')} ({age_str})",
            f"  Размер: {size_str}",
        ]

        # Попытка достать метаданные из pkl
        try:
            with open(ML_MODEL_PATH, "rb") as f:
                obj = pickle.load(f)
            if isinstance(obj, dict):
                n_samples = obj.get("n_samples")
                mean_cv_auc = obj.get("mean_cv_auc")
                if n_samples is not None or mean_cv_auc is not None:
                    parts = []
                    if n_samples is not None:
                        parts.append(f"N samples: {n_samples}")
                    if mean_cv_auc is not None:
                        parts.append(f"AUC: {mean_cv_auc:.3f}")
                    lines.append(f"  {', '.join(parts)}")
        except Exception:
            pass

        return "\n".join(lines)
    except Exception:
        return "⚠️ Ошибка чтения модели"


async def _format_ml_stats_24h() -> str:
    """Статистика ML-gate за 24 часа."""
    try:
        async with AsyncSessionLocal() as session:
            # Сколько auto_shorts открыто за 24ч (прошли ML-gate)
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM auto_shorts "
                    "WHERE created_at > NOW() - INTERVAL '24 hours'"
                )
            )
            passed = result.scalar() or 0

            # Сколько отклонено ML
            result = await session.execute(
                text(
                    "SELECT COUNT(*) FROM canceled_signals "
                    "WHERE cancel_reason = 'ml_low_confidence' "
                    "AND signal_ts > NOW() - INTERVAL '24 hours'"
                )
            )
            rejected = result.scalar() or 0

        total = passed + rejected
        if total > 0:
            reject_pct = rejected * 100 // total
            return (
                f"  Прошло ML: {passed}\n"
                f"  Отклонено: {rejected} ({reject_pct}%)"
            )
        return "  Прошло ML: 0\n  Отклонено: 0"
    except Exception:
        return "  статистика недоступна"


async def _format_ml_filter_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    enabled = cfg.get("ml_decision_enabled", True)
    threshold = cfg.get("ml_decision_threshold", 0.50)

    model_info = _format_model_info()
    stats_24h = await _format_ml_stats_24h()

    return (
        f"🤖 <b>ML фильтр</b>\n\n"
        f"Состояние: <b>{'ВКЛ ✅' if enabled else 'ВЫКЛ ❌'}</b>\n"
        f"Порог: <b>{threshold:.2f}</b>\n\n"
        f"📦 <b>Модель:</b>\n{model_info}\n\n"
        f"📊 <b>За 24ч:</b>\n{stats_24h}\n\n"
        f"<i>Если ML выключен — инференс не выполняется, "
        f"сделки проходят без ML-блокировки.</i>"
    )


# ── BTC 24h filter submenu ────────────────────────────────────────


async def _btc24_filter_keyboard() -> InlineKeyboardMarkup:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
    finally:
        await redis.aclose()

    builder = InlineKeyboardBuilder()

    enabled = cfg.get("btc_24h_filter_enabled", True)
    toggle_label = "📈 BTC 24h фильтр: ВКЛ ✅" if enabled else "📈 BTC 24h фильтр: ВЫКЛ ❌"
    builder.button(text=toggle_label, callback_data="strategy:btc24_toggle")

    # Пресеты порога роста
    current_up = cfg.get("btc_24h_filter_threshold_up_pct", 5.0)
    for value in [0, 3, 5, 7, 10]:
        if value == 0:
            label = "Выкл"
            marker = "✅ " if abs(current_up) < 0.01 else ""
        else:
            label = f"{value}%"
            marker = "✅ " if abs(current_up - value) < 0.01 else ""
        builder.button(
            text=f"{marker}⬆ {label}",
            callback_data=f"strategy:btc24_up:{value}",
        )

    # Пресеты порога падения
    current_down = cfg.get("btc_24h_filter_threshold_down_pct", 0.0)
    for value in [0, 3, 5, 7, 10]:
        if value == 0:
            label = "Выкл"
            marker = "✅ " if abs(current_down) < 0.01 else ""
        else:
            label = f"{value}%"
            marker = "✅ " if abs(current_down - value) < 0.01 else ""
        builder.button(
            text=f"{marker}⬇ {label}",
            callback_data=f"strategy:btc24_down:{value}",
        )

    builder.button(text="← Назад", callback_data="strategy:back")
    builder.adjust(1, 5, 5, 1)
    return builder.as_markup()


async def _format_btc24_filter_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        # Попробуем достать текущий btc_change_24h из Redis-кэша MarketContext
        btc_24h_str = "н/д"
        try:
            raw = await redis.get("market_context:btc_change_24h")
            if raw is not None:
                val = float(raw)
                sign = "+" if val >= 0 else ""
                btc_24h_str = f"{sign}{val:.2f}%"
        except Exception:
            pass
    finally:
        await redis.aclose()

    enabled = cfg.get("btc_24h_filter_enabled", True)
    threshold_up = cfg.get("btc_24h_filter_threshold_up_pct", 5.0)
    threshold_down = cfg.get("btc_24h_filter_threshold_down_pct", 0.0)

    up_str = f"{threshold_up}%" if threshold_up > 0 else "выкл"
    down_str = f"{threshold_down}%" if threshold_down > 0 else "выкл"

    return (
        f"📈 <b>BTC 24h фильтр</b>\n\n"
        f"Состояние: <b>{'ВКЛ ✅' if enabled else 'ВЫКЛ ❌'}</b>\n"
        f"BTC 24h: <b>{btc_24h_str}</b>\n\n"
        f"Порог роста: <b>{up_str}</b>\n"
        f"Порог падения: <b>{down_str}</b> (0 = выкл)\n\n"
        f"<i>Блокирует шорты при сильном движении BTC за 24ч.</i>"
    )


@router.callback_query(F.data == "strategy:ml_menu")
async def cb_ml_menu(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.message.edit_text(
            await _format_ml_filter_text(),
            reply_markup=await _ml_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "strategy:ml_toggle")
async def cb_ml_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_value = not cfg.get("ml_decision_enabled", True)
        await patch_runtime_strategy_config(redis, {"ml_decision_enabled": new_value})
        logger.info(
            "Strategy ml_decision_enabled toggled",
            value=new_value,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_ml_filter_text(),
            reply_markup=await _ml_filter_keyboard(),
        )
    except Exception:
        pass


# ── BTC 24h filter handlers ────────────────────────────────────────


@router.callback_query(F.data == "strategy:btc24_menu")
async def cb_btc24_menu(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    try:
        await query.message.edit_text(
            await _format_btc24_filter_text(),
            reply_markup=await _btc24_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "strategy:btc24_toggle")
async def cb_btc24_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    redis = await _get_redis()
    try:
        cfg = await get_runtime_strategy_config(redis)
        new_value = not cfg.get("btc_24h_filter_enabled", True)
        await patch_runtime_strategy_config(redis, {"btc_24h_filter_enabled": new_value})
        logger.info(
            "Strategy btc_24h_filter_enabled toggled",
            value=new_value,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc24_filter_text(),
            reply_markup=await _btc24_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:btc24_up:"))
async def cb_btc24_up(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"btc_24h_filter_threshold_up_pct": value})
        logger.info("Strategy btc_24h_filter_threshold_up_pct updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc24_filter_text(),
            reply_markup=await _btc24_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("strategy:btc24_down:"))
async def cb_btc24_down(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

    value = float(query.data.split(":")[-1])

    redis = await _get_redis()
    try:
        await patch_runtime_strategy_config(redis, {"btc_24h_filter_threshold_down_pct": value})
        logger.info("Strategy btc_24h_filter_threshold_down_pct updated", value=value)
    finally:
        await redis.aclose()

    try:
        await query.message.edit_text(
            await _format_btc24_filter_text(),
            reply_markup=await _btc24_filter_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "strategy:back")
async def cb_back_to_strategy(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass

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