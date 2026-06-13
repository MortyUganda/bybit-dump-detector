"""
Runtime-конфигурация Real-shorts (реальная торговля Bybit) в Redis.

Аналог ml_short_config.py / runtime_config.py. Редактируется через Telegram
без рестарта. БЕЗОПАСНЫЕ ДЕФОЛТЫ: real_enabled=False, testnet=True.

Real-shorts зеркалит ml_short 1:1 — здесь только параметры ИСПОЛНЕНИЯ
(сайзинг/риск/TP-SL), решение об открытии принимает ml_short.
"""
from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

RUNTIME_REAL_SHORT_KEY = "runtime_config:real_short"

# Безопасные дефолты для реальных денег:
#  - выключено;
#  - testnet (не боевой API);
#  - маленькая фикс. маржа;
#  - дневной стоп-лосс и лимит позиций включены.
DEFAULT_REAL_SHORT_CONFIG: dict[str, Any] = {
    "real_enabled": False,        # КРИТИЧНО: по умолчанию выключено
    "real_testnet": True,         # КРИТИЧНО: по умолчанию testnet
    "real_margin_usdt": 20.0,     # фикс. маржа на сделку
    "real_leverage": 10,          # плечо (как в проекте)
    "real_tp_pct": 1.0,           # движение цены для TP (P&L = ×leverage)
    "real_sl_pct": 1.0,           # движение цены для SL
    "real_trailing_enabled": False,  # задел под трейлинг (пока не реализован)
    "real_trailing_pct": 0.5,
    "real_max_open_positions": 3,    # лимит одновременных реальных позиций
    "real_max_daily_loss_usdt": 50.0,  # дневной стоп-лосс (kill-switch)
    "real_cooldown_sec": 60,         # антиспам по входам на один символ
    "real_allow_symbols": [],        # whitelist (пусто = все разрешены)
    "real_block_symbols": [],        # blacklist
}


def _as_symbol_list(value: Any) -> list[str]:
    """Нормализовать allow/block список символов (list или CSV-строка)."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    return sorted({str(s).strip().upper() for s in items if str(s).strip()})


def _normalize_real_short_config(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = DEFAULT_REAL_SHORT_CONFIG.copy()
    merged.update(config or {})

    merged["real_enabled"] = bool(merged.get("real_enabled", False))
    merged["real_testnet"] = bool(merged.get("real_testnet", True))

    # Маржа: 1..10000 USDT
    merged["real_margin_usdt"] = max(1.0, min(10000.0, float(merged.get("real_margin_usdt", 20.0))))
    # Плечо: 1..50
    merged["real_leverage"] = max(1, min(50, int(merged.get("real_leverage", 10))))
    # TP/SL: 0.1..50 % движения цены
    merged["real_tp_pct"] = max(0.1, min(50.0, float(merged.get("real_tp_pct", 1.0))))
    merged["real_sl_pct"] = max(0.1, min(50.0, float(merged.get("real_sl_pct", 1.0))))

    merged["real_trailing_enabled"] = bool(merged.get("real_trailing_enabled", False))
    merged["real_trailing_pct"] = max(0.1, min(50.0, float(merged.get("real_trailing_pct", 0.5))))

    # Риск-лимиты. 0 = без лимита для позиций; для дневного убытка 0 = выключено.
    merged["real_max_open_positions"] = max(0, min(100, int(merged.get("real_max_open_positions", 3))))
    merged["real_max_daily_loss_usdt"] = max(0.0, float(merged.get("real_max_daily_loss_usdt", 50.0)))
    merged["real_cooldown_sec"] = max(0, min(3600, int(merged.get("real_cooldown_sec", 60))))

    merged["real_allow_symbols"] = _as_symbol_list(merged.get("real_allow_symbols"))
    merged["real_block_symbols"] = _as_symbol_list(merged.get("real_block_symbols"))

    return merged


async def get_real_short_config(redis: aioredis.Redis) -> dict[str, Any]:
    raw = await redis.get(RUNTIME_REAL_SHORT_KEY)
    if not raw:
        config = _normalize_real_short_config(DEFAULT_REAL_SHORT_CONFIG)
        await redis.set(RUNTIME_REAL_SHORT_KEY, json.dumps(config))
        return config

    try:
        loaded = json.loads(raw)
        return _normalize_real_short_config(loaded)
    except Exception:
        config = _normalize_real_short_config(DEFAULT_REAL_SHORT_CONFIG)
        await redis.set(RUNTIME_REAL_SHORT_KEY, json.dumps(config))
        return config


async def save_real_short_config(
    redis: aioredis.Redis,
    config: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_real_short_config(config)
    await redis.set(RUNTIME_REAL_SHORT_KEY, json.dumps(normalized))
    return normalized


async def patch_real_short_config(
    redis: aioredis.Redis,
    patch: dict[str, Any],
) -> dict[str, Any]:
    current = await get_real_short_config(redis)
    current.update(patch or {})
    normalized = _normalize_real_short_config(current)
    await redis.set(RUNTIME_REAL_SHORT_KEY, json.dumps(normalized))
    return normalized
