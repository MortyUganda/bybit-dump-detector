"""
/ml_short — TG-меню для ML-short paper-trading сервиса.

Подменю:
- Статус (вкл/выкл, threshold, позиции, сигналы за 24h)
- Статистика (WR, avg PnL, по периодам)
- История (последние 10 закрытых)
- Настройки (все параметры из runtime_config:ml_short)
- Toggle вкл/выкл
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.services.ml_short_config import (
    get_ml_short_config,
    patch_ml_short_config,
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


# ── Клавиатуры ──────────────────────────────────────────────────────

def ml_short_main_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статус", callback_data="ml_short:status")
    builder.button(text="📈 Статистика", callback_data="ml_short:stats:24h")
    builder.button(text="📜 История", callback_data="ml_short:history")
    builder.button(text="⚙️ Настройки", callback_data="ml_short:settings")
    if enabled:
        builder.button(text="⏸ ВЫКЛ", callback_data="ml_short:toggle")
    else:
        builder.button(text="▶️ ВКЛ", callback_data="ml_short:toggle")
    builder.button(text="🔄 Refresh", callback_data="ml_short:refresh")
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def ml_short_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🧠 Threshold proba", callback_data="ml_short:set:threshold")
    builder.button(text="📊 Min score", callback_data="ml_short:set:min_score")
    builder.button(text="🔢 Max concurrent", callback_data="ml_short:set:max_concurrent")
    builder.button(text="⏰ Timeout (h)", callback_data="ml_short:set:timeout_hours")
    builder.button(text="📉 Adverse move %", callback_data="ml_short:set:adverse")
    builder.button(text="⏱ Delay (sec)", callback_data="ml_short:set:delay")
    builder.button(text="❄️ Cooldown вкл/выкл", callback_data="ml_short:toggle_cooldown")
    builder.button(text="🔢 Cooldown losses", callback_data="ml_short:set:cooldown_losses")
    builder.button(text="⏰ Cooldown hours", callback_data="ml_short:set:cooldown_hours")
    builder.button(text="📎 Paper/Real", callback_data="ml_short:paper_info")
    builder.button(text="⬅️ Назад", callback_data="ml_short:back")
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def ml_short_stats_period_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for period, label in [("24h", "24ч"), ("7d", "7д"), ("all", "Все")]:
        marker = "✅ " if current == period else ""
        builder.button(text=f"{marker}{label}", callback_data=f"ml_short:stats:{period}")
    builder.button(text="🤖 Активные", callback_data="ml_short:active")
    builder.button(text="⬅️ Назад", callback_data="ml_short:back")
    builder.adjust(3, 1, 1)
    return builder.as_markup()


def ml_short_active_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="ml_short:active")
    builder.button(text="📈 Статистика", callback_data="ml_short:stats:24h")
    builder.button(text="⬅️ Назад", callback_data="ml_short:back")
    builder.adjust(2, 1)
    return builder.as_markup()


def ml_short_threshold_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for val in [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
        builder.button(text=f"{val:.2f}", callback_data=f"ml_short:val:threshold:{val}")
    builder.button(text="⬅️ Назад", callback_data="ml_short:settings")
    builder.adjust(4, 3, 1)
    return builder.as_markup()


def ml_short_numeric_keyboard(param: str, values: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for val in values:
        builder.button(text=str(val), callback_data=f"ml_short:val:{param}:{val}")
    builder.button(text="⬅️ Назад", callback_data="ml_short:settings")
    per_row = min(4, len(values))
    rows = [per_row] * (len(values) // per_row)
    remainder = len(values) % per_row
    if remainder:
        rows.append(remainder)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


# ── Получение данных ─────────────────────────────────────────────────

async def _get_status_text() -> str:
    """Текстовый блок статуса ML-short."""
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()

    enabled_str = "✅ ВКЛ" if cfg["enabled"] else "❌ ВЫКЛ"

    # Статистика из БД
    open_count = 0
    signals_24h = {"total": 0, "opened": 0, "blocked_low_proba": 0, "blocked_other": 0, "disabled": 0, "no_model": 0, "inference_error": 0}
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            # Открытые позиции
            r = await session.execute(
                text("SELECT COUNT(*) FROM ml_short_positions WHERE status = 'open'")
            )
            open_count = r.scalar_one()

            # Сигналы за 24h
            ts_24h = datetime.now(timezone.utc) - timedelta(hours=24)
            r = await session.execute(
                text("""
                    SELECT ml_decision, COUNT(*) FROM ml_short_signals
                    WHERE signal_ts > :ts
                    GROUP BY ml_decision
                """),
                {"ts": ts_24h},
            )
            for row in r.fetchall():
                decision, cnt = row
                signals_24h["total"] += cnt
                if decision in signals_24h:
                    signals_24h[decision] = cnt
    except Exception as exc:
        logger.warning("ML-short статус: ошибка БД", error=str(exc))

    return (
        f"🤖 <b>ML-Short Paper Trading</b>\n\n"
        f"⚡ Статус: {enabled_str}\n"
        f"🧠 Threshold: <b>{cfg['proba_threshold']:.2f}</b>\n"
        f"📌 Открытых позиций: <b>{open_count}</b>\n"
        f"📎 Режим: Paper (Real disabled)\n\n"
        f"<b>Сигналы за 24h:</b>\n"
        f"  📊 Всего: {signals_24h['total']}\n"
        f"  ✅ Opened: {signals_24h['opened']}\n"
        f"  🔻 Low proba: {signals_24h['blocked_low_proba']}\n"
        f"  🚫 Blocked: {signals_24h['blocked_other']}\n"
        f"  ⏸ Disabled: {signals_24h['disabled']}\n"
        f"  🤷 No model: {signals_24h['no_model']}\n"
        f"  ⚠️ Inference error: {signals_24h['inference_error']}\n\n"
        f"<i>Настройки: min_score={cfg['min_score_to_enter']}, "
        f"max_concurrent={cfg['max_concurrent_positions']}, "
        f"delay={cfg['delay_seconds']}s</i>"
    )


async def _get_stats_text(period: str) -> str:
    """Статистика в стиле авто-шортов: total/open/closed + WR + PnL + best/worst + разбивка.

    Период фильтрует только закрытые позиции (exit_ts), open-счетчик всегда live.
    """
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        if period == "24h":
            ts_filter = datetime.now(timezone.utc) - timedelta(hours=24)
            period_label = "24 часа"
        elif period == "7d":
            ts_filter = datetime.now(timezone.utc) - timedelta(days=7)
            period_label = "7 дней"
        else:
            ts_filter = datetime(2020, 1, 1, tzinfo=timezone.utc)
            period_label = "всё время"

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE status = 'open') as open_cnt,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts) as closed_cnt,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND pnl_pct > 0) as wins,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND pnl_pct <= 0) as losses,
                        AVG(pnl_pct) FILTER (WHERE status = 'closed' AND exit_ts > :ts) as avg_pnl,
                        MAX(pnl_pct) FILTER (WHERE status = 'closed' AND exit_ts > :ts) as best_pnl,
                        MIN(pnl_pct) FILTER (WHERE status = 'closed' AND exit_ts > :ts) as worst_pnl,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'tp') as tp_count,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'sl') as sl_count,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'timeout') as timeout_count,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'manual') as manual_count
                    FROM ml_short_positions
                """),
                {"ts": ts_filter},
            )
            row = r.fetchone()

        (
            open_cnt, closed_cnt, wins, losses, avg_pnl, best_pnl, worst_pnl,
            tp_count, sl_count, timeout_count, manual_count,
        ) = row
        total = (open_cnt or 0) + (closed_cnt or 0)

        wr = (wins / closed_cnt * 100) if closed_cnt else 0.0
        wr_emoji = "🟢" if wr >= 60 else ("🟡" if wr >= 45 else "🔴")
        avg_val = float(avg_pnl) if avg_pnl is not None else 0.0
        avg_emoji = "🟢" if avg_val > 0 else ("🔴" if avg_val < 0 else "⚪")
        best_str = f"{float(best_pnl):+.2f}%" if best_pnl is not None else "—"
        worst_str = f"{float(worst_pnl):+.2f}%" if worst_pnl is not None else "—"

        return (
            f"📈 <b>Статистика ML-Short</b> ({period_label})\n\n"
            f"📈 Всего сделок: <b>{total}</b>\n"
            f"🟡 Открытых: <b>{open_cnt or 0}</b>\n"
            f"✅ Закрытых: <b>{closed_cnt or 0}</b>\n\n"
            f"{wr_emoji} Win rate: <b>{wr:.1f}%</b> ({wins or 0}W / {losses or 0}L)\n"
            f"{avg_emoji} Средний P&L: <b>{avg_val:+.2f}%</b>\n\n"
            f"🏆 Лучшая: <b>{best_str}</b>\n"
            f"💀 Худшая: <b>{worst_str}</b>\n\n"
            f"<b>По типу закрытия:</b>\n"
            f"  🎯 TP: {tp_count or 0}\n"
            f"  🛑 SL: {sl_count or 0}\n"
            f"  ⏰ Timeout: {timeout_count or 0}\n"
            f"  ✋ Вручную: {manual_count or 0}"
        )

    except Exception as exc:
        logger.error("ML-short статистика: ошибка", error=str(exc))
        return "❌ Ошибка загрузки статистики."



