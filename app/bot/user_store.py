"""
Хранение активных пользователей в Redis.
Пользователь добавляется при первом /start.
"""
from __future__ import annotations

import redis.asyncio as aioredis

REDIS_USERS_KEY = "active_users"


async def register_user(redis: aioredis.Redis, user_id: int) -> None:
    """Добавить пользователя в список активных."""
    await redis.sadd(REDIS_USERS_KEY, user_id)


async def get_active_users(redis: aioredis.Redis) -> list[int]:
    """Получить всех активных пользователей."""
    members = await redis.smembers(REDIS_USERS_KEY)
    return [int(uid) for uid in members]


async def remove_user(redis: aioredis.Redis, user_id: int) -> None:
    """Удалить пользователя (если написал /stop)."""
    await redis.srem(REDIS_USERS_KEY, user_id)