"""
Analyzer Service — scoring loop and overvalued ranking.

Runs on a fixed interval (default: 30s).
For each active symbol:
  1. Pull latest CoinFeatures from ingestion service
  2. Compute RiskScore via ScoringEngine
  3. If score >= threshold AND new signal: persist to DB + trigger alert
  4. Maintain ranked overvalued list in Redis

Cooldown enforcement:
  - Redis key: cooldown:{symbol}:{signal_type}
  - TTL = alert_cooldown_minutes * 60
  - If key exists: skip alert (but still score)
"""

from __future__ import annotations

import asyncio
import json
import uuid

import redis.asyncio as aioredis

from app.analytics.market_context import MarketContext
from app.analytics.ml_scorer import MLScorer
from app.config import get_settings
from app.scoring.engine import RiskScore, ScoringEngine
from app.services.ingestion import IngestionService
from app.utils.logging import get_logger
from app.utils.time_utils import utcnow

logger = get_logger(__name__)
settings = get_settings()

REDIS_SCORE_KEY = "score:{symbol}"
REDIS_SCORE_TTL = 300
REDIS_COOLDOWN_KEY = "cooldown:{symbol}:{signal_type}"
REDIS_OVERVALUED_KEY = "overvalued:latest"
REDIS_OVERVALUED_TTL = 600  # 10 min
REDIS_BTC_FILTER_KEY = "btc_filter"
REDIS_BTC_FILTER_TTL = 30  # 30 sec


