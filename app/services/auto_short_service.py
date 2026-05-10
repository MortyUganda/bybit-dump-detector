"""
AutoShortService — автоматически открывает paper short при сигнале,
мониторит цену и закрывает по TP / SL / времени,
сохраняет метрики в БД для дальнейшего обучения.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.scoring.engine import RiskScore
from app.services.runtime_config import get_runtime_strategy_config
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── ML decision model ────────────────────────────────────────────
# Путь к .pkl больше НЕ хардкожен — выбор делает app.services.ml_model_loader
# по манифесту / mtime. Две константы оставлены как aliases для обратной совместимости
# (их импортируют некоторые модули; сам файл может не существовать).
DECISION_MODEL_PATH = Path("models/decision_model.pkl")
DECISION_MODEL_FEATURES_PATH = Path("models/decision_model_features.json")

# Фичи, совпадающие с COMMON_FEATURES из scripts/train_decision_model.py — хардкод-fallback
ML_DECISION_FEATURES = [
    "score",
    "f_rsi", "f_rsi_5m", "f_vwap_extension", "f_volume_zscore",
    "f_trade_imbalance", "f_large_buy_cluster", "f_large_sell_cluster",
    "f_price_acceleration", "f_consecutive_greens", "f_ob_bid_thinning",
    "f_spread_expansion", "f_momentum_loss", "f_upper_wick", "f_funding_rate",
    "f_cvd_divergence", "f_liquidation_cascade",
    "realized_vol_1h", "volume_24h_usdt",
    "price_change_5m", "price_change_1h", "spread_pct",
    "bid_depth_change_5m", "btc_change_15m",
    "funding_rate_at_signal", "oi_change_pct_at_signal", "trend_strength_1h",
    "ob_bid_volume_top10", "ob_ask_volume_top10",
    "ob_imbalance_top10", "ob_spread_bps",
    "ob_bid_wall_price", "ob_bid_wall_size",
    "ob_ask_wall_price", "ob_ask_wall_size",
    "spread_pct_z", "bid_depth_change_5m_z", "realized_vol_1h_z",
    "volume_24h_usdt_z", "oi_change_pct_z",
    "btc_change_1h", "btc_change_4h", "btc_change_24h",
    "btc_adx_1h", "btc_atr_pct_1h",
    "recent_wr_20",
    "adverse_move_pct",
    # Engineered: time-of-day (Group 2)
    "hour_of_day",
    "day_of_week",
    "session",
    "is_weekend",
    # Engineered: symbol-specific (Group 1) — из Redis
    "symbol_recent_wr_20",
    "symbol_recent_wr_5",
    "symbol_trades_count_24h",
    "symbol_avg_pnl_5",
]

# ── Fallback defaults ─────────────────────────────────────────────

LEVERAGE = 10
TARGET_PNL_PCT = 20.0
TARGET_SL_PCT = 10.0

ENTRY_DELAY_SEC = 30
MONITOR_ATTEMPTS = 24
MONITOR_INTERVAL_SEC = 5
MIN_SCORE_TO_ENTER = 55
STABILIZATION_THRESHOLD_PCT = 0.2
MAX_RISE_PCT = 0.8
MAX_ENTRY_DROP_PCT = -0.3

TRADE_MONITOR_INTERVAL = 5
MAX_TRADE_DURATION = 60 * 60 * 4

REDIS_ACTIVE_SHORTS_KEY = "active_shorts"
REDIS_SHADOW_PAPER_KEY = "shadow_paper"

SHADOW_TP_PCT = 10.0   # TP на марже для shadow-paper
SHADOW_SL_PCT = 10.0   # SL на марже для shadow-paper
SHADOW_MONITOR_INTERVAL = 30  # секунд между проверками shadow-paper

REDIS_RECENT_WR_KEY = "recent_wr_20"
REDIS_RECENT_WR_TTL = 60  # 60s кеш


async def get_recent_wr_20(redis_client: aioredis.Redis) -> float:
    """Winrate последних 20 закрытых auto_shorts (кеш Redis 60s)."""
    cached = await redis_client.get(REDIS_RECENT_WR_KEY)
    if cached is not None:
        try:
            return float(cached)
        except (ValueError, TypeError):
            pass
    try:
        from sqlalchemy import text
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT pnl_pct FROM auto_shorts "
                    "WHERE status='closed' "
                    "AND close_reason IN ('tp_hit','sl_hit') "
                    "ORDER BY entry_ts DESC LIMIT 20"
                )
            )
            rows = result.fetchall()
        if not rows:
            wr = 0.5
        else:
            wins = sum(1 for r in rows if r[0] is not None and r[0] > 0)
            wr = wins / len(rows)
    except Exception as e:
        logger.warning("recent_wr_20 query failed", error=str(e))
        wr = 0.5

    await redis_client.setex(REDIS_RECENT_WR_KEY, REDIS_RECENT_WR_TTL, str(wr))
    return wr


def _serialize_trade(trade: dict[str, Any]) -> str:
    data = dict(trade)
    if isinstance(data.get("entry_ts"), datetime):
        data["entry_ts"] = data["entry_ts"].isoformat()
    return json.dumps(data)


def _deserialize_trade(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if isinstance(data.get("entry_ts"), str):
        data["entry_ts"] = datetime.fromisoformat(data["entry_ts"])
    return data


class AutoShortService:
    def __init__(self, redis: aioredis.Redis, bot=None, rest_client=None) -> None:
        self._redis = redis
        self._bot = bot
        self._rest_client = rest_client
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._trade_tasks: dict[int, asyncio.Task] = {}
        self._pending_symbols: set[str] = set()
        self._price_cache: dict[str, float] = {}
        self._shadow_paper_tasks: set[asyncio.Task] = set()
        # ML decision model (lazy init)
        self._ml_decision_model: Any = None
        self._ml_model_loaded: bool = False
        self._ml_model_warned: bool = False
        self._ml_disabled_logged: bool = False
        self._ml_model_path: Path | None = None
        # Список фичей из манифеста (или хардкод-fallback)
        self._ml_features: list[str] = ML_DECISION_FEATURES
        self._ml_features_warned: set[str] = set()

    # ── ML decision model ──────────────────────────────────────────

    def _ensure_ml_model(self) -> Any:
        """Lazy-load ML decision model. Возвращает модель или None."""
        if self._ml_model_loaded:
            return self._ml_decision_model
        self._ml_model_loaded = True
        from app.services.ml_model_loader import load_decision_model
        model, features, path = load_decision_model(ML_DECISION_FEATURES)
        self._ml_decision_model = model
        self._ml_features = features
        self._ml_model_path = path
        # Публикуем имя загруженного .pkl в Redis, чтобы TG-бот (в другом процессе)
        # мог показать сравнение «в памяти vs на диске».
        if path is not None:
            try:
                import asyncio as _asyncio
                _asyncio.create_task(
                    self._redis.set("ml:current_model:path", path.name)
                )
            except Exception:
                pass
        return self._ml_decision_model

    def reload_decision_model(self) -> tuple[Any, Path | None]:
        """
        Принудительная перезагрузка модели с диска без рестарта процесса.
        Используется TG-кнопкой «Перезагрузить модель». Возвращает (model, path).
        """
        self._ml_decision_model = None
        self._ml_model_loaded = False
        self._ml_model_warned = False
        model = self._ensure_ml_model()
        return model, self._ml_model_path

    async def _get_symbol_stats(self, symbol: str) -> dict[str, str]:
        """Читает Group 1 stats из Redis (sync fallback на дефолты)."""
        try:
            stats = await self._redis.hgetall(f"ml_features:symbol_stats:{symbol}")
            if stats:
                return stats
        except Exception:
            pass
        return {}

    def _build_ml_features(
        self,
        risk_score: RiskScore,
        ob_snap: dict | None,
        adverse_move_pct: float | None,
        symbol_stats: dict[str, str] | None = None,
    ) -> list[float]:
        """Собирает вектор фичей для инференса decision-модели.

        Все фичи собираются в dict {name: value}, потом возвращается
        вектор в порядке self._ml_features (из манифеста или хардкода).
        """
        features = risk_score.features_snapshot
        factor_map = {f.name: f.raw_value for f in risk_score.factors}
        now = datetime.now(timezone.utc)
        ss = symbol_stats or {}

        def _f(val: Any) -> float:
            if val is None:
                return 0.0
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        # Собираем ВСЕ возможные фичи в dict
        all_features: dict[str, float] = {
            "score": _f(risk_score.score),
            # factor-based features
            "f_rsi": _f(factor_map.get("rsi_1m") or factor_map.get("rsi")),
            "f_rsi_5m": _f(factor_map.get("rsi_5m")),
            "f_vwap_extension": _f(factor_map.get("vwap_extension")),
            "f_volume_zscore": _f(factor_map.get("volume_zscore")),
            "f_trade_imbalance": _f(factor_map.get("trade_imbalance")),
            "f_large_buy_cluster": _f(factor_map.get("large_buy_cluster")),
            "f_large_sell_cluster": _f(factor_map.get("large_sell_cluster")),
            "f_price_acceleration": _f(factor_map.get("price_acceleration")),
            "f_consecutive_greens": _f(factor_map.get("consecutive_greens")),
            "f_ob_bid_thinning": _f(factor_map.get("ob_bid_thinning")),
            "f_spread_expansion": _f(factor_map.get("spread_expansion")),
            "f_momentum_loss": _f(factor_map.get("momentum_loss")),
            "f_upper_wick": _f(factor_map.get("upper_wick")),
            "f_funding_rate": _f(factor_map.get("funding_rate")),
            "f_cvd_divergence": _f(factor_map.get("cvd_divergence")),
            "f_liquidation_cascade": _f(factor_map.get("liquidation_cascade")),
            # features from CoinFeatures
            "realized_vol_1h": _f(features.realized_vol_1h if features else None),
            "volume_24h_usdt": _f(features.volume_24h_usdt if features else None),
            "price_change_5m": _f(features.price_change_5m if features else None),
            "price_change_1h": _f(features.price_change_1h if features else None),
            "spread_pct": _f(features.spread_pct if features else None),
            "bid_depth_change_5m": _f(features.bid_depth_change_5m if features else None),
            "btc_change_15m": _f(features.btc_change_15m if features else None),
            "funding_rate_at_signal": _f(features.funding_rate if features else None),
            "oi_change_pct_at_signal": _f(features.oi_change_pct if features else None),
            "trend_strength_1h": _f(features.trend_context.trend_strength if features and features.trend_context else None),
            # OB snapshot features
            "ob_bid_volume_top10": _f(ob_snap.get("bid_volume_top10") if ob_snap else None),
            "ob_ask_volume_top10": _f(ob_snap.get("ask_volume_top10") if ob_snap else None),
            "ob_imbalance_top10": _f(ob_snap.get("imbalance_top10") if ob_snap else None),
            "ob_spread_bps": _f(ob_snap.get("spread_bps") if ob_snap else None),
            "ob_bid_wall_price": _f(ob_snap.get("bid_wall_price") if ob_snap else None),
            "ob_bid_wall_size": _f(ob_snap.get("bid_wall_size") if ob_snap else None),
            "ob_ask_wall_price": _f(ob_snap.get("ask_wall_price") if ob_snap else None),
            "ob_ask_wall_size": _f(ob_snap.get("ask_wall_size") if ob_snap else None),
            # Z-score features
            "spread_pct_z": _f(getattr(features, 'spread_pct_z', None) if features else None),
            "bid_depth_change_5m_z": _f(getattr(features, 'bid_depth_change_5m_z', None) if features else None),
            "realized_vol_1h_z": _f(getattr(features, 'realized_vol_1h_z', None) if features else None),
            "volume_24h_usdt_z": _f(getattr(features, 'volume_24h_usdt_z', None) if features else None),
            "oi_change_pct_z": _f(getattr(features, 'oi_change_pct_z', None) if features else None),
            # BTC regime features
            "btc_change_1h": _f(getattr(features, 'btc_change_1h', None) if features else None),
            "btc_change_4h": _f(getattr(features, 'btc_change_4h', None) if features else None),
            "btc_change_24h": _f(getattr(features, 'btc_change_24h', None) if features else None),
            "btc_adx_1h": _f(getattr(features, 'btc_adx_1h', None) if features else None),
            "btc_atr_pct_1h": _f(getattr(features, 'btc_atr_pct_1h', None) if features else None),
            # Context
            "recent_wr_20": _f(getattr(features, 'recent_wr_20', None) if features else None),
            # Adverse move
            "adverse_move_pct": _f(adverse_move_pct),
            # Engineered: time-of-day (Group 2)
            "hour_of_day": _f(now.hour),
            "day_of_week": _f(now.weekday()),
            "session": _f(0 if now.hour < 8 else 1 if now.hour < 13 else 2 if now.hour < 17 else 3),
            "is_weekend": _f(1 if now.weekday() >= 5 else 0),
            # Engineered: symbol-specific (Group 1) — из Redis
            "symbol_recent_wr_20": _f(ss.get("recent_wr_20", 0.5)),
            "symbol_recent_wr_5": _f(ss.get("recent_wr_5", 0.5)),
            "symbol_trades_count_24h": _f(ss.get("trades_count_24h", 0)),
            "symbol_avg_pnl_5": _f(ss.get("avg_pnl_5", 0.0)),
        }

        # Вернуть вектор в порядке self._ml_features (из манифеста)
        result: list[float] = []
        for name in self._ml_features:
            if name in all_features:
                result.append(all_features[name])
            else:
                result.append(0.0)
                if name not in self._ml_features_warned:
                    self._ml_features_warned.add(name)
                    logger.warning(
                        "Фича из манифеста отсутствует в коде — fallback 0.0",
                        feature=name,
                    )
        return result

    async def _ml_gate_check(
        self,
        risk_score: RiskScore,
        ob_snap: dict | None,
        adverse_move_pct: float | None,
    ) -> tuple[bool, float | None]:
        """ML-gate: проверяет уверенность decision-модели.

        Returns (passed, proba).
        passed=True означает что сделку можно открывать.
        Если модели нет — fail-open (passed=True, proba=None).
        Если ml_decision_enabled=False — пропускаем инференс полностью.
        """
        strategy = await self._get_strategy()
        if not strategy.get("ml_decision_enabled", True):
            if not getattr(self, "_ml_disabled_logged", False):
                logger.info("🤖 ML-gate выключен через runtime_config")
                self._ml_disabled_logged = True
            return True, None

        model = self._ensure_ml_model()
        if model is None:
            return True, None

        try:
            import numpy as np
            symbol_stats = await self._get_symbol_stats(risk_score.symbol)
            feature_vec = self._build_ml_features(
                risk_score, ob_snap, adverse_move_pct, symbol_stats,
            )
            X = np.array([feature_vec], dtype=np.float64)
            proba = float(model.predict_proba(X)[0][1])
            logger.debug(
                "Group 1 stats: wr20=%s, wr5=%s, count24h=%s, avg_pnl5=%s",
                symbol_stats.get("recent_wr_20", "miss"),
                symbol_stats.get("recent_wr_5", "miss"),
                symbol_stats.get("trades_count_24h", "miss"),
                symbol_stats.get("avg_pnl_5", "miss"),
                symbol=risk_score.symbol,
            )
        except Exception as exc:
            logger.warning(
                "ML-gate inference failed — fail-open",
                symbol=risk_score.symbol,
                error=str(exc),
            )
            return True, None

        strategy = await self._get_strategy()
        threshold = float(strategy.get("ml_decision_threshold", 0.50))
        passed = proba >= threshold
        verdict = "PASS" if passed else "REJECT"

        logger.info(
            "🤖 ML-gate: %s proba=%.2f (threshold=%.2f) → %s",
            risk_score.symbol,
            proba,
            threshold,
            verdict,
            symbol=risk_score.symbol,
            proba=round(proba, 4),
            threshold=threshold,
            verdict=verdict,
        )
        return passed, proba

    # ── Runtime strategy config ───────────────────────────────────

    async def _get_strategy(self) -> dict[str, Any]:
        return await get_runtime_strategy_config(self._redis)

    async def _get_entry_delay_sec(self) -> int:
        cfg = await self._get_strategy()
        return int(cfg.get("entry_delay_sec", ENTRY_DELAY_SEC))

    async def _get_monitor_attempts(self) -> int:
        cfg = await self._get_strategy()
        return int(cfg.get("monitor_attempts", MONITOR_ATTEMPTS))

    async def _get_monitor_interval_sec(self) -> int:
        cfg = await self._get_strategy()
        return int(cfg.get("monitor_interval_sec", MONITOR_INTERVAL_SEC))

    async def _get_min_score_to_enter(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("min_score_to_enter", MIN_SCORE_TO_ENTER))

    async def _get_stabilization_threshold_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(
            cfg.get("stabilization_threshold_pct", STABILIZATION_THRESHOLD_PCT)
        )

    async def _get_max_rise_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("max_rise_pct", MAX_RISE_PCT))

    async def _get_max_entry_drop_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("max_entry_drop_pct", MAX_ENTRY_DROP_PCT))

    async def _get_adverse_move_threshold_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("adverse_move_threshold_pct", 0.2))

    async def _get_leverage(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("leverage", LEVERAGE))

    async def _get_target_pnl_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("target_pnl_pct", TARGET_PNL_PCT))

    async def _get_target_sl_pct(self) -> float:
        cfg = await self._get_strategy()
        return float(cfg.get("target_sl_pct", TARGET_SL_PCT))

    async def _get_trade_monitor_interval(self) -> int:
        cfg = await self._get_strategy()
        return int(cfg.get("trade_monitor_interval", TRADE_MONITOR_INTERVAL))

    async def _get_max_trade_duration(self) -> int:
        cfg = await self._get_strategy()
        return int(cfg.get("max_trade_duration_sec", MAX_TRADE_DURATION))

    async def _get_tp_price_move_pct(self) -> float:
        leverage = await self._get_leverage()
        target_pnl_pct = await self._get_target_pnl_pct()
        if leverage <= 0:
            leverage = LEVERAGE
        return target_pnl_pct / leverage

    async def _get_sl_price_move_pct(self) -> float:
        leverage = await self._get_leverage()
        target_sl_pct = await self._get_target_sl_pct()
        if leverage <= 0:
            leverage = LEVERAGE
        return target_sl_pct / leverage

    async def _build_tp_price_runtime(self, entry_price: float) -> float:
        tp_price_move = await self._get_tp_price_move_pct()
        return entry_price * (1 - tp_price_move / 100)

    async def _build_sl_price_runtime(self, entry_price: float) -> float:
        sl_price_move = await self._get_sl_price_move_pct()
        return entry_price * (1 + sl_price_move / 100)

    # ── Redis-backed active shorts ───────────────────────────────

    async def _get_active_short(self, trade_id: int) -> dict[str, Any] | None:
        raw = await self._redis.hget(REDIS_ACTIVE_SHORTS_KEY, str(trade_id)) # type: ignore
        if raw:
            return _deserialize_trade(raw)
        return None

    async def _set_active_short(self, trade_id: int, trade: dict[str, Any]) -> None:
        await self._redis.hset(
            REDIS_ACTIVE_SHORTS_KEY,
            str(trade_id),
            _serialize_trade(trade),
        ) # type: ignore

    async def _del_active_short(self, trade_id: int) -> None:
        await self._redis.hdel(REDIS_ACTIVE_SHORTS_KEY, str(trade_id)) # type: ignore

    async def _get_all_active_shorts(self) -> dict[int, dict[str, Any]]:
        raw_all = await self._redis.hgetall(REDIS_ACTIVE_SHORTS_KEY) # type: ignore
        result: dict[int, dict[str, Any]] = {}
        for k, v in raw_all.items():
            result[int(k)] = _deserialize_trade(v)
        return result

    def set_bot(self, bot) -> None:
        self._bot = bot

    def _get_symbol_lock(self, symbol: str) -> asyncio.Lock:
        lock = self._symbol_locks.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            self._symbol_locks[symbol] = lock
        return lock

    async def _is_symbol_already_open(self, symbol: str) -> bool:
        all_trades = await self._get_all_active_shorts()
        return any(
            trade["symbol"] == symbol and trade["status"] == "open"
            for trade in all_trades.values()
        )

    def _calc_price_move_pct(self, from_price: float, to_price: float) -> float:
        return ((to_price - from_price) / from_price) * 100

    async def _calc_short_pnl_pct(self, entry_price: float, current_price: float) -> float:
        leverage = await self._get_leverage()
        price_move_pct = ((entry_price - current_price) / entry_price) * 100
        return price_move_pct * leverage

    def _track_task(self, trade_id: int, task: asyncio.Task) -> None:
        self._trade_tasks[trade_id] = task

        def _cleanup(done_task: asyncio.Task) -> None:
            self._trade_tasks.pop(trade_id, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                logger.info("Trade monitor task cancelled", trade_id=trade_id)
            except Exception as e:
                logger.exception(
                    "Trade monitor task crashed",
                    trade_id=trade_id,
                    error=str(e),
                )

        task.add_done_callback(_cleanup)

    # ── Order book snapshot ────────────────────────────────────────

    async def _get_ob_snapshot(self, symbol: str, current_price: float) -> dict | None:
        """Fetch raw orderbook from Redis and build ML snapshot."""
        try:
            raw = await self._redis.get(f"ob:{symbol}")
            if not raw:
                return None
            from app.analytics.orderbook import make_ob_snapshot
            ob_data = json.loads(raw)
            return make_ob_snapshot(ob_data, current_price)
        except Exception as e:
            logger.debug("OB snapshot fetch failed", symbol=symbol, error=str(e))
            return None

    # ── Entry conditions ──────────────────────────────────────────

    async def _evaluate_entry_conditions(
        self,
        price_change_pct: float,
        current_score: float,
        symbol: str,
    ) -> str:
        max_entry_drop_pct = await self._get_max_entry_drop_pct()
        max_rise_pct = await self._get_max_rise_pct()
        stabilization_threshold_pct = await self._get_stabilization_threshold_pct()

        if price_change_pct < max_entry_drop_pct:
            logger.debug(
                "Entry check: price dropped too much",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                threshold=max_entry_drop_pct,
            )
            decision = "cancel_drop"
        elif price_change_pct > max_rise_pct:
            logger.debug(
                "Entry check: price rose too much",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                max_rise=max_rise_pct,
            )
            decision = "cancel_rise"
        elif price_change_pct > stabilization_threshold_pct:
            logger.debug(
                "Entry check: price still rising above stabilization threshold",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                threshold=stabilization_threshold_pct,
            )
            decision = "monitor"
        else:
            decision = "enter"

        logger.info(
            "Auto-short entry decision",
            symbol=symbol,
            decision=decision,
            price_change_pct=round(price_change_pct, 3),
            score=round(current_score, 1),
            max_entry_drop_pct=max_entry_drop_pct,
            max_rise_pct=max_rise_pct,
            stabilization_threshold_pct=stabilization_threshold_pct,
        )
        return decision

    
    # ── Current score ─────────────────────────────────────────────

    async def _get_current_score(self, symbol: str) -> float | None:
        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                score = data.get("score")
                if score is not None:
                    return float(score)
        except Exception as e:
            logger.debug("Redis score fetch failed", symbol=symbol, error=str(e))
        return None

    # ── Entry monitoring ──────────────────────────────────────────

    async def _monitor_entry(
        self,
        risk_score: RiskScore,
        symbol: str,
        signal_price: float,
        initial_score: float,
    ) -> tuple[float, float, float] | None:
        monitor_attempts = await self._get_monitor_attempts()
        monitor_interval_sec = await self._get_monitor_interval_sec()

        logger.info(
            "Price still rising — monitoring for entry",
            symbol=symbol,
            max_attempts=monitor_attempts,
            interval_sec=monitor_interval_sec,
        )

        for attempt in range(monitor_attempts):
            await asyncio.sleep(monitor_interval_sec)

            current_price = await self._get_price(symbol)
            if not current_price:
                continue

            current_score = await self._get_current_score(symbol)
            if current_score is None:
                current_score = initial_score

            price_change_pct = self._calc_price_move_pct(signal_price, current_price)

            logger.info(
                "Monitoring entry",
                symbol=symbol,
                attempt=attempt + 1,
                max_attempts=monitor_attempts,
                signal_price=signal_price,
                current_price=current_price,
                change_pct=round(price_change_pct, 3),
                current_score=round(current_score, 1),
            )

            decision = await self._evaluate_entry_conditions(
                price_change_pct=price_change_pct,
                current_score=current_score,
                symbol=symbol,
            )

            if decision == "enter":
                logger.info(
                    "Price stabilized — entering short",
                    symbol=symbol,
                    attempt=attempt + 1,
                    change_pct=round(price_change_pct, 3),
                    score=round(current_score, 1),
                )
                return current_price, price_change_pct, float(current_score)

            if decision in ("cancel_drop", "cancel_rise"):
                mon_ob_snap = await self._get_ob_snapshot(symbol, current_price)

            if decision == "cancel_drop":
                logger.info(
                    "Canceled because price dropped too much during monitoring",
                    symbol=symbol,
                    attempt=attempt + 1,
                    change_pct=round(price_change_pct, 3),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=current_score,
                    cancel_reason="cancel_drop",
                    entry_mode_candidate="after_monitor",
                    ob_snapshot_data=mon_ob_snap,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=current_price,
                    price_change_pct=price_change_pct,
                    score=current_score,
                    reason="price_dropped",
                )
                return None

            if decision == "cancel_rise":
                logger.info(
                    "Canceled because price rose too much",
                    symbol=symbol,
                    attempt=attempt + 1,
                    change_pct=round(price_change_pct, 3),
                    max_rise=await self._get_max_rise_pct(),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=current_score,
                    cancel_reason="cancel_rise",
                    entry_mode_candidate="after_monitor",
                    ob_snapshot_data=mon_ob_snap,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=current_price,
                    price_change_pct=price_change_pct,
                    score=current_score,
                    reason="price_too_high",
                )
                return None

        total_sec = monitor_attempts * monitor_interval_sec
        logger.info(
            "Canceled because monitoring timeout",
            symbol=symbol,
            attempts=monitor_attempts,
            total_sec=total_sec,
        )

        last_price = await self._get_price(symbol)
        last_score = await self._get_current_score(symbol) or initial_score
        last_change = (
            self._calc_price_move_pct(signal_price, last_price)
            if last_price
            else 0.0
        )
        timeout_ob_snap = await self._get_ob_snapshot(symbol, last_price or signal_price)

        await self._save_canceled_signal(
            risk_score=risk_score,
            signal_price=signal_price,
            final_price=last_price or current_price,
            price_change_pct=last_change,
            final_score=last_score,
            cancel_reason="timeout",
            entry_mode_candidate="after_monitor",
            ob_snapshot_data=timeout_ob_snap,
        )
        await self._notify_entry_canceled(
            symbol=symbol,
            signal_price=signal_price,
            entry_price=last_price or current_price,
            price_change_pct=last_change,
            score=last_score,
            reason="timeout",
        )
        return None
    # ── Notify canceled entry ─────────────────────────────────────

    async def _notify_entry_canceled(
        self,
        symbol: str,
        signal_price: float,
        entry_price: float,
        price_change_pct: float,
        score: float,
        reason: str,
        adverse_move_pct: float | None = None,
    ) -> None:
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
            max_entry_drop_pct = await self._get_max_entry_drop_pct()
            max_rise_pct = await self._get_max_rise_pct()
            monitor_attempts = await self._get_monitor_attempts()
            monitor_interval_sec = await self._get_monitor_interval_sec()

            reason_details = {
                "price_dropped": (
                    f"📉 Изменение: <b>{price_change_pct:+.2f}%</b> "
                    f"(порог {max_entry_drop_pct}%)\n\n"
                    f"<i>Цена уже упала — движение произошло без нас</i>"
                ),
                "price_too_high": (
                    f"📈 Рост: <b>+{abs(price_change_pct):.2f}%</b> "
                    f"(порог +{max_rise_pct}%)\n\n"
                    f"<i>Памп слишком сильный — вход отменён во избежание риска</i>"
                ),
                "timeout": (
                    f"📈 Изменение: <b>{price_change_pct:+.2f}%</b>\n"
                    f"⏱ Мониторинг: {monitor_attempts} × {monitor_interval_sec}с "
                    f"({monitor_attempts * monitor_interval_sec}с)\n\n"
                    f"<i>Стабилизация не наступила — вход отменён по таймауту</i>"
                ),
            }

            # Adverse move reason
            adverse_threshold = await self._get_adverse_move_threshold_pct()
            adverse_str = f"{adverse_move_pct:+.2f}%" if adverse_move_pct is not None else "N/A"
            reason_details["adverse_move"] = (
                f"📈 Движение против позиции: <b>{adverse_str}</b> "
                f"(порог +{adverse_threshold}%)\n\n"
                f"<i>Цена выросла за время delay — вход в шорт отменён</i>"
            )

            # Trend filter reason
            reason_details["trend_filter_blocked"] = (
                "📈 Trend filter заблокировал вход\n\n"
                "<i>Обнаружен сильный аптренд (BTC / price / RSI / свечи) — шорт опасен</i>"
            )

            detail = reason_details.get(reason, f"<i>Причина: {reason}</i>")

            text = (
                f"⏭ <b>Сигнал пропущен</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"📊 Score: <b>{score:.0f}</b>\n\n"
                f"📍 Цена сигнала: <b>${signal_price:.6g}</b>\n"
                f"📍 Текущая цена: <b>${entry_price:.6g}</b>\n"
                f"{detail}"
            )

            for user_id in user_ids:
                try:
                    await asyncio.wait_for(
                        self._bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Entry cancel notify TIMEOUT (>10s)",
                        user_id=user_id,
                        symbol=symbol,
                    )
                except Exception as e:
                    logger.warning(
                        "Entry cancel notify failed",
                        user_id=user_id,
                        error=str(e),
                    )

            logger.info(
                "Entry cancel notification sent",
                symbol=symbol,
                reason=reason,
                change_pct=round(price_change_pct, 3),
            )

        except Exception as e:
            logger.error("Entry cancel notification error", error=str(e))

    # ── Restore open trades ───────────────────────────────────────

    async def restore_active_trades(self) -> None:
        # Разовый backfill synthetic PnL для потерянных при рестарте воркеров
        try:
            await self._backfill_canceled_signals_pnl()
        except Exception as e:
            logger.warning("Initial canceled signals backfill failed", error=str(e))
        # Периодический backfill раз в 5 минут
        if not hasattr(self, "_canceled_backfill_task") or self._canceled_backfill_task.done():
            self._canceled_backfill_task = asyncio.create_task(self._canceled_backfill_loop())

        # Восстанавливаем shadow-paper и запускаем мониторинг
        try:
            await self.restore_shadow_paper_trades()
        except Exception as e:
            logger.warning("Shadow-paper restore failed", error=str(e))

        try:
            from sqlalchemy import select
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            await self._redis.delete(REDIS_ACTIVE_SHORTS_KEY)

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(AutoShort).where(AutoShort.status == "open")
                )
                open_trades = result.scalars().all()

            if not open_trades:
                logger.info("No open trades to restore")
                return

            logger.info("Restoring open trades", count=len(open_trades))
            restored_count = 0
            # Автозакрытие по таймауту при рестарте отключено:
            # expired давал ~16% шумных сделок, мешал ML.
            # Все открытые позиции восстанавливаются как есть.

            for trade in open_trades:
                now = datetime.now(timezone.utc)
                elapsed = (now - trade.entry_ts).total_seconds()

                trade_payload = {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "entry_price": trade.entry_price,
                    "tp_price": trade.tp_price,
                    "sl_price": trade.sl_price,
                    "entry_ts": trade.entry_ts,
                    "status": "open",
                    "close_reason": None,
                    "price_15m_saved": trade.price_15m is not None,
                    "price_30m_saved": trade.price_30m is not None,
                    "price_60m_saved": trade.price_60m is not None,
                }
                await self._set_active_short(trade.id, trade_payload)
                restored_count += 1

                logger.info(
                    "Trade restored",
                    trade_id=trade.id,
                    symbol=trade.symbol,
                    entry=trade.entry_price,
                    elapsed_min=int(elapsed / 60),
                )

                task = asyncio.create_task(self._monitor_trade(trade.id))
                self._track_task(trade.id, task)

            logger.info(
                "Trades restored",
                count=len(open_trades),
                active=restored_count,
            )

        except Exception as e:
            logger.exception("Failed to restore trades", error=str(e))


    async def _maybe_save_shadow_trade(
        self,
        risk_score: RiskScore,
        cancel_reason: str,
    ) -> None:
        """
        Сохраняет shadow trade — сигнал, который отфильтрован по служебной логике
        (уже открытый символ, дубль, стратегия выкл., тип отключён).
        Синтетический PnL посчитается автоматически через _track_canceled_signal_price.
        """
        try:
            strategy = await self._get_strategy()
            if not strategy.get("shadow_trades_enabled", True):
                return

            symbol = risk_score.symbol
            signal_price = await self._get_price(symbol)
            if not signal_price and risk_score.features_snapshot:
                signal_price = risk_score.features_snapshot.last_price
            if not signal_price:
                return

            ob_snap = await self._get_ob_snapshot(symbol, float(signal_price))

            await self._save_canceled_signal(
                risk_score=risk_score,
                signal_price=float(signal_price),
                final_price=float(signal_price),
                price_change_pct=0.0,
                final_score=float(risk_score.score),
                cancel_reason=cancel_reason,
                entry_mode_candidate="shadow",
                ob_snapshot_data=ob_snap,
            )
        except Exception as e:
            logger.debug(
                "Shadow trade save failed",
                cancel_reason=cancel_reason,
                error=str(e),
            )

    # ── Shadow-paper: golden dataset all_opened_signals ──────────

    async def _create_shadow_paper_signal(
        self,
        risk_score: RiskScore,
        would_have_opened: bool,
        actual_blocked_by: str | None,
        linked_auto_short_id: int | None = None,
        linked_canceled_signal_id: int | None = None,
        ob_snapshot_data: dict | None = None,
        adverse_move_pct: float | None = None,
    ) -> None:
        """Создаёт shadow-paper запись в all_opened_signals.

        Вызывается ровно один раз для КАЖДОГО risk_score, независимо
        от того, прошёл ли сигнал фильтры или нет.
        TP/SL всегда 10%/10%.  Без TG-уведомлений.
        """
        try:
            from app.db.models.all_opened_signal import AllOpenedSignal
            from app.db.session import AsyncSessionLocal

            symbol = risk_score.symbol
            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

            entry_price = await self._get_price(symbol)
            if not entry_price and features:
                entry_price = features.last_price
            if not entry_price:
                logger.debug("Shadow-paper skipped — no price", symbol=symbol)
                return

            # Если OB snapshot не передан, попробуем получить сами
            if ob_snapshot_data is None:
                ob_snapshot_data = await self._get_ob_snapshot(symbol, entry_price)

            signal_price = entry_price
            leverage = LEVERAGE  # всегда 10x для shadow-paper
            tp_move = SHADOW_TP_PCT / leverage  # 1% движение цены
            sl_move = SHADOW_SL_PCT / leverage  # 1% движение цены
            tp_price = entry_price * (1 - tp_move / 100)
            sl_price = entry_price * (1 + sl_move / 100)

            row = AllOpenedSignal(
                symbol=symbol,
                signal_type=(
                    risk_score.signal_type.value
                    if risk_score.signal_type
                    else "unknown"
                ),
                would_have_opened=would_have_opened,
                actual_blocked_by=actual_blocked_by,
                linked_auto_short_id=linked_auto_short_id,
                linked_canceled_signal_id=linked_canceled_signal_id,
                entry_price=entry_price,
                signal_price=signal_price,
                leverage=leverage,
                tp_pct=SHADOW_TP_PCT,
                sl_pct=SHADOW_SL_PCT,
                tp_price=tp_price,
                sl_price=sl_price,
                score=float(risk_score.score),
                entry_score=float(risk_score.score),
                triggered_count=risk_score.triggered_count,
                status="open",
                f_rsi=factor_map.get("rsi_1m") or factor_map.get("rsi"),
                f_vwap_extension=factor_map.get("vwap_extension"),
                f_volume_zscore=factor_map.get("volume_zscore"),
                f_trade_imbalance=factor_map.get("trade_imbalance"),
                f_large_buy_cluster=factor_map.get("large_buy_cluster"),
                f_price_acceleration=factor_map.get("price_acceleration"),
                f_consecutive_greens=factor_map.get("consecutive_greens"),
                f_ob_bid_thinning=factor_map.get("ob_bid_thinning"),
                f_spread_expansion=factor_map.get("spread_expansion"),
                f_momentum_loss=factor_map.get("momentum_loss"),
                f_upper_wick=factor_map.get("upper_wick"),
                f_funding_rate=factor_map.get("funding_rate"),
                f_rsi_5m=factor_map.get("rsi_5m"),
                f_large_sell_cluster=factor_map.get("large_sell_cluster"),
                f_cvd_divergence=factor_map.get("cvd_divergence"),
                f_liquidation_cascade=factor_map.get("liquidation_cascade"),
                realized_vol_1h=features.realized_vol_1h if features else None,
                volume_24h_usdt=features.volume_24h_usdt if features else None,
                price_change_5m=features.price_change_5m if features else None,
                price_change_1h=features.price_change_1h if features else None,
                spread_pct=features.spread_pct if features else None,
                bid_depth_change_5m=features.bid_depth_change_5m if features else None,
                btc_change_15m=features.btc_change_15m if features else None,
                funding_rate_at_signal=features.funding_rate if features else None,
                oi_change_pct_at_signal=features.oi_change_pct if features else None,
                trend_strength_1h=(
                    features.trend_context.trend_strength
                    if features and features.trend_context
                    else None
                ),
                # ── OB snapshot ───────────────────────────────────
                ob_snapshot=ob_snapshot_data.get("snapshot") if ob_snapshot_data else None,
                ob_bid_volume_top10=ob_snapshot_data.get("bid_volume_top10") if ob_snapshot_data else None,
                ob_ask_volume_top10=ob_snapshot_data.get("ask_volume_top10") if ob_snapshot_data else None,
                ob_imbalance_top10=ob_snapshot_data.get("imbalance_top10") if ob_snapshot_data else None,
                ob_spread_bps=ob_snapshot_data.get("spread_bps") if ob_snapshot_data else None,
                ob_bid_wall_price=ob_snapshot_data.get("bid_wall_price") if ob_snapshot_data else None,
                ob_bid_wall_size=ob_snapshot_data.get("bid_wall_size") if ob_snapshot_data else None,
                ob_ask_wall_price=ob_snapshot_data.get("ask_wall_price") if ob_snapshot_data else None,
                ob_ask_wall_size=ob_snapshot_data.get("ask_wall_size") if ob_snapshot_data else None,
                # Z-score нормализация
                spread_pct_z=getattr(features, 'spread_pct_z', None) if features else None,
                bid_depth_change_5m_z=getattr(features, 'bid_depth_change_5m_z', None) if features else None,
                realized_vol_1h_z=getattr(features, 'realized_vol_1h_z', None) if features else None,
                volume_24h_usdt_z=getattr(features, 'volume_24h_usdt_z', None) if features else None,
                oi_change_pct_z=getattr(features, 'oi_change_pct_z', None) if features else None,
                # Режимные BTC-фичи
                btc_change_1h=getattr(features, 'btc_change_1h', None) if features else None,
                btc_change_4h=getattr(features, 'btc_change_4h', None) if features else None,
                btc_change_24h=getattr(features, 'btc_change_24h', None) if features else None,
                btc_adx_1h=getattr(features, 'btc_adx_1h', None) if features else None,
                btc_atr_pct_1h=getattr(features, 'btc_atr_pct_1h', None) if features else None,
                recent_wr_20=getattr(features, 'recent_wr_20', None) if features else None,
                adverse_move_pct=adverse_move_pct,
            )

            async with AsyncSessionLocal() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                shadow_id = row.id

            # Регистрируем в Redis для мониторинга
            shadow_data = json.dumps({
                "id": shadow_id,
                "symbol": symbol,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
            })
            await self._redis.hset(REDIS_SHADOW_PAPER_KEY, str(shadow_id), shadow_data)  # type: ignore

            logger.debug(
                "Shadow-paper signal created",
                shadow_id=shadow_id,
                symbol=symbol,
                would_have_opened=would_have_opened,
                actual_blocked_by=actual_blocked_by,
                entry_price=entry_price,
                tp_price=round(tp_price, 6),
                sl_price=round(sl_price, 6),
            )

        except Exception as e:
            logger.debug(
                "Shadow-paper signal creation failed",
                symbol=risk_score.symbol,
                error=str(e),
            )

    async def _monitor_shadow_paper_loop(self) -> None:
        """Бесконечный цикл мониторинга shadow-paper сделок.

        Проверяет все open shadow-paper по TP/SL каждые 30 секунд.
        Без таймаута — висит пока не сработает TP или SL.
        """
        from sqlalchemy import update
        from app.db.models.all_opened_signal import AllOpenedSignal
        from app.db.session import AsyncSessionLocal

        logger.info("Shadow-paper monitor loop started")

        while True:
            try:
                await asyncio.sleep(SHADOW_MONITOR_INTERVAL)

                raw_all = await self._redis.hgetall(REDIS_SHADOW_PAPER_KEY)  # type: ignore
                if not raw_all:
                    continue

                for key, raw_val in raw_all.items():
                    try:
                        data = json.loads(raw_val)
                        shadow_id = data["id"]
                        symbol = data["symbol"]
                        tp_price = data["tp_price"]
                        sl_price = data["sl_price"]
                        entry_price = data["entry_price"]

                        current_price = await self._get_price(symbol)
                        if not current_price:
                            continue

                        close_reason: str | None = None
                        pnl_pct: float | None = None
                        ml_label: int | None = None

                        # Шорт: TP когда цена падает, SL когда цена растёт
                        if current_price <= tp_price:
                            close_reason = "tp_hit"
                            pnl_pct = SHADOW_TP_PCT
                            ml_label = 1
                        elif current_price >= sl_price:
                            close_reason = "sl_hit"
                            pnl_pct = -SHADOW_SL_PCT
                            ml_label = 0

                        if close_reason:
                            now = datetime.now(timezone.utc)
                            async with AsyncSessionLocal() as session:
                                await session.execute(
                                    update(AllOpenedSignal)
                                    .where(AllOpenedSignal.id == shadow_id)
                                    .values(
                                        status="closed",
                                        close_reason=close_reason,
                                        exit_price=current_price,
                                        exit_ts=now,
                                        pnl_pct=pnl_pct,
                                        ml_label=ml_label,
                                    )
                                )
                                await session.commit()

                            await self._redis.hdel(REDIS_SHADOW_PAPER_KEY, str(shadow_id))  # type: ignore

                            logger.debug(
                                "Shadow-paper closed",
                                shadow_id=shadow_id,
                                symbol=symbol,
                                close_reason=close_reason,
                                pnl_pct=pnl_pct,
                                entry_price=entry_price,
                                exit_price=current_price,
                            )

                    except Exception as e:
                        logger.debug(
                            "Shadow-paper monitor item error",
                            key=key,
                            error=str(e),
                        )

            except asyncio.CancelledError:
                logger.info("Shadow-paper monitor loop cancelled")
                raise
            except Exception as e:
                logger.warning("Shadow-paper monitor loop error", error=str(e))

    async def restore_shadow_paper_trades(self) -> None:
        """Восстанавливает open shadow-paper сделки из БД в Redis при старте."""
        try:
            from sqlalchemy import select
            from app.db.models.all_opened_signal import AllOpenedSignal
            from app.db.session import AsyncSessionLocal

            await self._redis.delete(REDIS_SHADOW_PAPER_KEY)

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(AllOpenedSignal).where(AllOpenedSignal.status == "open")
                )
                open_shadows = result.scalars().all()

            if not open_shadows:
                logger.info("No open shadow-paper trades to restore")
            else:
                for s in open_shadows:
                    shadow_data = json.dumps({
                        "id": s.id,
                        "symbol": s.symbol,
                        "entry_price": s.entry_price,
                        "tp_price": s.tp_price,
                        "sl_price": s.sl_price,
                    })
                    await self._redis.hset(REDIS_SHADOW_PAPER_KEY, str(s.id), shadow_data)  # type: ignore
                logger.info("Shadow-paper trades restored", count=len(open_shadows))

            # Запускаем мониторинг shadow-paper
            task = asyncio.create_task(self._monitor_shadow_paper_loop())
            self._shadow_paper_tasks.add(task)
            task.add_done_callback(self._shadow_paper_tasks.discard)

        except Exception as e:
            logger.exception("Failed to restore shadow-paper trades", error=str(e))

    async def _save_canceled_signal(
        self,
        risk_score: RiskScore,
        signal_price: float,
        final_price: float,
        price_change_pct: float,
        final_score: float,
        cancel_reason: str,
        entry_mode_candidate: str = "direct",
        ob_snapshot_data: dict | None = None,
        adverse_move_pct: float | None = None,
    ) -> int | None:
        try:
            from app.db.models.canceled_signal import CanceledSignal
            from app.db.session import AsyncSessionLocal

            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

            entry_delay_sec = await self._get_entry_delay_sec()
            monitor_attempts = await self._get_monitor_attempts()
            monitor_interval_sec = await self._get_monitor_interval_sec()
            min_score_to_enter = await self._get_min_score_to_enter()
            stabilization_threshold_pct = await self._get_stabilization_threshold_pct()
            max_rise_pct = await self._get_max_rise_pct()
            max_entry_drop_pct = await self._get_max_entry_drop_pct()

            row = CanceledSignal(
                symbol=risk_score.symbol,
                signal_type=(
                    risk_score.signal_type.value
                    if risk_score.signal_type
                    else "unknown"
                ),
                cancel_reason=cancel_reason,
                signal_price=signal_price,
                final_price=final_price,
                price_change_pct=price_change_pct,
                score=float(risk_score.score),
                final_score=float(final_score),
                min_score_at_entry=float(min_score_to_enter),
                entry_mode_candidate=entry_mode_candidate,
                triggered_count=risk_score.triggered_count,
                entry_delay_sec=entry_delay_sec,
                monitor_attempts=monitor_attempts,
                monitor_interval_sec=monitor_interval_sec,
                stabilization_threshold_pct=stabilization_threshold_pct,
                max_rise_pct=max_rise_pct,
                max_entry_drop_pct=max_entry_drop_pct,
                f_rsi=factor_map.get("rsi_1m") or factor_map.get("rsi"),
                f_vwap_extension=factor_map.get("vwap_extension"),
                f_volume_zscore=factor_map.get("volume_zscore"),
                f_trade_imbalance=factor_map.get("trade_imbalance"),
                f_large_buy_cluster=factor_map.get("large_buy_cluster"),
                f_price_acceleration=factor_map.get("price_acceleration"),
                f_consecutive_greens=factor_map.get("consecutive_greens"),
                f_ob_bid_thinning=factor_map.get("ob_bid_thinning"),
                f_spread_expansion=factor_map.get("spread_expansion"),
                f_momentum_loss=factor_map.get("momentum_loss"),
                f_upper_wick=factor_map.get("upper_wick"),
                f_funding_rate=factor_map.get("funding_rate"),
                f_rsi_5m=factor_map.get("rsi_5m"),
                f_large_sell_cluster=factor_map.get("large_sell_cluster"),
                f_cvd_divergence=factor_map.get("cvd_divergence"),
                f_liquidation_cascade=factor_map.get("liquidation_cascade"),
                realized_vol_1h=features.realized_vol_1h if features else None,
                volume_24h_usdt=features.volume_24h_usdt if features else None,
                price_change_5m=features.price_change_5m if features else None,
                price_change_1h=features.price_change_1h if features else None,
                spread_pct=features.spread_pct if features else None,
                bid_depth_change_5m=features.bid_depth_change_5m if features else None,
                btc_change_15m=features.btc_change_15m if features else None,
                funding_rate_at_signal=features.funding_rate if features else None,
                oi_change_pct_at_signal=features.oi_change_pct if features else None,
                trend_strength_1h=(
                    features.trend_context.trend_strength
                    if features and features.trend_context
                    else None
                ),
                # ── OB snapshot ───────────────────────────────────
                ob_snapshot=ob_snapshot_data.get("snapshot") if ob_snapshot_data else None,
                ob_bid_volume_top10=ob_snapshot_data.get("bid_volume_top10") if ob_snapshot_data else None,
                ob_ask_volume_top10=ob_snapshot_data.get("ask_volume_top10") if ob_snapshot_data else None,
                ob_imbalance_top10=ob_snapshot_data.get("imbalance_top10") if ob_snapshot_data else None,
                ob_spread_bps=ob_snapshot_data.get("spread_bps") if ob_snapshot_data else None,
                ob_bid_wall_price=ob_snapshot_data.get("bid_wall_price") if ob_snapshot_data else None,
                ob_bid_wall_size=ob_snapshot_data.get("bid_wall_size") if ob_snapshot_data else None,
                ob_ask_wall_price=ob_snapshot_data.get("ask_wall_price") if ob_snapshot_data else None,
                ob_ask_wall_size=ob_snapshot_data.get("ask_wall_size") if ob_snapshot_data else None,
                # Z-score нормализация
                spread_pct_z=features.spread_pct_z if features else None,
                bid_depth_change_5m_z=features.bid_depth_change_5m_z if features else None,
                realized_vol_1h_z=features.realized_vol_1h_z if features else None,
                volume_24h_usdt_z=features.volume_24h_usdt_z if features else None,
                oi_change_pct_z=features.oi_change_pct_z if features else None,
                # Режимные BTC-фичи
                btc_change_1h=features.btc_change_1h if features else None,
                btc_change_4h=features.btc_change_4h if features else None,
                btc_change_24h=features.btc_change_24h if features else None,
                btc_adx_1h=features.btc_adx_1h if features else None,
                btc_atr_pct_1h=features.btc_atr_pct_1h if features else None,
                recent_wr_20=features.recent_wr_20 if features else None,
                adverse_move_pct=adverse_move_pct,
            )

            async with AsyncSessionLocal() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                canceled_id = row.id

            # Запускаем фоновый мониторинг цены для synthetic PnL
            try:
                task = asyncio.create_task(
                    self._track_canceled_signal_price(
                        canceled_id=canceled_id,
                        symbol=risk_score.symbol,
                        signal_price=signal_price,
                    )
                )
                # Сохраняем ссылку на task, чтобы GC не убил
                if not hasattr(self, "_canceled_tracker_tasks"):
                    self._canceled_tracker_tasks = set()
                self._canceled_tracker_tasks.add(task)
                task.add_done_callback(self._canceled_tracker_tasks.discard)
            except Exception as e:
                logger.warning(
                    "Failed to start canceled signal tracker",
                    canceled_id=canceled_id,
                    error=str(e),
                )

            # Shadow-paper: создаём запись в all_opened_signals
            await self._create_shadow_paper_signal(
                risk_score=risk_score,
                would_have_opened=False,
                actual_blocked_by=cancel_reason,
                linked_canceled_signal_id=canceled_id,
                adverse_move_pct=adverse_move_pct,
            )

            return canceled_id

        except Exception as e:
            logger.exception(
                "Canceled signal DB save failed",
                symbol=risk_score.symbol,
                reason=cancel_reason,
                error=str(e),
            )
            return None

    # ── Post-cancel price tracking ──────────────────────────────

    async def _track_canceled_signal_price(
        self,
        canceled_id: int,
        symbol: str,
        signal_price: float,
    ) -> None:
        """Отслеживает цену после отмены: 15/30/60 мин и min/max в окне.

        Опрашивает цену каждые 30 секунд, чтобы не терять касания TP/SL.
        По истечении 60 минут вызывает _compute_synthetic_pnl.
        """
        from sqlalchemy import update
        from app.db.models.canceled_signal import CanceledSignal
        from app.db.session import AsyncSessionLocal

        try:
            tp_pct = await self._get_tp_price_move_pct()
            sl_pct = await self._get_sl_price_move_pct()
        except Exception:
            tp_pct, sl_pct = 10.0, 10.0

        tp_price = signal_price * (1 - tp_pct / 100.0)
        sl_price = signal_price * (1 + sl_pct / 100.0)

        start_ts = datetime.now(timezone.utc)
        price_min = signal_price
        price_max = signal_price
        time_to_tp_sec: int | None = None
        time_to_sl_sec: int | None = None
        saved_15m = saved_30m = saved_60m = False
        poll_interval = 30  # секунд
        end_sec = 60 * 60

        try:
            elapsed = 0
            while elapsed < end_sec:
                await asyncio.sleep(poll_interval)
                elapsed = int((datetime.now(timezone.utc) - start_ts).total_seconds())

                price = await self._get_price(symbol)
                if price is None or price <= 0:
                    continue

                if price < price_min:
                    price_min = price
                if price > price_max:
                    price_max = price
                if time_to_tp_sec is None and price <= tp_price:
                    time_to_tp_sec = elapsed
                if time_to_sl_sec is None and price >= sl_price:
                    time_to_sl_sec = elapsed

                # Достигли следующей точки — сохраняем в БД
                updates: dict[str, Any] = {}
                now = datetime.now(timezone.utc)
                if not saved_15m and elapsed >= 15 * 60:
                    updates["price_15m"] = price
                    updates["price_15m_ts"] = now
                    saved_15m = True
                if not saved_30m and elapsed >= 30 * 60:
                    updates["price_30m"] = price
                    updates["price_30m_ts"] = now
                    saved_30m = True
                if not saved_60m and elapsed >= 60 * 60:
                    updates["price_60m"] = price
                    updates["price_60m_ts"] = now
                    saved_60m = True
                if updates:
                    try:
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(CanceledSignal)
                                .where(CanceledSignal.id == canceled_id)
                                .values(**updates)
                            )
                            await session.commit()
                    except Exception as e:
                        logger.warning(
                            "Canceled price snapshot save failed",
                            canceled_id=canceled_id,
                            error=str(e),
                        )

            # Окно закрыто — сохраняем min/max и time_to_tp/sl, считаем synthetic PnL
            try:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(CanceledSignal)
                        .where(CanceledSignal.id == canceled_id)
                        .values(
                            price_min_60m=price_min,
                            price_max_60m=price_max,
                            time_to_tp_sec=time_to_tp_sec,
                            time_to_sl_sec=time_to_sl_sec,
                        )
                    )
                    await session.commit()
            except Exception as e:
                logger.warning(
                    "Canceled min/max save failed",
                    canceled_id=canceled_id,
                    error=str(e),
                )

            await self._compute_synthetic_pnl(canceled_id)

            logger.info(
                "Canceled signal tracking finished",
                canceled_id=canceled_id,
                symbol=symbol,
                price_min=price_min,
                price_max=price_max,
                hit_tp=time_to_tp_sec is not None,
                hit_sl=time_to_sl_sec is not None,
            )

        except asyncio.CancelledError:
            logger.info(
                "Canceled signal tracker cancelled",
                canceled_id=canceled_id,
                symbol=symbol,
            )
            raise
        except Exception as e:
            logger.exception(
                "Canceled signal tracker failed",
                canceled_id=canceled_id,
                symbol=symbol,
                error=str(e),
            )

    async def _compute_synthetic_pnl(self, canceled_id: int) -> None:
        """Считает synthetic PnL для отменённого сигнала.

        Логика:
        - Если time_to_sl_sec и time_to_tp_sec оба есть — берём раньше по времени.
        - Если только точка (15/30/60) показывает касание — используем min/max для проверки.
        - Иначе по price_60m — фактический исход.
        """
        from sqlalchemy import update
        from app.db.models.canceled_signal import CanceledSignal
        from app.db.session import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as session:
                row = await session.get(CanceledSignal, canceled_id)
                if row is None:
                    return

                signal_price = row.signal_price
                if not signal_price or signal_price <= 0:
                    return

                try:
                    tp_pct = await self._get_tp_price_move_pct()
                    sl_pct = await self._get_sl_price_move_pct()
                except Exception:
                    tp_pct, sl_pct = 10.0, 10.0

                tp_price = signal_price * (1 - tp_pct / 100.0)
                sl_price = signal_price * (1 + sl_pct / 100.0)

                hit_tp = (
                    row.time_to_tp_sec is not None
                    or (row.price_min_60m is not None and row.price_min_60m <= tp_price)
                )
                hit_sl = (
                    row.time_to_sl_sec is not None
                    or (row.price_max_60m is not None and row.price_max_60m >= sl_price)
                )

                close_reason = "expired_60m"
                pnl_pct: float | None = None

                if hit_tp and hit_sl:
                    # Берём раньше по времени; если одно из time_* None — считаем консервативно (SL)
                    if row.time_to_tp_sec is not None and row.time_to_sl_sec is not None:
                        if row.time_to_tp_sec < row.time_to_sl_sec:
                            close_reason = "tp_hit"
                            pnl_pct = tp_pct
                        else:
                            close_reason = "sl_hit"
                            pnl_pct = -sl_pct
                    else:
                        close_reason = "sl_hit"
                        pnl_pct = -sl_pct
                elif hit_tp:
                    close_reason = "tp_hit"
                    pnl_pct = tp_pct
                elif hit_sl:
                    close_reason = "sl_hit"
                    pnl_pct = -sl_pct
                else:
                    if row.price_60m and row.price_60m > 0:
                        pnl_pct = (signal_price - row.price_60m) / signal_price * 100.0
                        close_reason = "expired_60m"
                    else:
                        close_reason = "no_data"

                await session.execute(
                    update(CanceledSignal)
                    .where(CanceledSignal.id == canceled_id)
                    .values(
                        synthetic_pnl_pct=pnl_pct,
                        would_hit_tp=hit_tp,
                        would_hit_sl=hit_sl,
                        synthetic_close_reason=close_reason,
                    )
                )
                await session.commit()

                logger.info(
                    "Synthetic PnL computed",
                    canceled_id=canceled_id,
                    pnl_pct=pnl_pct,
                    reason=close_reason,
                )

        except Exception as e:
            logger.warning(
                "Synthetic PnL compute failed",
                canceled_id=canceled_id,
                error=str(e),
            )

    async def _canceled_backfill_loop(self) -> None:
        """Периодический backfill для canceled_signals без synthetic PnL."""
        while True:
            try:
                await asyncio.sleep(300)  # 5 минут
                await self._backfill_canceled_signals_pnl()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Canceled backfill loop iter failed", error=str(e))

    async def _backfill_canceled_signals_pnl(self) -> int:
        """Добивает сигналы, у которых окно 60 мин истекло, но synthetic_pnl_pct = NULL.

        Используется для сигналов, чьи воркеры потерялись при рестарте бота.
        """
        from datetime import timedelta
        from sqlalchemy import select
        from app.db.models.canceled_signal import CanceledSignal
        from app.db.session import AsyncSessionLocal

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=60)
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(CanceledSignal.id).where(
                        CanceledSignal.decision_ts < cutoff,
                        CanceledSignal.synthetic_pnl_pct.is_(None),
                    ).limit(50)
                )
                ids = [r[0] for r in result.all()]

            for cid in ids:
                await self._compute_synthetic_pnl(cid)

            if ids:
                logger.info("Canceled signals backfill done", count=len(ids))
            return len(ids)
        except Exception as e:
            logger.warning("Canceled signals backfill failed", error=str(e))
            return 0


    async def save_to_db(
            self,
            risk_score: RiskScore,
            entry_price: float,
            signal_price: float,
            price_change_at_entry: float,
            tp_price: float,
            sl_price: float,
            entry_score: float,
            entry_mode: str = "direct",
            ob_snapshot_data: dict | None = None,
        ) -> int | None:
            try:
                from app.db.models.auto_short import AutoShort
                from app.db.session import AsyncSessionLocal

                features = risk_score.features_snapshot
                factor_map = {f.name: f.raw_value for f in risk_score.factors}

                leverage = await self._get_leverage()
                target_pnl_pct = await self._get_target_pnl_pct()
                target_sl_pct = await self._get_target_sl_pct()
                entry_delay_sec = await self._get_entry_delay_sec()
                min_score_to_enter = await self._get_min_score_to_enter()

                trade = AutoShort(
                    symbol=risk_score.symbol,
                    signal_type=(
                        risk_score.signal_type.value
                        if risk_score.signal_type
                        else "unknown"
                    ),
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_at_entry=price_change_at_entry,
                    entry_delay_sec=entry_delay_sec,
                    leverage=leverage,
                    tp_pct=target_pnl_pct,
                    sl_pct=target_sl_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    status="open",
                    score=float(risk_score.score),
                    entry_score=float(entry_score),
                    min_score_at_entry=float(min_score_to_enter),
                    entry_mode=entry_mode,
                    triggered_count=risk_score.triggered_count,
                    f_rsi=factor_map.get("rsi_1m") or factor_map.get("rsi"),
                    f_vwap_extension=factor_map.get("vwap_extension"),
                    f_volume_zscore=factor_map.get("volume_zscore"),
                    f_trade_imbalance=factor_map.get("trade_imbalance"),
                    f_large_buy_cluster=factor_map.get("large_buy_cluster"),
                    f_price_acceleration=factor_map.get("price_acceleration"),
                    f_consecutive_greens=factor_map.get("consecutive_greens"),
                    f_ob_bid_thinning=factor_map.get("ob_bid_thinning"),
                    f_spread_expansion=factor_map.get("spread_expansion"),
                    f_momentum_loss=factor_map.get("momentum_loss"),
                    f_upper_wick=factor_map.get("upper_wick"),
                    f_funding_rate=factor_map.get("funding_rate"),
                    f_rsi_5m=factor_map.get("rsi_5m"),
                    f_large_sell_cluster=factor_map.get("large_sell_cluster"),
                    f_cvd_divergence=factor_map.get("cvd_divergence"),
                    f_liquidation_cascade=factor_map.get("liquidation_cascade"),
                    realized_vol_1h=features.realized_vol_1h if features else None,
                    volume_24h_usdt=features.volume_24h_usdt if features else None,
                    price_change_5m=features.price_change_5m if features else None,
                    price_change_1h=features.price_change_1h if features else None,
                    spread_pct=features.spread_pct if features else None,
                    bid_depth_change_5m=features.bid_depth_change_5m if features else None,
                    btc_change_15m=features.btc_change_15m if features else None,
                    funding_rate_at_signal=features.funding_rate if features else None,
                    oi_change_pct_at_signal=features.oi_change_pct if features else None,
                    trend_strength_1h=(
                        features.trend_context.trend_strength
                        if features and features.trend_context
                        else None
                    ),
                    # ── OB snapshot ───────────────────────────────────
                    ob_snapshot=ob_snapshot_data.get("snapshot") if ob_snapshot_data else None,
                    ob_bid_volume_top10=ob_snapshot_data.get("bid_volume_top10") if ob_snapshot_data else None,
                    ob_ask_volume_top10=ob_snapshot_data.get("ask_volume_top10") if ob_snapshot_data else None,
                    ob_imbalance_top10=ob_snapshot_data.get("imbalance_top10") if ob_snapshot_data else None,
                    ob_spread_bps=ob_snapshot_data.get("spread_bps") if ob_snapshot_data else None,
                    ob_bid_wall_price=ob_snapshot_data.get("bid_wall_price") if ob_snapshot_data else None,
                    ob_bid_wall_size=ob_snapshot_data.get("bid_wall_size") if ob_snapshot_data else None,
                    ob_ask_wall_price=ob_snapshot_data.get("ask_wall_price") if ob_snapshot_data else None,
                    ob_ask_wall_size=ob_snapshot_data.get("ask_wall_size") if ob_snapshot_data else None,
                    # Z-score нормализация
                    spread_pct_z=features.spread_pct_z if features else None,
                    bid_depth_change_5m_z=features.bid_depth_change_5m_z if features else None,
                    realized_vol_1h_z=features.realized_vol_1h_z if features else None,
                    volume_24h_usdt_z=features.volume_24h_usdt_z if features else None,
                    oi_change_pct_z=features.oi_change_pct_z if features else None,
                    # Режимные BTC-фичи
                    btc_change_1h=features.btc_change_1h if features else None,
                    btc_change_4h=features.btc_change_4h if features else None,
                    btc_change_24h=features.btc_change_24h if features else None,
                    btc_adx_1h=features.btc_adx_1h if features else None,
                    btc_atr_pct_1h=features.btc_atr_pct_1h if features else None,
                    recent_wr_20=features.recent_wr_20 if features else None,
                )

                async with AsyncSessionLocal() as session:
                    session.add(trade)
                    await session.commit()
                    await session.refresh(trade)
                    return trade.id

            except Exception as e:
                logger.exception("Auto short DB save failed", error=str(e))
                return None


    # ── Symbol loss cooldown ─────────────────────────────────────

    async def _check_symbol_loss_cooldown(self, symbol: str) -> tuple[bool, int]:
        """Проверяет cooldown по символу: >= K убытков за N часов → блокировка.

        Returns (blocked, loss_count).
        """
        strategy = await self._get_strategy()
        if not strategy.get("symbol_loss_cooldown_enabled", True):
            return False, 0

        threshold = int(strategy.get("symbol_loss_cooldown_count", 2))
        hours = int(strategy.get("symbol_loss_cooldown_hours", 24))

        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM auto_shorts "
                        "WHERE symbol = :symbol "
                        "AND status = 'closed' "
                        "AND pnl_pct < 0 "
                        "AND exit_ts > NOW() - make_interval(hours => :hours)"
                    ),
                    {"symbol": symbol, "hours": hours},
                )
                count = result.scalar() or 0
        except Exception as exc:
            logger.warning(
                "Symbol loss cooldown check failed — skip",
                symbol=symbol,
                error=str(exc),
            )
            return False, 0

        return count >= threshold, int(count)

    # ── Open short ────────────────────────────────────────────────

    async def open_short(self, risk_score: RiskScore) -> None:
        symbol = risk_score.symbol
        lock = self._get_symbol_lock(symbol)
        signal_type = risk_score.signal_type.value if risk_score.signal_type else None

        async with lock:
            if await self._is_symbol_already_open(symbol):
                logger.info(
                    "Skipping short — already have open trade for symbol",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                await self._maybe_save_shadow_trade(
                    risk_score=risk_score,
                    cancel_reason="already_open",
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by="duplicate",
                )
                return

            if symbol in self._pending_symbols:
                logger.info(
                    "Skipping short — symbol already pending entry",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                await self._maybe_save_shadow_trade(
                    risk_score=risk_score,
                    cancel_reason="pending_duplicate",
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by="duplicate",
                )
                return

            self._pending_symbols.add(symbol)

        try:
            strategy = await self._get_strategy()
            if not strategy.get("enabled", True):
                logger.info(
                    "Skipping short — strategy disabled",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                await self._maybe_save_shadow_trade(
                    risk_score=risk_score,
                    cancel_reason="strategy_disabled",
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by="strategy_disabled",
                )
                return

            # ── Trend filter (BTC + price/RSI/candles) ────────────
            if not await self._check_trend_filter(risk_score):
                logger.info(
                    "auto_short_service.open_short_blocked_by_trend_filter",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                signal_price_for_tf = await self._get_price(symbol)
                if signal_price_for_tf:
                    await self._save_canceled_signal(
                        risk_score=risk_score,
                        signal_price=signal_price_for_tf,
                        final_price=signal_price_for_tf,
                        price_change_pct=0.0,
                        final_score=float(risk_score.score),
                        cancel_reason="trend_filter_blocked",
                        entry_mode_candidate="direct",
                    )
                    await self._notify_entry_canceled(
                        symbol=symbol,
                        signal_price=signal_price_for_tf,
                        entry_price=signal_price_for_tf,
                        price_change_pct=0.0,
                        score=float(risk_score.score),
                        reason="trend_filter_blocked",
                    )
                return

            # ── Symbol loss cooldown ─────────────────────────────
            cooldown_blocked, losses_count = await self._check_symbol_loss_cooldown(symbol)
            if cooldown_blocked:
                strategy = await self._get_strategy()
                cooldown_hours = int(strategy.get("symbol_loss_cooldown_hours", 24))
                logger.info(
                    "🚫 Symbol cooldown: %s заблокирован (%d убыточных за %dч)",
                    symbol,
                    losses_count,
                    cooldown_hours,
                    symbol=symbol,
                    signal_type=signal_type,
                    losses_count=losses_count,
                    cooldown_hours=cooldown_hours,
                )
                await self._maybe_save_shadow_trade(
                    risk_score=risk_score,
                    cancel_reason="symbol_loss_cooldown",
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by="symbol_loss_cooldown",
                )
                return

            # ── BTC 24h trend filter ────────────────────────────────
            btc_24h_blocked, btc_24h_reason = await self._check_btc_24h_filter(
                risk_score,
            )
            if btc_24h_blocked:
                await self._maybe_save_shadow_trade(
                    risk_score=risk_score,
                    cancel_reason=btc_24h_reason,
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by=btc_24h_reason,
                )
                return

            signal_price = await self._get_price(symbol)
            if not signal_price:
                logger.warning(
                    "Cannot open short — no price at signal",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                return

            entry_delay_sec = await self._get_entry_delay_sec()
            min_score_to_enter = await self._get_min_score_to_enter()

            logger.info(
                "Short signal received — waiting before entry",
                symbol=symbol,
                signal_type=signal_type,
                signal_price=signal_price,
                delay_sec=entry_delay_sec,
                score=round(risk_score.score, 1),
                min_score_to_enter=min_score_to_enter,
            )

            await asyncio.sleep(entry_delay_sec)

            entry_price = await self._get_price(symbol)
            if not entry_price:
                logger.warning(
                    "Cannot open short — no price after delay",
                    symbol=symbol,
                    signal_type=signal_type,
                )
                return

            # OB snapshot для сохранения в canceled / trade
            ob_snap = await self._get_ob_snapshot(symbol, entry_price)

            effective_score = await self._get_current_score(symbol)
            effective_score = (
                float(effective_score) if effective_score is not None else float(risk_score.score)
            )
            price_change_pct = self._calc_price_move_pct(signal_price, entry_price)

            logger.info(
                "Price check after delay",
                symbol=symbol,
                signal_type=signal_type,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                effective_score=round(effective_score, 1),
            )

            # ── adverse move check (price moved against short during delay) ──
            adverse_move_pct = (entry_price / signal_price - 1.0) * 100.0
            strategy = await self._get_strategy()
            adverse_threshold = float(strategy.get("adverse_move_threshold_pct", 0.2))

            if adverse_move_pct >= adverse_threshold:
                logger.info(
                    "[adverse_move] %s delay=%ds adverse_move=%.2f%%",
                    symbol,
                    entry_delay_sec,
                    adverse_move_pct,
                    symbol=symbol,
                    signal_type=signal_type,
                    adverse_move_pct=round(adverse_move_pct, 2),
                    threshold=adverse_threshold,
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=entry_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="adverse_move",
                    entry_mode_candidate="direct",
                    ob_snapshot_data=ob_snap,
                    adverse_move_pct=adverse_move_pct,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="adverse_move",
                    adverse_move_pct=adverse_move_pct,
                )
                return

            # ── ML-gate: decision model confidence check ─────────
            ml_passed, ml_proba = await self._ml_gate_check(
                risk_score=risk_score,
                ob_snap=ob_snap,
                adverse_move_pct=adverse_move_pct,
            )
            if not ml_passed:
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=entry_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="ml_low_confidence",
                    entry_mode_candidate="direct",
                    ob_snapshot_data=ob_snap,
                    adverse_move_pct=adverse_move_pct,
                )
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=False,
                    actual_blocked_by="ml_low_confidence",
                    ob_snapshot_data=ob_snap,
                    adverse_move_pct=adverse_move_pct,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="ml_low_confidence",
                )
                return

            decision = await self._evaluate_entry_conditions(
                price_change_pct=price_change_pct,
                current_score=effective_score,
                symbol=symbol,
            )

            if decision == "cancel_drop":
                logger.info(
                    "Canceled because price dropped too much before entry",
                    symbol=symbol,
                    signal_type=signal_type,
                    change_pct=round(price_change_pct, 3),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=entry_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="cancel_drop",
                    entry_mode_candidate="direct",
                    ob_snapshot_data=ob_snap,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_dropped",
                )
                return

            if decision == "cancel_rise":
                logger.info(
                    "Canceled because price rose too much before entry",
                    symbol=symbol,
                    signal_type=signal_type,
                    change_pct=round(price_change_pct, 3),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=entry_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="cancel_rise",
                    entry_mode_candidate="direct",
                    ob_snapshot_data=ob_snap,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_too_high",
                )
                return

            entry_mode = "direct"

            if decision == "monitor":
                logger.info(
                    "Auto-short entering monitoring mode",
                    symbol=symbol,
                    signal_type=signal_type,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                    score=round(effective_score, 1),
                )

                entry_result = await self._monitor_entry(
                    risk_score=risk_score,
                    symbol=symbol,
                    signal_price=signal_price,
                    initial_score=effective_score,
                )
                if entry_result is None:
                    logger.info(
                        "Auto-short entry finished with no trade after monitoring",
                        symbol=symbol,
                        signal_type=signal_type,
                    )
                    return

                entry_price, price_change_pct, effective_score = entry_result
                entry_mode = "after_monitor"

                logger.info(
                    "Auto-short monitoring result",
                    symbol=symbol,
                    signal_type=signal_type,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                    effective_score=round(effective_score, 1),
                    entry_mode=entry_mode,
                )

            # Обновляем OB snapshot перед финальным открытием (после мониторинга мог измениться)
            ob_snap = await self._get_ob_snapshot(symbol, entry_price)

            async with lock:
                if await self._is_symbol_already_open(symbol):
                    logger.info(
                        "Skipping short after monitoring — trade already opened in parallel",
                        symbol=symbol,
                        signal_type=signal_type,
                    )
                    return

                tp_price = await self._build_tp_price_runtime(entry_price)
                sl_price = await self._build_sl_price_runtime(entry_price)

                trade_id = await self.save_to_db(
                    risk_score=risk_score,
                    entry_price=entry_price,
                    signal_price=signal_price,
                    price_change_at_entry=price_change_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    entry_score=effective_score,
                    entry_mode=entry_mode,
                    ob_snapshot_data=ob_snap,
                )

                if not trade_id:
                    logger.warning(
                        "Failed to persist short trade",
                        symbol=symbol,
                        signal_type=signal_type,
                        entry_price=entry_price,
                    )
                    return

                trade_payload = {
                    "id": trade_id,
                    "symbol": symbol,
                    "status": "open",
                    "close_reason": None,
                    "signal_price": signal_price,
                    "entry_price": entry_price,
                    "price_change_at_entry": price_change_pct,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                    "score": effective_score,
                    "entry_ts": datetime.now(timezone.utc),
                    "entry_mode": entry_mode,
                    "price_15m_saved": False,
                    "price_30m_saved": False,
                    "price_60m_saved": False,
                }

                await self._set_active_short(trade_id, trade_payload)

                logger.info(
                    "Auto short opened",
                    trade_id=trade_id,
                    symbol=symbol,
                    signal_type=signal_type,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                    tp_price=tp_price,
                    sl_price=sl_price,
                    score=round(effective_score, 1),
                    entry_mode=entry_mode,
                )

                await self._notify_opened(
                    trade_id=trade_id,
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    price_change_pct=price_change_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    score=effective_score,
                )

                task = asyncio.create_task(self._monitor_trade(trade_id))
                self._track_task(trade_id, task)

                # Shadow-paper: создаём запись для золотого датасета
                await self._create_shadow_paper_signal(
                    risk_score=risk_score,
                    would_have_opened=True,
                    actual_blocked_by=None,
                    linked_auto_short_id=trade_id,
                )

        except Exception as e:
            logger.exception(
                "Open short failed",
                symbol=symbol,
                signal_type=signal_type,
                error=str(e),
            )
        finally:
            self._pending_symbols.discard(symbol)

    # ── BTC 24h trend filter ────────────────────────────────────────

    async def _check_btc_24h_filter(
        self,
        risk_score: RiskScore,
    ) -> tuple[bool, str | None]:
        """Блокирует шорт при сильном движении BTC за 24ч.

        Returns (blocked, reason).
        blocked=True → сделку не открываем.
        reason: 'btc_24h_uptrend' | 'btc_24h_downtrend' | None.
        """
        strategy = await self._get_strategy()
        if not strategy.get("btc_24h_filter_enabled", True):
            return False, None

        features = risk_score.features_snapshot
        btc_change_24h = getattr(features, "btc_change_24h", 0.0) if features else 0.0

        threshold_up = float(strategy.get("btc_24h_filter_threshold_up_pct", 5.0))
        threshold_down = float(strategy.get("btc_24h_filter_threshold_down_pct", 0.0))

        # Рост > threshold_up % → блок (bull-режим)
        if threshold_up > 0 and btc_change_24h >= threshold_up:
            logger.info(
                "🚫 BTC 24h filter: BTC=+%.1f%% > порог +%.1f%% → блокировка",
                btc_change_24h,
                threshold_up,
                symbol=risk_score.symbol,
                btc_change_24h=round(btc_change_24h, 2),
                threshold=threshold_up,
                direction="uptrend",
            )
            return True, "btc_24h_uptrend"

        # Падение > threshold_down % → блок (отскок близко)
        if threshold_down > 0 and btc_change_24h <= -threshold_down:
            logger.info(
                "🚫 BTC 24h filter: BTC=%.1f%% < порог -%.1f%% → блокировка",
                btc_change_24h,
                threshold_down,
                symbol=risk_score.symbol,
                btc_change_24h=round(btc_change_24h, 2),
                threshold=threshold_down,
                direction="downtrend",
            )
            return True, "btc_24h_downtrend"

        return False, None

    # ── Trend filter ──────────────────────────────────────────────

    async def _check_trend_filter(self, risk_score: RiskScore) -> bool:
        """Проверяет BTC trend filter + price/RSI/candles.

        Возвращает True если вход разрешён, False если заблокирован.
        """
        strategy = await self._get_strategy()

        # ── BTC trend filter (runtime-configurable) ───────────────
        btc_filter_enabled = strategy.get("btc_filter_enabled", True)
        if btc_filter_enabled:
            threshold_15m = float(strategy.get("btc_filter_change_15m_threshold", 0.5))
            threshold_1h = float(strategy.get("btc_filter_change_1h_threshold", 1.0))
            btc_mode = strategy.get("btc_filter_mode", "any")

            # Берём BTC данные из Redis (обновляются analyzer btc_filter_loop)
            btc_15m_val: float | None = None
            btc_1h_val: float | None = None
            try:
                btc_raw = await self._redis.get("btc_filter")
                if btc_raw:
                    btc_snap = json.loads(btc_raw)
                    btc_15m_val = btc_snap.get("btc_change_15m")
                    btc_1h_val = btc_snap.get("btc_change_1h")
            except Exception:
                pass

            hit_15m = btc_15m_val is not None and btc_15m_val >= threshold_15m
            hit_1h = btc_1h_val is not None and btc_1h_val >= threshold_1h

            blocked = False
            if btc_mode == "both":
                # Блокируем только если оба порога пробиты
                # Если 1h недоступен — используем только 15m
                if btc_1h_val is not None:
                    blocked = hit_15m and hit_1h
                else:
                    blocked = hit_15m
            else:  # "any"
                blocked = hit_15m or hit_1h

            if blocked:
                symbol = risk_score.features_snapshot.symbol if risk_score.features_snapshot else risk_score.symbol
                logger.info(
                    "BTC trend filter blocked short entry",
                    symbol=symbol,
                    btc_15m=round(btc_15m_val or 0, 3),
                    btc_1h=round(btc_1h_val or 0, 3),
                    threshold_15m=threshold_15m,
                    threshold_1h=threshold_1h,
                    mode=btc_mode,
                    hit_15m=hit_15m,
                    hit_1h=hit_1h,
                )
                return False

        # ── Классический trend filter (price / candles / RSI) ─────
        features = risk_score.features_snapshot
        if not features:
            return True

        price_change_15m = features.price_change_15m
        green_candles = features.consecutive_green_candles
        rsi = features.rsi_14_1m

        trend_signals = 0

        if price_change_15m > 3.0:
            trend_signals += 1
            logger.debug(
                "Trend signal: price_change_15m",
                symbol=features.symbol,
                value=round(price_change_15m, 2),
            )

        if green_candles >= 7:
            trend_signals += 1
            logger.debug(
                "Trend signal: consecutive_greens",
                symbol=features.symbol,
                value=green_candles,
            )

        if rsi > 85:
            trend_signals += 1
            logger.debug(
                "Trend signal: rsi extreme",
                symbol=features.symbol,
                value=round(rsi, 1),
            )

        if trend_signals >= 2:
            logger.info(
                "Strong uptrend detected — blocking short entry",
                symbol=features.symbol,
                price_change_15m=round(price_change_15m, 2),
                green_candles=green_candles,
                rsi=round(rsi, 1),
                trend_signals=trend_signals,
            )
            return False

        return True

    # ── Monitor trade ─────────────────────────────────────────────

    async def _monitor_trade(self, trade_id: int) -> None:
        trade = await self._get_active_short(trade_id)
        if not trade:
            return

        symbol = trade["symbol"]
        entry_price = trade["entry_price"]
        entry_ts = trade["entry_ts"]

        TRAILING_ACTIVATE_PCT = 10.0
        BREAKEVEN_BUFFER_PCT = 0.1
        MAX_LOSS_PCT = -50.0

        trailing_activated = False

        while trade["status"] == "open":
            trade_monitor_interval = await self._get_trade_monitor_interval()

            await asyncio.sleep(trade_monitor_interval)

            current_price = await self._get_price(symbol)
            if not current_price:
                continue

            now = datetime.now(timezone.utc)
            elapsed = (now - entry_ts).total_seconds()

            await self._save_price_snapshot(trade_id, trade, current_price, elapsed, now)

            pnl = await self._calc_short_pnl_pct(entry_price, current_price)

            if pnl <= MAX_LOSS_PCT:
                logger.warning(
                    "Max loss reached — emergency stop",
                    trade_id=trade_id,
                    symbol=symbol,
                    pnl=f"{pnl:+.2f}%",
                )
                await self._close_trade(trade_id, current_price, "sl_hit", pnl)
                return

            if pnl >= TRAILING_ACTIVATE_PCT and not trailing_activated:
                breakeven_sl = entry_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
                if breakeven_sl < trade["sl_price"]:
                    old_sl = trade["sl_price"]
                    trade["sl_price"] = breakeven_sl
                    await self._set_active_short(trade_id, trade)
                    trailing_activated = True
                    logger.info(
                        "Trailing: SL moved to breakeven",
                        trade_id=trade_id,
                        symbol=symbol,
                        old_sl=round(old_sl, 6),
                        new_sl=round(breakeven_sl, 6),
                        pnl=f"{pnl:+.2f}%",
                    )

            if current_price <= trade["tp_price"]:
                await self._close_trade(trade_id, current_price, "tp_hit", pnl)
                return

            if current_price >= trade["sl_price"]:
                reason = "trailing_sl" if trailing_activated else "sl_hit"
                await self._close_trade(trade_id, current_price, reason, pnl)
                return

            # Автозакрытие по таймауту отключено:
            # expired давал ~16% шумных сделок, мешал ML.
            # Позиция закрывается только по TP/SL/manual/reversal_signal.

    # ── Close trade ───────────────────────────────────────────────

    async def _close_trade(
        self,
        trade_id: int,
        exit_price: float,
        reason: str,
        pnl: float,
    ) -> None:
        trade = await self._get_active_short(trade_id)
        if not trade:
            return

        allowed_reasons = {
            "tp_hit",
            "sl_hit",
            "trailing_sl",
            "manual",
            "expired",
            "closed_manual",
        }
        if reason not in allowed_reasons:
            logger.warning(
                "Unknown close reason, fallback applied",
                trade_id=trade_id,
                reason=reason,
            )
            reason = "manual"

        now = datetime.now(timezone.utc)
        ml_label = 1 if pnl > 0 else 0

        trade["status"] = "closed"
        trade["close_reason"] = reason

        await self._update_db(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_ts=now,
            status="closed",
            close_reason=reason,
            pnl=pnl,
            ml_label=ml_label,
        )

        logger.info(
            "Auto short closed",
            trade_id=trade_id,
            symbol=trade["symbol"],
            status="closed",
            close_reason=reason,
            pnl=f"{pnl:+.2f}%",
            leverage=await self._get_leverage(),
            ml_label=ml_label,
        )

        await self._notify_closed(trade_id, trade["symbol"], exit_price, pnl, reason)
        await self._del_active_short(trade_id)


    async def close_trade_manually(self, trade_id: int) -> str | None:
        trade = await self._get_active_short(trade_id)
        if not trade:
            logger.info("Manual close skipped — trade not active in redis", trade_id=trade_id)
            return None

        if trade.get("status") != "open":
            logger.info(
                "Manual close skipped — trade already not open",
                trade_id=trade_id,
                status=trade.get("status"),
            )
            return None

        task = self._trade_tasks.pop(trade_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info("Trade monitor cancelled before manual close", trade_id=trade_id)
            except Exception as e:
                logger.warning(
                    "Trade monitor cancel raised during manual close",
                    trade_id=trade_id,
                    error=str(e),
                )

        trade = await self._get_active_short(trade_id)
        if not trade or trade.get("status") != "open":
            return None

        symbol = trade["symbol"]
        current_price = await self._get_price(symbol)
        if not current_price:
            logger.warning(
                "Manual close failed — no current price",
                trade_id=trade_id,
                symbol=symbol,
            )
            return None

        pnl = await self._calc_short_pnl_pct(trade["entry_price"], current_price)
        await self._close_trade(trade_id, current_price, "closed_manual", pnl)

        return (
            f"✋ <b>Сделка закрыта вручную</b>\n\n"
            f"📌 #{trade_id} {symbol}\n"
            f"💰 Вход: <b>${float(trade['entry_price']):.6g}</b>\n"
            f"💹 Выход: <b>${float(current_price):.6g}</b>\n"
            f"📊 Результат: <b>{pnl:+.2f}%</b>"
        )

    # ── Save price snapshots ──────────────────────────────────────

    async def _save_price_snapshot(
        self,
        trade_id: int,
        trade: dict[str, Any],
        current_price: float,
        elapsed: float,
        now: datetime,
    ) -> None:
        try:
            from sqlalchemy import update
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            updates: dict[str, Any] = {}

            if elapsed >= 15 * 60 and not trade["price_15m_saved"]:
                updates["price_15m"] = current_price
                updates["price_15m_ts"] = now
                trade["price_15m_saved"] = True

            if elapsed >= 30 * 60 and not trade["price_30m_saved"]:
                updates["price_30m"] = current_price
                updates["price_30m_ts"] = now
                trade["price_30m_saved"] = True

            if elapsed >= 60 * 60 and not trade["price_60m_saved"]:
                updates["price_60m"] = current_price
                updates["price_60m_ts"] = now
                trade["price_60m_saved"] = True

            if not updates:
                return

            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(AutoShort)
                    .where(AutoShort.id == trade_id)
                    .values(**updates)
                )
                await session.commit()

        except Exception as e:
            logger.error(
                "Price snapshot save failed",
                trade_id=trade_id,
                error=str(e),
            )

    # ── Save trade to DB ──────────────────────────────────────────
 
    async def _update_db(
        self,
        trade_id: int,
        exit_price: float,
        exit_ts: datetime,
        status: str,
        close_reason: str,
        pnl: float,
        ml_label: int,
    ) -> None:
        try:
            from sqlalchemy import update
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    update(AutoShort)
                    .where(AutoShort.id == trade_id)
                    .values(
                        status=status,
                        exit_price=exit_price,
                        exit_ts=exit_ts,
                        pnl_pct=pnl,
                        ml_label=ml_label,
                        close_reason=close_reason,
                    )
                )
                await session.commit()

                if result.rowcount == 0:
                    logger.warning(
                        "Trade update affected 0 rows",
                        trade_id=trade_id,
                        status=status,
                        close_reason=close_reason,
                    )

        except Exception as e:
            logger.exception(
                "Failed to update closed trade in DB",
                trade_id=trade_id,
                status=status,
                close_reason=close_reason,
                error=str(e),
            )
            raise

    # ── Price fetch ───────────────────────────────────────────────

    async def _get_price(self, symbol: str) -> float | None:
        try:
            raw = await self._redis.get(f"features:{symbol}")
            if raw:
                data = json.loads(raw)
                price = data.get("last_price")
                if price and float(price) > 0:
                    self._price_cache[symbol] = float(price)
                    return float(price)
        except Exception as e:
            logger.debug("Redis features price fetch failed", symbol=symbol, error=str(e))

        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                snapshot = data.get("features_snapshot") or {}
                price = snapshot.get("last_price")
                if price is not None and float(price) > 0:
                    self._price_cache[symbol] = float(price)
                    return float(price)
        except Exception as e:
            logger.debug("Redis score price fetch failed", symbol=symbol, error=str(e))

        if self._rest_client:
            try:
                ticker = await self._rest_client.get_ticker(symbol, category="linear")
                if ticker:
                    price = float(ticker.get("lastPrice", 0))
                    if price > 0:
                        self._price_cache[symbol] = price
                        return price
            except Exception as e:
                logger.debug("REST client price fetch failed", symbol=symbol, error=str(e))

        cached = self._price_cache.get(symbol)
        if cached:
            logger.debug("Using cached price", symbol=symbol, price=cached)
            return cached

        return None

    # ── Notify opened ─────────────────────────────────────────────

    async def _notify_opened(
        self,
        trade_id: int,
        symbol: str,
        signal_price: float,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        score: float,
        price_change_pct: float,
    ) -> None:
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users
            from app.bot.keyboards import trade_action_keyboard

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
            change_em = "🔴" if price_change_pct > 0 else "🟢"
            entry_delay_sec = await self._get_entry_delay_sec()
            tp_price_move = await self._get_tp_price_move_pct()
            sl_price_move = await self._get_sl_price_move_pct()
            target_pnl_pct = await self._get_target_pnl_pct()
            target_sl_pct = await self._get_target_sl_pct()
            leverage = await self._get_leverage()

            text = (
                f"🤖 <b>Авто-шорт открыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"📊 Score: <b>{score:.0f}</b>\n\n"
                f"📍 Цена сигнала: <b>${signal_price:.6g}</b>\n"
                f"{change_em} Цена входа: <b>${entry_price:.6g}</b> "
                f"({price_change_pct:+.2f}% за {entry_delay_sec}с)\n\n"
                f"🎯 TP: ${tp_price:.6g} (-{tp_price_move:.2f}% = +{target_pnl_pct:.0f}% P&L)\n"
                f"🛑 SL: ${sl_price:.6g} (+{sl_price_move:.2f}% = -{target_sl_pct:.0f}% P&L)\n"
                f"⚡ Плечо: {leverage:.0f}x\n\n"
                f"<i>Сделка #{trade_id} | Бот следит автоматически</i>"
            )

            keyboard = trade_action_keyboard(symbol, trade_id)

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning("Notify open failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Open notification failed", error=str(e))

    # ── Notify closed ─────────────────────────────────────────────

    async def _notify_closed(
        self,
        trade_id: int,
        symbol: str,
        exit_price: float,
        pnl: float,
        reason: str,
    ) -> None:
        if not self._bot:
            logger.warning("Bot not set — cannot send close notification", trade_id=trade_id)
            return

        try:
            from app.bot.user_store import get_active_users
            from app.bot.keyboards import trade_action_keyboard

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                logger.warning("No active users for close notification")
                return

            reason_text = {
                "tp_hit": "🎯 Тейк профит достигнут",
                "sl_hit": "🛑 Стоп лосс сработал",
                "trailing_sl": "📉 Трейлинг стоп сработал",
                "expired": "⏰ Время сделки истекло (4 часа)",
                "closed_manual": "✋ Закрыта вручную",
                "manual": "✋ Закрыта вручную",
            }.get(reason, reason)

            pnl_em = "🟢" if pnl > 0 else "🔴"
            result_em = "✅" if pnl > 0 else "❌"
            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
            leverage = await self._get_leverage()

            text = (
                f"{result_em} <b>Авто-шорт закрыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"{reason_text}\n\n"
                f"💰 Выход: <b>${exit_price:.6g}</b>\n"
                f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>\n"
                f"⚡ Плечо: {leverage:.0f}x\n\n"
                f"<i>Сделка #{trade_id} | /stats для статистики</i>"
            )

            keyboard = trade_action_keyboard(symbol, trade_id)

            for user_id in user_ids:
                try:
                    await asyncio.wait_for(
                        self._bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            reply_markup=keyboard,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Notify close TIMEOUT (>10s)", user_id=user_id, symbol=symbol)
                except Exception as e:
                    logger.warning("Notify close failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Close notification failed", error=str(e))