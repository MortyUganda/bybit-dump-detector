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

# Фичи из auto_short_service.ML_DECISION_FEATURES (COMMON_FEATURES)
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

    # ── Построение вектора фичей (идентично auto_short) ─────────────

    def _build_ml_features(
        self,
        risk_score: RiskScore,
        adverse_move_pct: float | None = None,
    ) -> list[float]:
        """Собирает вектор фичей для инференса decision-модели."""
        features = risk_score.features_snapshot
        factor_map = {f.name: f.raw_value for f in risk_score.factors}

        def _f(val: Any) -> float:
            if val is None:
                return 0.0
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        return [
            _f(risk_score.score),
            _f(factor_map.get("rsi_1m") or factor_map.get("rsi")),
            _f(factor_map.get("rsi_5m")),
            _f(factor_map.get("vwap_extension")),
            _f(factor_map.get("volume_zscore")),
            _f(factor_map.get("trade_imbalance")),
            _f(factor_map.get("large_buy_cluster")),
            _f(factor_map.get("large_sell_cluster")),
            _f(factor_map.get("price_acceleration")),
            _f(factor_map.get("consecutive_greens")),
            _f(factor_map.get("ob_bid_thinning")),
            _f(factor_map.get("spread_expansion")),
            _f(factor_map.get("momentum_loss")),
            _f(factor_map.get("upper_wick")),
            _f(factor_map.get("funding_rate")),
            _f(factor_map.get("cvd_divergence")),
            _f(factor_map.get("liquidation_cascade")),
            _f(features.realized_vol_1h if features else None),
            _f(features.volume_24h_usdt if features else None),
            _f(features.price_change_5m if features else None),
            _f(features.price_change_1h if features else None),
            _f(features.spread_pct if features else None),
            _f(features.bid_depth_change_5m if features else None),
            _f(features.btc_change_15m if features else None),
            _f(features.funding_rate if features else None),
            _f(features.oi_change_pct if features else None),
            _f(features.trend_context.trend_strength if features and features.trend_context else None),
            _f(getattr(features, 'ob_bid_volume_top10', None) if features else None),
            _f(getattr(features, 'ob_ask_volume_top10', None) if features else None),
            _f(getattr(features, 'ob_imbalance_top10', None) if features else None),
            _f(getattr(features, 'ob_spread_bps', None) if features else None),
            _f(getattr(features, 'ob_bid_wall_price', None) if features else None),
            _f(getattr(features, 'ob_bid_wall_size', None) if features else None),
            _f(getattr(features, 'ob_ask_wall_price', None) if features else None),
            _f(getattr(features, 'ob_ask_wall_size', None) if features else None),
            _f(getattr(features, 'spread_pct_z', None) if features else None),
            _f(getattr(features, 'bid_depth_change_5m_z', None) if features else None),
            _f(getattr(features, 'realized_vol_1h_z', None) if features else None),
            _f(getattr(features, 'volume_24h_usdt_z', None) if features else None),
            _f(getattr(features, 'oi_change_pct_z', None) if features else None),
            _f(getattr(features, 'btc_change_1h', None) if features else None),
            _f(getattr(features, 'btc_change_4h', None) if features else None),
            _f(getattr(features, 'btc_change_24h', None) if features else None),
            _f(getattr(features, 'btc_adx_1h', None) if features else None),
            _f(getattr(features, 'btc_atr_pct_1h', None) if features else None),
            _f(getattr(features, 'recent_wr_20', None) if features else None),
            _f(adverse_move_pct),
        ]

    def _build_features_snapshot_json(
        self,
        risk_score: RiskScore,
        adverse_move_pct: float | None = None,
    ) -> dict:
        """JSON-snapshot фичей для оффлайн-анализа."""
        vec = self._build_ml_features(risk_score, adverse_move_pct)
        return dict(zip(ML_DECISION_FEATURES, vec))

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

        # 2. Загрузить модель
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
                features_snapshot=self._build_features_snapshot_json(risk_score),
            )
            logger.info(
                "ML-short: модель не найдена, пропускаю сигнал",
                symbol=symbol,
            )
            return

        # 3. Собрать фичи и получить proba
        feature_vec = self._build_ml_features(risk_score)
        try:
            import numpy as np
            X = np.array([feature_vec], dtype=np.float64)
            proba = float(model.predict_proba(X)[0][1])
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
                ml_decision="no_model",
                blocked_reason="inference_error",
                features_snapshot=self._build_features_snapshot_json(risk_score),
            )
            return

        features_json = self._build_features_snapshot_json(risk_score)

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
                features_json = self._build_features_snapshot_json(risk_score, adverse_move_pct)
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
