"""
AutoShortService — автоматически открывает paper short при сигнале,
мониторит цену и закрывает по TP / SL / времени,
сохраняет метрики в БД для дальнейшего обучения.

Плечо: 20x
TP: 45% PnL  -> движение цены -2.25%
SL: 25% PnL  -> движение цены +1.25%
Risk/Reward: 1:1.8
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.scoring.engine import RiskScore
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Параметры шорта ───────────────────────────────────────────────

LEVERAGE = 20
TARGET_PNL_PCT = 45.0
TARGET_SL_PCT = 25.0

TP_PRICE_MOVE = TARGET_PNL_PCT / LEVERAGE   # 2.25% движения цены вниз
SL_PRICE_MOVE = TARGET_SL_PCT / LEVERAGE    # 1.25% движения цены вверх

# ── Параметры отложенного входа ───────────────────────────────────

ENTRY_DELAY_SEC = 60                    # задержка после сигнала перед первой проверкой
MONITOR_ATTEMPTS = 24                   # макс. количество проверок при мониторинге входа
MONITOR_INTERVAL_SEC = 5                # интервал между проверками входа (секунды)
MIN_SCORE_TO_ENTER = 40                 # минимальный score для входа в сделку
STABILIZATION_THRESHOLD_PCT = 0.3       # порог стабилизации — входим если рост ≤ этого значения
MAX_RISE_PCT = 1.5                      # отмена входа если рост превышает этот порог
MAX_ENTRY_DROP_PCT = -0.30              # отмена входа если цена уже упала от сигнала

# ── Параметры мониторинга открытых сделок ─────────────────────────

TRADE_MONITOR_INTERVAL = 5              # интервал проверки открытых сделок (секунды)
MAX_TRADE_DURATION = 60 * 60 * 4        # макс. длительность сделки (4 часа)

# ── Redis key for active shorts (shared across workers) ──────────
REDIS_ACTIVE_SHORTS_KEY = "active_shorts"


def _serialize_trade(trade: dict[str, Any]) -> str:
    """Serialize trade dict to JSON for Redis storage."""
    data = dict(trade)
    if isinstance(data.get("entry_ts"), datetime):
        data["entry_ts"] = data["entry_ts"].isoformat()
    return json.dumps(data)


def _deserialize_trade(raw: str) -> dict[str, Any]:
    """Deserialize trade dict from Redis JSON."""
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

    # ── Redis-backed active shorts ───────────────────────────────

    async def _get_active_short(self, trade_id: int) -> dict[str, Any] | None:
        raw = await self._redis.hget(REDIS_ACTIVE_SHORTS_KEY, str(trade_id))
        if raw:
            return _deserialize_trade(raw)
        return None

    async def _set_active_short(self, trade_id: int, trade: dict[str, Any]) -> None:
        await self._redis.hset(REDIS_ACTIVE_SHORTS_KEY, str(trade_id), _serialize_trade(trade))

    async def _del_active_short(self, trade_id: int) -> None:
        await self._redis.hdel(REDIS_ACTIVE_SHORTS_KEY, str(trade_id))

    async def _get_all_active_shorts(self) -> dict[int, dict[str, Any]]:
        raw_all = await self._redis.hgetall(REDIS_ACTIVE_SHORTS_KEY)
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

    def _calc_short_pnl_pct(self, entry_price: float, current_price: float) -> float:
        price_move_pct = ((entry_price - current_price) / entry_price) * 100
        return price_move_pct * LEVERAGE

    def _build_tp_price(self, entry_price: float) -> float:
        return entry_price * (1 - TP_PRICE_MOVE / 100)

    def _build_sl_price(self, entry_price: float) -> float:
        return entry_price * (1 + SL_PRICE_MOVE / 100)

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

    # ── Оценка условий входа ──────────────────────────────────────

    def _evaluate_entry_conditions(
        self,
        price_change_pct: float,
        current_score: float,
        symbol: str,
    ) -> str:
        """
        Проверяет условия входа и возвращает решение:
        - "enter"        — можно открывать шорт
        - "monitor"      — цена ещё растёт, нужен мониторинг
        - "cancel_score" — score упал ниже минимума
        - "cancel_drop"  — цена уже упала слишком сильно
        - "cancel_rise"  — цена улетела слишком высоко
        """
        if current_score < MIN_SCORE_TO_ENTER:
            logger.debug(
                "Entry check: score below minimum",
                symbol=symbol,
                score=round(current_score, 1),
                min_score=MIN_SCORE_TO_ENTER,
            )
            return "cancel_score"

        if price_change_pct < MAX_ENTRY_DROP_PCT:
            logger.debug(
                "Entry check: price dropped too much",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                threshold=MAX_ENTRY_DROP_PCT,
            )
            return "cancel_drop"

        if price_change_pct > MAX_RISE_PCT:
            logger.debug(
                "Entry check: price rose too much",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                max_rise=MAX_RISE_PCT,
            )
            return "cancel_rise"

        if price_change_pct > STABILIZATION_THRESHOLD_PCT:
            logger.debug(
                "Entry check: price still rising above stabilization threshold",
                symbol=symbol,
                change_pct=round(price_change_pct, 3),
                threshold=STABILIZATION_THRESHOLD_PCT,
            )
            return "monitor"

        return "enter"

    # ── Получение текущего score из Redis ─────────────────────────

    async def _get_current_score(self, symbol: str) -> float | None:
        """Получает актуальный score символа из Redis."""
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

    # ── Мониторинг входа (ожидание стабилизации) ──────────────────

    async def _monitor_entry(
        self,
        symbol: str,
        signal_price: float,
        initial_score: float,
    ) -> tuple[float, float] | None:
        """
        Мониторинг входа: до MONITOR_ATTEMPTS проверок каждые MONITOR_INTERVAL_SEC.
        На каждой проверке заново получает price и score.
        Возвращает (entry_price, price_change_pct) при стабилизации или None при отмене.
        """
        logger.info(
            "Price still rising — monitoring for entry",
            symbol=symbol,
            max_attempts=MONITOR_ATTEMPTS,
            interval_sec=MONITOR_INTERVAL_SEC,
        )

        for attempt in range(MONITOR_ATTEMPTS):
            await asyncio.sleep(MONITOR_INTERVAL_SEC)

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
                max_attempts=MONITOR_ATTEMPTS,
                signal_price=signal_price,
                current_price=current_price,
                change_pct=round(price_change_pct, 3),
                current_score=round(current_score, 1),
            )

            decision = self._evaluate_entry_conditions(
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
                return current_price, price_change_pct

            if decision == "cancel_score":
                logger.info(
                    "Canceled because score dropped",
                    symbol=symbol,
                    attempt=attempt + 1,
                    score=round(current_score, 1),
                    min_score=MIN_SCORE_TO_ENTER,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
                    price_change_pct=price_change_pct,
                    score=current_score,
                    reason="score_dropped",
                )
                return None

            if decision == "cancel_drop":
                logger.info(
                    "Canceled because price dropped too much during monitoring",
                    symbol=symbol,
                    attempt=attempt + 1,
                    change_pct=round(price_change_pct, 3),
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
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
                    max_rise=MAX_RISE_PCT,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
                    price_change_pct=price_change_pct,
                    score=current_score,
                    reason="price_too_high",
                )
                return None

            # decision == "monitor" → продолжаем цикл

        # ── Таймаут мониторинга ───────────────────────────────────
        logger.info(
            "Canceled because monitoring timeout",
            symbol=symbol,
            attempts=MONITOR_ATTEMPTS,
            total_sec=MONITOR_ATTEMPTS * MONITOR_INTERVAL_SEC,
        )

        last_price = await self._get_price(symbol)
        last_score = await self._get_current_score(symbol) or initial_score
        last_change = (
            self._calc_price_move_pct(signal_price, last_price)
            if last_price
            else 0.0
        )

        await self._notify_entry_canceled(
            symbol=symbol,
            signal_price=signal_price,
            current_price=last_price or signal_price,
            price_change_pct=last_change,
            score=last_score,
            reason="timeout",
        )
        return None

    # ── Уведомление об отмене входа ───────────────────────────────

    async def _notify_entry_canceled(
        self,
        symbol: str,
        signal_price: float,
        current_price: float,
        price_change_pct: float,
        score: float,
        reason: str,
    ) -> None:
        """Отправляет уведомление об отмене входа с указанием причины."""
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

            reason_details = {
                "score_dropped": (
                    f"⚠️ Score: <b>{score:.0f}</b> (мин. {MIN_SCORE_TO_ENTER})\n\n"
                    f"<i>Score упал ниже порога — вход отменён</i>"
                ),
                "price_dropped": (
                    f"📉 Изменение: <b>{price_change_pct:+.2f}%</b> "
                    f"(порог {MAX_ENTRY_DROP_PCT}%)\n\n"
                    f"<i>Цена уже упала — движение произошло без нас</i>"
                ),
                "price_too_high": (
                    f"📈 Рост: <b>+{abs(price_change_pct):.2f}%</b> "
                    f"(порог +{MAX_RISE_PCT}%)\n\n"
                    f"<i>Памп слишком сильный — вход отменён во избежание риска</i>"
                ),
                "timeout": (
                    f"📈 Изменение: <b>{price_change_pct:+.2f}%</b>\n"
                    f"⏱ Мониторинг: {MONITOR_ATTEMPTS} × {MONITOR_INTERVAL_SEC}с "
                    f"({MONITOR_ATTEMPTS * MONITOR_INTERVAL_SEC}с)\n\n"
                    f"<i>Стабилизация не наступила — вход отменён по таймауту</i>"
                ),
            }

            detail = reason_details.get(reason, f"<i>Причина: {reason}</i>")

            text = (
                f"⏭ <b>Сигнал пропущен</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"📊 Score: <b>{score:.0f}</b>\n\n"
                f"📍 Цена сигнала: <b>${signal_price:.6g}</b>\n"
                f"📍 Текущая цена: <b>${current_price:.6g}</b>\n"
                f"{detail}"
            )

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
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

    # ── Восстановление активных сделок ────────────────────────────

    async def restore_active_trades(self) -> None:
        try:
            from sqlalchemy import select
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            # Clear stale Redis entries before restoring from DB
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

            for trade in open_trades:
                now = datetime.now(timezone.utc)
                elapsed = (now - trade.entry_ts).total_seconds()

                if elapsed >= MAX_TRADE_DURATION:
                    current_price = await self._get_price(trade.symbol)
                    if current_price:
                        pnl = self._calc_short_pnl_pct(trade.entry_price, current_price)
                        await self._update_db(
                            trade_id=trade.id,
                            exit_price=current_price,
                            exit_ts=now,
                            status="closed",
                            close_reason="expired",
                            pnl=pnl,
                            ml_label=1 if pnl > 0 else 0,
                        )
                        logger.info(
                            "Expired trade closed on restore",
                            trade_id=trade.id,
                            symbol=trade.symbol,
                            pnl=f"{pnl:+.2f}%",
                        )
                    continue

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

    # ── Открытие шорта (основной flow) ────────────────────────────

    async def open_short(self, risk_score: RiskScore) -> None:
        symbol = risk_score.symbol
        lock = self._get_symbol_lock(symbol)

        # Phase 1: Acquire lock, check duplicates, mark pending, release lock
        async with lock:
            if await self._is_symbol_already_open(symbol):
                logger.info(
                    "Skipping short — already have open trade for symbol",
                    symbol=symbol,
                )
                return
            if symbol in self._pending_symbols:
                logger.info(
                    "Skipping short — symbol already pending entry",
                    symbol=symbol,
                )
                return
            self._pending_symbols.add(symbol)

        # Phase 2: Sleep and monitor entry WITHOUT holding the lock
        try:
            signal_price = await self._get_price(symbol)
            if not signal_price:
                logger.warning("Cannot open short — no price at signal", symbol=symbol)
                return

            logger.info(
                "Short signal received — waiting before entry",
                symbol=symbol,
                signal_price=signal_price,
                delay_sec=ENTRY_DELAY_SEC,
                score=risk_score.score,
            )

            await asyncio.sleep(ENTRY_DELAY_SEC)

            entry_price = await self._get_price(symbol)
            if not entry_price:
                logger.warning("Cannot open short — no price after delay", symbol=symbol)
                return

            current_score = await self._get_current_score(symbol)
            effective_score = (
                current_score if current_score is not None else risk_score.score
            )

            price_change_pct = self._calc_price_move_pct(signal_price, entry_price)

            logger.info(
                "Price check after delay",
                symbol=symbol,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                current_score=round(effective_score, 1),
            )

            # ── Трендовый фильтр ──────────────────────────────────
            trend_ok = await self._check_trend_filter(risk_score)
            if not trend_ok:
                logger.info(
                    "Skipping short — strong uptrend detected",
                    symbol=symbol,
                    score=risk_score.score,
                )
                return

            # ── Оценка условий входа ──────────────────────────────
            decision = self._evaluate_entry_conditions(
                price_change_pct=price_change_pct,
                current_score=effective_score,
                symbol=symbol,
            )

            if decision == "cancel_score":
                logger.info(
                    "Canceled because score dropped",
                    symbol=symbol,
                    score=round(effective_score, 1),
                    min_score=MIN_SCORE_TO_ENTER,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="score_dropped",
                )
                return

            if decision == "cancel_drop":
                logger.info(
                    "Skipping short — price already dropped too much from signal",
                    symbol=symbol,
                    signal_price=signal_price,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_dropped",
                )
                return

            if decision == "cancel_rise":
                logger.info(
                    "Canceled because price rose too much after delay",
                    symbol=symbol,
                    change_pct=round(price_change_pct, 3),
                    max_rise=MAX_RISE_PCT,
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=entry_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_too_high",
                )
                return

            if decision == "monitor":
                entry_result = await self._monitor_entry(
                    symbol=symbol,
                    signal_price=signal_price,
                    initial_score=effective_score,
                )
                if entry_result is None:
                    # Мониторинг завершился отменой — уведомление уже отправлено
                    return
                entry_price, price_change_pct = entry_result

            # decision == "enter" или успешный выход из мониторинга

            # Phase 3: Re-acquire lock for final check and trade creation
            async with lock:
                if await self._is_symbol_already_open(symbol):
                    logger.info(
                        "Skipping short after monitoring — trade already opened in parallel",
                        symbol=symbol,
                    )
                    return

                tp_price = self._build_tp_price(entry_price)  # type: ignore
                sl_price = self._build_sl_price(entry_price)  # type: ignore

                trade_id = await self._save_to_db(
                    risk_score=risk_score,
                    entry_price=entry_price,  # type: ignore
                    signal_price=signal_price,
                    price_change_at_entry=price_change_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
                )

                if not trade_id:
                    logger.warning(
                        "Failed to persist short trade",
                        symbol=symbol,
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
                    "score": risk_score.score,
                    "entry_ts": datetime.now(timezone.utc),
                    "price_15m_saved": False,
                    "price_30m_saved": False,
                    "price_60m_saved": False,
                }

                await self._set_active_short(trade_id, trade_payload)

            logger.info(
                "Auto short opened",
                trade_id=trade_id,
                symbol=symbol,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                tp_price=tp_price,
                sl_price=sl_price,
                tp_pct=TP_PRICE_MOVE,
                sl_pct=SL_PRICE_MOVE,
                score=risk_score.score,
            )

            await self._notify_opened(
                trade_id=trade_id,
                symbol=symbol,
                signal_price=signal_price,
                entry_price=entry_price,  # type: ignore
                price_change_pct=price_change_pct,
                tp_price=tp_price,
                sl_price=sl_price,
                score=risk_score.score,
            )

            task = asyncio.create_task(self._monitor_trade(trade_id))
            self._track_task(trade_id, task)

        finally:
            self._pending_symbols.discard(symbol)

    async def _check_trend_filter(self, risk_score: RiskScore) -> bool:
        """
        Возвращает True если можно открывать шорт.
        Возвращает False если обнаружен сильный восходящий тренд.

        Признаки сильного тренда (все три = блокируем вход):
        1. price_change_15m > +3% — цена выросла более чем на 3% за 15 минут
        2. consecutive_green_candles >= 7 — 7+ зелёных свечей подряд
        3. rsi_14_1m > 85 — RSI в экстремальной зоне (памп ещё в разгаре)
        """
        features = risk_score.features_snapshot
        if not features:
            return True  # нет данных — не блокируем

        price_change_15m = features.price_change_15m
        green_candles = features.consecutive_green_candles
        rsi = features.rsi_14_1m

        # Считаем сколько признаков тренда сработало
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

        # Блокируем если 2 или более признаков тренда
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

    # ── Мониторинг открытой сделки (TP / SL / время) ──────────────

    async def _monitor_trade(self, trade_id: int) -> None:
        trade = await self._get_active_short(trade_id)
        if not trade:
            return

        symbol = trade["symbol"]
        entry_price = trade["entry_price"]
        entry_ts = trade["entry_ts"]

        TRAILING_ACTIVATE_PCT = 10.0   # активировать при +10% P&L
        BREAKEVEN_BUFFER_PCT = 0.1     # SL на 0.1% выше входа
        MAX_LOSS_PCT = -50.0           # аварийный стоп

        trailing_activated = False

        while trade["status"] == "open":
            await asyncio.sleep(TRADE_MONITOR_INTERVAL)

            current_price = await self._get_price(symbol)
            if not current_price:
                continue

            now = datetime.now(timezone.utc)
            elapsed = (now - entry_ts).total_seconds()

            await self._save_price_snapshot(trade_id, trade, current_price, elapsed, now)

            pnl = self._calc_short_pnl_pct(entry_price, current_price)

            # ── Аварийный стоп ────────────────────────────────────
            if pnl <= MAX_LOSS_PCT:
                logger.warning(
                    "Max loss reached — emergency stop",
                    trade_id=trade_id,
                    symbol=symbol,
                    pnl=f"{pnl:+.2f}%",
                )
                await self._close_trade(trade_id, current_price, "sl_hit", pnl)
                return

            # ── Трейлинг = перенос SL на безубыток ───────────────
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

            # ── Проверяем TP ──────────────────────────────────────
            if current_price <= trade["tp_price"]:
                await self._close_trade(trade_id, current_price, "tp_hit", pnl)
                return

            # ── Проверяем SL ──────────────────────────────────────
            if current_price >= trade["sl_price"]:
                reason = "trailing_sl" if trailing_activated else "sl_hit"
                await self._close_trade(trade_id, current_price, reason, pnl)
                return

            # ── Истечение времени ─────────────────────────────────
            if elapsed >= MAX_TRADE_DURATION:
                await self._close_trade(trade_id, current_price, "expired", pnl)
                return

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

        allowed_reasons = {"tp_hit", "sl_hit", "trailing_sl", "manual", "expired", "closed_manual"}
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
            leverage=LEVERAGE,
            ml_label=ml_label,
        )

        await self._notify_closed(trade_id, trade["symbol"], exit_price, pnl, reason)
        await self._del_active_short(trade_id)

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

    async def _save_to_db(
        self,
        risk_score: RiskScore,
        entry_price: float,
        signal_price: float,
        price_change_at_entry: float,
        tp_price: float,
        sl_price: float,
    ) -> int | None:
        try:
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

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
                entry_delay_sec=ENTRY_DELAY_SEC,
                leverage=LEVERAGE,
                tp_pct=TARGET_PNL_PCT,
                sl_pct=TARGET_SL_PCT,
                tp_price=tp_price,
                sl_price=sl_price,
                status="open",
                score=risk_score.score,
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
                volume_24h_usdt=features.volume_24h_usdt if features else None,
                price_change_5m=features.price_change_5m if features else None,
                spread_pct=features.spread_pct if features else None,
                bid_depth_change_5m=features.bid_depth_change_5m if features else None,
                # ML enrichment columns
                btc_change_15m=features.btc_change_15m if features else None,
                funding_rate_at_signal=features.funding_rate if features else None,
                oi_change_pct_at_signal=features.oi_change_pct if features else None,
                trend_strength_1h=(
                    features.trend_context.trend_strength
                    if features and features.trend_context else None
                ),
            )

            async with AsyncSessionLocal() as session:
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                return trade.id

        except Exception as e:
            logger.exception("Auto short DB save failed", error=str(e))
            return None

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

    async def _get_price(self, symbol: str) -> float | None:
        # Try 1: Redis features key (real-time WS data, written by ingestion)
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

        # Try 2: Redis score key (scoring cycle snapshot, up to 30s stale)
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

        # Try 3: Shared REST client (reuses persistent session)
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

        # Try 4: Fallback to in-memory cache
        cached = self._price_cache.get(symbol)
        if cached:
            logger.debug("Using cached price", symbol=symbol, price=cached)
            return cached

        return None

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

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
            change_em = "🔴" if price_change_pct > 0 else "🟢"

            text = (
                f"🤖 <b>Авто-шорт открыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"📊 Score: <b>{score:.0f}</b>\n\n"
                f"📍 Цена сигнала: <b>${signal_price:.6g}</b>\n"
                f"{change_em} Цена входа: <b>${entry_price:.6g}</b> "
                f"({price_change_pct:+.2f}% за {ENTRY_DELAY_SEC}с)\n\n"
                f"🎯 TP: ${tp_price:.6g} (-{TP_PRICE_MOVE:.2f}% = +{TARGET_PNL_PCT:.0f}% P&L)\n"
                f"🛑 SL: ${sl_price:.6g} (+{SL_PRICE_MOVE:.2f}% = -{TARGET_SL_PCT:.0f}% P&L)\n"
                f"⚡ Плечо: {LEVERAGE}x\n\n"
                f"<i>Сделка #{trade_id} | Бот следит автоматически</i>"
            )

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Notify open failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Open notification failed", error=str(e))

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

            text = (
                f"{result_em} <b>Авто-шорт закрыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"{reason_text}\n\n"
                f"💰 Выход: <b>${exit_price:.6g}</b>\n"
                f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>\n"
                f"⚡ Плечо: {LEVERAGE}x\n\n"
                f"<i>Сделка #{trade_id} | /stats для статистики</i>"
            )

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    logger.info(
                        "Close notification sent",
                        trade_id=trade_id,
                        user_id=user_id,
                        pnl=f"{pnl:+.2f}%",
                    )
                except Exception as e:
                    logger.warning(
                        "Close notification failed",
                        user_id=user_id,
                        error=str(e),
                    )

        except Exception as e:
            logger.error("Close notification error", error=str(e))
