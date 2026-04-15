"""
/start, /help, /status handlers.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from app.bot.handlers.auto_shorts import _format_active_shorts, auto_shorts_keyboard
from app.bot.keyboards import main_menu_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()

# Store bot start time for uptime calculation
_BOT_START_TS: datetime | None = None


def mark_bot_started() -> None:
    """Call once on bot startup to record start time."""
    global _BOT_START_TS
    _BOT_START_TS = datetime.now(timezone.utc)


HELP_TEXT = """
<b>🔍 Bybit Dump Detector</b>

Привет Бездельникам и Татарам!
Отслеживает спекулятивные монеты на Bybit и находит перегретые активы с риском слива.
При обнаружении сигнала автоматически открывает paper шорт и записывает результат.

<b>Команды:</b>
/overvalued — переоценённые монеты прямо сейчас
/signals — история сигналов риска
/coin SYMBOL — полная диагностика монеты
/auto_shorts — активные авто-шорты
/stats — статистика по всем сделкам
/history — история закрытых сделок
/watchlist — список отслеживания
/add SYMBOL — добавить монету в список
/remove SYMBOL — удалить из списка
/settings — настройки уведомлений
/strategy — глобальная стратегия авто-шорта
/status — статус сервисов бота
/help — эта справка

<b>Уровни риска:</b>
🟢 НИЗКИЙ (0–24) — без действий
🟡 УМЕРЕННЫЙ (25–49) — наблюдать
🟠 ВЫСОКИЙ (50–74) — повышенный риск слива
🔴 КРИТИЧЕСКИЙ (75–100) — сильный сигнал разворота

<b>Типы сигналов:</b>
⚠️ Раннее предупреждение — первые признаки перегрева
🔥 Перегрев — RSI + объём + VWAP все высокие
⬇️ Риск разворота — импульс слабеет, wick rejection
💥 Слив начался — цена падает, ликвидность уходит

<b>Авто-шорт:</b>
Параметры стратегии теперь можно менять прямо из Telegram через /strategy.
Настройки применяются на лету без редеплоя.

