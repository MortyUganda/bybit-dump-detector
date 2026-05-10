"""
MlShortService — paper-trading сервис на основе ML-фильтрации.

Слушает те же сигналы что и auto_short, фильтрует по ML-proba ≥ threshold,
открывает paper-позиции в свою БД, результат используется для оценки модели.
"""
from __future__ import annotations

import asyncio
import json
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.scoring.engine import RiskScore
from app.services.ml_short_config import get_ml_short_config
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

DECISION_MODEL_PATH = Path("models/decision_model.pkl")
DECISION_MODEL_FEATURES_PATH = Path("models/decision_model_features.json")

# Фичи из auto_short_service.ML_DECISION_FEATURES (COMMON_FEATURES) — хардкод-fallback
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

# TP/SL по спеке — 10%/10%, не менять
TP_PCT = 10.0
SL_PCT = 10.0


class MlShortService:
    """Paper-trading сервис с ML-фильтрацией сигналов."""

    def __init__(
        self,
        redis: aioredis.Redis,
        bot=None,
        rest_client=None,
    ) -> None:
        self._redis = redis
        self._bot = bot
        self._rest_client = rest_client
        self._price_cache: dict[str, float] = {}
        # ML decision model — lazy с кэшем
        self._ml_model: Any = None
        self._ml_model_loaded: bool = False
        self._ml_model_warned: bool = False
        # Список фичей из манифеста (или хардкод-fallback)
        self._ml_features: list[str] = ML_DECISION_FEATURES
        self._ml_features_warned: set[str] = set()

    # ── ML модель (lazy загрузка с кэшем) ──────────────────────────

    def _ensure_ml_model(self) -> Any:
        """Загрузить модель decision_model.pkl если ещё не загружена."""
        if self._ml_model_loaded:
            return self._ml_model
        self._ml_model_loaded = True
        if not DECISION_MODEL_PATH.exists():
            if not self._ml_model_warned:
                self._ml_model_warned = True
                logger.warning(
                    "ML decision модель не найдена — ml_short работает в режиме no_model",
                    path=str(DECISION_MODEL_PATH),
                )
            return None
        try:
            with open(DECISION_MODEL_PATH, "rb") as f:
                self._ml_model = pickle.load(f)
            logger.info(
                "ML decision модель загружена для ml_short",
                path=str(DECISION_MODEL_PATH),
            )
        except Exception as exc:
            logger.warning(
                "Не удалось загрузить ML decision модель",
                error=str(exc),
            )
            return self._ml_model

        # Загрузка манифеста фичей
        if DECISION_MODEL_FEATURES_PATH.exists():
            try:
                with open(DECISION_MODEL_FEATURES_PATH, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                self._ml_features = manifest["features"]
                logger.info(
                    "Список фичей загружен из манифеста",
                    n_features=len(self._ml_features),
                    path=str(DECISION_MODEL_FEATURES_PATH),
                )
            except Exception as exc:
                logger.warning(
                    "Ошибка чтения манифеста — fallback на хардкод",
                    error=str(exc),
                )
                self._ml_features = ML_DECISION_FEATURES
        else:
            logger.warning(
                "Манифест фичей не найден — fallback на хардкод (возможен рассинхрон!)",
                path=str(DECISION_MODEL_FEATURES_PATH),
            )
            self._ml_features = ML_DECISION_FEATURES

        return self._ml_model

    # ── Group 1 stats из Redis ────────────────────────────────────────

    async def _get_symbol_stats(self, symbol: str) -> dict[str, str]:
        """Читает Group 1 stats из Redis (дефолты при miss)."""
        try:
            stats = await self._redis.hgetall(f"ml_features:symbol_stats:{symbol}")
            if stats:
                return stats
        except Exception:
            pass
        return {}

    # ── Построение вектора фичей (идентично auto_short) ─────────────

    def _build_ml_features(
        self,
        risk_score: RiskScore,
        adverse_move_pct: float | None = None,
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
            "ob_bid_volume_top10": _f(getattr(features, 'ob_bid_volume_top10', None) if features else None),
            "ob_ask_volume_top10": _f(getattr(features, 'ob_ask_volume_top10', None) if features else None),
            "ob_imbalance_top10": _f(getattr(features, 'ob_imbalance_top10', None) if features else None),
            "ob_spread_bps": _f(getattr(features, 'ob_spread_bps', None) if features else None),
            "ob_bid_wall_price": _f(getattr(features, 'ob_bid_wall_price', None) if features else None),
            "ob_bid_wall_size": _f(getattr(features, 'ob_bid_wall_size', None) if features else None),
            "ob_ask_wall_price": _f(getattr(features, 'ob_ask_wall_price', None) if features else None),
            "ob_ask_wall_size": _f(getattr(features, 'ob_ask_wall_size', None) if features else None),
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

    def _build_features_snapshot_json(
        self,
        risk_score: RiskScore,
        adverse_move_pct: float | None = None,
        symbol_stats: dict[str, str] | None = None,
    ) -> dict:
        """JSON-snapshot фичей для оффлайн-анализа."""
        vec = self._build_ml_features(risk_score, adverse_move_pct, symbol_stats)
        return dict(zip(self._ml_features, vec))

    # ── Получение цены ──────────────────────────────────────────────

    async def _get_price(self, symbol: str) -> float | None:
        """Получить текущую цену (Redis features → Redis score → REST)."""
        try:
            raw = await self._redis.get(f"features:{symbol}")
            if raw:
                data = json.loads(raw)
                price = data.get("last_price")
                if price and float(price) > 0:
                    self._price_cache[symbol] = float(price)
                    return float(price)
        except Exception:
            pass

        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                snapshot = data.get("features_snapshot") or {}
                price = snapshot.get("last_price")
                if price is not None and float(price) > 0:
                    self._price_cache[symbol] = float(price)
                    return float(price)
        except Exception:
            pass

        if self._rest_client:
            try:
                ticker = await self._rest_client.get_ticker(symbol, category="linear")
                if ticker:
                    price = float(ticker.get("lastPrice", 0))
                    if price > 0:
                        self._price_cache[symbol] = price
                        return price
            except Exception:
                pass

        return self._price_cache.get(symbol)

    # ── Главный метод: обработка сигнала ────────────────────────────

    async def on_signal(self, risk_score: RiskScore) -> None:
        """Обработать сигнал от analyzer'а — ML-фильтрация + paper-trading."""
        symbol = risk_score.symbol
        now = datetime.now(timezone.utc)
        cfg = await get_ml_short_config(self._redis)

        signal_price = None
        if risk_score.features_snapshot:
            signal_price = risk_score.features_snapshot.last_price

        # 1. Проверка enabled
        if not cfg.get("enabled", False):
            await self._save_signal(
                signal_ts=now,
                symbol=symbol,
                signal_price=signal_price,
                score=risk_score.score,
                ml_proba=None,
                ml_decision="disabled",
                blocked_reason=None,
                features_snapshot=None,
            )
            return

        # 1.5. Ранний выход по score — не грузить ML-inference/SQL низкими сигналами
        min_score_early = cfg.get("min_score_to_enter", 45)
        if risk_score.score < min_score_early:
            await self._save_signal(
                signal_ts=now,
                symbol=symbol,
                signal_price=signal_price,
                score=risk_score.score,
                ml_proba=None,
                ml_decision="blocked_other",
                blocked_reason="min_score",
                features_snapshot=None,
            )
            return

        # 2. Загрузить модель + Group 1 stats из Redis
        symbol_stats = await self._get_symbol_stats(symbol)
        logger.debug(
            "Group 1 stats: wr20=%s, wr5=%s, count24h=%s, avg_pnl5=%s",
            symbol_stats.get("recent_wr_20", "miss"),
            symbol_stats.get("recent_wr_5", "miss"),
            symbol_stats.get("trades_count_24h", "miss"),
            symbol_stats.get("avg_pnl_5", "miss"),
            symbol=symbol,
        )

        model = self._ensure_ml_model()
        if model is None:
            await self._save_signal(
                signal_ts=now,
                symbol=symbol,
                signal_price=signal_price,
                score=risk_score.score,
                ml_proba=None,
                ml_decision="no_model",
                blocked_reason=None,
                features_snapshot=self._build_features_snapshot_json(
                    risk_score, symbol_stats=symbol_stats,
                ),
            )
            logger.info(
                "ML-short: модель не найдена, пропускаю сигнал",
                symbol=symbol,
            )
            return

        # 3. Собрать фичи и получить proba.
        # ВАЖНО: predict_proba — блокирующий sync-вызов sklearn (сотни мс),
        # выносим в thread чтобы не блокировать event loop analyzer'а
        # (иначе send_message/scoring/WS простаивают на каждый сигнал).
        feature_vec = self._build_ml_features(risk_score, symbol_stats=symbol_stats)
        try:
            import numpy as np
            X = np.array([feature_vec], dtype=np.float64)
            proba_arr = await asyncio.to_thread(model.predict_proba, X)
            proba = float(proba_arr[0][1])
        except Exception as exc:
            logger.warning(
                "ML-short: ошибка инференса",
                symbol=symbol,
                error=str(exc),
            )
            await self._save_signal(
                signal_ts=now,
                symbol=symbol,
                signal_price=signal_price,
                score=risk_score.score,
                ml_proba=None,
                ml_decision="inference_error",
                blocked_reason=str(exc)[:200],
                features_snapshot=self._build_features_snapshot_json(
                    risk_score, symbol_stats=symbol_stats,
                ),
            )
            return

        features_json = self._build_features_snapshot_json(
            risk_score, symbol_stats=symbol_stats,
        )

        # 4. Фильтры (в порядке из спеки)

        # already_open
        is_open = await self._has_open_position(symbol)
        if is_open:
            await self._save_signal(
                signal_ts=now, symbol=symbol, signal_price=signal_price,
                score=risk_score.score, ml_proba=proba,
                ml_decision="blocked_other", blocked_reason="already_open",
                features_snapshot=features_json,
            )
            return

        # min_score
        min_score = cfg.get("min_score_to_enter", 45)
        if risk_score.score < min_score:
            await self._save_signal(
                signal_ts=now, symbol=symbol, signal_price=signal_price,
                score=risk_score.score, ml_proba=proba,
                ml_decision="blocked_other", blocked_reason="min_score",
                features_snapshot=features_json,
            )
            return

        # max_concurrent
        max_concurrent = cfg.get("max_concurrent_positions", 5)
        open_count = await self._count_open_positions()
        if open_count >= max_concurrent:
            await self._save_signal(
                signal_ts=now, symbol=symbol, signal_price=signal_price,
                score=risk_score.score, ml_proba=proba,
                ml_decision="blocked_other", blocked_reason="max_concurrent",
                features_snapshot=features_json,
            )
            return

        # cooldown
        if cfg.get("cooldown_enabled", True):
            cooldown_active = await self._check_cooldown(symbol)
            if cooldown_active:
                await self._save_signal(
                    signal_ts=now, symbol=symbol, signal_price=signal_price,
                    score=risk_score.score, ml_proba=proba,
                    ml_decision="blocked_other", blocked_reason="cooldown",
                    features_snapshot=features_json,
                )
                return

        # signal_price sanity
        current_price = await self._get_price(symbol)
        if not current_price:
            await self._save_signal(
                signal_ts=now, symbol=symbol, signal_price=signal_price,
                score=risk_score.score, ml_proba=proba,
                ml_decision="blocked_other", blocked_reason="signal_price",
                features_snapshot=features_json,
            )
            return

        # delay
        delay_seconds = cfg.get("delay_seconds", 30)
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        # adverse_move после delay
        price_after_delay = await self._get_price(symbol)
        if price_after_delay and current_price:
            adverse_move_pct = ((price_after_delay - current_price) / current_price) * 100
            threshold = cfg.get("adverse_move_threshold_pct", 0.2)
            if adverse_move_pct >= threshold:
                # Пересобрать фичи с adverse_move
                features_json = self._build_features_snapshot_json(
                    risk_score, adverse_move_pct, symbol_stats=symbol_stats,
                )
                await self._save_signal(
                    signal_ts=now, symbol=symbol, signal_price=signal_price,
                    score=risk_score.score, ml_proba=proba,
                    ml_decision="blocked_other", blocked_reason="adverse_move",
                    features_snapshot=features_json,
                )
                return

        # ml_proba — главный фильтр
        threshold = cfg.get("proba_threshold", 0.60)
        if proba < threshold:
            await self._save_signal(
                signal_ts=now, symbol=symbol, signal_price=signal_price,
                score=risk_score.score, ml_proba=proba,
                ml_decision="blocked_low_proba",
                blocked_reason=None,
                features_snapshot=features_json,
            )
            logger.info(
                "ML-short: заблокировано по proba",
                symbol=symbol,
                proba=round(proba, 4),
                threshold=threshold,
            )
            return

        # 5. Открываем paper-позицию
        entry_price = price_after_delay or current_price
        signal_id = await self._save_signal(
            signal_ts=now, symbol=symbol, signal_price=signal_price,
            score=risk_score.score, ml_proba=proba,
            ml_decision="opened",
            blocked_reason=None,
            features_snapshot=features_json,
        )

        position_id = await self._open_position(
            signal_id=signal_id,
            symbol=symbol,
            entry_price=entry_price,
            ml_proba=proba,
            score=risk_score.score,
            timeout_hours=cfg.get("position_timeout_hours", 24),
        )

        logger.info(
            "ML-short: paper-позиция открыта",
            symbol=symbol,
            position_id=position_id,
            entry_price=entry_price,
            proba=round(proba, 4),
            score=round(risk_score.score, 1),
        )

        # Уведомление в TG
        await self._notify_opened(symbol, entry_price, proba, risk_score.score, position_id)

        # Кросс-ссылка: обновить auto_short_decision через 60с
        asyncio.create_task(self._update_auto_short_crossref(signal_id, symbol, now))

    # ── Вспомогательные методы БД ───────────────────────────────────

    async def _save_signal(
        self,
        signal_ts: datetime,
        symbol: str,
        signal_price: float | None,
        score: float | None,
        ml_proba: float | None,
        ml_decision: str,
        blocked_reason: str | None,
        features_snapshot: dict | None,
    ) -> int | None:
        """Записать сигнал в ml_short_signals, вернуть id."""
        try:
            from app.db.models.ml_short import MlShortSignal
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                sig = MlShortSignal(
                    signal_ts=signal_ts,
                    symbol=symbol,
                    signal_price=signal_price,
                    score=score,
                    ml_proba=ml_proba,
                    ml_decision=ml_decision,
                    blocked_reason=blocked_reason,
                    features_snapshot=features_snapshot,
                )
                session.add(sig)
                await session.commit()
                await session.refresh(sig)
                return sig.id
        except Exception as exc:
            logger.error(
                "ML-short: ошибка записи сигнала в БД",
                symbol=symbol,
                error=str(exc),
            )
            return None

    async def _open_position(
        self,
        signal_id: int | None,
        symbol: str,
        entry_price: float,
        ml_proba: float | None,
        score: float | None,
        timeout_hours: int = 24,
    ) -> int | None:
        """Открыть paper-позицию в ml_short_positions."""
        try:
            from app.db.models.ml_short import MlShortPosition
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                pos = MlShortPosition(
                    signal_id=signal_id,
                    symbol=symbol,
                    entry_ts=datetime.now(timezone.utc),
                    entry_price=entry_price,
                    ml_proba=ml_proba,
                    score=score,
                    tp_pct=TP_PCT,
                    sl_pct=SL_PCT,
                    timeout_hours=timeout_hours,
                    status="open",
                    is_paper=True,
                )
                session.add(pos)
                await session.commit()
                await session.refresh(pos)
                return pos.id
        except Exception as exc:
            logger.error(
                "ML-short: ошибка открытия позиции в БД",
                symbol=symbol,
                error=str(exc),
            )
            return None

    async def _has_open_position(self, symbol: str) -> bool:
        """Есть ли открытая позиция по символу."""
        try:
            from sqlalchemy import select, func
            from app.db.models.ml_short import MlShortPosition
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.count()).where(
                        MlShortPosition.symbol == symbol,
                        MlShortPosition.status == "open",
                    )
                )
                return result.scalar_one() > 0
        except Exception as exc:
            logger.error("ML-short: ошибка проверки позиции", error=str(exc))
            return False

    async def _count_open_positions(self) -> int:
        """Количество открытых позиций."""
        try:
            from sqlalchemy import select, func
            from app.db.models.ml_short import MlShortPosition
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(func.count()).where(
                        MlShortPosition.status == "open",
                    )
                )
                return result.scalar_one()
        except Exception as exc:
            logger.error("ML-short: ошибка подсчёта позиций", error=str(exc))
            return 0

    async def _check_cooldown(self, symbol: str) -> bool:
        """Проверка cooldown для символа."""
        try:
            from sqlalchemy import select
            from app.db.models.ml_short import MlShortCooldown
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(MlShortCooldown).where(MlShortCooldown.symbol == symbol)
                )
                row = result.scalar_one_or_none()
                if not row:
                    return False
                if row.cooldown_until and row.cooldown_until > datetime.now(timezone.utc):
                    return True
                return False
        except Exception as exc:
            logger.error("ML-short: ошибка проверки cooldown", error=str(exc))
            return False

    # ── Кросс-ссылка на auto_short ──────────────────────────────────

    async def _update_auto_short_crossref(
        self,
        signal_id: int | None,
        symbol: str,
        signal_ts: datetime,
    ) -> None:
        """Через 60с найти соответствующую запись auto_short и заполнить crossref."""
        if not signal_id:
            return
        await asyncio.sleep(60)
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                # Ищем auto_short по symbol + signal_ts ± 30с
                result = await session.execute(
                    text("""
                        SELECT id, status FROM auto_shorts
                        WHERE symbol = :symbol
                          AND entry_ts BETWEEN :ts_min AND :ts_max
                        ORDER BY entry_ts DESC
                        LIMIT 1
                    """),
                    {
                        "symbol": symbol,
                        "ts_min": signal_ts - timedelta(seconds=120),
                        "ts_max": signal_ts + timedelta(seconds=120),
                    },
                )
                row = result.fetchone()

                if row:
                    auto_decision = "opened"
                    auto_position_id = row[0]
                else:
                    auto_decision = "not_seen"
                    auto_position_id = None

                await session.execute(
                    text("""
                        UPDATE ml_short_signals
                        SET auto_short_decision = :decision,
                            auto_short_position_id = :pos_id
                        WHERE id = :sig_id
                    """),
                    {
                        "decision": auto_decision,
                        "pos_id": auto_position_id,
                        "sig_id": signal_id,
                    },
                )
                await session.commit()
        except Exception as exc:
            logger.warning(
                "ML-short: ошибка обновления crossref",
                signal_id=signal_id,
                error=str(exc),
            )

    # ── TG уведомления ──────────────────────────────────────────────

    async def _notify_opened(
        self,
        symbol: str,
        entry_price: float,
        proba: float,
        score: float,
        position_id: int | None,
    ) -> None:
        if not self._bot:
            return
        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                user_ids = settings.allowed_user_ids

            tp_price = entry_price * (1 - TP_PCT / 100)
            sl_price = entry_price * (1 + SL_PCT / 100)

            text = (
                f"🤖 <b>ML-Short: позиция открыта</b>\n\n"
                f"📌 #{position_id} <b>{symbol}</b>\n"
                f"💰 Вход: <b>${entry_price:.6g}</b>\n"
                f"🧠 ML proba: <b>{proba:.2%}</b>\n"
                f"📊 Score: <b>{score:.0f}</b>\n"
                f"🎯 TP: ${tp_price:.6g} (-{TP_PCT}%)\n"
                f"🛑 SL: ${sl_price:.6g} (+{SL_PCT}%)\n"
                f"📎 Режим: Paper (Real disabled)"
            )
            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("ML-short: ошибка TG-уведомления", error=str(exc))
