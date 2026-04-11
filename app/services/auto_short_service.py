"""
AutoShortService — автоматически открывает paper short при сигнале,
мониторит цену и закрывает по TP / SL / времени,
сохраняет метрики в БД для дальнейшего обучения.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.scoring.engine import RiskScore
from app.services.runtime_config import get_runtime_strategy_config
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Fallback defaults ─────────────────────────────────────────────

LEVERAGE = 10
TARGET_PNL_PCT = 20.0
TARGET_SL_PCT = 10.0

ENTRY_DELAY_SEC = 60
MONITOR_ATTEMPTS = 24
MONITOR_INTERVAL_SEC = 5
MIN_SCORE_TO_ENTER = 55
STABILIZATION_THRESHOLD_PCT = 0.2
MAX_RISE_PCT = 0.8
MAX_ENTRY_DROP_PCT = -0.3

TRADE_MONITOR_INTERVAL = 5
MAX_TRADE_DURATION = 60 * 60 * 4

REDIS_ACTIVE_SHORTS_KEY = "active_shorts"


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
        raw = await self._redis.hget(REDIS_ACTIVE_SHORTS_KEY, str(trade_id))
        if raw:
            return _deserialize_trade(raw)
        return None

    async def _set_active_short(self, trade_id: int, trade: dict[str, Any]) -> None:
        await self._redis.hset(
            REDIS_ACTIVE_SHORTS_KEY,
            str(trade_id),
            _serialize_trade(trade),
        )

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

    # ── Entry conditions ──────────────────────────────────────────

    async def _evaluate_entry_conditions(
        self,
        price_change_pct: float,
        current_score: float,
        symbol: str,
    ) -> str:
        min_score_to_enter = await self._get_min_score_to_enter()
        max_entry_drop_pct = await self._get_max_entry_drop_pct()
        max_rise_pct = await self._get_max_rise_pct()
        stabilization_threshold_pct = await self._get_stabilization_threshold_pct()

        if current_score < min_score_to_enter:
            logger.debug(
                "Entry check: score below minimum",
                symbol=symbol,
                score=round(current_score, 1),
                min_score=min_score_to_enter,
            )
            decision = "cancel_score"
        elif price_change_pct < max_entry_drop_pct:
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
            min_score=min_score_to_enter,
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
        symbol: str,
        signal_price: float,
        initial_score: float,
    ) -> tuple[float, float] | None:
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
                return current_price, price_change_pct

            if decision == "cancel_score":
                logger.info(
                    "Canceled because score dropped",
                    symbol=symbol,
                    attempt=attempt + 1,
                    score=round(current_score, 1),
                    min_score=await self._get_min_score_to_enter(),
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
                    max_rise=await self._get_max_rise_pct(),
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

        await self._notify_entry_canceled(
            symbol=symbol,
            signal_price=signal_price,
            current_price=last_price or signal_price,
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
        current_price: float,
        price_change_pct: float,
        score: float,
        reason: str,
    ) -> None:
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"
            min_score_to_enter = await self._get_min_score_to_enter()
            max_entry_drop_pct = await self._get_max_entry_drop_pct()
            max_rise_pct = await self._get_max_rise_pct()
            monitor_attempts = await self._get_monitor_attempts()
            monitor_interval_sec = await self._get_monitor_interval_sec()

            reason_details = {
                "score_dropped": (
                    f"⚠️ Score: <b>{score:.0f}</b> (мин. {min_score_to_enter})\n\n"
                    f"<i>Score упал ниже порога входа — вход отменён</i>"
                ),
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

    # ── Restore open trades ───────────────────────────────────────

    async def restore_active_trades(self) -> None:
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
            max_trade_duration = await self._get_max_trade_duration()

            for trade in open_trades:
                now = datetime.now(timezone.utc)
                elapsed = (now - trade.entry_ts).total_seconds()

                if elapsed >= max_trade_duration:
                    current_price = await self._get_price(trade.symbol)
                    if current_price:
                        pnl = await self._calc_short_pnl_pct(
                            trade.entry_price,
                            current_price,
                        )
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
                return

            if symbol in self._pending_symbols:
                logger.info(
                    "Skipping short — symbol already pending entry",
                    symbol=symbol,
                    signal_type=signal_type,
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

            current_score = await self._get_current_score(symbol)
            effective_score = (
                float(current_score) if current_score is not None else float(risk_score.score)
            )
            price_change_pct = self._calc_price_move_pct(signal_price, entry_price)

            logger.info(
                "Price check after delay",
                symbol=symbol,
                signal_type=signal_type,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                current_score=round(effective_score, 1),
                min_score_to_enter=min_score_to_enter,
            )

            trend_ok = await self._check_trend_filter(risk_score)
            if not trend_ok:
                logger.info(
                    "Auto-short entry decision",
                    symbol=symbol,
                    signal_type=signal_type,
                    decision="cancel_trend",
                    signal_price=signal_price,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                    score=round(effective_score, 1),
                )
                logger.info(
                    "Skipping short — strong uptrend detected",
                    symbol=symbol,
                    signal_type=signal_type,
                    score=round(effective_score, 1),
                )
                return

            decision = await self._evaluate_entry_conditions(
                price_change_pct=price_change_pct,
                current_score=effective_score,
                symbol=symbol,
            )

            logger.info(
                "Auto-short entry decision after delay",
                symbol=symbol,
                signal_type=signal_type,
                decision=decision,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                score=round(effective_score, 1),
                min_score_to_enter=min_score_to_enter,
            )

            if decision == "cancel_score":
                logger.info(
                    "Canceled because score dropped",
                    symbol=symbol,
                    signal_type=signal_type,
                    score=round(effective_score, 1),
                    min_score=min_score_to_enter,
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
                    signal_type=signal_type,
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
                max_rise_pct = await self._get_max_rise_pct()
                logger.info(
                    "Canceled because price rose too much after delay",
                    symbol=symbol,
                    signal_type=signal_type,
                    change_pct=round(price_change_pct, 3),
                    max_rise=max_rise_pct,
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

                entry_price, price_change_pct = entry_result

                logger.info(
                    "Auto-short monitoring result",
                    symbol=symbol,
                    signal_type=signal_type,
                    decision="enter_after_monitor",
                    signal_price=signal_price,
                    entry_price=entry_price,
                    change_pct=round(price_change_pct, 3),
                    score=round(effective_score, 1),
                )

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

                trade_id = await self._save_to_db(
                    risk_score=risk_score,
                    entry_price=entry_price,
                    signal_price=signal_price,
                    price_change_at_entry=price_change_pct,
                    tp_price=tp_price,
                    sl_price=sl_price,
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
                signal_type=signal_type,
                signal_price=signal_price,
                entry_price=entry_price,
                change_pct=round(price_change_pct, 3),
                tp_price=tp_price,
                sl_price=sl_price,
                tp_pct=await self._get_target_pnl_pct(),
                sl_pct=await self._get_target_sl_pct(),
                score=round(risk_score.score, 1),
            )

            await self._notify_opened(
                trade_id=trade_id,
                symbol=symbol,
                signal_price=signal_price,
                entry_price=entry_price,
                price_change_pct=price_change_pct,
                tp_price=tp_price,
                sl_price=sl_price,
                score=risk_score.score,
            )

            task = asyncio.create_task(self._monitor_trade(trade_id))
            self._track_task(trade_id, task)

        finally:
            self._pending_symbols.discard(symbol)


    # ── Trend filter ──────────────────────────────────────────────

    async def _check_trend_filter(self, risk_score: RiskScore) -> bool:
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
            max_trade_duration = await self._get_max_trade_duration()

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

            if elapsed >= max_trade_duration:
                await self._close_trade(trade_id, current_price, "expired", pnl)
                return

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

            leverage = await self._get_leverage()
            target_pnl_pct = await self._get_target_pnl_pct()
            target_sl_pct = await self._get_target_sl_pct()
            entry_delay_sec = await self._get_entry_delay_sec()

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
                f_cvd_divergence=factor_map.get("cvd_divergence"),
                f_liquidation_cascade=factor_map.get("liquidation_cascade"),
                realized_vol_1h=features.realized_vol_1h if features else None,
                volume_24h_usdt=features.volume_24h_usdt if features else None,
                price_change_5m=features.price_change_5m if features else None,
                spread_pct=features.spread_pct if features else None,
                bid_depth_change_5m=features.bid_depth_change_5m if features else None,
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

    # ── Update trade in DB ────────────────────────────────────────

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
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                except Exception as e:
                    logger.warning("Notify close failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Close notification failed", error=str(e))