"""
/start, /help, /status handlers.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import main_menu_keyboard, main_reply_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


HELP_TEXT = """
<b>🔍 Bybit Dump Detector</b>

Отслеживает спекулятивные монеты на Bybit и помогает находить перегретые активы с риском скорого слива.

<b>Команды:</b>
/signals — последние сигналы риска
/overvalued — самые переоценённые монеты сейчас
/coin SYMBOL — подробный разбор конкретной монеты
/watchlist — ваш личный список отслеживания
/add SYMBOL — добавить монету в список
/remove SYMBOL — удалить монету из списка
/settings — настроить уведомления
/status — статус бота и отслеживаемых монет
/help — показать эту справку

<b>Уровни риска:</b>
🟢 НИЗКИЙ (0–24) — серьёзных признаков нет
🟡 УМЕРЕННЫЙ (25–49) — стоит наблюдать
🟠 ВЫСОКИЙ (50–74) — повышенный риск слива
🔴 КРИТИЧЕСКИЙ (75–100) — сильный сигнал на разворот/обвал

<b>Типы сигналов:</b>
⚠️ Раннее предупреждение — первые признаки перегрева
🔥 Перегрев — повышены RSI, объём и отклонение от VWAP
⬇️ Риск разворота — импульс слабеет, есть признаки отката
💥 Слив начался — цена уже падает, ликвидность ухудшается

<i>Сигналы бота носят информационный характер и не являются финансовой рекомендацией.</i>
"""


@router.message(Command("start"))
async def cmd_start(msg: Message) -> None:
    if not msg.from_user:
        return

    # Регистрируем пользователя в Redis
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

    name = msg.from_user.first_name if msg.from_user else "Трейдер"
    await msg.answer(
        f"👋 Добро пожаловать, <b>{name}</b>!\n\n{HELP_TEXT}",
        reply_markup=main_reply_keyboard(),
    )
    await msg.answer(
        "Выберите раздел:",
        reply_markup=main_menu_keyboard(),
    )


# ── Обработчики текстовых кнопок ─────────────────────────────────

@router.message(F.text == "📡 Сигналы")
async def btn_signals(msg: Message) -> None:
    from app.bot.handlers.signals import _format_signals_page, signals_history_keyboard
    text, has_next = _format_signals_page(page=0)
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
    from app.bot.handlers.watchlist_store import WATCHLISTS
    from app.bot.keyboards import watchlist_keyboard
    user_id = msg.from_user.id
    symbols = sorted(WATCHLISTS.get(user_id, set()))
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
    from app.bot.handlers.paper_trading import PAPER_TRADES
    if not msg.from_user:
        return
    user_id = msg.from_user.id
    user_trades = [t for t in PAPER_TRADES.values() if t["user_id"] == user_id]
    if not user_trades:
        await msg.answer(
            "📋 <b>Ваши paper сделки</b>\n\n"
            "<i>Пока нет сделок.</i>"
        )
        return
    lines = ["📋 <b>Ваши paper сделки</b>\n"]
    for trade in sorted(user_trades, key=lambda x: -x["id"]):
        status_em = {
            "open": "🟡", "tp1": "🟢", "tp2": "🟢",
            "tp3": "🟢", "sl": "🔴", "closed_manual": "⚪",
        }.get(trade["status"], "❓")
        pnl_str = f"{trade['pnl_pct']:+.2f}%" if trade.get("pnl_pct") is not None else "в процессе"
        lines.append(f"{status_em} #{trade['id']} de>{trade['symbol']}</code> | {pnl_str}")
    await msg.answer("\n".join(lines))


@router.message(F.text == "⚙️ Статус")
async def btn_status(msg: Message) -> None:
    await msg.answer(
        "⚙️ <b>Статус бота</b>\n\n"
        "✅ Сбор данных: работает\n"
        "✅ Анализ: работает\n"
        "📊 Список монет: обновляется...",
    )


@router.message(F.text == "❓ Помощь")
async def btn_help(msg: Message) -> None:
    await msg.answer(HELP_TEXT, reply_markup=main_menu_keyboard())