"""
💵 Real-shorts — TG-меню для реальной торговли Bybit (зеркало ml_short).

Real-shorts ИСПОЛНЯЕТ решения ml_short реальными ордерами. Здесь только
управление исполнением: вкл/выкл, режим testnet/mainnet, сайзинг, риск-лимиты,
просмотр позиций и РУЧНОЕ закрытие.

⚠️ РЕАЛЬНЫЕ ДЕНЬГИ: по умолчанию real_enabled=False и testnet=True.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import get_settings
from app.services.real_short_config import (
    get_real_short_config,
    patch_real_short_config,
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

def real_short_main_keyboard(enabled: bool, testnet: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статус", callback_data="real_short:status")
    builder.button(text="📈 Статистика", callback_data="real_short:stats:24h")
    builder.button(text="🤖 Активные", callback_data="real_short:active")
    builder.button(text="📜 История", callback_data="real_short:history")
    builder.button(text="⚙️ Настройки", callback_data="real_short:settings")
    if enabled:
        builder.button(text="⏸ ВЫКЛ real", callback_data="real_short:toggle")
    else:
        builder.button(text="▶️ ВКЛ real", callback_data="real_short:toggle")
    net_label = "🧪 → MAINNET" if testnet else "🔴 → TESTNET"
    builder.button(text=net_label, callback_data="real_short:toggle_net")
    builder.button(text="🔄 Refresh", callback_data="real_short:refresh")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def real_short_settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Маржа USDT", callback_data="real_short:set:margin")
    builder.button(text="⚖️ Плечо", callback_data="real_short:set:leverage")
    builder.button(text="🎯 TP %", callback_data="real_short:set:tp")
    builder.button(text="🛑 SL %", callback_data="real_short:set:sl")
    builder.button(text="🔢 Max позиций", callback_data="real_short:set:max_open")
    builder.button(text="📉 Дневной стоп USDT", callback_data="real_short:set:daily_loss")
    builder.button(text="❄️ Cooldown сек", callback_data="real_short:set:cooldown")
    builder.button(text="⬅️ Назад", callback_data="real_short:back")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def real_short_numeric_keyboard(param: str, values: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for val in values:
        builder.button(text=str(val), callback_data=f"real_short:val:{param}:{val}")
    builder.button(text="⬅️ Назад", callback_data="real_short:settings")
    per_row = min(4, len(values))
    rows = [per_row] * (len(values) // per_row)
    remainder = len(values) % per_row
    if remainder:
        rows.append(remainder)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


# ── Тексты ───────────────────────────────────────────────────────────

async def _get_status_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
    finally:
        await redis.aclose()

    enabled_str = "✅ ВКЛ" if cfg["real_enabled"] else "❌ ВЫКЛ"
    net_str = "🧪 TESTNET" if cfg["real_testnet"] else "🔴 MAINNET"

    open_count = 0
    pnl_today = 0.0
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("SELECT COUNT(*) FROM real_short_positions WHERE status IN ('open','opening')")
            )
            open_count = r.scalar_one()
            r = await session.execute(
                text("""
                    SELECT COALESCE(SUM(pnl_usdt), 0) FROM real_short_positions
                    WHERE status = 'closed' AND exit_ts >= :start
                """),
                {"start": start},
            )
            pnl_today = float(r.scalar_one() or 0.0)
    except Exception as exc:
        logger.warning("Real-short статус: ошибка БД", error=str(exc))

    pnl_em = "🟢" if pnl_today > 0 else "🔴" if pnl_today < 0 else "⚪"

    warn = "" if cfg["real_testnet"] else "\n⚠️ <b>БОЕВОЙ РЕЖИМ — реальные деньги!</b>\n"

    return (
        f"💵 <b>Real-Short (реальная торговля)</b>\n\n"
        f"⚡ Статус: {enabled_str}\n"
        f"🌐 Сеть: <b>{net_str}</b>{warn}\n"
        f"📌 Открытых позиций: <b>{open_count}</b>\n"
        f"{pnl_em} PnL сегодня: <b>{pnl_today:+.4f} USDT</b>\n\n"
        f"<i>Маржа={cfg['real_margin_usdt']}$ × {cfg['real_leverage']}x, "
        f"TP={cfg['real_tp_pct']}% / SL={cfg['real_sl_pct']}%</i>\n"
        f"<i>Лимиты: max_open={cfg['real_max_open_positions'] or '∞'}, "
        f"daily_stop=-{cfg['real_max_daily_loss_usdt']}$</i>"
    )


async def _get_settings_text() -> str:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
    finally:
        await redis.aclose()

    return (
        f"⚙️ <b>Настройки Real-Short</b>\n\n"
        f"💰 Маржа: <b>{cfg['real_margin_usdt']} USDT</b>\n"
        f"⚖️ Плечо: <b>{cfg['real_leverage']}x</b>\n"
        f"🎯 TP: <b>{cfg['real_tp_pct']}%</b> (движение цены, P&L = ×плечо)\n"
        f"🛑 SL: <b>{cfg['real_sl_pct']}%</b>\n"
        f"🔢 Max позиций: <b>{cfg['real_max_open_positions'] or '∞'}</b>\n"
        f"📉 Дневной стоп: <b>-{cfg['real_max_daily_loss_usdt']} USDT</b>\n"
        f"❄️ Cooldown: <b>{cfg['real_cooldown_sec']}s</b>\n\n"
        f"<i>Нажмите кнопку для изменения</i>"
    )


async def _get_active_text() -> str:
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT id, symbol, entry_price, qty, leverage, entry_ts,
                           tp_price, sl_price, testnet, status
                    FROM real_short_positions
                    WHERE status IN ('open','opening')
                    ORDER BY id DESC
                """)
            )
            rows = r.fetchall()

        if not rows:
            return "💵 <b>Real-Short активные</b>\n\n<i>Нет открытых позиций.</i>"

        now = datetime.now(timezone.utc)
        lines = [f"💵 <b>Real-Short активные</b> ({len(rows)})"]
        for row in rows:
            pos_id, symbol, entry, qty, lev, entry_ts, tp, sl, testnet, status = row
            net = "🧪" if testnet else "🔴"
            elapsed_min = int((now - entry_ts).total_seconds() / 60) if entry_ts else 0
            tp_str = f"${float(tp):.6g}" if tp is not None else "—"
            sl_str = f"${float(sl):.6g}" if sl is not None else "—"
            lines.append(
                f"{net} #{pos_id} <b>{symbol}</b> ({status})\n"
                f"   💰 Вход: <b>${float(entry):.6g}</b> | 📦 {float(qty):g} | ⚖️ {float(lev):g}x | ⏱ {elapsed_min}м\n"
                f"   🎯 TP: {tp_str} | 🛑 SL: {sl_str}\n"
                f"   ✋ Закрыть: /rsclose_{pos_id}"
            )
        return "\n\n".join(lines)
    except Exception as exc:
        logger.error("Real-short активные: ошибка", error=str(exc))
        return "❌ Ошибка загрузки активных позиций."


