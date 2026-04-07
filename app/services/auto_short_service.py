"""
AutoShortService — автоматически открывает paper шорт при сигнале.
Мониторит цену и закрывает при TP/SL.
Сохраняет все метрики в БД для обучения ИИ.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import aiohttp
import redis.asyncio as aioredis

from app.config import get_settings
from app.scoring.engine import RiskScore
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

TP_PCT = 20.0
SL_PCT = 10.0
MONITOR_INTERVAL = 30
MAX_TRADE_DURATION = 60 * 60 * 4  # 4 часа

ACTIVE_SHORTS: dict[int, dict] = {}


class AutoShortService:

    def __init__(self, redis: aioredis.Redis, bot=None) -> None:
        self._redis = redis
        self._bot = bot

        
    async def restore_active_trades(self) -> None:
        """
        При старте сервиса восстановить активные сделки из БД.
        Перезапускает мониторинг для каждой открытой сделки.
        """
        try:
            from sqlalchemy import select

            from app.db.models.auto_short import AutoShort
            from app.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(AutoShort).where(AutoShort.status == "open")
                )
                open_trades = result.scalars().all()

            if not open_trades:
                logger.info("No open trades to restore")
                return

            logger.info(f"Restoring {len(open_trades)} open trades from DB")

            for trade in open_trades:
                now = datetime.now(timezone.utc)
                elapsed = (now - trade.entry_ts).total_seconds()

                # Если сделка уже старше MAX_TRADE_DURATION — закрываем
                if elapsed >= MAX_TRADE_DURATION:
                    current_price = await self._get_price(trade.symbol)
                    if current_price:
                        pnl = (trade.entry_price - current_price) / trade.entry_price * 100
                        await self._update_db(
                            trade_id=trade.id,
                            exit_price=current_price,
                            exit_ts=now,
                            status="expired",
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

                # Восстанавливаем в памяти
                ACTIVE_SHORTS[trade.id] = {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "entry_price": trade.entry_price,
                    "tp_price": trade.tp_price,
                    "sl_price": trade.sl_price,
                    "entry_ts": trade.entry_ts,
                    "status": "open",
                    "price_15m_saved": trade.price_15m is not None,
                    "price_30m_saved": trade.price_30m is not None,
                    "price_60m_saved": trade.price_60m is not None,
                }

                logger.info(
                    "Trade restored",
                    trade_id=trade.id,
                    symbol=trade.symbol,
                    entry=trade.entry_price,
                    elapsed_min=int(elapsed / 60),
                )

                # Перезапускаем мониторинг
                asyncio.create_task(self._monitor_trade(trade.id))

            logger.info(
                "Trades restored",
                count=len(open_trades),
                active=len(ACTIVE_SHORTS),
            )

        except Exception as e:
            logger.error("Failed to restore trades", error=str(e))


    def set_bot(self, bot) -> None:
        self._bot = bot

    async def open_short(self, risk_score: RiskScore) -> None:
        """
        Автоматически открыть paper шорт при сигнале.
        Вызывается из AlertManager.
        """
        symbol = risk_score.symbol
        entry_price = await self._get_price(symbol)
        if not entry_price:
            logger.warning("Cannot open short — no price", symbol=symbol)
            return

        # Шорт: TP ниже входа, SL выше входа
        tp_price = entry_price * (1 - TP_PCT / 100)
        sl_price = entry_price * (1 + SL_PCT / 100)

        trade_id = await self._save_to_db(
            risk_score=risk_score,
            entry_price=entry_price,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        if not trade_id:
            return

        now = datetime.now(timezone.utc)
        ACTIVE_SHORTS[trade_id] = {
            "id": trade_id,
            "symbol": symbol,
            "entry_price": entry_price,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "entry_ts": now,
            "status": "open",
            "price_15m_saved": False,
            "price_30m_saved": False,
            "price_60m_saved": False,
        }

        logger.info(
            "Auto short opened",
            trade_id=trade_id,
            symbol=symbol,
            entry=entry_price,
            tp=tp_price,
            sl=sl_price,
            score=risk_score.score,
        )

        await self._notify_opened(
            trade_id, symbol, entry_price, tp_price, sl_price, risk_score.score
        )

        asyncio.create_task(self._monitor_trade(trade_id))

    async def _monitor_trade(self, trade_id: int) -> None:
        """Мониторить цену каждые 30 секунд."""
        trade = ACTIVE_SHORTS.get(trade_id)
        if not trade:
            return

        symbol = trade["symbol"]
        entry_price = trade["entry_price"]
        entry_ts = trade["entry_ts"]

        while trade["status"] == "open":
            await asyncio.sleep(MONITOR_INTERVAL)

            current_price = await self._get_price(symbol)
            if not current_price:
                continue

            now = datetime.now(timezone.utc)
            elapsed = (now - entry_ts).total_seconds()

            # Сохраняем снимки цены через 15/30/60 минут
            await self._save_price_snapshot(trade_id, trade, current_price, elapsed, now)

            # P&L для шорта: положительный = цена упала = прибыль
            pnl = (entry_price - current_price) / entry_price * 100

            # TP: цена упала до уровня
            if current_price <= trade["tp_price"]:
                await self._close_trade(trade_id, current_price, "tp_hit", pnl)
                return

            # SL: цена выросла до уровня
            if current_price >= trade["sl_price"]:
                await self._close_trade(trade_id, current_price, "sl_hit", pnl)
                return

            # Истечение времени
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
        """Закрыть сделку и обновить БД."""
        trade = ACTIVE_SHORTS.get(trade_id)
        if not trade:
            return

        trade["status"] = reason
        now = datetime.now(timezone.utc)
        ml_label = 1 if pnl > 0 else 0

        await self._update_db(trade_id, exit_price, now, reason, pnl, ml_label)

        logger.info(
            "Auto short closed",
            trade_id=trade_id,
            symbol=trade["symbol"],
            reason=reason,
            pnl=f"{pnl:+.2f}%",
            ml_label=ml_label,
        )

        await self._notify_closed(trade_id, trade["symbol"], exit_price, pnl, reason)
        ACTIVE_SHORTS.pop(trade_id, None)

    async def _save_price_snapshot(
        self,
        trade_id: int,
        trade: dict,
        current_price: float,
        elapsed: float,
        now: datetime,
    ) -> None:
        """Сохранить цену через 15/30/60 минут после входа."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models.auto_short import AutoShort
            from sqlalchemy import update

            updates = {}

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

            if updates:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(AutoShort)
                        .where(AutoShort.id == trade_id)
                        .values(**updates)
                    )
                    await session.commit()

        except Exception as e:
            logger.error("Price snapshot save failed", error=str(e))

    async def _save_to_db(
        self,
        risk_score: RiskScore,
        entry_price: float,
        tp_price: float,
        sl_price: float,
    ) -> int | None:
        """Сохранить новую сделку в БД со всеми метриками."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models.auto_short import AutoShort

            features = risk_score.features_snapshot
            factor_map = {f.name: f.raw_value for f in risk_score.factors}

            trade = AutoShort(
                symbol=risk_score.symbol,
                signal_type=risk_score.signal_type.value if risk_score.signal_type else "unknown",
                entry_price=entry_price,
                tp_pct=TP_PCT,
                sl_pct=SL_PCT,
                tp_price=tp_price,
                sl_price=sl_price,
                status="open",
                score=risk_score.score,
                triggered_count=risk_score.triggered_count,
                # Факторы движка
                f_rsi=factor_map.get("rsi"),
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
                # Рыночный контекст
                volume_24h_usdt=features.volume_24h_usdt if features else None,
                price_change_5m=features.price_change_5m if features else None,
                spread_pct=features.spread_pct if features else None,
                bid_depth_change_5m=features.bid_depth_change_5m if features else None,
            )

            async with AsyncSessionLocal() as session:
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                return trade.id

        except Exception as e:
            logger.error("Auto short DB save failed", error=str(e))
            return None

    async def _update_db(
        self,
        trade_id: int,
        exit_price: float,
        exit_ts: datetime,
        status: str,
        pnl: float,
        ml_label: int,
    ) -> None:
        """Обновить запись в БД при закрытии."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models.auto_short import AutoShort
            from sqlalchemy import update

            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(AutoShort)
                    .where(AutoShort.id == trade_id)
                    .values(
                        status=status,
                        exit_price=exit_price,
                        exit_ts=exit_ts,
                        pnl_pct=pnl,
                        ml_label=ml_label,
                    )
                )
                await session.commit()

        except Exception as e:
            logger.error("Auto short DB update failed", error=str(e))

    async def _get_price(self, symbol: str) -> float | None:
        """Получить текущую цену из Redis или Bybit REST."""
        try:
            raw = await self._redis.get(f"score:{symbol}")
            if raw:
                data = json.loads(raw)
                snapshot = data.get("features_snapshot") or {}
                price = snapshot.get("last_price")
                if price:
                    return float(price)
        except Exception:
            pass

        try:
            url = f"https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}"
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
                    items = data.get("result", {}).get("list", [])
                    if items:
                        return float(items[0]["lastPrice"])
        except Exception:
            pass

        return None

    async def _notify_opened(
        self,
        trade_id: int,
        symbol: str,
        entry_price: float,
        tp_price: float,
        sl_price: float,
        score: float,
    ) -> None:
        """Уведомить пользователей об открытой сделке."""
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users
            user_ids = await get_active_users(self._redis)

            base = symbol.replace("USDT", "")
            bybit_url = f"https://www.bybit.com/trade/usdt/{symbol}"

            text = (
                f"🤖 <b>Авто-шорт открыт</b>\n\n"
                f"📌 <a href=\"{bybit_url}\">{symbol}</a>\n"
                f"📊 Score: <b>{score:.0f}</b>\n"
                f"💰 Вход: <b>${entry_price:.6g}</b>\n\n"
                f"🎯 TP: ${tp_price:.6g} (-{TP_PCT:.0f}%)\n"
                f"🛑 SL: ${sl_price:.6g} (+{SL_PCT:.0f}%)\n\n"
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
                    logger.warning("Notify failed", user_id=user_id, error=str(e))

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
        """Уведомить пользователей о закрытой сделке."""
        if not self._bot:
            return

        try:
            from app.bot.user_store import get_active_users
            user_ids = await get_active_users(self._redis)

            reason_text = {
                "tp_hit": "🎯 Тейк профит достигнут",
                "sl_hit": "🛑 Стоп лосс сработал",
                "expired": "⏰ Время сделки истекло",
                "closed_manual": "✋ Закрыта вручную",
            }.get(reason, reason)

            pnl_em = "🟢" if pnl > 0 else "🔴"

            text = (
                f"{'✅' if pnl > 0 else '❌'} <b>Авто-шорт закрыт</b>\n\n"
                f"📌 <b>{symbol}</b>\n"
                f"{reason_text}\n\n"
                f"💰 Выход: <b>${exit_price:.6g}</b>\n"
                f"P&L: {pnl_em} <b>{pnl:+.2f}%</b>\n\n"
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
                    logger.warning("Notify failed", user_id=user_id, error=str(e))

        except Exception as e:
            logger.error("Close notification failed", error=str(e))