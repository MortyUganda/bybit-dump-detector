"""
Global runtime strategy config stored in Redis.

Used for live strategy tuning from Telegram without redeploy/restart.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

RUNTIME_STRATEGY_KEY = "runtime_config:auto_short"

VALID_SIGNAL_TYPES = {
    "early_warning",
    "overheated",
    "reversal_risk",
    "dump_started",
}

DEFAULT_AUTO_SHORT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "allowed_signal_types": [
        "overheated",
        "reversal_risk",
        "dump_started",
    ],
    "leverage": 10,
    "target_pnl_pct": 20.0,
    "target_sl_pct": 10.0,
    "entry_delay_sec": 60,
    "monitor_attempts": 24,
    "monitor_interval_sec": 5,
    "min_score_to_enter": 50,
    "stabilization_threshold_pct": 0.2,
    "max_rise_pct": 0.8,
    "max_entry_drop_pct": -0.3,
    "trade_monitor_interval": 5,
    "max_trade_duration_sec": 60 * 60 * 4,
    # Reversal Risk settings
    "reversal_enabled": True,
    "reversal_warning_threshold": 4,
    "reversal_critical_threshold": 7,
    "reversal_action": "tighten_trailing",  # notify_only | tighten_trailing | auto_close
    "reversal_pnl_filter": "always",        # profit_only | always
}


def _normalize_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = DEFAULT_AUTO_SHORT_CONFIG.copy()
    merged.update(config or {})

    merged["enabled"] = bool(merged.get("enabled", True))

    allowed_signal_types = merged.get("allowed_signal_types", [])
    if not isinstance(allowed_signal_types, list):
        allowed_signal_types = DEFAULT_AUTO_SHORT_CONFIG["allowed_signal_types"]

    merged["allowed_signal_types"] = sorted(
        s for s in allowed_signal_types if s in VALID_SIGNAL_TYPES
    )

    if not merged["allowed_signal_types"]:
        merged["allowed_signal_types"] = DEFAULT_AUTO_SHORT_CONFIG["allowed_signal_types"][:]

    merged["leverage"] = max(1, int(merged.get("leverage", 10)))
    merged["target_pnl_pct"] = float(merged.get("target_pnl_pct", 20.0))
    merged["target_sl_pct"] = float(merged.get("target_sl_pct", 10.0))
    merged["entry_delay_sec"] = max(0, int(merged.get("entry_delay_sec", 60)))
    merged["monitor_attempts"] = max(1, int(merged.get("monitor_attempts", 24)))
    merged["monitor_interval_sec"] = max(1, int(merged.get("monitor_interval_sec", 5)))
    merged["min_score_to_enter"] = max(0, min(100, int(merged.get("min_score_to_enter", 55))))
    merged["stabilization_threshold_pct"] = float(
        merged.get("stabilization_threshold_pct", 0.2)
    )
    merged["max_rise_pct"] = float(merged.get("max_rise_pct", 0.8))
    merged["max_entry_drop_pct"] = float(merged.get("max_entry_drop_pct", -0.3))
    merged["trade_monitor_interval"] = max(
        1, int(merged.get("trade_monitor_interval", 5))
    )
    merged["max_trade_duration_sec"] = max(
        60, int(merged.get("max_trade_duration_sec", 60 * 60 * 4))
    )

    # Reversal Risk normalization
    merged["reversal_enabled"] = bool(merged.get("reversal_enabled", True))
    merged["reversal_warning_threshold"] = max(
        1, min(11, int(merged.get("reversal_warning_threshold", 4)))
    )
    merged["reversal_critical_threshold"] = max(
        merged["reversal_warning_threshold"] + 1,
        min(11, int(merged.get("reversal_critical_threshold", 7))),
    )
    reversal_action = merged.get("reversal_action", "tighten_trailing")
    if reversal_action not in ("notify_only", "tighten_trailing", "auto_close"):
        reversal_action = "tighten_trailing"
    merged["reversal_action"] = reversal_action
    reversal_pnl_filter = merged.get("reversal_pnl_filter", "always")
    if reversal_pnl_filter not in ("profit_only", "always"):
        reversal_pnl_filter = "always"
    merged["reversal_pnl_filter"] = reversal_pnl_filter

    return merged


async def get_runtime_strategy_config(redis: aioredis.Redis) -> dict[str, Any]:
    raw = await redis.get(RUNTIME_STRATEGY_KEY)
    if not raw:
        config = _normalize_config(DEFAULT_AUTO_SHORT_CONFIG)
        await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(config))
        return config

    try:
        loaded = json.loads(raw)
        config = _normalize_config(loaded)
        return config
    except Exception:
        config = _normalize_config(DEFAULT_AUTO_SHORT_CONFIG)
        await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(config))
        return config


async def save_runtime_strategy_config(
    redis: aioredis.Redis,
    config: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_config(config)
    await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(normalized))
    return normalized


async def patch_runtime_strategy_config(
    redis: aioredis.Redis,
    patch: dict[str, Any],
) -> dict[str, Any]:
    current = await get_runtime_strategy_config(redis)
    current.update(patch or {})
    normalized = _normalize_config(current)
    await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(normalized))
    return normalized


async def reset_runtime_strategy_config(redis: aioredis.Redis) -> dict[str, Any]:
    config = _normalize_config(DEFAULT_AUTO_SHORT_CONFIG)
    await redis.set(RUNTIME_STRATEGY_KEY, json.dumps(config))
    return config