async def _get_history_text() -> str:
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT id, symbol, entry_price, exit_price, pnl_pct, pnl_usdt,
                           close_reason, testnet
                    FROM real_short_positions
                    WHERE status = 'closed'
                    ORDER BY exit_ts DESC
                    LIMIT 10
                """)
            )
            rows = r.fetchall()

        if not rows:
            return "📜 <b>История Real-Short</b>\n\n<i>Закрытых позиций пока нет.</i>"

        lines = ["📜 <b>История Real-Short</b> (последние 10)\n"]
        labels = {"tp": "🎯", "sl": "🛑", "timeout": "⏰", "manual": "✋"}
        for row in rows:
            pos_id, symbol, entry, exit_p, pnl_pct, pnl_usdt, reason, testnet = row
            net = "🧪" if testnet else "🔴"
            pnl_v = float(pnl_usdt or 0)
            em = "🟢" if pnl_v > 0 else "🔴" if pnl_v < 0 else "⚪"
            icon = labels.get(reason, "❓")
            exit_str = f"${float(exit_p):.4g}" if exit_p is not None else "—"
            pct_str = f"{float(pnl_pct):+.1f}%" if pnl_pct is not None else "—"
            lines.append(
                f"{net} #{pos_id} <b>{symbol}</b> "
                f"${float(entry):.4g}→{exit_str} "
                f"{em}<b>{pnl_v:+.3f}$</b> ({pct_str}) {icon}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("Real-short история: ошибка", error=str(exc))
        return "❌ Ошибка загрузки истории."


# ── Точки входа ───────────────────────────────────────────────────────

@router.message(Command("real_short"))
async def cmd_real_short(msg: Message) -> None:
    await _send_main(msg)


@router.message(F.text == "💵 Real-shorts")
async def real_short_from_reply_keyboard(msg: Message) -> None:
    await _send_main(msg)


async def _send_main(msg: Message) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
    finally:
        await redis.aclose()
    text = await _get_status_text()
    await msg.answer(
        text,
        reply_markup=real_short_main_keyboard(cfg["real_enabled"], cfg["real_testnet"]),
    )


# ── Навигация / refresh / статус ──────────────────────────────────────

async def _edit_main(query: CallbackQuery) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
    finally:
        await redis.aclose()
    status = await _get_status_text()
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    text = f"{status}\n\n<i>🔄 {now_str}</i>"
    try:
        await query.message.edit_text(
            text,
            reply_markup=real_short_main_keyboard(cfg["real_enabled"], cfg["real_testnet"]),
        )
    except Exception:
        pass


@router.callback_query(F.data == "real_short:status")
async def cb_status(query: CallbackQuery) -> None:
    try:
        await query.answer("📊 Статус обновлён")
    except Exception:
        pass
    await _edit_main(query)


@router.callback_query(F.data == "real_short:refresh")
async def cb_refresh(query: CallbackQuery) -> None:
    try:
        await query.answer("🔄 Обновляю...")
    except Exception:
        pass
    await _edit_main(query)


@router.callback_query(F.data == "real_short:back")
async def cb_back(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    await _edit_main(query)


# ── Toggle real_enabled ───────────────────────────────────────────────

@router.callback_query(F.data == "real_short:toggle")
async def cb_toggle(query: CallbackQuery) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
        new_value = not cfg["real_enabled"]
        await patch_real_short_config(redis, {"real_enabled": new_value})
        logger.info(
            "Real-short enabled toggle", value=new_value,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()
    try:
        if new_value:
            await query.answer("▶️ Real-trading ВКЛЮЧЕНА", show_alert=True)
        else:
            await query.answer("⏸ Real-trading выключена")
    except Exception:
        pass
    await _edit_main(query)


# ── Toggle testnet/mainnet ────────────────────────────────────────────

@router.callback_query(F.data == "real_short:toggle_net")
async def cb_toggle_net(query: CallbackQuery) -> None:
    redis = await _get_redis()
    try:
        cfg = await get_real_short_config(redis)
        new_testnet = not cfg["real_testnet"]
        await patch_real_short_config(redis, {"real_testnet": new_testnet})
        logger.info(
            "Real-short testnet toggle", testnet=new_testnet,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()
    try:
        if new_testnet:
            await query.answer("🧪 Переключено на TESTNET")
        else:
            await query.answer("🔴 ВНИМАНИЕ: переключено на MAINNET (реальные деньги!)", show_alert=True)
    except Exception:
        pass
    await _edit_main(query)


# ── Статистика ────────────────────────────────────────────────────────

async def _get_stats_text(period: str) -> str:
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        if period == "24h":
            ts_filter = datetime.now(timezone.utc) - timedelta(hours=24)
            label = "24 часа"
        elif period == "7d":
            ts_filter = datetime.now(timezone.utc) - timedelta(days=7)
            label = "7 дней"
        else:
            ts_filter = datetime(2020, 1, 1, tzinfo=timezone.utc)
            label = "всё время"

        async with AsyncSessionLocal() as session:
            r = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE status IN ('open','opening')) AS open_cnt,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts) AS closed_cnt,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND pnl_usdt > 0) AS wins,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND pnl_usdt <= 0) AS losses,
                        COALESCE(SUM(pnl_usdt) FILTER (WHERE status = 'closed' AND exit_ts > :ts), 0) AS total_pnl,
                        AVG(pnl_pct) FILTER (WHERE status = 'closed' AND exit_ts > :ts) AS avg_pnl,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'tp') AS tp_count,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'sl') AS sl_count,
                        COUNT(*) FILTER (WHERE status = 'closed' AND exit_ts > :ts AND close_reason = 'manual') AS manual_count
                    FROM real_short_positions
                """),
                {"ts": ts_filter},
            )
            row = r.fetchone()

        open_cnt, closed_cnt, wins, losses, total_pnl, avg_pnl, tp_count, sl_count, manual_count = row
        wr = (wins / closed_cnt * 100) if closed_cnt else 0.0
        wr_em = "🟢" if wr >= 60 else ("🟡" if wr >= 45 else "🔴")
        total_v = float(total_pnl or 0)
        total_em = "🟢" if total_v > 0 else "🔴" if total_v < 0 else "⚪"
        avg_v = float(avg_pnl) if avg_pnl is not None else 0.0

        return (
            f"📈 <b>Статистика Real-Short</b> ({label})\n\n"
            f"🟡 Открытых: <b>{open_cnt or 0}</b>\n"
            f"✅ Закрытых: <b>{closed_cnt or 0}</b>\n\n"
            f"{wr_em} Win rate: <b>{wr:.1f}%</b> ({wins or 0}W / {losses or 0}L)\n"
            f"{total_em} Итого PnL: <b>{total_v:+.4f} USDT</b>\n"
            f"📊 Средний P&L: <b>{avg_v:+.2f}%</b>\n\n"
            f"<b>По типу закрытия:</b>\n"
            f"  🎯 TP: {tp_count or 0}\n"
            f"  🛑 SL: {sl_count or 0}\n"
            f"  ✋ Вручную: {manual_count or 0}"
        )
    except Exception as exc:
        logger.error("Real-short статистика: ошибка", error=str(exc))
        return "❌ Ошибка загрузки статистики."


