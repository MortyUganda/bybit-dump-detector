"""
Alert Manager — broadcasts risk signals to subscribed Telegram users.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from aiogram import Bot

from app.bot.formatters import format_risk_alert
from app.bot.handlers.signals import add_signal
from app.bot.keyboards import alert_action_keyboard
from app.config import get_settings
from app.scoring.engine import RiskScore
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


SIGNAL_TYPE_PREFS = {
    "early_warning": "notify_early_warning",
    "overheated": "notify_overheated",
    "reversal_risk": "notify_reversal_risk",
    "dump_started": "notify_dump_started",
}


class AlertManager:
    def __init__(
        self,
        bot: Bot,
        auto_short_service: Any | None = None,
        redis: aioredis.Redis | None = None,
    ) -> None:
        self._bot = bot
        self._auto_short = auto_short_service
        self._redis = redis
        self._owns_redis = redis is None
        self._alert_tasks: set[asyncio.Task] = set()

    async def _get_redis(self) -> aioredis.Redis | None:
        if self._redis is not None:
            return self._redis

        try:
            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            self._owns_redis = True
            return self._redis
        except Exception as e:
            logger.warning("Redis init failed in AlertManager", error=str(e))
            return None

    def _track_task(self, task: asyncio.Task) -> None:
        self._alert_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            self._alert_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.info("Background alert task cancelled")
            except Exception as e:
                logger.exception("Background alert task crashed", error=str(e))

        task.add_done_callback(_cleanup)

    async def _set_last_signal_ts(self) -> None:
        redis = await self._get_redis()
        if redis is None:
            return

        try:
            await redis.set("last_signal_ts", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.debug("Failed to store last_signal_ts", error=str(e))

    async def _trigger_auto_short(self, symbol: str, risk_score: RiskScore) -> None:
        if not self._auto_short:
            return

        if not risk_score.is_actionable:
            logger.debug(
                "Auto short skipped — score is not actionable",
                symbol=symbol,
                score=risk_score.score,
            )
            return

        try:
            task = asyncio.create_task(self._auto_short.open_short(risk_score))
            self._track_task(task)
            logger.info(
                "Auto short triggered",
                symbol=symbol,
                score=risk_score.score,
                signal_type=(
                    risk_score.signal_type.value if risk_score.signal_type else "unknown"
                ),
            )
        except Exception as e:
            logger.exception(
                "Failed to trigger auto short",
                symbol=symbol,
                score=risk_score.score,
                error=str(e),
            )

    async def _should_send_to_user(
        self,
        user_id: int,
        risk_score: RiskScore,
    ) -> bool:
        try:
            from app.bot.handlers.settings import get_user_settings

            user_settings = await get_user_settings(user_id)
        except Exception as e:
            logger.warning(
                "Failed to load user settings",
                user_id=user_id,
                error=str(e),
            )
            return False

        if user_settings.get("quiet_mode"):
            return False

        if not user_settings.get("alerts_enabled", True):
            return False

        if risk_score.score < user_settings.get("min_score", 45):
            return False

        signal_type = risk_score.signal_type.value if risk_score.signal_type else ""
        pref_key = SIGNAL_TYPE_PREFS.get(signal_type)
        if pref_key and not user_settings.get(pref_key, True):
            return False

        return True

    async def send_alert(self, symbol: str, risk_score: RiskScore) -> None:
        text = format_risk_alert(risk_score)
        keyboard = alert_action_keyboard(symbol)

        user_ids = settings.allowed_user_ids
        if not user_ids:
            logger.warning("No user IDs configured — alert not sent", symbol=symbol)
            return

        price = None
        if risk_score.features_snapshot:
            price = risk_score.features_snapshot.last_price

        await add_signal(
            symbol=symbol,
            signal_type=risk_score.signal_type.value if risk_score.signal_type else "unknown",
            score=risk_score.score,
            price=price,
        )

        await self._set_last_signal_ts()
        await self._trigger_auto_short(symbol, risk_score)

        for user_id in user_ids:
            try:
                should_send = await self._should_send_to_user(user_id, risk_score)
                if not should_send:
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
                    signal_type=(
                        risk_score.signal_type.value if risk_score.signal_type else "unknown"
                    ),
                )

            except Exception as e:
                logger.warning(
                    "Alert send failed",
                    user_id=user_id,
                    symbol=symbol,
                    error=str(e),
                )

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

    async def close(self) -> None:
        for task in list(self._alert_tasks):
            if not task.done():
                task.cancel()

        if self._redis is not None and self._owns_redis:
            try:
                await self._redis.aclose()
            except Exception as e:
                logger.debug("AlertManager redis close failed", error=str(e))