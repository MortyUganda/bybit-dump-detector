"""
Watchlist storage backed by Redis sets.
Key: watchlist:{user_id}, values: symbol strings.
"""
from __future__ import annotations

import redis.asyncio as aioredis

from app.config import get_settings

REDIS_WATCHLIST_PREFIX = "watchlist"


async def _get_redis() -> aioredis.Redis:
    settings = get_settings()
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol


async def get_watchlist(user_id: int, redis: aioredis.Redis | None = None) -> set[str]:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        members = await redis.smembers(f"{REDIS_WATCHLIST_PREFIX}:{user_id}")
        return {m for m in members}
    finally:
        if close_after:
            await redis.aclose()


async def add_to_watchlist(
    user_id: int, symbol: str, redis: aioredis.Redis | None = None
) -> None:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        await redis.sadd(f"{REDIS_WATCHLIST_PREFIX}:{user_id}", symbol)
    finally:
        if close_after:
            await redis.aclose()


async def remove_from_watchlist(
    user_id: int, symbol: str, redis: aioredis.Redis | None = None
) -> None:
    close_after = False
    if redis is None:
        redis = await _get_redis()
        close_after = True

    try:
        await redis.srem(f"{REDIS_WATCHLIST_PREFIX}:{user_id}", symbol)
    finally:
        if close_after:
            await redis.aclose()
