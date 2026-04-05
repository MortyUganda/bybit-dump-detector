"""
Alert Manager — broadcasts risk signals to subscribed Telegram users.

Flow:
  1. AnalyzerService calls alert_manager.send_alert(symbol, risk_score)
  2. AlertManager queries DB for all users with alerts_enabled
  3. Checks per-user settings (min_score, signal_type preferences, watchlist priority)
  4. Sends Telegram message to each eligible user
  5. Logs to alert_history
"""
from __future__ import annotations

from aiogram import Bot

from app.bot.formatters import format_risk_alert
from app.bot.keyboards import coin_detail_keyboard
from app.config import get_settings
from app.scoring.engine import RiskScore
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

SIGNAL_TYPE_PREFS = {
    "early_warning":  "notify_early_warning",
    "overheated":     "notify_overheated",
    "reversal_risk":  "notify_reversal_risk",
    "dump_started":   "notify_dump_started",
}


class AlertManager:
    """
    Sends alerts to Telegram users based on their settings.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_alert(self, symbol: str, risk_score: RiskScore) -> None:
        """
        Broadcast alert to all eligible users.
        In MVP: sends to all allowed_user_ids from settings.
        Week 3: extend to per-user DB settings.
        """
        text = format_risk_alert(risk_score)
        keyboard = coin_detail_keyboard(symbol)

        # MVP: broadcast to all configured admin users
        user_ids = settings.allowed_user_ids

        if not user_ids:
            logger.warning("No user IDs configured — alert not sent", symbol=symbol)
            return

        for user_id in user_ids:
            try:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                logger.info("Alert sent", symbol=symbol, user_id=user_id, score=risk_score.score)
            except Exception as e:
                logger.warning("Alert send failed", user_id=user_id, error=str(e))

    async def send_broadcast(self, text: str) -> None:
        """Send a plain text broadcast to all admin users."""
        for user_id in settings.allowed_user_ids:
            try:
                await self._bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
            except Exception as e:
                logger.warning("Broadcast failed", user_id=user_id, error=str(e))
