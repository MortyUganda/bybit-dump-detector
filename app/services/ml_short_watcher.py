"""
ML-Short Watcher — фоновый процесс мониторинга paper-позиций.

Каждые 5 секунд проверяет открытые ml_short_positions:
- TP hit: current_price ≤ entry_price * (1 - tp_pct/100)
- SL hit: current_price ≥ entry_price * (1 + sl_pct/100)
- Timeout: NOW() - entry_ts ≥ timeout_hours

При закрытии с убытком — инкремент cooldown.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.services.ml_short_config import get_ml_short_config
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

WATCHER_INTERVAL = 5  # секунд


class MlShortWatcher:
    """Мониторинг открытых ML-short paper-позиций."""

    def __init__(
        self,
        redis: aioredis.Redis,
        bot=None,
        rest_client=None,
    ) -> None:
        self._redis = redis
        self._bot = bot
        self._rest_client = rest_client
        self._running = False
        self._task: asyncio.Task | None = None
        self._price_cache: dict[str, float] = {}

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("ML-Short watcher запущен")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("ML-Short watcher остановлен")

    async def _get_price(self, symbol: str) -> float | None:
        """Получить текущую цену (Redis → REST → кэш)."""
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

    async def _watch_loop(self) -> None:
        """Основной цикл проверки позиций."""
        while self._running:
            try:
                await self._check_positions()
            except asyncio.CancelledError:
                logger.info("ML-Short watcher loop отменён")
                return
            except Exception:
                logger.exception("ML-Short watcher: ошибка в цикле")
            await asyncio.sleep(WATCHER_INTERVAL)

    async def _check_positions(self) -> None:
        """Проверить все открытые позиции на TP/SL/timeout."""
        try:
            from sqlalchemy import select
            from app.db.models.ml_short import MlShortPosition
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(MlShortPosition).where(MlShortPosition.status == "open")
                )
                positions = result.scalars().all()
        except Exception as exc:
            logger.error("ML-Short watcher: ошибка загрузки позиций", error=str(exc))
            return

        if not positions:
            return

        now = datetime.now(timezone.utc)

        for pos in positions:
            try:
                current_price = await self._get_price(pos.symbol)
                if current_price is None:
                    continue

                entry_price = float(pos.entry_price)
                tp_pct = float(pos.tp_pct)
                sl_pct = float(pos.sl_pct)
                timeout_hours = float(pos.timeout_hours)

                tp_price = entry_price * (1 - tp_pct / 100)
                sl_price = entry_price * (1 + sl_pct / 100)

                # TP hit (для шорта: цена упала)
                if current_price <= tp_price:
                    pnl_pct = tp_pct
                    await self._close_position(
                        pos, current_price, pnl_pct, "tp", now,
                    )
                    continue

                # SL hit (для шорта: цена выросла)
                if current_price >= sl_price:
                    pnl_pct = -sl_pct
                    await self._close_position(
                        pos, current_price, pnl_pct, "sl", now,
                    )
                    continue

                # Timeout
                elapsed = now - pos.entry_ts
                if elapsed >= timedelta(hours=timeout_hours):
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100
                    await self._close_position(
                        pos, current_price, pnl_pct, "timeout", now,
                    )

            except Exception as exc:
                logger.error(
                    "ML-Short watcher: ошибка обработки позиции",
                    position_id=pos.id,
                    symbol=pos.symbol,
                    error=str(exc),
                )

    async def _close_position(
        self,
        pos: Any,
        exit_price: float,
        pnl_pct: float,
        close_reason: str,
        now: datetime,
    ) -> None:
        """Закрыть позицию в БД и обновить cooldown при убытке."""
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                await session.execute(
                    text("""
                        UPDATE ml_short_positions
                        SET status = 'closed',
                            exit_ts = :exit_ts,
                            exit_price = :exit_price,
                            pnl_pct = :pnl_pct,
                            close_reason = :close_reason,
                            updated_at = :updated_at
                        WHERE id = :pos_id
                    """),
                    {
                        "exit_ts": now,
                        "exit_price": exit_price,
                        "pnl_pct": round(pnl_pct, 4),
                        "close_reason": close_reason,
                        "updated_at": now,
                        "pos_id": pos.id,
                    },
                )
                await session.commit()

            logger.info(
                "ML-Short: позиция закрыта",
                position_id=pos.id,
                symbol=pos.symbol,
                close_reason=close_reason,
                pnl_pct=round(pnl_pct, 2),
                entry_price=float(pos.entry_price),
                exit_price=exit_price,
            )

            # При убытке — обновить cooldown
            if pnl_pct < 0:
                await self._update_cooldown(pos.symbol)

            # TG уведомление
            await self._notify_closed(pos, exit_price, pnl_pct, close_reason)

        except Exception as exc:
            logger.error(
                "ML-Short watcher: ошибка закрытия позиции",
                position_id=pos.id,
                error=str(exc),
            )

    async def _update_cooldown(self, symbol: str) -> None:
        """Обновить cooldown счётчик после убытка."""
        try:
            cfg = await get_ml_short_config(self._redis)
            if not cfg.get("cooldown_enabled", True):
                return

            max_losses = cfg.get("cooldown_loss_count", 2)
            cooldown_hours = cfg.get("cooldown_hours", 24)

            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal

            now = datetime.now(timezone.utc)

            async with AsyncSessionLocal() as session:
                # Получить текущий счётчик
                result = await session.execute(
                    text("SELECT loss_count FROM ml_short_cooldowns WHERE symbol = :sym"),
                    {"sym": symbol},
                )
                row = result.fetchone()

                if row:
                    new_count = row[0] + 1
                    if new_count >= max_losses:
                        # Активировать cooldown и сбросить счётчик
                        await session.execute(
                            text("""
                                UPDATE ml_short_cooldowns
                                SET loss_count = 0,
                                    last_loss_ts = :now,
                                    cooldown_until = :until,
                                    updated_at = :now
                                WHERE symbol = :sym
                            """),
                            {
                                "sym": symbol,
                                "now": now,
                                "until": now + timedelta(hours=cooldown_hours),
                            },
                        )
                        logger.info(
                            "ML-Short: cooldown активирован",
                            symbol=symbol,
                            cooldown_hours=cooldown_hours,
                        )
                    else:
                        await session.execute(
                            text("""
                                UPDATE ml_short_cooldowns
                                SET loss_count = :cnt,
                                    last_loss_ts = :now,
                                    updated_at = :now
                                WHERE symbol = :sym
                            """),
                            {"sym": symbol, "cnt": new_count, "now": now},
                        )
                else:
                    # Первый убыток по этому символу
                    new_count = 1
                    if new_count >= max_losses:
                        cooldown_until = now + timedelta(hours=cooldown_hours)
                        new_count = 0
                    else:
                        cooldown_until = None
                    await session.execute(
                        text("""
                            INSERT INTO ml_short_cooldowns (symbol, loss_count, last_loss_ts, cooldown_until, updated_at)
                            VALUES (:sym, :cnt, :now, :until, :now)
                        """),
                        {
                            "sym": symbol,
                            "cnt": new_count,
                            "now": now,
                            "until": cooldown_until,
                        },
                    )

                await session.commit()

        except Exception as exc:
            logger.error(
                "ML-Short: ошибка обновления cooldown",
                symbol=symbol,
                error=str(exc),
            )

    async def _notify_closed(
        self,
        pos: Any,
        exit_price: float,
        pnl_pct: float,
        close_reason: str,
    ) -> None:
        """Отправить TG-уведомление о закрытии позиции."""
        if not self._bot:
            return
        try:
            from app.bot.user_store import get_active_users

            user_ids = await get_active_users(self._redis)
            if not user_ids:
                user_ids = settings.allowed_user_ids

            reason_labels = {
                "tp": "🎯 Take Profit",
                "sl": "🛑 Stop Loss",
                "timeout": "⏰ Timeout",
                "manual": "✋ Вручную",
            }
            reason_text = reason_labels.get(close_reason, close_reason)
            pnl_emoji = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < 0 else "⚪"

            text = (
                f"🤖 <b>ML-Short: позиция закрыта</b>\n\n"
                f"📌 #{pos.id} <b>{pos.symbol}</b>\n"
                f"💰 Вход: <b>${float(pos.entry_price):.6g}</b>\n"
                f"💹 Выход: <b>${exit_price:.6g}</b>\n"
                f"{pnl_emoji} PnL: <b>{pnl_pct:+.2f}%</b>\n"
                f"📋 Причина: {reason_text}\n"
                f"🧠 ML proba: <b>{float(pos.ml_proba):.2%}</b>"
                if pos.ml_proba else
                f"🤖 <b>ML-Short: позиция закрыта</b>\n\n"
                f"📌 #{pos.id} <b>{pos.symbol}</b>\n"
                f"💰 Вход: <b>${float(pos.entry_price):.6g}</b>\n"
                f"💹 Выход: <b>${exit_price:.6g}</b>\n"
                f"{pnl_emoji} PnL: <b>{pnl_pct:+.2f}%</b>\n"
                f"📋 Причина: {reason_text}"
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
            logger.warning("ML-Short watcher: ошибка TG-уведомления", error=str(exc))