class AnalyzerService:
    """
    Periodic scoring loop.
    """

    def __init__(
        self,
        ingestion: IngestionService,
        redis: aioredis.Redis,
        db_session_factory=None,
        alert_callback=None,  # async (symbol, risk_score) -> None
        bot=None,
    ) -> None:
        self._ingestion = ingestion
        self._redis = redis
        self._db = db_session_factory
        self._alert_callback = alert_callback
        self._bot = bot
        self._scoring = ScoringEngine()
        self._market_context = MarketContext(ingestion._rest)
        self._ml_scorer = MLScorer()
        self._running = False
        self._cycle_count = 0
        self._tasks: dict[str, asyncio.Task] = {}
        self._overvalued_broadcast_count = 0

    async def start(self) -> None:
        self._running = True
        logger.info("Analyzer service started")
        self._launch_task("scoring_loop", self._scoring_loop())
        self._launch_task("overvalued_loop", self._overvalued_loop())
        self._launch_task("btc_filter_loop", self._btc_filter_loop())

    def _launch_task(self, name: str, coro) -> None:
        """Create a named task with a done-callback that logs + auto-restarts."""
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        name = task.get_name()
        if task.cancelled():
            logger.info("Analyzer task cancelled", task=name)
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Analyzer task died with exception — restarting",
                task=name,
                error=str(exc),
                exc_type=type(exc).__name__,
            )
        else:
            logger.warning(
                "Analyzer task exited cleanly (should not happen) — restarting",
                task=name,
            )
        # Auto-restart if still running
        if self._running:
            coro_map = {
                "scoring_loop": self._scoring_loop,
                "overvalued_loop": self._overvalued_loop,
                "btc_filter_loop": self._btc_filter_loop,
            }
            factory = coro_map.get(name)
            if factory:
                logger.info("Auto-restarting analyzer task", task=name)
                self._launch_task(name, factory())

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ── BTC filter background loop ─────────────────────────────────

    async def _btc_filter_loop(self) -> None:
        """Fetch BTC candles every 10s and persist to Redis for /status and entry filter."""
        # Dedicated MarketContext with no internal cache (we control the interval)
        btc_ctx = MarketContext(self._ingestion._rest)
        btc_ctx._last_update = 0.0  # force first refresh immediately

        while self._running:
            try:
                # Force refresh by resetting internal cache timer
                btc_ctx._last_update = 0.0
                await btc_ctx.refresh()

                payload = json.dumps({
                    "btc_change_1m": btc_ctx.btc_change_1m,
                    "btc_change_5m": btc_ctx.btc_change_5m,
                    "btc_change_15m": btc_ctx.btc_change_15m,
                    "btc_change_1h": btc_ctx.btc_change_1h,
                    "updated_at": utcnow().isoformat(),
                })
                await self._redis.set(
                    REDIS_BTC_FILTER_KEY, payload, ex=REDIS_BTC_FILTER_TTL,
                )
            except asyncio.CancelledError:
                logger.info("BTC filter loop cancelled")
                return
            except Exception:
                logger.exception("BTC filter loop error — will retry in 10s")

            await asyncio.sleep(10)

    # ── Main scoring loop ─────────────────────────────────────────

    async def _scoring_loop(self) -> None:
        """Score all symbols every 10 seconds."""
        while self._running:
            try:
                await asyncio.wait_for(
                    self._run_scoring_cycle(),
                    timeout=120,
                )
            except asyncio.TimeoutError:
                logger.error("Scoring cycle timed out after 120s — skipping")
                continue
            except asyncio.CancelledError:
                logger.info("Scoring loop cancelled")
                return
            except Exception:
                logger.exception("Scoring loop error — will retry in 10s")
                await asyncio.sleep(10)
                continue
            await asyncio.sleep(10)

    async def _run_scoring_cycle(self) -> None:
        self._cycle_count += 1

        # Refresh BTC market context for correlation filter
        try:
            await self._market_context.refresh()
        except Exception:
            logger.exception("Market context refresh failed — using stale data")

        btc_suppressing = self._market_context.should_suppress_shorts()
        if btc_suppressing:
            logger.info(
                "BTC rally detected — suppressing alt short signals",
                btc_change_5m=round(self._market_context.btc_change_5m or 0, 2),
                btc_change_15m=round(self._market_context.btc_change_15m or 0, 2),
                btc_change_1h=round(self._market_context.btc_change_1h or 0, 2),
            )

        features_list = await self._ingestion.get_all_features()

        if not features_list:
            logger.debug("No features available yet")
            return

        scored = []
        for features in features_list:
            try:
                # Populate BTC context on each feature
                features.btc_change_15m = self._market_context.btc_change_15m or 0.0

                risk_score = self._scoring.score(features)

                # ML blending (when model is trained)
                if self._ml_scorer.is_ready():
                    factor_map = {f.name: f.raw_value for f in risk_score.factors}
                    ml_features = {
                        **factor_map,
                        "btc_change_15m": features.btc_change_15m,
                        "funding_rate_at_signal": features.funding_rate or 0,
                        "oi_change_pct_at_signal": features.oi_change_pct,
                        "trend_strength_1h": (
                            features.trend_context.trend_strength
                            if features.trend_context else 0
                        ),
                    }
                    ml_prob = self._ml_scorer.predict_probability(ml_features)
                    risk_score.ml_probability = ml_prob
                    # Blend: 70% rule-based + 30% ML
                    rule_score = risk_score.score
                    risk_score.score = 0.7 * rule_score + 0.3 * (ml_prob * 100)
                    risk_score.score = max(0.0, min(100.0, risk_score.score))

                scored.append(risk_score)

                # Persist score to Redis
                key = REDIS_SCORE_KEY.format(symbol=features.symbol)
                await self._redis.setex(key, REDIS_SCORE_TTL, json.dumps(risk_score.to_dict()))

                # Check if alert should fire (suppress during BTC rally or ML veto)
                if risk_score.is_alertable and risk_score.signal_type:
                    if btc_suppressing:
                        logger.debug(
                            "Alert suppressed by BTC correlation filter",
                            symbol=features.symbol,
                            score=risk_score.score,
                        )
                    elif (risk_score.ml_probability is not None
                          and risk_score.ml_probability < 0.35):
                        logger.debug(
                            "Alert suppressed by ML scorer — pattern historically loses",
                            symbol=features.symbol,
                            ml_prob=round(risk_score.ml_probability, 3),
                        )
                    else:
                        if risk_score.score >= 50:
                            logger.info(
                                "Pre-alert check",
                                symbol=features.symbol,
                                score=round(risk_score.score, 2),
                                signal_type=str(risk_score.signal_type),
                                is_alertable=risk_score.is_alertable,
                                triggered_count=risk_score.triggered_count,
                            )
                        await self._maybe_alert(risk_score)

            except Exception as e:
                logger.error("Scoring error", symbol=features.symbol, error=str(e), exc_info=True)

        logger.info(
            "Scoring cycle complete",
            cycle=self._cycle_count,
            scored=len(scored),
            high_risk=sum(1 for s in scored if s.score >= settings.score_alert_threshold),
        )

    # ── Alert dispatch with cooldown ──────────────────────────────

    async def _maybe_alert(self, risk_score: RiskScore) -> None:
        symbol = risk_score.symbol
        signal_type = risk_score.signal_type.value

        cooldown_key = REDIS_COOLDOWN_KEY.format(symbol=symbol, signal_type=signal_type)

        # Check cooldown
        existing = await self._redis.get(cooldown_key)
        if existing:
            logger.info("Alert suppressed by cooldown", symbol=symbol, score=risk_score.score, signal_type=signal_type)
            return

        # Set cooldown
        ttl = settings.alert_cooldown_minutes * 60
        await self._redis.setex(cooldown_key, ttl, "1")

        # Persist signal to DB
        if self._db:
            await self._persist_signal(risk_score)

        # Fire alert callback (bot sends message)
        if self._alert_callback:
            try:
                await self._alert_callback(symbol, risk_score)
            except Exception as e:
                logger.error("Alert callback failed", symbol=symbol, error=str(e))

        logger.info(
            "Alert fired",
            symbol=symbol,
            score=risk_score.score,
            signal=signal_type,
            level=risk_score.level.value,
        )

    async def _persist_signal(self, risk_score: RiskScore) -> None:
        """Save signal to PostgreSQL."""
        try:
            from app.db.models.signal import Signal
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                signal = Signal(
                    symbol=risk_score.symbol,
                    signal_type=risk_score.signal_type.value if risk_score.signal_type else "none",
                    risk_level=risk_score.level.value,
                    score=risk_score.score,
                    triggered_count=risk_score.triggered_count,
                    top_reasons=",".join(risk_score.top_reasons),
                    factors_json=risk_score.to_dict()["factors"],
                    price_at_signal=risk_score.features_snapshot.last_price
                    if risk_score.features_snapshot
                    else 0.0,
                    alert_sent=True,
                    ts=utcnow(),
                )
                session.add(signal)
                await session.commit()
        except Exception as e:
            logger.error("DB signal persist failed", error=str(e))

    # ── Overvalued ranking ────────────────────────────────────────

    async def _overvalued_loop(self) -> None:
        """Rebuild overvalued ranking every 5 minutes."""
        while self._running:
            try:
                await asyncio.wait_for(
                    self._rebuild_overvalued(),
                    timeout=180,
                )
            except asyncio.TimeoutError:
                logger.error("Overvalued rebuild timed out after 180s — skipping")
                continue
            except asyncio.CancelledError:
                logger.info("Overvalued loop cancelled")
                return
            except Exception:
                logger.exception("Overvalued loop error — will retry in 60s")
                await asyncio.sleep(60)
                continue
            await asyncio.sleep(300)

    async def _rebuild_overvalued(self) -> None:
        features_list = await self._ingestion.get_all_features()
        scored = []

        # Получаем 24h тикеры одним запросом для всех монет
        try:
            tickers_list = await self._ingestion._rest.get_tickers(category="linear")
            tickers = {t["symbol"]: t for t in tickers_list}
        except Exception as e:
            logger.warning("Failed to fetch tickers for 24h change", error=str(e))
            tickers = {}

        all_scores = []  # для дебага

        for features in features_list:
            try:
                risk_score = self._scoring.score(features)
                all_scores.append((features.symbol, risk_score.score))

                if risk_score.score >= 30:  # повысили до 30
                    ticker = tickers.get(features.symbol, {})
                    try:
                        price_change_24h = float(ticker.get("price24hPcnt", 0.0)) * 100
                    except (ValueError, TypeError):
                        price_change_24h = 0.0

                    scored.append({
                        "symbol": features.symbol,
                        "score": risk_score.score,
                        "risk_level": risk_score.level.value,
                        "price": features.last_price,
                        "price_change_24h_pct": price_change_24h,
                        "volume_24h_usdt": features.volume_24h_usdt,
                        "rsi": features.rsi_14_1m,
                        "vwap_extension_pct": features.vwap_extension_pct,
                        "top_reasons": risk_score.top_reasons,
                        "signal_type": risk_score.signal_type.value
                        if risk_score.signal_type
                        else None,
                    })
            except Exception:
                pass

        # Лог топ-5 монет по score для диагностики
        if all_scores:
            top5 = sorted(all_scores, key=lambda x: -x[1])[:5]
            for sym, sc in top5:
                logger.info("Top coin score", symbol=sym, score=round(sc, 1))
        else:
            logger.warning("No scores computed — features list may be empty")

        # Sort by score descending, take top N
        scored.sort(key=lambda x: -x["score"])
        top_n = scored[: settings.overvalued_top_n]

        # Сравниваем с предыдущим списком
        prev_raw = await self._redis.get(REDIS_OVERVALUED_KEY)
        prev_symbols: set[str] = set()
        if prev_raw:
            try:
                prev_symbols = {item["symbol"] for item in json.loads(prev_raw)}
            except Exception:
                pass

        new_symbols = {item["symbol"] for item in top_n}

        # Сохраняем новый список
        await self._redis.setex(
            REDIS_OVERVALUED_KEY, REDIS_OVERVALUED_TTL, json.dumps(top_n)
        )

        # Уведомляем если появились новые монеты
        # или каждые 3 пересчёта (15 минут) если список не пустой
        added_symbols = new_symbols - prev_symbols
        self._overvalued_broadcast_count += 1

        should_broadcast = (added_symbols and top_n) or (
            top_n and self._overvalued_broadcast_count % 3 == 0
        )

        if should_broadcast:
            await self._broadcast_overvalued(top_n, added_symbols or new_symbols)

        # Persist snapshot to DB
        if top_n and self._db:
            await self._persist_overvalued_snapshot(top_n)

        logger.info(
            "Overvalued ranking rebuilt",
            total_scored=len(scored),
            top_n=len(top_n),
            new_entries=len(added_symbols),
            threshold=25,
        )

    async def _broadcast_overvalued(
        self,
        items: list[dict],
        new_symbols: set[str],
    ) -> None:
        """
        Отправить обновлённый список переоценённых монет всем пользователям из Redis.
        """
        if not self._alert_callback:
            return

        try:
            from app.bot.formatters import format_overvalued_list
            from app.bot.handlers.overvalued import overvalued_keyboard
            from app.bot.user_store import get_active_users

            # Получаем пользователей из Redis
            user_ids = await get_active_users(self._redis)

            if not user_ids:
                logger.warning("No active users for overvalued broadcast")
                return

            new_list = ", ".join(f"<b>{s}</b>" for s in sorted(new_symbols))
            text = (
                f"📊 <b>Список переоценённых монет обновился</b>\n"
                f"🆕 Новые монеты: {new_list}\n\n" + format_overvalued_list(items)
            )
            keyboard = overvalued_keyboard()

            if not self._bot:
                return

            bot = self._bot

            for user_id in user_ids:
                try:
                    await bot.send_message(
                        chat_id=user_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(
                        "Overvalued broadcast failed",
                        user_id=user_id,
                        error=str(e),
                    )

            logger.info(
                "Overvalued broadcast sent",
                users=len(user_ids),
                new_symbols=list(new_symbols),
            )

        except Exception as e:
            logger.error("Overvalued broadcast error", error=str(e))

    async def _persist_overvalued_snapshot(self, items: list[dict]) -> None:
        """Save ranked snapshot to PostgreSQL."""
        try:
            from app.db.models.overvalued import OvervaluedSnapshot
            from app.db.session import AsyncSessionLocal

            batch_id = str(uuid.uuid4())
            async with AsyncSessionLocal() as session:
                for rank, item in enumerate(items, 1):
                    row = OvervaluedSnapshot(
                        batch_id=batch_id,
                        rank=rank,
                        symbol=item["symbol"],
                        score=item["score"],
                        risk_level=item["risk_level"],
                        price=item["price"],
                        volume_24h_usdt=item["volume_24h_usdt"],
                        rsi=item["rsi"],
                        vwap_extension_pct=item["vwap_extension_pct"],
                        top_reasons=",".join(item.get("top_reasons", [])),
                    )
                    session.add(row)
                await session.commit()
        except Exception as e:
            logger.error("DB overvalued persist failed", error=str(e))

