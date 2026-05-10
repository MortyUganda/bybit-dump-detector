"""
Runtime-конфигурация ml_short в Redis.
Аналог runtime_config.py, но для сервиса ML-short paper-trading.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

RUNTIME_ML_SHORT_KEY = "runtime_config:ml_short"

DEFAULT_ML_SHORT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "proba_threshold": 0.60,
    "min_score_to_enter": 45,
    "max_concurrent_positions": 5,
    "position_timeout_hours": 24,
    "adverse_move_threshold_pct": 0.2,
    "delay_seconds": 30,
    "cooldown_enabled": True,
    "cooldown_loss_count": 2,
    "cooldown_hours": 24,
    "is_paper": True,
}


def _normalize_ml_short_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = DEFAULT_ML_SHORT_CONFIG.copy()
    merged.update(config or {})

    merged["enabled"] = bool(merged.get("enabled", False))
    merged["proba_threshold"] = max(0.30, min(0.95, float(merged.get("proba_threshold", 0.60))))
    merged["min_score_to_enter"] = max(0, min(100, int(merged.get("min_score_to_enter", 45))))
    # 0 = «без лимита». Верхняя граница 999 — фактический потолок (никогда не достижим).
    merged["max_concurrent_positions"] = max(0, min(999, int(merged.get("max_concurrent_positions", 5))))
    merged["position_timeout_hours"] = max(1, min(72, int(merged.get("position_timeout_hours", 24))))
    merged["adverse_move_threshold_pct"] = max(0.1, min(2.0, float(merged.get("adverse_move_threshold_pct", 0.2))))
    merged["delay_seconds"] = max(0, min(120, int(merged.get("delay_seconds", 30))))
    merged["cooldown_enabled"] = bool(merged.get("cooldown_enabled", True))
    merged["cooldown_loss_count"] = max(1, min(10, int(merged.get("cooldown_loss_count", 2))))
    merged["cooldown_hours"] = max(1, min(72, int(merged.get("cooldown_hours", 24))))
    merged["is_paper"] = True  # Всегда True — real пока не реализован

    return merged


async def get_ml_short_config(redis: aioredis.Redis) -> dict[str, Any]:
    raw = await redis.get(RUNTIME_ML_SHORT_KEY)
    if not raw:
        config = _normalize_ml_short_config(DEFAULT_ML_SHORT_CONFIG)
        await redis.set(RUNTIME_ML_SHORT_KEY, json.dumps(config))
        return config

    try:
        loaded = json.loads(raw)
        return _normalize_ml_short_config(loaded)
    except Exception:
        config = _normalize_ml_short_config(DEFAULT_ML_SHORT_CONFIG)
        await redis.set(RUNTIME_ML_SHORT_KEY, json.dumps(config))
        return config


async def save_ml_short_config(
    redis: aioredis.Redis,
    config: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_ml_short_config(config)
    await redis.set(RUNTIME_ML_SHORT_KEY, json.dumps(normalized))
    return normalized


async def patch_ml_short_config(
    redis: aioredis.Redis,
    patch: dict[str, Any],
) -> dict[str, Any]:
    current = await get_ml_short_config(redis)
    current.update(patch or {})
    normalized = _normalize_ml_short_config(current)
    await redis.set(RUNTIME_ML_SHORT_KEY, json.dumps(normalized))
    return normalized
