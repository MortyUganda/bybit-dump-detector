"""
Alert Manager — broadcasts risk signals to subscribed Telegram users.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from aiogram import Bot

from app.bot.formatters import format_risk_alert
from app.bot.keyboards import alert_action_keyboard
from app.bot.handlers.signals import add_signal
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

    def __init__(self, bot: Bot, auto_short_service=None) -> None:
        self._bot = bot
        self._auto_short = auto_short_service

    async def send_alert(self, symbol: str, risk_score: RiskScore) -> None:
        text = format_risk_alert(risk_score)
        keyboard = alert_action_keyboard(symbol)

        user_ids = settings.allowed_user_ids

        if not user_ids:
            logger.warning("No user IDs configured — alert not sent", symbol=symbol)
            return

        # Сохраняем в историю сигналов
        price = None
        if risk_score.features_snapshot:
            price = risk_score.features_snapshot.last_price

        add_signal(
            symbol=symbol,
            signal_type=risk_score.signal_type.value if risk_score.signal_type else "unknown",
            score=risk_score.score,
            price=price,
        )

        # Записываем время последнего сигнала в Redis для мониторинга
        try:
            import redis.asyncio as aioredis
            _redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await _redis.set("last_signal_ts", datetime.now(timezone.utc).isoformat())
            await _redis.aclose()
        except Exception:
            pass

        # Автоматически открываем paper шорт
        if self._auto_short and risk_score.is_actionable:
            asyncio.create_task(self._auto_short.open_short(risk_score))
            logger.info(
                "Auto short triggered",
                symbol=symbol,
                score=risk_score.score,
            )

        # Отправляем уведомление пользователям с учётом их настроек
        for user_id in user_ids:
            try:
                # Проверяем настройки пользователя
                from app.bot.handlers.settings import get_user_settings
                s = get_user_settings(user_id)

                # Тихий режим
                if s.get("quiet_mode"):
                    continue

                # Уведомления выключены
                if not s.get("alerts_enabled", True):
                    continue

                # Минимальный score
                if risk_score.score < s.get("min_score", 45):
                    continue

                # Тип сигнала
                signal_type = risk_score.signal_type.value if risk_score.signal_type else ""
                signal_pref_map = {
                    "early_warning":  "notify_early_warning",
                    "overheated":     "notify_overheated",
                    "reversal_risk":  "notify_reversal_risk",
                    "dump_started":   "notify_dump_started",
                }
                pref_key = signal_pref_map.get(signal_type)
                if pref_key and not s.get(pref_key, True):
                    continue

                await self._bot.send_message(
                    chat_id=user_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                logger.info(
                    "Alert sent",
                    symbol=symbol,
                    user_id=user_id,
                    score=risk_score.score,
                )

            except Exception as e:
                logger.warning("Alert send failed", user_id=user_id, error=str(e))

    async def send_broadcast(self, text: str) -> None:
        """Send a plain text broadcast to all admin users."""
        for user_id in settings.allowed_user_ids:
            try:
                await self._bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning("Broadcast failed", user_id=user_id, error=str(e))