TG_MAX_MESSAGE_CHARS = 3800


async def _get_active_text() -> str:
    """Список активных (open) ML-short paper-позиций — логика как у авто-шортов."""
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal
        from app.bot.handlers.auto_shorts import _get_current_price

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT id, symbol, entry_price, entry_ts, ml_proba, score,
                           tp_pct, sl_pct, timeout_hours
                    FROM ml_short_positions
                    WHERE status = 'open'
                    ORDER BY id DESC
                """)
            )
            rows = r.fetchall()

        if not rows:
            return (
                "🤖 <b>ML-Short активные</b>\n\n"
                "<i>Нет открытых позиций.</i>"
            )

        header = f"🤖 <b>ML-Short активные</b> ({len(rows)})"
        lines: list[str] = [header]
        now = datetime.now(timezone.utc)

        for row in rows:
            pos_id, symbol, entry_price, entry_ts, ml_proba, score, tp_pct, sl_pct, timeout_h = row
            entry_price_f = float(entry_price)
            tp_price = entry_price_f * (1 - float(tp_pct) / 100.0)
            sl_price = entry_price_f * (1 + float(sl_pct) / 100.0)
            elapsed_min = int((now - entry_ts).total_seconds() / 60) if entry_ts else 0
            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

            current_price = await _get_current_price(symbol)
            if current_price is not None:
                # ML-short paper: leverage = 1
                pnl_now = ((entry_price_f - float(current_price)) / entry_price_f) * 100.0
                pnl_em = "🟢" if pnl_now > 0 else "🔴" if pnl_now < 0 else "⚪"
                price_line = f"   💹 Сейчас: <b>${float(current_price):.6g}</b>\n"
                pnl_line = f"   {pnl_em} PnL now: <b>{pnl_now:+.2f}%</b>\n"
            else:
                price_line = "   💹 Сейчас: <b>н/д</b>\n"
                pnl_line = "   ⚪ PnL now: <b>н/д</b>\n"

            proba_str = f"{float(ml_proba) * 100:.1f}%" if ml_proba is not None else "—"
            score_str = f"{float(score):.0f}" if score is not None else "—"

            lines.append(
                f"📌 #{pos_id} <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"   💰 Вход: <b>${entry_price_f:.6g}</b> | ⏱ {elapsed_min}м\n"
                f"{price_line}"
                f"{pnl_line}"
                f"   🎯 TP: ${tp_price:.6g} | 🛑 SL: ${sl_price:.6g}\n"
                f"   🧠 Proba: {proba_str} | 📊 Score: {score_str} | ⏰ {float(timeout_h):.0f}ч"
            )

        # Одним сообщением (обычно позиций мало, max_concurrent=5).
        # Если вдруг перевалили лимит — просто обрежем.
        out = "\n\n".join(lines)
        if len(out) > TG_MAX_MESSAGE_CHARS:
            out = out[: TG_MAX_MESSAGE_CHARS - 50] + "\n\n<i>… обрезано</i>"
        return out

    except Exception as exc:
        logger.error("ML-short активные: ошибка", error=str(exc))
        return "❌ Ошибка загрузки активных позиций."


async def _get_history_text() -> str:
    """Последние 10 закрытых позиций."""
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT id, symbol, ml_proba, entry_price, exit_price,
                           pnl_pct, close_reason, exit_ts
                    FROM ml_short_positions
                    WHERE status = 'closed'
                    ORDER BY exit_ts DESC
                    LIMIT 10
                """)
            )
            rows = r.fetchall()

        if not rows:
            return (
                "📜 <b>История ML-Short</b>\n\n"
                "<i>Закрытых позиций пока нет.</i>"
            )

        lines = ["📜 <b>История ML-Short</b> (последние 10)\n"]
        for row in rows:
            pos_id, symbol, proba, entry, exit_p, pnl, reason, exit_ts = row
            pnl_val = float(pnl or 0)
            pnl_emoji = "🟢" if pnl_val > 0 else "🔴" if pnl_val < 0 else "⚪"
            reason_labels = {"tp": "🎯", "sl": "🛑", "timeout": "⏰", "manual": "✋"}
            reason_icon = reason_labels.get(reason, "❓")
            proba_str = f"{float(proba):.0%}" if proba else "?"

            lines.append(
                f"#{pos_id} <b>{symbol}</b> "
                f"proba={proba_str} "
                f"${float(entry):.4g}→${float(exit_p):.4g} "
                f"{pnl_emoji}<b>{pnl_val:+.1f}%</b> {reason_icon}"
            )

        return "\n".join(lines)

    except Exception as exc:
        logger.error("ML-short история: ошибка", error=str(exc))
        return "❌ Ошибка загрузки истории."


