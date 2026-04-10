"""
Global runtime strategy config stored in Redis.

Used for live strategy tuning from Telegram without redeploy/restart.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

RUNTIME_STRATEGY_KEY = "runtime_config:auto_short"

DEFAULT_AUTO_SHORT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "allowed_signal_types": [
        "reversal_risk",
        "dump_started",
    ],
    "leverage": 10,
    "target_pnl_pct": 20.0,
    "target_sl_pct": 10.0,
    "entry_delay_sec": 60,
    "monitor_attempts": 24,
    "monitor_interval_sec": 5,
    "min_score_to_enter": 55,
    "stabilization_threshold_pct": 0.2,
    "max_rise_pct": 0.8,
    "max_entry_drop_pct": -0.3,
    "trade_monitor_interval": 5,
    "max_trade_duration_sec": 60 * 60 * 4,
}


async def get_runtime_strategy_config(redis: aioredis.Redis) -> dict[str, Any]:
    raw = await redis.get(RUNTIME_STRATEGY_KEY)
    if not raw:
        return DEFAULT_AUTO_SHORT_CONFIG.copy()

    try:
        loaded = json.loads(raw)
        merged = DEFAULT_AUTO_SHORT_CONFIG.copy()
        merged.update(loaded or {})
        return merged
    except Exception:
        return DEFAULT_AUTO_SHORT_CONFIG.copy()


async def save_runtime_strategy_config(
    redis: aioredis.Redis,
    config: dict[str, Any],
) -> dict[str, Any]:
    merged = DEFAULT_AUTO_SHORT_CONFIG.copy()
    merged.update(config or {})
    await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(merged))
    return merged


async def patch_runtime_strategy_config(
    redis: aioredis.Redis,
    patch: dict[str, Any],
) -> dict[str, Any]:
    current = await get_runtime_strategy_config(redis)
    current.update(patch or {})
    await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(current))
    return current


async def reset_runtime_strategy_config(redis: aioredis.Redis) -> dict[str, Any]:
    await redis.delete(RUNTIME_STRATEGY_KEY)
    return DEFAULT_AUTO_SHORT_CONFIG.copy()