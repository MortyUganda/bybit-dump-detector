"""
/start, /help, /status handlers.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from app.bot.keyboards import main_menu_keyboard
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


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
При сигнале score ≥ 45 бот автоматически открывает
paper шорт с TP -20% и SL +10%.
Все данные сохраняются в БД для анализа и обучения ИИ.

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


@router.message(Command("status"))
async def cmd_status(msg: Message) -> None:
    await msg.answer(
        "⚙️ <b>Статус бота</b>\n\n"
        "✅ Сбор данных: работает\n"
        "✅ Анализ: работает\n"
        "📊 Список монет: обновляется...",
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
    await msg.answer(
        "⚙️ <b>Статус бота</b>\n\n"
        "✅ Сбор данных: работает\n"
        "✅ Анализ: работает\n"
        "📊 Список монет: обновляется...",
    )


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
    from app.bot.handlers.auto_shorts import _format_active_shorts, auto_shorts_keyboard
    text = _format_active_shorts()
    await msg.answer(await text, reply_markup=auto_shorts_keyboard())


@router.message(F.text == "📊 Статистика")
async def btn_stats(msg: Message) -> None:
    from app.bot.handlers.auto_shorts import _format_stats, stats_keyboard
    text = await _format_stats()
    await msg.answer(text, reply_markup=stats_keyboard())