async def _get_settings_text() -> str:
    """Текст текущих настроек."""
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()

    cooldown_str = "✅" if cfg["cooldown_enabled"] else "❌"

    return (
        f"⚙️ <b>Настройки ML-Short</b>\n\n"
        f"🧠 Threshold proba: <b>{cfg['proba_threshold']:.2f}</b>\n"
        f"📊 Min score: <b>{cfg['min_score_to_enter']}</b>\n"
        f"🔢 Max concurrent: <b>{cfg['max_concurrent_positions']}</b>\n"
        f"⏰ Timeout: <b>{cfg['position_timeout_hours']}h</b>\n"
        f"📉 Adverse move: <b>{cfg['adverse_move_threshold_pct']}%</b>\n"
        f"⏱ Delay: <b>{cfg['delay_seconds']}s</b>\n"
        f"❄️ Cooldown: {cooldown_str}\n"
        f"🔢 Cooldown losses: <b>{cfg['cooldown_loss_count']}</b>\n"
        f"⏰ Cooldown hours: <b>{cfg['cooldown_hours']}</b>\n"
        f"📎 Paper/Real: <b>Paper (Real disabled)</b>\n\n"
        f"<i>Нажмите кнопку для изменения</i>"
    )


# ── Команды и обработчики ────────────────────────────────────────────

