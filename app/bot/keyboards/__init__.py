"""Inline and reply keyboards for the bot."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton  # noqa: I001
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📡 Signals", callback_data="nav:signals"),
            InlineKeyboardButton(text="📊 Overvalued", callback_data="nav:overvalued"),
        ],
        [
            InlineKeyboardButton(text="⭐ Watchlist", callback_data="nav:watchlist"),
            InlineKeyboardButton(text="⚙️ Settings", callback_data="nav:settings"),
        ],
        [
            InlineKeyboardButton(text="ℹ️ Status", callback_data="nav:status"),
        ],
    ])

def alert_action_keyboard(symbol: str) -> InlineKeyboardMarkup:
    base = symbol.replace("USDT", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Детали", callback_data=f"coin:detail:{symbol}"),
            InlineKeyboardButton(text="⭐ В watchlist", callback_data=f"watch_add:{symbol}"),
            InlineKeyboardButton(
                text="🔗 Bybit",
                url=f"https://www.bybit.com/trade/usdt/{base}USDT",
            ),
        ]
    ])


def trade_action_keyboard(symbol: str, trade_id: int) -> InlineKeyboardMarkup:
    base = symbol.replace("USDT", "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Детали", callback_data=f"coin:detail:{symbol}"),
            InlineKeyboardButton(
                text="🔗 Bybit",
                url=f"https://www.bybit.com/trade/usdt/{base}USDT",
            ),
        ]
    ])

def watchlist_keyboard(symbols: list[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for symbol in symbols:
        builder.button(
            text=f"🗑 Удалить {symbol}",
            callback_data=f"watch:remove:{symbol}",
        )

    builder.adjust(1)
    return builder.as_markup()

def signals_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"signals:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"signals:page:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data=f"signals:page:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def coin_detail_keyboard(symbol: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⭐ Add to watchlist", callback_data=f"watch:add:{symbol}"),
            InlineKeyboardButton(text="🔄 Refresh", callback_data=f"coin:refresh:{symbol}"),
        ],
        [
            InlineKeyboardButton(
                text="📈 View on Bybit",
                url=f"https://www.bybit.com/trade/spot/{symbol[:len(symbol)-4]}/USDT",
            ),
        ],
    ])

def alert_detail_keyboard(symbol: str, signal_type: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔍 Подробнее",
        callback_data=f"alert:detail:{symbol}",
    )
    builder.button(
        text="📈 Bybit",
        url=f"https://www.bybit.com/trade/usdt/{symbol}",
    )
    builder.adjust(2)
    return builder.as_markup()

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
                KeyboardButton(text="⚙️ Статус бота"),
                KeyboardButton(text="📋 История"),
            ],
            [
                KeyboardButton(text="🔧 Настройки"),
            ],
            [
                KeyboardButton(text="❓ Помощь"),
            ],
        ],
        resize_keyboard=True,
        persistent=True,
    )

def risk_level_emoji(level: str) -> str:
    return {
        "low": "🟢",
        "moderate": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }.get(level, "⚪")


def signal_type_emoji(signal_type: str) -> str:
    return {
        "early_warning": "⚠️",
        "overheated": "🔥",
        "reversal_risk": "⬇️",
        "dump_started": "💥",
    }.get(signal_type, "📊")