def _stats_keyboard(current: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for period, lbl in [("24h", "24ч"), ("7d", "7д"), ("all", "Все")]:
        marker = "✅ " if current == period else ""
        builder.button(text=f"{marker}{lbl}", callback_data=f"real_short:stats:{period}")
    builder.button(text="⬅️ Назад", callback_data="real_short:back")
    builder.adjust(3, 1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("real_short:stats:"))
async def cb_stats(query: CallbackQuery) -> None:
    try:
        await query.answer("📈 Загружаю...")
    except Exception:
        pass
    period = query.data.split(":")[-1]
    if period not in ("24h", "7d", "all"):
        period = "24h"
    text = await _get_stats_text(period)
    try:
        await query.message.edit_text(text, reply_markup=_stats_keyboard(period))
    except Exception:
        pass


@router.callback_query(F.data == "real_short:active")
async def cb_active(query: CallbackQuery) -> None:
    try:
        await query.answer("🤖 Загружаю активные...")
    except Exception:
        pass
    body = await _get_active_text()
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    text = f"{body}\n\n<i>🔄 {now_str}</i>"
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="real_short:active")
    builder.button(text="⬅️ Назад", callback_data="real_short:back")
    builder.adjust(2)
    try:
        await query.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        pass


@router.callback_query(F.data == "real_short:history")
async def cb_history(query: CallbackQuery) -> None:
    try:
        await query.answer("📜 Загружаю историю...")
    except Exception:
        pass
    text = await _get_history_text()
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="real_short:back")
    try:
        await query.message.edit_text(text, reply_markup=builder.as_markup())
    except Exception:
        pass