@router.message(Command("ml_short"))
async def cmd_ml_short(msg: Message) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()
    text = await _get_status_text()
    await msg.answer(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))


@router.message(F.text == "🤖 ML-shorts")
async def ml_short_from_reply_keyboard(msg: Message) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()
    text = await _get_status_text()
    await msg.answer(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))


# ── Callback: статус ──────────────────────────────────────────────────

@router.callback_query(F.data == "ml_short:status")
async def cb_ml_short_status(query: CallbackQuery) -> None:
    try:
        await query.answer("📊 Статус обновлён")
    except Exception:
        pass
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()
    # Добавляем таймстамп, чтобы edit_text не падал на "message is not modified"
    status_text = await _get_status_text()
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    text = f"{status_text}\n\n<i>🔄 {now_str}</i>"
    try:
        await query.message.edit_text(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))
    except Exception:
        pass


# ── Callback: refresh ─────────────────────────────────────────────────

@router.callback_query(F.data == "ml_short:refresh")
async def cb_ml_short_refresh(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю...")
    except Exception:
        pass
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()
    text = await _get_status_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))
    except Exception:
        pass


# ── Callback: toggle enabled ─────────────────────────────────────────

@router.callback_query(F.data == "ml_short:toggle")
async def cb_ml_short_toggle(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
        new_value = not cfg["enabled"]
        await patch_ml_short_config(redis, {"enabled": new_value})
        logger.info("ML-short enabled toggle", value=new_value,
                    user_id=query.from_user.id if query.from_user else None)
        cfg["enabled"] = new_value
    finally:
        await redis.aclose()
    text = await _get_status_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))
    except Exception:
        pass


