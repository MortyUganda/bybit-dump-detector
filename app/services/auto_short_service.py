"""
AutoShortService — автоматически открывает paper short при сигнале,
мониторит цену и закрывает по TP / SL / времени,
сохраняет метрики в БД для дальнейшего обучения.
"""
from __future__ import annotations

import asyncio
import json
import random
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
SCORE_RECHECK_TOLERANCE = 5
SCORE_RECHECK_FLOOR = 40
STABILIZATION_THRESHOLD_PCT = 0.2
MAX_RISE_PCT = 0.8
MAX_ENTRY_DROP_PCT = -0.3

TRADE_MONITOR_INTERVAL = 5
MAX_TRADE_DURATION = 60 * 60 * 4

REDIS_ACTIVE_SHORTS_KEY = "active_shorts"

# Multi-level BTC entry filter — block short if BTC pumps on ANY timeframe
BTC_ENTRY_FILTER_1M = 0.15
BTC_ENTRY_FILTER_5M = 0.3
BTC_ENTRY_FILTER_15M = 0.45
BTC_ENTRY_FILTER_1H = 0.8


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
        self._canceled_price_tasks: set[asyncio.Task] = set()
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

        allowed_min_score = max(min_score_to_enter - SCORE_RECHECK_TOLERANCE, SCORE_RECHECK_FLOOR)
        if current_score < allowed_min_score:
            logger.debug(
                "Entry check: score below allowed minimum",
                symbol=symbol,
                score=round(current_score, 1),
                min_score=min_score_to_enter,
                allowed_min_score=allowed_min_score,
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
            allowed_min_score=allowed_min_score,
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

    # ── Reversal risk detection ──────────────────────────────────

    async def _get_reversal_config(self) -> dict[str, Any]:
        cfg = await self._get_strategy()
        return {
            "enabled": cfg.get("reversal_enabled", True),
            "warning_threshold": int(cfg.get("reversal_warning_threshold", 4)),
            "critical_threshold": int(cfg.get("reversal_critical_threshold", 7)),
            "action": cfg.get("reversal_action", "tighten_trailing"),
            "pnl_filter": cfg.get("reversal_pnl_filter", "always"),
        }

    async def _get_entry_snapshot(self, symbol: str) -> dict[str, Any]:
        """Capture feature values at entry for reversal comparison."""
        snapshot: dict[str, Any] = {}
        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                factors = {f["name"]: f["raw_value"] for f in data.get("factors", [])}
                snapshot["volume_zscore"] = factors.get("volume_zscore", 0.0)
                snapshot["large_buy_cluster"] = factors.get("large_buy_cluster", 0.0)
                snapshot["rsi_1m"] = factors.get("rsi_1m", 50.0)
                snapshot["ob_bid_thinning"] = factors.get("ob_bid_thinning", 0.0)
                snapshot["funding_rate"] = factors.get("funding_rate", 0.0)
                snapshot["cvd_divergence"] = factors.get("cvd_divergence", 0.0)
                snapshot["consecutive_greens"] = factors.get("consecutive_greens", 0.0)
                snapshot["spread_expansion"] = factors.get("spread_expansion", 0.0)
        except Exception as e:
            logger.debug("Entry snapshot capture failed", symbol=symbol, error=str(e))
        return snapshot

    async def _calc_reversal_score(
        self, symbol: str, entry_snapshot: dict[str, Any],
    ) -> tuple[int, list[str]]:
        """
        Calculate reversal risk score (0-11) by evaluating factors
        that suggest the dump may be reversing.
        Returns (score, list of triggered factor descriptions).
        """
        score = 0
        triggered: list[str] = []

        try:
            raw = await self._redis.get(f"score:{symbol}")
            if not raw:
                return 0, []
            data = json.loads(raw)
        except Exception:
            return 0, []

        factors = {f["name"]: f["raw_value"] for f in data.get("factors", [])}
        entry_vol = entry_snapshot.get("volume_zscore", 0.0)

        # 1. Volume exhaustion: current volume < 50% of entry volume (weight 1)
        cur_vol = factors.get("volume_zscore", 0.0)
        if entry_vol > 0 and cur_vol < entry_vol * 0.5:
            score += 1
            triggered.append(f"Объём упал ({cur_vol:.1f} vs {entry_vol:.1f} при входе)")

        # 2. Large buy cluster appeared (weight 2)
        cur_large_buy = factors.get("large_buy_cluster", 0.0)
        if cur_large_buy > 0:
            score += 2
            triggered.append(f"Крупные покупки ({cur_large_buy:.0f})")

        # 3. RSI oversold < 30 (weight 1)
        cur_rsi = factors.get("rsi_1m", 50.0)
        if cur_rsi < 30:
            score += 1
            triggered.append(f"RSI перепродан ({cur_rsi:.1f})")

        # 4. Bid depth recovery: ob_bid_thinning improved 20%+ (weight 1)
        entry_bid = entry_snapshot.get("ob_bid_thinning", 0.0)
        cur_bid = factors.get("ob_bid_thinning", 0.0)
        # ob_bid_thinning is negative when bids thin; recovery = less negative / positive
        if entry_bid < -5 and (cur_bid - entry_bid) >= abs(entry_bid) * 0.2:
            score += 1
            triggered.append(f"Bid depth восстановился ({cur_bid:.1f}% vs {entry_bid:.1f}%)")

        # 5. Funding rate negative < -0.01% — squeeze risk (weight 2)
        cur_funding = factors.get("funding_rate", 0.0)
        if cur_funding < -0.0001:
            score += 2
            triggered.append(f"Funding отрицательный ({cur_funding * 100:.4f}%)")

        # 6. CVD reversal: was negative, turned positive (weight 2)
        entry_cvd = entry_snapshot.get("cvd_divergence", 0.0)
        cur_cvd = factors.get("cvd_divergence", 0.0)
        if entry_cvd < 0 and cur_cvd > 0:
            score += 2
            triggered.append(f"CVD развернулся вверх ({cur_cvd:.2f})")

        # 7. Consecutive green candles >= 2 after reds (weight 1)
        cur_greens = factors.get("consecutive_greens", 0.0)
        entry_greens = entry_snapshot.get("consecutive_greens", 0.0)
        if cur_greens >= 2 and entry_greens < 2:
            score += 1
            triggered.append(f"Зелёные свечи подряд ({int(cur_greens)})")

        # 8. Spread normalization: spread returned to normal (weight 1)
        entry_spread = entry_snapshot.get("spread_expansion", 0.0)
        cur_spread = factors.get("spread_expansion", 0.0)
        if entry_spread > 0.1 and cur_spread < entry_spread * 0.5:
            score += 1
            triggered.append("Спред нормализовался")

        return score, triggered

    async def _notify_reversal_risk(
        self,
        trade_id: int,
        symbol: str,
        reversal_score: int,
        pnl: float,
        triggered_factors: list[str],
        level: str,
        action_text: str,
    ) -> None:
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                return

            emoji = "⚠️" if level == "warning" else "🔴"
            level_text = "Риск разворота" if level == "warning" else "КРИТИЧЕСКИЙ риск разворота"

            factors_text = "\n".join(f"• {f}" for f in triggered_factors)

            text = (
                f"{emoji} <b>{level_text} {symbol}</b>\n\n"
                f"📊 Reversal score: <b>{reversal_score}/11</b>\n"
                f"💰 Текущий PnL: <b>{pnl:+.1f}%</b>\n\n"
                f"Сработавшие факторы:\n{factors_text}\n\n"
                f"Действие: <b>{action_text}</b>\n\n"
                f"<i>Сделка #{trade_id}</i>"
            )

            for user_id in user_ids:
                try:
                    await self._bot.send_message(
                        chat_id=user_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("Reversal notify failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Reversal notification failed", error=str(e))

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

            if decision == "cancel_score":
                _min = await self._get_min_score_to_enter()
                logger.info(
                    "Canceled because score dropped",
                    symbol=symbol,
                    attempt=attempt + 1,
                    score=round(current_score, 1),
                    min_score=_min,
                    allowed_min_score=max(_min - SCORE_RECHECK_TOLERANCE, SCORE_RECHECK_FLOOR),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=current_score,
                    cancel_reason="score_dropped",
                    entry_mode_candidate="after_monitor",
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
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=current_score,
                    cancel_reason="price_dropped",
                    entry_mode_candidate="after_monitor",
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
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=current_score,
                    cancel_reason="price_too_high",
                    entry_mode_candidate="after_monitor",
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

        await self._save_canceled_signal(
            risk_score=risk_score,
            signal_price=signal_price,
            final_price=last_price or signal_price,
            price_change_pct=last_change,
            final_score=last_score,
            cancel_reason="timeout",
            entry_mode_candidate="after_monitor",
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
            strategy = await self._get_strategy()
            min_score_to_enter = float(strategy.get("min_score_to_enter", MIN_SCORE_TO_ENTER))
            max_entry_drop_pct = float(strategy.get("max_entry_drop_pct", MAX_ENTRY_DROP_PCT))
            max_rise_pct = float(strategy.get("max_rise_pct", MAX_RISE_PCT))
            monitor_attempts = int(strategy.get("monitor_attempts", MONITOR_ATTEMPTS))
            monitor_interval_sec = int(strategy.get("monitor_interval_sec", MONITOR_INTERVAL_SEC))

            allowed_min_score = max(min_score_to_enter - SCORE_RECHECK_TOLERANCE, SCORE_RECHECK_FLOOR)
            reason_details = {
                "score_dropped": (
                    f"⚠️ Допустимый мин.: <b>{allowed_min_score:.0f}</b> (порог {min_score_to_enter:.0f} − толеранс {SCORE_RECHECK_TOLERANCE})\n\n"
                    f"<i>Score упал ниже допустимого минимума — вход отменён</i>"
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

            # BTC filter reasons
            for window in ("1m", "5m", "15m", "1h"):
                reason_details[f"btc_filter_{window}"] = (
                    f"₿ BTC фильтр ({window}) заблокировал вход\n\n"
                    f"<i>BTC растёт слишком быстро на окне {window} — шорт опасен</i>"
                )

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
                        raw_pnl = await self._calc_short_pnl_pct(
                            trade.entry_price,
                            current_price,
                        )
                        leverage = await self._get_leverage()
                        fee_pct = (settings.paper_entry_fee + settings.paper_exit_fee) * leverage * 100
                        final_pnl = raw_pnl - fee_pct
                        await self._update_db(
                            trade_id=trade.id,
                            exit_price=current_price,
                            exit_ts=now,
                            status="closed",
                            close_reason="expired",
                            pnl=final_pnl,
                            ml_label=1 if final_pnl > 0 else 0,
                            raw_pnl_pct=raw_pnl,
                            fee_pct=fee_pct,
                            slippage_pct=0.0,
                            funding_pct=0.0,
                        )
                        logger.info(
                            "Expired trade closed on restore",
                            trade_id=trade.id,
                            symbol=trade.symbol,
                            raw_pnl=f"{raw_pnl:+.2f}%",
                            fee_pct=f"-{fee_pct:.2f}%",
                            pnl=f"{final_pnl:+.2f}%",
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



    async def _save_canceled_signal(
        self,
        risk_score: RiskScore,
        signal_price: float,
        final_price: float,
        price_change_pct: float,
        final_score: float,
        cancel_reason: str,
        entry_mode_candidate: str = "direct",
    ) -> int | None:
        logger.info(
            "Entering canceled signal save",
            symbol=risk_score.symbol,
            cancel_reason=cancel_reason,
            entry_mode_candidate=entry_mode_candidate,
            signal_price=signal_price,
            final_price=final_price,
            price_change_pct=round(price_change_pct, 3),
            final_score=round(float(final_score), 2),
            event1="Entering _save_canceled_signal",
        )
        from app.db.models.canceled_signal import CanceledSignal
        from app.db.session import AsyncSessionLocal

        try:
            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

            volume_24h_usdt = (
                features.volume_24h_usdt
                if features and features.volume_24h_usdt is not None
                else await self._get_volume_24h_usdt(risk_score.symbol)
            )

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
                volume_24h_usdt=volume_24h_usdt,
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
            )
            logger.info(
                "Canceled signal row prepared",
                symbol=risk_score.symbol,
                cancel_reason=cancel_reason,
                signal_type=(
                    risk_score.signal_type.value
                    if risk_score.signal_type
                    else "unknown"
                ),
                volume_24h_usdt=volume_24h_usdt,
                has_features=features is not None,
                event1="Canceled signal row prepared",
            )
            async with AsyncSessionLocal() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
                logger.info(
                    "Canceled signal saved",
                    symbol=risk_score.symbol,
                    cancel_reason=cancel_reason,
                    canceled_signal_id=row.id,
                    event1="Canceled signal saved",
                )
            self._schedule_canceled_price_updates(row.id, risk_score.symbol)
            return row.id

        except Exception as exc:
            logger.exception(
                "Canceled signal DB save failed",
                symbol=risk_score.symbol,
                cancel_reason=cancel_reason,
                error=str(exc),
                error_type=type(exc).__name__,
                has_features=features is not None if "features" in locals() else None,
                event1="Canceled signal DB save failed",
            )
            return None

    # ── Delayed price updates for canceled signals ────────────────

    def _schedule_canceled_price_updates(
        self, canceled_id: int, symbol: str
    ) -> None:
        task = asyncio.create_task(
            self._update_canceled_delayed_prices(canceled_id, symbol)
        )
        self._canceled_price_tasks.add(task)
        task.add_done_callback(self._canceled_price_tasks.discard)

    async def _update_canceled_delayed_prices(
        self, canceled_id: int, symbol: str
    ) -> None:
        from sqlalchemy import update
        from app.db.models.canceled_signal import CanceledSignal
        from app.db.session import AsyncSessionLocal

        delays = [
            (15 * 60, "price_15m", "price_15m_ts"),
            (30 * 60, "price_30m", "price_30m_ts"),
            (60 * 60, "price_60m", "price_60m_ts"),
        ]

        prev_wait = 0
        for delay_sec, col_price, col_ts in delays:
            try:
                await asyncio.sleep(delay_sec - prev_wait)
                prev_wait = delay_sec

                price = await self._get_price(symbol)
                if price is None:
                    logger.warning(
                        "Canceled price update: price unavailable",
                        canceled_id=canceled_id,
                        symbol=symbol,
                        col=col_price,
                    )
                    continue

                now = datetime.now(timezone.utc)
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(CanceledSignal)
                        .where(CanceledSignal.id == canceled_id)
                        .values(**{col_price: price, col_ts: now})
                    )
                    await session.commit()

                logger.info(
                    "Canceled price snapshot saved",
                    canceled_id=canceled_id,
                    symbol=symbol,
                    col=col_price,
                    price=price,
                )
            except asyncio.CancelledError:
                logger.info(
                    "Canceled price update task cancelled",
                    canceled_id=canceled_id,
                    symbol=symbol,
                )
                return
            except Exception as e:
                logger.error(
                    "Canceled price update failed",
                    canceled_id=canceled_id,
                    symbol=symbol,
                    col=col_price,
                    error=str(e),
                )

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
    ) -> int | None:
        try:
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

            volume_24h_usdt = (
                features.volume_24h_usdt
                if features and features.volume_24h_usdt is not None
                else await self._get_volume_24h_usdt(risk_score.symbol)
            )

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
                volume_24h_usdt=volume_24h_usdt,
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
            )

            async with AsyncSessionLocal() as session:
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                return trade.id

        except Exception as e:
            logger.exception("Auto short DB save failed", error=str(e))
            return None

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

            # ── Max concurrent shorts limit ───────────────────────
            all_trades = await self._get_all_active_shorts()
            open_count = sum(
                1 for t in all_trades.values() if t.get("status") == "open"
            )
            if open_count >= settings.max_concurrent_shorts:
                logger.info(
                    "Max concurrent shorts reached",
                    symbol=symbol,
                    signal_type=signal_type,
                    open_count=open_count,
                    limit=settings.max_concurrent_shorts,
                )
                return

            # ── BTC multi-level rally filter at entry ────────────
            # Read pre-computed BTC data from Redis (updated by analyzer btc_filter_loop)
            try:
                btc_raw = await self._redis.get("btc_filter")
                if btc_raw:
                    btc_snap = json.loads(btc_raw)
                    btc_1m = btc_snap.get("btc_change_1m")
                    btc_5m = btc_snap.get("btc_change_5m")
                    btc_15m = btc_snap.get("btc_change_15m")
                    btc_1h = btc_snap.get("btc_change_1h")

                    blocked_window = None
                    # Skip window if value is None (API error) — don't block on missing data
                    if btc_1m is not None and btc_1m >= BTC_ENTRY_FILTER_1M:
                        blocked_window = ("1m", btc_1m, BTC_ENTRY_FILTER_1M)
                    elif btc_5m is not None and btc_5m >= BTC_ENTRY_FILTER_5M:
                        blocked_window = ("5m", btc_5m, BTC_ENTRY_FILTER_5M)
                    elif btc_15m is not None and btc_15m >= BTC_ENTRY_FILTER_15M:
                        blocked_window = ("15m", btc_15m, BTC_ENTRY_FILTER_15M)
                    elif btc_1h is not None and btc_1h >= BTC_ENTRY_FILTER_1H:
                        blocked_window = ("1h", btc_1h, BTC_ENTRY_FILTER_1H)
                    if blocked_window:
                        window, value, threshold = blocked_window
                        logger.info(
                            "Short blocked by BTC rally filter at entry",
                            symbol=symbol,
                            signal_type=signal_type,
                            blocked_by=window,
                            btc_change=round(value, 2),
                            threshold=threshold,
                            btc_1m=round(btc_1m or 0, 2),
                            btc_5m=round(btc_5m or 0, 2),
                            btc_15m=round(btc_15m or 0, 2),
                            btc_1h=round(btc_1h or 0, 2),
                        )
                        signal_price_for_btc = await self._get_price(symbol)
                        if signal_price_for_btc:
                            await self._save_canceled_signal(
                                risk_score=risk_score,
                                signal_price=signal_price_for_btc,
                                final_price=signal_price_for_btc,
                                price_change_pct=0.0,
                                final_score=float(risk_score.score),
                                cancel_reason=f"btc_filter_{window}",
                                entry_mode_candidate="direct",
                            )
                            await self._notify_entry_canceled(
                                symbol=symbol,
                                signal_price=signal_price_for_btc,
                                current_price=signal_price_for_btc,
                                price_change_pct=0.0,
                                score=float(risk_score.score),
                                reason=f"btc_filter_{window}",
                            )
                        return
                else:
                    logger.debug("BTC filter data not in Redis — skipping BTC check")
            except Exception as e:
                logger.warning(
                    "BTC entry filter check failed — proceeding",
                    error=str(e),
                )

            if not await self._check_trend_filter(risk_score):
                logger.info(
                    "Short blocked by trend filter",
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

            entry_delay_sec = int(strategy.get("entry_delay_sec", ENTRY_DELAY_SEC))
            min_score_to_enter = float(strategy.get("min_score_to_enter", MIN_SCORE_TO_ENTER))

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
                float(current_score)
                if current_score is not None
                else float(risk_score.score)
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
            )

            decision = await self._evaluate_entry_conditions(
                price_change_pct=price_change_pct,
                current_score=effective_score,
                symbol=symbol,
            )

            # ── immediate cancels before monitoring ────────────────

            if decision == "cancel_score":
                current_price = entry_price
                allowed_min_score = max(min_score_to_enter - SCORE_RECHECK_TOLERANCE, SCORE_RECHECK_FLOOR)
                logger.info(
                    "Canceled because score dropped before entry",
                    symbol=symbol,
                    signal_type=signal_type,
                    score=round(effective_score, 1),
                    min_score=min_score_to_enter,
                    allowed_min_score=allowed_min_score,
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="score_dropped",
                    entry_mode_candidate="direct",
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="score_dropped",
                )
                return

            if decision == "cancel_drop":
                current_price = entry_price
                logger.info(
                    "Canceled because price dropped too much before entry",
                    symbol=symbol,
                    signal_type=signal_type,
                    change_pct=round(price_change_pct, 3),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="price_dropped",
                    entry_mode_candidate="direct",
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_dropped",
                )
                return

            if decision == "cancel_rise":
                current_price = entry_price
                logger.info(
                    "Canceled because price rose too much before entry",
                    symbol=symbol,
                    signal_type=signal_type,
                    change_pct=round(price_change_pct, 3),
                )
                await self._save_canceled_signal(
                    risk_score=risk_score,
                    signal_price=signal_price,
                    final_price=current_price,
                    price_change_pct=price_change_pct,
                    final_score=effective_score,
                    cancel_reason="price_too_high",
                    entry_mode_candidate="direct",
                )
                await self._notify_entry_canceled(
                    symbol=symbol,
                    signal_price=signal_price,
                    current_price=current_price,
                    price_change_pct=price_change_pct,
                    score=effective_score,
                    reason="price_too_high",
                )
                return

            # ── maybe monitor, maybe enter сразу ──────────────────

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

            # ── final open under lock ─────────────────────────────

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
                )

                if not trade_id:
                    logger.warning(
                        "Failed to persist short trade",
                        symbol=symbol,
                        signal_type=signal_type,
                        entry_price=entry_price,
                    )
                    return

                # Capture entry snapshot for reversal risk detection
                entry_snapshot = await self._get_entry_snapshot(symbol)

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
                    "entry_snapshot": entry_snapshot,
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

        except Exception as e:
            logger.exception(
                "Open short failed",
                symbol=symbol,
                signal_type=signal_type,
                error=str(e),
            )
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

        # Локальные защитные параметры
        MAX_LOSS_PCT = -50.0  # аварийный стоп по PnL, если что-то пошло совсем не так

        # Трейлинг задаём в долях от целевого TP
        TRAILING_FROM_TP_FRACTION = 0.4   # включаем трейлинг, когда достигнуто 40% от TP
        LOCK_IN_FROM_TP_FRACTION  = 0.8   # при 80% от TP фиксируем минимум безубыток
        MAX_DRAWDOWN_FRACTION     = 0.5   # допускаем откат не больше 50% от max_pnl

        trailing_activated = False
        max_pnl_seen = 0.0

        # Funding rate tracking: accumulate funding fees across the trade
        accumulated_funding_pct = 0.0
        last_funding_hour: int | None = None

        # Reversal risk tracking
        last_reversal_notify_ts: float = 0.0
        REVERSAL_COOLDOWN_SEC = 60
        entry_snapshot: dict[str, Any] = trade.get("entry_snapshot", {})

        while trade["status"] == "open":
            trade_monitor_interval = await self._get_trade_monitor_interval()
            max_trade_duration = await self._get_max_trade_duration()

            # Берём актуальные TP/SL из рантайма (в PnL%, а не в движении цены)
            target_pnl_pct = await self._get_target_pnl_pct()  # например, 20.0
            target_sl_pct = await self._get_target_sl_pct()    # например, 10.0

            trailing_activate_pnl = target_pnl_pct * TRAILING_FROM_TP_FRACTION
            lock_in_pnl = target_pnl_pct * LOCK_IN_FROM_TP_FRACTION

            await asyncio.sleep(trade_monitor_interval)

            current_price = await self._get_price(symbol)
            if not current_price:
                continue

            now = datetime.now(timezone.utc)
            elapsed = (now - entry_ts).total_seconds()

            await self._save_price_snapshot(trade_id, trade, current_price, elapsed, now)

            # ── Funding rate check (00:00, 08:00, 16:00 UTC) ─────
            funding_hours = {0, 8, 16}
            current_hour = now.hour
            if current_hour in funding_hours and current_hour != last_funding_hour:
                if now.minute < 5:  # within 5 min window of funding moment
                    funding_rate = await self._get_funding_rate(symbol)
                    if funding_rate is not None:
                        leverage = await self._get_leverage()
                        # For shorts: positive funding → short receives, negative → short pays
                        # funding_impact_pct = funding_rate * leverage * 100 (as % of margin)
                        funding_impact_pct = funding_rate * leverage * 100
                        accumulated_funding_pct += funding_impact_pct
                        last_funding_hour = current_hour
                        logger.info(
                            "Funding rate applied",
                            trade_id=trade_id,
                            symbol=symbol,
                            funding_rate=f"{funding_rate:.6f}",
                            funding_impact_pct=f"{funding_impact_pct:+.4f}%",
                            accumulated_funding_pct=f"{accumulated_funding_pct:+.4f}%",
                        )

            pnl = await self._calc_short_pnl_pct(entry_price, current_price)

            # Жёсткий аварийный стоп по PnL (защита от багов/дегенерата)
            if pnl <= MAX_LOSS_PCT:
                logger.warning(
                    "Max loss reached — emergency stop",
                    trade_id=trade_id,
                    symbol=symbol,
                    pnl=f"{pnl:+.2f}%",
                )
                await self._close_trade(trade_id, current_price, "sl_hit", pnl, accumulated_funding_pct)
                return

            # Обновляем максимум профита
            if pnl > max_pnl_seen:
                max_pnl_seen = pnl

            # Трейлинг: как только профит достиг части TP, начинаем подтягивать SL
            if max_pnl_seen >= trailing_activate_pnl:
                # желаемый минимальный PnL, который хотим гарантировать
                # допускаем откат не больше MAX_DRAWDOWN_FRACTION от max_pnl_seen,
                # но не хуже исходного SL (‑target_sl_pct)
                desired_min_pnl = max(
                    -target_sl_pct,
                    max_pnl_seen * (1.0 - MAX_DRAWDOWN_FRACTION),
                )

                # если почти достигли TP — гарантируем минимум безубыток
                if max_pnl_seen >= lock_in_pnl:
                    desired_min_pnl = max(desired_min_pnl, 0.0)

                # считаем цену SL из желаемого PnL
                # pnl = (entry_price - sl_price) / entry_price * leverage * 100  (для шорта)
                leverage = await self._get_leverage()
                pnl_factor = desired_min_pnl / (leverage * 100.0)
                new_sl_price = entry_price * (1.0 - pnl_factor)

                old_sl = trade["sl_price"]

                # Обновляем SL только если это улучшает нашу позицию:
                # для шорта меньшая цена SL = меньше риск/лучше PnL
                if new_sl_price < old_sl:
                    trade["sl_price"] = new_sl_price
                    await self._set_active_short(trade_id, trade)
                    trailing_activated = True
                    logger.info(
                        "Trailing SL updated",
                        trade_id=trade_id,
                        symbol=symbol,
                        max_pnl_seen=f"{max_pnl_seen:+.2f}%",
                        desired_min_pnl=f"{desired_min_pnl:+.2f}%",
                        old_sl=round(old_sl, 6),
                        new_sl=round(new_sl_price, 6),
                    )

            # ── Reversal risk check ───────────────────────────────
            reversal_cfg = await self._get_reversal_config()
            if reversal_cfg["enabled"] and entry_snapshot:
                should_check = (
                    reversal_cfg["pnl_filter"] == "always"
                    or pnl > 0
                )
                if should_check:
                    rev_score, rev_factors = await self._calc_reversal_score(
                        symbol, entry_snapshot,
                    )

                    now_ts = now.timestamp()
                    can_notify = (now_ts - last_reversal_notify_ts) >= REVERSAL_COOLDOWN_SEC

                    if rev_score >= reversal_cfg["critical_threshold"] and can_notify:
                        action_text = "мониторинг"

                        if reversal_cfg["action"] == "tighten_trailing":
                            # Подтянуть trailing stop вдвое
                            leverage = await self._get_leverage()
                            if max_pnl_seen > 0:
                                old_drawdown = MAX_DRAWDOWN_FRACTION
                                new_drawdown = old_drawdown / 2
                                desired_min_pnl = max(
                                    -target_sl_pct,
                                    max_pnl_seen * (1.0 - new_drawdown),
                                )
                                pnl_factor = desired_min_pnl / (leverage * 100.0)
                                new_sl_price = entry_price * (1.0 - pnl_factor)
                                if new_sl_price < trade["sl_price"]:
                                    old_sl = trade["sl_price"]
                                    trade["sl_price"] = new_sl_price
                                    await self._set_active_short(trade_id, trade)
                                    trailing_activated = True
                                    action_text = f"trailing stop подтянут (SL {old_sl:.6g} → {new_sl_price:.6g})"
                                else:
                                    action_text = "trailing stop уже оптимален"
                            else:
                                action_text = "trailing stop не активирован (нет профита)"

                        elif reversal_cfg["action"] == "auto_close":
                            action_text = "авто-закрытие позиции"
                            await self._notify_reversal_risk(
                                trade_id=trade_id,
                                symbol=symbol,
                                reversal_score=rev_score,
                                pnl=pnl,
                                triggered_factors=rev_factors,
                                level="critical",
                                action_text=action_text,
                            )
                            await self._close_trade(
                                trade_id, current_price, "reversal_close", pnl, accumulated_funding_pct,
                            )
                            return

                        else:
                            action_text = "только уведомление"

                        await self._notify_reversal_risk(
                            trade_id=trade_id,
                            symbol=symbol,
                            reversal_score=rev_score,
                            pnl=pnl,
                            triggered_factors=rev_factors,
                            level="critical",
                            action_text=action_text,
                        )
                        last_reversal_notify_ts = now_ts
                        logger.info(
                            "Reversal risk CRITICAL",
                            trade_id=trade_id,
                            symbol=symbol,
                            reversal_score=rev_score,
                            pnl=f"{pnl:+.2f}%",
                            action=reversal_cfg["action"],
                        )

                    elif rev_score >= reversal_cfg["warning_threshold"] and can_notify:
                        await self._notify_reversal_risk(
                            trade_id=trade_id,
                            symbol=symbol,
                            reversal_score=rev_score,
                            pnl=pnl,
                            triggered_factors=rev_factors,
                            level="warning",
                            action_text="мониторинг",
                        )
                        last_reversal_notify_ts = now_ts
                        logger.info(
                            "Reversal risk WARNING",
                            trade_id=trade_id,
                            symbol=symbol,
                            reversal_score=rev_score,
                            pnl=f"{pnl:+.2f}%",
                        )

            # TP/SL/expiry логика остаётся как была
            if current_price <= trade["tp_price"]:
                await self._close_trade(trade_id, current_price, "tp_hit", pnl, accumulated_funding_pct)
                return

            if current_price >= trade["sl_price"]:
                reason = "trailing_sl" if trailing_activated else "sl_hit"
                await self._close_trade(trade_id, current_price, reason, pnl, accumulated_funding_pct)
                return

            if elapsed >= max_trade_duration:
                await self._close_trade(trade_id, current_price, "expired", pnl, accumulated_funding_pct)
                return
            

    # ── Close trade ───────────────────────────────────────────────

    async def _close_trade(
        self,
        trade_id: int,
        exit_price: float,
        reason: str,
        pnl: float,
        accumulated_funding_pct: float = 0.0,
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
            "reversal_close",
        }
        if reason not in allowed_reasons:
            logger.warning(
                "Unknown close reason, fallback applied",
                trade_id=trade_id,
                reason=reason,
            )
            reason = "manual"

        # ── Slippage on exit (shorts: slippage pushes price up = worse) ──
        slippage_pct = 0.0
        if reason in ("sl_hit", "trailing_sl"):
            slip = random.uniform(0.0001, 0.001)  # 0.01-0.1%
            exit_price *= (1 + slip)
            slippage_pct = slip * 100  # as percentage
        elif reason == "tp_hit":
            slip = random.uniform(0.0001, 0.0005)  # 0.01-0.05%
            exit_price *= (1 + slip)
            slippage_pct = slip * 100

        # ── Recalculate PnL with slippage-adjusted exit price ────
        entry_price = trade["entry_price"]
        raw_pnl = await self._calc_short_pnl_pct(entry_price, exit_price)

        # ── Fees: entry + exit as % of margin ────────────────────
        leverage = await self._get_leverage()
        fee_pct = (settings.paper_entry_fee + settings.paper_exit_fee) * leverage * 100

        # ── Final PnL = raw - fees + funding (funding already signed) ──
        final_pnl = raw_pnl - fee_pct + accumulated_funding_pct

        # Slippage is already baked into raw_pnl via exit_price adjustment,
        # but we track it separately for analytics
        # Convert slippage_pct to margin impact for logging
        slippage_margin_pct = slippage_pct * leverage

        now = datetime.now(timezone.utc)
        ml_label = 1 if final_pnl > 0 else 0

        trade["status"] = "closed"
        trade["close_reason"] = reason

        await self._update_db(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_ts=now,
            status="closed",
            close_reason=reason,
            pnl=final_pnl,
            ml_label=ml_label,
            raw_pnl_pct=raw_pnl,
            fee_pct=fee_pct,
            slippage_pct=slippage_margin_pct,
            funding_pct=accumulated_funding_pct,
        )

        logger.info(
            "Auto short closed",
            trade_id=trade_id,
            symbol=trade["symbol"],
            status="closed",
            close_reason=reason,
            raw_pnl=f"{raw_pnl:+.2f}%",
            fee_impact=f"-{fee_pct:.2f}%",
            slippage_impact=f"-{slippage_margin_pct:.3f}%",
            funding_impact=f"{accumulated_funding_pct:+.4f}%",
            pnl=f"{final_pnl:+.2f}%",
            leverage=leverage,
            ml_label=ml_label,
        )

        logger.info(
            "Applied costs",
            trade_id=trade_id,
            symbol=trade["symbol"],
            fees=f"-{fee_pct:.2f}%",
            slippage=f"-{slippage_margin_pct:.3f}%",
            funding=f"{accumulated_funding_pct:+.4f}%",
        )

        await self._notify_closed(trade_id, trade["symbol"], exit_price, final_pnl, reason, fee_pct, slippage_margin_pct, accumulated_funding_pct)
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
        await self._close_trade(trade_id, current_price, "closed_manual", pnl, 0.0)

        # Re-read the final PnL after costs are applied by _close_trade
        leverage = await self._get_leverage()
        fee_pct = (settings.paper_entry_fee + settings.paper_exit_fee) * leverage * 100
        final_pnl = pnl - fee_pct

        return (
            f"✋ <b>Сделка закрыта вручную</b>\n\n"
            f"📌 #{trade_id} {symbol}\n"
            f"💰 Вход: <b>${float(trade['entry_price']):.6g}</b>\n"
            f"💹 Выход: <b>${float(current_price):.6g}</b>\n"
            f"📊 Результат: <b>{final_pnl:+.2f}%</b> (комиссии: -{fee_pct:.2f}%)"
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
        raw_pnl_pct: float | None = None,
        fee_pct: float | None = None,
        slippage_pct: float | None = None,
        funding_pct: float | None = None,
    ) -> None:
        try:
            from sqlalchemy import update
            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            values = dict(
                status=status,
                exit_price=exit_price,
                exit_ts=exit_ts,
                pnl_pct=pnl,
                ml_label=ml_label,
                close_reason=close_reason,
            )
            if raw_pnl_pct is not None:
                values["raw_pnl_pct"] = raw_pnl_pct
            if fee_pct is not None:
                values["fee_pct"] = fee_pct
            if slippage_pct is not None:
                values["slippage_pct"] = slippage_pct
            if funding_pct is not None:
                values["funding_pct"] = funding_pct

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    update(AutoShort)
                    .where(AutoShort.id == trade_id)
                    .values(**values)
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

    # ── Get 24h volume usdt ───────────────────────────────────────────────
    async def _get_volume_24h_usdt(self, symbol: str) -> float | None:
        try:
            raw = await self._redis.get(f"features:{symbol}")
            if raw:
                data = json.loads(raw)
                volume = data.get("volume_24h_usdt")
                if volume is not None and float(volume) > 0:
                    return float(volume)
        except Exception as e:
            logger.debug("Redis features volume fetch failed", symbol=symbol, error=str(e))

        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                snapshot = data.get("features_snapshot") or {}
                volume = snapshot.get("volume_24h_usdt")
                if volume is not None and float(volume) > 0:
                    return float(volume)
        except Exception as e:
            logger.debug("Redis score volume fetch failed", symbol=symbol, error=str(e))

        if self._rest_client:
            try:
                ticker = await self._rest_client.get_ticker(symbol, category="linear")
                if ticker:
                    volume = ticker.get("turnover24h") or ticker.get("volume24h")
                    if volume is not None and float(volume) > 0:
                        return float(volume)
            except Exception as e:
                logger.debug("REST client volume fetch failed", symbol=symbol, error=str(e))

        return None

    # ── Funding rate fetch ────────────────────────────────────────

    async def _get_funding_rate(self, symbol: str) -> float | None:
        """Get current funding rate from Redis features cache."""
        try:
            raw = await self._redis.get(f"features:{symbol}")
            if raw:
                data = json.loads(raw)
                rate = data.get("funding_rate")
                if rate is not None:
                    return float(rate)
        except Exception as e:
            logger.debug("Redis funding rate fetch failed", symbol=symbol, error=str(e))

        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                snapshot = data.get("features_snapshot") or {}
                rate = snapshot.get("funding_rate")
                if rate is not None:
                    return float(rate)
        except Exception as e:
            logger.debug("Redis score funding rate fetch failed", symbol=symbol, error=str(e))

        return None

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
        fee_pct: float = 0.0,
        slippage_pct: float = 0.0,
        funding_pct: float = 0.0,
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

            # Build costs breakdown line
            costs_parts = []
            if fee_pct > 0:
                costs_parts.append(f"комиссии: -{fee_pct:.2f}%")
            if slippage_pct > 0:
                costs_parts.append(f"slippage: -{slippage_pct:.3f}%")
            if funding_pct != 0:
                costs_parts.append(f"funding: {funding_pct:+.4f}%")
            costs_line = f"\n💸 <i>{', '.join(costs_parts)}</i>" if costs_parts else ""

            text = (
                f"{result_em} <b>Авто-шорт закрыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"{reason_text}\n\n"
                f"💰 Выход: <b>${exit_price:.6g}</b>\n"
                f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>{costs_line}\n"
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