<i>Сигналы носят информационный характер.
Не является финансовой рекомендацией.</i>
"""


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📡 Сигналы"),
                KeyboardButton(text="📊 Переоценённые"),
            ],
            [
                KeyboardButton(text="⭐ Watchlist"),
                KeyboardButton(text="🤖 Авто-шорты"),
            ],
            [
                KeyboardButton(text="📊 Статистика"),
                KeyboardButton(text="📋 История"),
            ],
            [
                KeyboardButton(text="⚙️ Статус"),
                KeyboardButton(text="❓ Помощь"),
            ],
        ],
        resize_keyboard=True,
        persistent=True,
    )


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not msg.from_user:
        return

    try:
        import redis.asyncio as aioredis
        from app.bot.user_store import register_user
        from app.config import get_settings

        settings = get_settings()
        redis = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await register_user(redis, msg.from_user.id)
        await redis.aclose()
    except Exception:
        pass

    name = msg.from_user.first_name or "Трейдер"
    await msg.answer(
        f"👋 Добро пожаловать, <b>{name}</b>!\n\n{HELP_TEXT}",
        reply_markup=main_reply_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(msg: Message) -> None:
    await msg.answer(HELP_TEXT)


async def _build_status_dashboard() -> str:
    """Build a rich status dashboard from Redis + DB data."""
    import redis.asyncio as aioredis
    from app.config import get_settings

    settings = get_settings()
    lines = ["📊 <b>Dump Detector — Dashboard</b>\n"]

    try:
        redis = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True,
        )

        # ── Services status ──────────────────────────────────────────
        try:
            await redis.ping()
            redis_ok = True
        except Exception:
            redis_ok = False

        lines.append(
            f"🔗 Сервисы: {'✅' if redis_ok else '❌'} Ingestion | "
            f"{'✅' if redis_ok else '❌'} Analyzer | ✅ Bot"
        )

        # ── Monitored symbols count ─────────────────────────────────
        symbol_count = 0
        try:
            keys = await redis.keys("score:*")
            symbol_count = len(keys)
        except Exception:
            pass
        lines.append(f"📡 Мониторинг: <b>{symbol_count}</b> монет\n")

        # ── BTC filter ───────────────────────────────────────────────
        try:
            btc_raw = await redis.get("score:BTCUSDT")
            if btc_raw:
                btc_data = json.loads(btc_raw)
                btc_snap = btc_data.get("features_snapshot") or {}
            else:
                btc_snap = {}
        except Exception:
            btc_snap = {}

        btc_windows = [
            ("1m", btc_snap.get("btc_change_1m", 0), 0.15),
            ("5m", btc_snap.get("btc_change_5m", 0), 0.30),
            ("15m", btc_snap.get("btc_change_15m", 0), 0.45),
            ("1h", btc_snap.get("btc_change_1h", 0), 0.80),
        ]
        filter_active = False
        btc_lines = "📈 <b>BTC фильтр:</b>\n"
        for label, change, threshold in btc_windows:
            exceeded = change >= threshold
            if exceeded:
                filter_active = True
            warn = " ⚠️" if exceeded else ""
            btc_lines += f"  {label}: <b>{change:+.2f}%</b>{warn} (порог ≥{threshold:.2f}%)\n"
        if filter_active:
            btc_lines += "  🔴 Фильтр активен — входы заблокированы\n"
        else:
            btc_lines += "  🟢 Фильтр неактивен\n"
        lines.append(btc_lines)

        # ── Active shorts ────────────────────────────────────────────
        active_count = 0
        try:
            active_count = await redis.hlen("active_shorts")
        except Exception:
            pass

        max_shorts = 5
        lines.append(
            f"📊 <b>Авто-шорты:</b>\n"
            f"  Активных: <b>{active_count} / {max_shorts}</b>"
        )

        # ── Today's trade stats from DB ──────────────────────────────
        today_opened = 0
        today_closed_tp = 0
        today_closed_total = 0
        win_rate_24h = 0.0
        wins_24h = 0
        total_closed_24h = 0
        avg_pnl_24h = 0.0
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models.auto_short import AutoShort
            from sqlalchemy import select

            now = datetime.now(timezone.utc)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_ago = now - timedelta(hours=24)

            async with AsyncSessionLocal() as session:
                # Today's opens
                res = await session.execute(
                    select(AutoShort).where(AutoShort.entry_ts >= today_start)
                )
                today_trades = res.scalars().all()
                today_opened = len(today_trades)
                today_closed_total = sum(
                    1 for t in today_trades if t.status != "open"
                )
                today_closed_tp = sum(
                    1 for t in today_trades if t.status == "tp_hit"
                )

                # 24h closed trades for winrate
                res24 = await session.execute(
                    select(AutoShort).where(
                        AutoShort.status != "open",
                        AutoShort.exit_ts >= day_ago,
                    )
                )
                closed_24h = res24.scalars().all()
                total_closed_24h = len(closed_24h)
                wins_24h = sum(1 for t in closed_24h if t.ml_label == 1)
                pnls = [t.pnl_pct for t in closed_24h if t.pnl_pct is not None]
                avg_pnl_24h = sum(pnls) / len(pnls) if pnls else 0
                win_rate_24h = (
                    wins_24h / total_closed_24h * 100
                    if total_closed_24h
                    else 0
                )
        except Exception:
            pass

        close_reason = f"TP" if today_closed_tp else ""
        if today_closed_total > 0 and close_reason:
            close_reason = f" ({close_reason})"
        elif today_closed_total > 0:
            close_reason = ""
        else:
            close_reason = ""

        lines.append(
            f"  Сегодня: {today_opened} открыто, "
            f"{today_closed_total} закрыто{close_reason}\n"
        )

        # ── 24h stats ────────────────────────────────────────────────
        win_em = "🟢" if win_rate_24h >= 50 else "🔴"
        pnl_em = "🟢" if avg_pnl_24h > 0 else "🔴"
        lines.append(
            f"📈 <b>Статистика 24ч:</b>\n"
            f"  Winrate: {win_em} <b>{win_rate_24h:.0f}%</b> "
            f"({wins_24h}/{total_closed_24h})\n"
            f"  Средний PnL: {pnl_em} <b>{avg_pnl_24h:+.1f}%</b>\n"
        )

        # ── Uptime ───────────────────────────────────────────────────
        if _BOT_START_TS:
            delta = datetime.now(timezone.utc) - _BOT_START_TS
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            minutes = rem // 60
            lines.append(f"⏱ Аптайм: <b>{days}д {hours}ч {minutes}м</b>")
        else:
            lines.append("⏱ Аптайм: <i>N/A</i>")

        await redis.aclose()

    except Exception as e:
        logger.error("Status dashboard build failed", error=str(e))
        lines.append("\n<i>Ошибка получения данных.</i>")

    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    text = await _build_status_dashboard()
    await msg.answer(text)


# ── Обработчики текстовых кнопок ─────────────────────────────────

@router.message(F.text == "📡 Сигналы")
async def btn_signals(msg: Message) -> None:
    from app.bot.handlers.signals import _format_signals_page, signals_history_keyboard
    text, has_next = await _format_signals_page(page=0)
    await msg.answer(text, reply_markup=signals_history_keyboard(page=0, has_next=has_next))


@router.message(F.text == "📊 Переоценённые")
async def btn_overvalued(msg: Message) -> None:
    from app.bot.handlers.overvalued import _fetch_and_format, overvalued_keyboard
    text, success = await _fetch_and_format()
    await msg.answer(text, reply_markup=overvalued_keyboard() if success else None)


@router.message(F.text == "⭐ Watchlist")
async def btn_watchlist(msg: Message) -> None:
    if not msg.from_user:
        return
    from app.bot.handlers.watchlist_store import get_watchlist
    from app.bot.keyboards import watchlist_keyboard
    user_id = msg.from_user.id
    symbols = sorted(await get_watchlist(user_id))
    if not symbols:
        await msg.answer(
            "⭐ <b>Ваш список отслеживания</b>\n\n"
            "<i>Пусто. Добавьте монеты командой /add SYMBOL</i>"
        )
        return
    text = "⭐ <b>Ваш список отслеживания</b>\n\n"
    text += "\n".join(f"• <b>{s}</b>" for s in symbols)
    await msg.answer(text, reply_markup=watchlist_keyboard(symbols))


@router.message(F.text == "📋 Сделки")
async def btn_trades(msg: Message) -> None:
    if not msg.from_user:
        return
    # Read active shorts from Redis (cross-process safe)
    import redis.asyncio as aioredis
    from app.config import get_settings
    from app.services.auto_short_service import REDIS_ACTIVE_SHORTS_KEY, _deserialize_trade
    _settings = get_settings()
    _redis = aioredis.from_url(_settings.redis_url, decode_responses=True)
    try:
        raw_all = await _redis.hgetall(REDIS_ACTIVE_SHORTS_KEY)
    finally:
        await _redis.aclose()

    if not raw_all:
        await msg.answer(
            "📋 <b>Авто-шорты</b>\n\n"
            "<i>Нет активных сделок.</i>\n\n"
            "Сделки открываются автоматически при score ≥ 45."
        )
        return
    active_shorts = {int(k): _deserialize_trade(v) for k, v in raw_all.items()}
    lines = ["📋 <b>Активные авто-шорты</b>\n"]
    for trade_id, trade in sorted(active_shorts.items(), reverse=True):
        lines.append(
            f"🟡 #{trade_id} <code>{trade['symbol']}</code>\n"
            f"   Вход: ${trade['entry_price']:.6g}\n"
            f"   TP: ${trade['tp_price']:.6g} | SL: ${trade['sl_price']:.6g}"
        )
    await msg.answer("\n\n".join(lines))


@router.message(F.text == "⚙️ Статус")
async def btn_status(msg: Message) -> None:
    text = await _build_status_dashboard()
    await msg.answer(text)


@router.message(F.text == "📋 История")
async def btn_history(msg: Message) -> None:
    from app.bot.handlers.history import _fetch_history, _format_history, history_keyboard
    trades, has_next = await _fetch_history()
    text = _format_history(trades, "all", "all", 0)
    await msg.answer(text, reply_markup=history_keyboard(0, has_next, "all", "all"))


@router.message(F.text == "❓ Помощь")
async def btn_help(msg: Message) -> None:
    await msg.answer(HELP_TEXT)


@router.message(F.text == "🤖 Авто-шорты")
async def btn_auto_shorts(msg: Message) -> None:
    text, trade_ids = await _format_active_shorts()
    await msg.answer(text, reply_markup=auto_shorts_keyboard(trade_ids))

@router.message(F.text == "📊 Статистика")
async def btn_stats(msg: Message) -> None:
    from app.bot.handlers.auto_shorts import _format_stats, stats_keyboard
    text = await _format_stats()
    await msg.answer(text, reply_markup=stats_keyboard())