# ── Callback: back (к главному меню ml_short) ─────────────────────────

@router.callback_query(F.data == "ml_short:back")
async def cb_ml_short_back(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
    finally:
        await redis.aclose()
    text = await _get_status_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_main_keyboard(cfg["enabled"]))
    except Exception:
        pass


# ── Callback: статистика ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("ml_short:stats:"))
async def cb_ml_short_stats(query: CallbackQuery) -> None:
    try:
        await query.answer("📈 Загружаю статистику...")
    except Exception:
        pass
    period = query.data.split(":")[-1]
    if period not in ("24h", "7d", "all"):
        period = "24h"
    text = await _get_stats_text(period)
    try:
        await query.message.edit_text(text, reply_markup=ml_short_stats_period_keyboard(period))
    except Exception:
        pass


# ── Callback: активные ML-short позиции ────────────────────────

@router.callback_query(F.data == "ml_short:active")
async def cb_ml_short_active(query: CallbackQuery) -> None:
    try:
        await query.answer("🤖 Загружаю активные...")
    except Exception:
        pass
    body = await _get_active_text()
    # Таймстамп — чтобы edit_text не падал на "message is not modified"
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    text = f"{body}\n\n<i>🔄 {now_str}</i>"
    try:
        await query.message.edit_text(text, reply_markup=ml_short_active_keyboard())
    except Exception:
        pass


# ── Callback: история ─────────────────────────────────────────────────

@router.callback_query(F.data == "ml_short:history")
async def cb_ml_short_history(query: CallbackQuery) -> None:
    try:
        await query.answer("📜 Загружаю историю...")
    except Exception:
        pass
    text = await _get_history_text()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="ml_short:back")
    try:
        await query.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        pass


# ── Callback: настройки (подменю) ────────────────────────────────────

@router.callback_query(F.data == "ml_short:settings")
async def cb_ml_short_settings(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    text = await _get_settings_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_settings_keyboard())
    except Exception:
        pass


# ── Callback: threshold picker ────────────────────────────────────────

@router.callback_query(F.data == "ml_short:set:threshold")
async def cb_set_threshold(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "🧠 <b>Выберите threshold proba</b>\n\n"
            "Минимальная вероятность ML-модели для открытия позиции.\n"
            "Sweet spot: 0.60 (WR 60.2% по decision_v2).",
            reply_markup=ml_short_threshold_keyboard(),
        )
    except Exception:
        pass


# ── Callback: numeric settings pickers ────────────────────────────────