# ── Настройки ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "real_short:settings")
async def cb_settings(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    text = await _get_settings_text()
    try:
        await query.message.edit_text(text, reply_markup=real_short_settings_keyboard())
    except Exception:
        pass


_SETTING_PROMPTS = {
    "margin": ("💰 <b>Маржа на сделку (USDT)</b>\n\nФиксированная маржа. Notional = маржа × плечо.",
               [5, 10, 20, 30, 50, 100, 200, 500]),
    "leverage": ("⚖️ <b>Плечо</b>\n\nКредитное плечо для реальной позиции.",
                 [1, 2, 3, 5, 10, 15, 20, 25]),
    "tp": ("🎯 <b>TP % (движение цены)</b>\n\nP&L = это % × плечо. Напр. TP 1% при 10x = +10% P&L.",
           [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0]),
    "sl": ("🛑 <b>SL % (движение цены)</b>\n\nЛимитный reduce-only ордер на бирже.",
           [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0]),
    "max_open": ("🔢 <b>Max открытых позиций</b>\n\n0 = без лимита (kill-switch по убытку остаётся).",
                 [0, 1, 2, 3, 5, 7, 10]),
    "daily_loss": ("📉 <b>Дневной стоп-лосс (USDT)</b>\n\nПри достижении real-trading авто-выключается.",
                   [10, 25, 50, 100, 200, 500, 1000]),
    "cooldown": ("❄️ <b>Cooldown (сек)</b>\n\nАнтиспам по входам на один символ.",
                 [0, 30, 60, 120, 300, 600]),
}


@router.callback_query(F.data.startswith("real_short:set:"))
async def cb_set_prompt(query: CallbackQuery) -> None:
    try:
        await query.answer()
    except Exception:
        pass
    param = query.data.split(":")[-1]
    prompt = _SETTING_PROMPTS.get(param)
    if not prompt:
        return
    text, values = prompt
    try:
        await query.message.edit_text(
            text, reply_markup=real_short_numeric_keyboard(param, values)
        )
    except Exception:
        pass


PARAM_MAP = {
    "margin": ("real_margin_usdt", float),
    "leverage": ("real_leverage", int),
    "tp": ("real_tp_pct", float),
    "sl": ("real_sl_pct", float),
    "max_open": ("real_max_open_positions", int),
    "daily_loss": ("real_max_daily_loss_usdt", float),
    "cooldown": ("real_cooldown_sec", int),
}


@router.callback_query(F.data.startswith("real_short:val:"))
async def cb_set_value(query: CallbackQuery) -> None:
    try:
        await query.answer("✅ Сохранено")
    except Exception:
        pass
    parts = query.data.split(":")
    if len(parts) < 4:
        return
    param, raw_value = parts[2], parts[3]
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
        await patch_real_short_config(redis, {config_key: value})
        logger.info(
            "Real-short настройка изменена", param=config_key, value=value,
            user_id=query.from_user.id if query.from_user else None,
        )
    finally:
        await redis.aclose()
    text = await _get_settings_text()
    try:
        await query.message.edit_text(text, reply_markup=real_short_settings_keyboard())
    except Exception:
        pass


# ── Ручное закрытие позиции (/rsclose_<id>) ───────────────────────────

@router.message(F.text.regexp(r"^/rsclose_(\d+)$"))
async def cmd_manual_close(msg: Message) -> None:
    if not msg.text:
        return
    try:
        real_id = int(msg.text.split("_", 1)[1])
    except (ValueError, IndexError):
        return

    await msg.answer(f"✋ Закрываю реальную позицию #{real_id}...")

    redis = await _get_redis()
    try:
        from app.services.real_short_service import RealShortService

        service = RealShortService(redis=redis, bot=msg.bot)
        ok, message = await service.manual_close(real_id)
    except Exception as exc:
        logger.error("Real-short ручное закрытие: ошибка", error=str(exc))
        ok, message = False, f"Ошибка: {exc}"
    finally:
        await redis.aclose()

    await msg.answer(f"{'✅' if ok else '❌'} {message}")
