"""
MonitorService — следит за здоровьем системы.
Отправляет алерт если analyzer молчит больше часа.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# Максимальное время тишины перед алертом
MAX_SILENCE_MINUTES = 60
CHECK_INTERVAL = 60 * 15  # проверяем каждые 15 минут


class MonitorService:

    def __init__(self, redis: aioredis.Redis, bot=None) -> None:
        self._redis = redis
        self._bot = bot
        self._running = False
        self._alert_sent = False  # не спамить повторными алертами

    def set_bot(self, bot) -> None:
        self._bot = bot

    async def start(self) -> None:
        self._running = True
        logger.info("Monitor service started")
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._running = False

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(CHECK_INTERVAL)
            try:
                await self._check_analyzer_health()
                await self._check_redis_health()
            except Exception as e:
                logger.error("Monitor check failed", error=str(e))

    async def _check_analyzer_health(self) -> None:
        """Проверить когда был последний сигнал."""
        try:
            raw = await self._redis.get("last_signal_ts")

            if not raw:
                # Сигналов ещё не было — не алертим первые 2 часа
                return

            last_ts = datetime.fromisoformat(raw)
            now = datetime.now(timezone.utc)
            silence_minutes = (now - last_ts).total_seconds() / 60

            if silence_minutes >= MAX_SILENCE_MINUTES:
                if not self._alert_sent:
                    await self._send_alert(
                        f"⚠️ <b>Мониторинг: нет сигналов</b>\n\n"
                        f"Analyzer молчит уже <b>{int(silence_minutes)} минут</b>.\n"
                        f"Последний сигнал: {last_ts.strftime('%d.%m %H:%M')} UTC\n\n"
                        f"Проверьте логи:\n"
                        f"de>docker logs --tail=50 dd_analyzer</code>"
                    )
                    self._alert_sent = True
            else:
                # Сигналы есть — сбрасываем флаг
                if self._alert_sent:
                    await self._send_alert(
                        f"✅ <b>Мониторинг: всё в порядке</b>\n\n"
                        f"Analyzer снова присылает сигналы."
                    )
                self._alert_sent = False

        except Exception as e:
            logger.error("Analyzer health check failed", error=str(e))

    async def _check_redis_health(self) -> None:
        """Проверить что Redis отвечает."""
        try:
            await self._redis.ping()
        except Exception as e:
            logger.error("Redis health check failed", error=str(e))
            await self._send_alert(
                f"🚨 <b>Мониторинг: Redis недоступен</b>\n\n"
                f"Ошибка: {str(e)}"
            )

    async def _send_alert(self, text: str) -> None:
        if not self._bot:
            logger.warning("Bot not set — cannot send monitor alert")
            return

        try:
            from app.bot.user_store import get_active_users
            user_ids = await get_active_users(self._redis)

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Monitor alert failed", user_id=user_id, error=str(e))

            logger.info("Monitor alert sent", users=len(user_ids))

        except Exception as e:
            logger.error("Monitor send failed", error=str(e))