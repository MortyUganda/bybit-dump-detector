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
    "adverse_move_threshold_pct": 0.2,
    "trade_monitor_interval": 5,
    "max_trade_duration_sec": 0,  # disabled: автозакрытие по таймауту отключено
    "shadow_trades_enabled": True,
    # ML decision model
    "ml_decision_enabled": True,
    "ml_decision_threshold": 0.50,
    # BTC trend filter settings
    "btc_filter_enabled": True,
    "btc_filter_change_15m_threshold": 0.5,
    "btc_filter_change_1h_threshold": 1.0,
    "btc_filter_mode": "any",
    # BTC 24h trend filter — блокировка при сильном движении BTC за 24ч
    "btc_24h_filter_enabled": True,
    "btc_24h_filter_threshold_up_pct": 5.0,
    "btc_24h_filter_threshold_down_pct": 0.0,
    # Symbol loss cooldown — блокировка входа после серии убытков
    "symbol_loss_cooldown_enabled": True,
    "symbol_loss_cooldown_count": 2,
    "symbol_loss_cooldown_hours": 24,
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
    merged["adverse_move_threshold_pct"] = max(
        0.0, float(merged.get("adverse_move_threshold_pct", 0.2))
    )
    merged["trade_monitor_interval"] = max(
        1, int(merged.get("trade_monitor_interval", 5))
    )
    # 0 = disabled (автозакрытие по таймауту отключено)
    merged["max_trade_duration_sec"] = int(merged.get("max_trade_duration_sec", 0))
    merged["shadow_trades_enabled"] = bool(merged.get("shadow_trades_enabled", True))

    # ML decision model
    merged["ml_decision_enabled"] = bool(merged.get("ml_decision_enabled", True))
    merged["ml_decision_threshold"] = max(
        0.0, min(1.0, float(merged.get("ml_decision_threshold", 0.50)))
    )

    # BTC trend filter settings
    merged["btc_filter_enabled"] = bool(merged.get("btc_filter_enabled", True))
    merged["btc_filter_change_15m_threshold"] = max(
        0.0, float(merged.get("btc_filter_change_15m_threshold", 0.5))
    )
    merged["btc_filter_change_1h_threshold"] = max(
        0.0, float(merged.get("btc_filter_change_1h_threshold", 1.0))
    )
    btc_mode = merged.get("btc_filter_mode", "any")
    if btc_mode not in ("any", "both"):
        btc_mode = "any"
    merged["btc_filter_mode"] = btc_mode

    # BTC 24h trend filter
    merged["btc_24h_filter_enabled"] = bool(
        merged.get("btc_24h_filter_enabled", True)
    )
    merged["btc_24h_filter_threshold_up_pct"] = max(
        0.0, float(merged.get("btc_24h_filter_threshold_up_pct", 5.0))
    )
    merged["btc_24h_filter_threshold_down_pct"] = max(
        0.0, float(merged.get("btc_24h_filter_threshold_down_pct", 0.0))
    )

    # Symbol loss cooldown
    merged["symbol_loss_cooldown_enabled"] = bool(
        merged.get("symbol_loss_cooldown_enabled", True)
    )
    merged["symbol_loss_cooldown_count"] = max(
        1, int(merged.get("symbol_loss_cooldown_count", 2))
    )
    merged["symbol_loss_cooldown_hours"] = max(
        1, int(merged.get("symbol_loss_cooldown_hours", 24))
    )

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