from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.paper_trading.strategies import STRATEGIES


def alert_action_keyboard(symbol: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📈 Открыть позицию",
        callback_data=f"pt:open:{symbol}",
    )
    builder.button(
        text="⏭ Пропустить",
        callback_data=f"pt:skip:{symbol}",
    )
    builder.adjust(2)
    return builder.as_markup()


def strategy_keyboard(symbol: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, strat in STRATEGIES.items():
        builder.button(
            text=f"{strat.label} | {strat.description}",
            callback_data=f"pt:strategy:{symbol}:{key}",
        )
    builder.adjust(1)
    return builder.as_markup()


def trade_status_keyboard(trade_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📊 Статус сделки",
        callback_data=f"pt:status:{trade_id}",
    )
    builder.button(
        text="❌ Закрыть вручную",
        callback_data=f"pt:close:{trade_id}",
    )
    builder.adjust(2)
    return builder.as_markup()