@router.callback_query(F.data == "ml_short:set:min_score")
async def cb_set_min_score(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "📊 <b>Min signal score</b>\n\nМинимальный score сигнала для рассмотрения.",
            reply_markup=ml_short_numeric_keyboard("min_score", [30, 35, 40, 45, 50, 55, 60]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:max_concurrent")
async def cb_set_max_concurrent(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "🔢 <b>Max concurrent positions</b>\n\nМаксимальное количество одновременно открытых позиций.",
            reply_markup=ml_short_numeric_keyboard("max_concurrent", [1, 2, 3, 5, 7, 10, 15, 20]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:timeout_hours")
async def cb_set_timeout(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "⏰ <b>Position timeout (hours)</b>\n\nЧерез сколько часов позиция закроется автоматически.",
            reply_markup=ml_short_numeric_keyboard("timeout_hours", [4, 8, 12, 24, 36, 48, 72]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:adverse")
async def cb_set_adverse(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "📉 <b>Adverse move %</b>\n\nЕсли за время delay цена выросла больше этого % — не открывать.",
            reply_markup=ml_short_numeric_keyboard("adverse", [0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:delay")
async def cb_set_delay(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "⏱ <b>Delay (seconds)</b>\n\nЗадержка перед открытием позиции после сигнала.",
            reply_markup=ml_short_numeric_keyboard("delay", [0, 10, 15, 20, 30, 45, 60, 90, 120]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:cooldown_losses")
async def cb_set_cooldown_losses(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "🔢 <b>Cooldown loss count</b>\n\nСколько убытков подряд по символу до cooldown.",
            reply_markup=ml_short_numeric_keyboard("cooldown_losses", [1, 2, 3, 4, 5, 7, 10]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "ml_short:set:cooldown_hours")
async def cb_set_cooldown_hours(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    try:
        await query.message.edit_text(
            "⏰ <b>Cooldown hours</b>\n\nДлительность cooldown в часах.",
            reply_markup=ml_short_numeric_keyboard("cooldown_hours", [1, 4, 8, 12, 24, 36, 48, 72]),
        )
    except Exception:
        pass


# ── Callback: toggle cooldown ─────────────────────────────────────────

@router.callback_query(F.data == "ml_short:toggle_cooldown")
async def cb_toggle_cooldown(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    redis = await _get_redis()
    try:
        cfg = await get_ml_short_config(redis)
        new_value = not cfg["cooldown_enabled"]
        await patch_ml_short_config(redis, {"cooldown_enabled": new_value})
    finally:
        await redis.aclose()
    text = await _get_settings_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_settings_keyboard())
    except Exception:
        pass


# ── Callback: paper/real info ─────────────────────────────────────────

@router.callback_query(F.data == "ml_short:paper_info")
async def cb_paper_info(query: CallbackQuery) -> None:
    try:
        await query.answer(
            "📎 Сейчас только Paper-режим. Real-trading будет добавлен позже.",
            show_alert=True,
        )
    except Exception:
        pass


# ── Callback: сохранение значений ────────────────────────────────────

PARAM_MAP = {
    "threshold": ("proba_threshold", float),
    "min_score": ("min_score_to_enter", int),
    "max_concurrent": ("max_concurrent_positions", int),
    "timeout_hours": ("position_timeout_hours", int),
    "adverse": ("adverse_move_threshold_pct", float),
    "delay": ("delay_seconds", int),
    "cooldown_losses": ("cooldown_loss_count", int),
    "cooldown_hours": ("cooldown_hours", int),
}


@router.callback_query(F.data.startswith("ml_short:val:"))
async def cb_set_value(query: CallbackQuery) -> None:
    try:
        await query.answer("✅ Сохранено")
    except Exception:
        pass

    parts = query.data.split(":")
    # ml_short:val:param:value
    if len(parts) < 4:
        return
    param = parts[2]
    raw_value = parts[3]

    mapping = PARAM_MAP.get(param)
    if not mapping:
        return

    config_key, type_fn = mapping
    try:
        value = type_fn(raw_value)
    except (ValueError, TypeError):
        return

    redis = await _get_redis()
    try:
        await patch_ml_short_config(redis, {config_key: value})
        logger.info(
            "ML-short настройка изменена",
            param=config_key,
            value=value,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()

    text = await _get_settings_text()
    try:
        await query.message.edit_text(text, reply_markup=ml_short_settings_keyboard())
    except Exception:
        pass
