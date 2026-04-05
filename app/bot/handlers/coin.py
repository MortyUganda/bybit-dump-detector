"""
/coin SYMBOL — full diagnostic snapshot for one coin.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.formatters import format_coin_diagnostic
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = Router()


@router.message(Command("coin"))
async def cmd_coin(msg: Message) -> None:
    args = msg.text.split() if msg.text else []
    if len(args) < 2:
        await msg.answer("Usage: <code>/coin SYMBOL</code>\nExample: <code>/coin DOGEUSDT</code>")
        return

    symbol = args[1].upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    # TODO: fetch latest features + risk score from Redis cache
    await msg.answer(
        f"🔍 <b>{symbol} Diagnostics</b>\n\n"
        f"<i>Data not yet available. The analyzer needs ~2 min to warm up.</i>\n\n"
        f"<b>Will show when live:</b>\n"
        f"• Risk Score: 0–100\n"
        f"• RSI (1m/5m)\n"
        f"• VWAP Extension %\n"
        f"• Volume Z-Score\n"
        f"• Trade Imbalance (5m)\n"
        f"• Large Buys / Large Sells (5m)\n"
        f"• Consecutive Green Candles\n"
        f"• OB Bid Depth Change\n"
        f"• Spread %\n"
        f"• Momentum Loss: Yes/No\n"
        f"• Last Signal: type + time\n"
    )
