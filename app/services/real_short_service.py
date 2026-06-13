"""
RealShortService — реальная торговля Bybit, зеркало ml_short 1:1.

Подключается к ml_short flow ХУКАМИ (не дублирует логику решения):
- on_ml_open(...)  вызывается ПОСЛЕ того как ml_short открыл бумажную позицию;
- on_ml_close(...) вызывается ПОСЛЕ того как ml_short закрыл бумажную позицию.

RealShortService только ИСПОЛНЯЕТ решение ml_short. Все защиты для реальных
денег здесь:
  - real_enabled=False → ни одного реального ордера;
  - идемпотентность по ml_signal_id (UNIQUE-индекс + проверка перед входом);
  - kill-switch (дневной лимит убытка + лимит открытых позиций);
  - whitelist/blacklist символов; cooldown по входам;
  - проверка баланса; округление qty/price под фильтры инструмента;
  - reduce-only на ВСЕХ закрывающих ордерах.

Числовая логика вынесена в real_short_logic.py (покрыта тестами).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as aioredis

from app.config import get_settings
from app.services.real_short_config import (
    get_real_short_config,
    patch_real_short_config,
)
from app.services.real_short_logic import (
    InstrumentFilter,
    compute_position_size,
    evaluate_kill_switch,
    short_sl_price,
    short_tp_price,
    symbol_allowed,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

COOLDOWN_REDIS_PREFIX = "real_short:cooldown:"


class RealShortService:
    """Реальная торговля Bybit зеркально к ml_short."""

    def __init__(
        self,
        redis: aioredis.Redis,
        bot=None,
        rest_client=None,
    ) -> None:
        self._redis = redis
        self._bot = bot
        self._rest_client = rest_client  # market-data client (для цены, опц.)
        self._trade_client: Any = None
        self._trade_client_testnet: bool | None = None
        self._lock = asyncio.Lock()

    # ── Trade client (ленивая инициализация + пересоздание при смене testnet) ──

    async def _get_trade_client(self, testnet: bool):
        """Вернуть BybitTradeClient под нужный режим, пересоздав при смене testnet."""
        if self._trade_client is not None and self._trade_client_testnet == testnet:
            return self._trade_client

        from app.bybit.trade_client import BybitTradeClient, BybitTradeError

        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitTradeError("BYBIT_API_KEY/SECRET не заданы")

        if self._trade_client is not None:
            await self._trade_client.stop()

        client = BybitTradeClient(
            api_key=settings.bybit_api_key,
            api_secret=settings.bybit_api_secret,
            testnet=testnet,
        )
        await client.start()
        self._trade_client = client
        self._trade_client_testnet = testnet
        return client

    # ── ХУК ОТКРЫТИЯ ───────────────────────────────────────────────

    async def on_ml_open(
        self,
        ml_signal_id: int | None,
        ml_position_id: int | None,
        symbol: str,
        entry_price: float,
    ) -> int | None:
        """
        Вызывается когда ml_short открыл бумажную позицию.
        Открывает реальную позицию Bybit зеркально. Возвращает real_position_id
        или None (если real выключен / заблокировано / ошибка — НЕ падаем).
        """
        # Сериализуем входы — иначе параллельные сигналы могут пробить лимиты
        async with self._lock:
            try:
                return await self._open_real(
                    ml_signal_id, ml_position_id, symbol, float(entry_price)
                )
            except Exception as exc:
                logger.error(
                    "Real-short: ошибка открытия (поглощена)",
                    symbol=symbol,
                    error=str(exc),
                )
                return None

    async def _open_real(
        self,
        ml_signal_id: int | None,
        ml_position_id: int | None,
        symbol: str,
        entry_price: float,
    ) -> int | None:
        cfg = await get_real_short_config(self._redis)

        # 1. Жёсткий гейт: real выключен → НИКОГДА не торгуем
        if not cfg.get("real_enabled", False):
            return None

        # 2. Идемпотентность: уже есть реальная позиция на этот signal_id?
        if ml_signal_id is not None and await self._has_real_for_signal(ml_signal_id):
            logger.info("Real-short: дубль по signal_id, skip", ml_signal_id=ml_signal_id)
            return None

        # 3. Whitelist / blacklist
        if not symbol_allowed(
            symbol, cfg.get("real_allow_symbols"), cfg.get("real_block_symbols")
        ):
            logger.info("Real-short: символ не разрешён", symbol=symbol)
            return None

        # 4. Cooldown по входам
        if cfg.get("real_cooldown_sec", 0) > 0 and await self._cooldown_active(symbol):
            logger.info("Real-short: cooldown активен", symbol=symbol)
            return None

        # 5. Kill-switch: дневной убыток + лимит открытых позиций
        realized_today = await self._realized_pnl_today()
        open_count = await self._count_open_real()
        ks = evaluate_kill_switch(
            realized_pnl_today_usdt=realized_today,
            max_daily_loss_usdt=float(cfg.get("real_max_daily_loss_usdt", 0)),
            open_positions=open_count,
            max_open_positions=int(cfg.get("real_max_open_positions", 0)),
        )
        if ks.tripped:
            logger.warning("Real-short: kill-switch", reason=ks.reason, symbol=symbol)
            if ks.reason == "daily_loss_limit":
                # Авто-выключение реальной торговли при дневной просадке
                await patch_real_short_config(self._redis, {"real_enabled": False})
                await self._notify_kill_switch(realized_today, cfg)
            return None

        testnet = bool(cfg.get("real_testnet", True))

        # 6. Trade client + фильтры инструмента
        try:
            client = await self._get_trade_client(testnet)
        except Exception as exc:
            logger.error("Real-short: trade client недоступен", error=str(exc))
            return None

        flt = await client.get_instrument_filter(symbol)
        if flt is None:
            flt = InstrumentFilter(symbol=symbol, qty_step=0.0, min_order_qty=0.0, tick_size=0.0)

        # 7. Сайзинг с проверкой баланса
        leverage = int(cfg.get("real_leverage", 10))
        margin = float(cfg.get("real_margin_usdt", 20.0))
        balance = await client.get_available_usdt()
        sizing = compute_position_size(
            margin_usdt=margin,
            leverage=leverage,
            entry_price=entry_price,
            flt=flt,
            available_balance_usdt=balance,
        )
        if not sizing.ok:
            logger.warning(
                "Real-short: сайзинг отклонён",
                symbol=symbol, reason=sizing.reason, balance=balance, margin=margin,
            )
            return None

        tp_pct = float(cfg.get("real_tp_pct", 1.0))
        sl_pct = float(cfg.get("real_sl_pct", 1.0))
        tp_price = short_tp_price(entry_price, tp_pct, flt)
        sl_price = short_sl_price(entry_price, sl_pct, flt)

        # 8. Открыть реальную позицию в БД (status=opening) ДО ордера — для аудита/идемпотентности
        real_id = await self._insert_position(
            ml_signal_id=ml_signal_id,
            ml_position_id=ml_position_id,
            symbol=symbol,
            entry_price=entry_price,
            qty=sizing.qty,
            leverage=leverage,
            margin_usdt=sizing.required_margin_usdt,
            notional_usdt=sizing.notional_usdt,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            tp_price=tp_price,
            sl_price=sl_price,
            testnet=testnet,
        )
        if real_id is None:
            # Вероятно сработал UNIQUE по ml_signal_id (гонка) — идемпотентность ОК
            logger.info("Real-short: позиция не создана (дубль/ошибка)", symbol=symbol)
            return None

        order_link = f"rs_{real_id}_entry"

        # 9. Плечо + рыночный вход в шорт
        try:
            await client.set_leverage(symbol, leverage)
            entry_resp = await client.open_short_market(symbol, sizing.qty, order_link)
            await self._log_order(
                real_id, symbol, "entry", "Sell", sizing.qty, entry_price,
                reduce_only=False, order_link_id=order_link,
                order_id=entry_resp.get("orderId"), status="submitted",
                raw=entry_resp,
            )
        except Exception as exc:
            await self._mark_position_failed(real_id, str(exc))
            await self._log_order(
                real_id, symbol, "entry", "Sell", sizing.qty, entry_price,
                reduce_only=False, order_link_id=order_link, status="error",
                error=str(exc),
            )
            logger.error("Real-short: вход отклонён", symbol=symbol, error=str(exc))
            return None

        # 10. Лимитный reduce-only SL (TP исполняет watcher маркетом)
        sl_link = f"rs_{real_id}_sl"
        try:
            sl_resp = await client.place_reduce_only_sl(symbol, sizing.qty, sl_price, sl_link)
            await self._log_order(
                real_id, symbol, "sl", "Buy", sizing.qty, sl_price,
                reduce_only=True, order_link_id=sl_link,
                order_id=sl_resp.get("orderId"), status="submitted", raw=sl_resp,
            )
            await self._set_sl_order_id(real_id, sl_resp.get("orderId"))
        except Exception as exc:
            # SL не выставился — позиция открыта, watcher всё равно мониторит и закроет
            logger.warning("Real-short: SL не выставлен (watcher подстрахует)", symbol=symbol, error=str(exc))
            await self._log_order(
                real_id, symbol, "sl", "Buy", sizing.qty, sl_price,
                reduce_only=True, order_link_id=sl_link, status="error", error=str(exc),
            )

        await self._mark_position_open(real_id)
        await self._set_cooldown(symbol, int(cfg.get("real_cooldown_sec", 0)))

        logger.info(
            "Real-short: реальная позиция открыта",
            real_id=real_id, symbol=symbol, qty=sizing.qty,
            entry_price=entry_price, testnet=testnet, leverage=leverage,
        )
        await self._notify_opened(symbol, entry_price, sizing.qty, leverage, tp_price, sl_price, testnet, real_id)
        return real_id

    # ── ХУК ЗАКРЫТИЯ ───────────────────────────────────────────────

    async def on_ml_close(
        self,
        ml_position_id: int | None,
        exit_price: float | None,
        close_reason: str,
    ) -> None:
        """Вызывается когда ml_short закрыл бумажную позицию — закрываем реальную."""
        if ml_position_id is None:
            return
        async with self._lock:
            try:
                pos = await self._get_open_by_ml_position(ml_position_id)
                if pos is None:
                    return
                await self._close_real(pos, exit_price, close_reason)
            except Exception as exc:
                logger.error("Real-short: ошибка закрытия (поглощена)", error=str(exc))

    async def manual_close(self, real_position_id: int) -> tuple[bool, str]:
        """Ручное закрытие реальной позиции маркет reduce-only (web/Telegram)."""
        async with self._lock:
            pos = await self._get_open_by_id(real_position_id)
            if pos is None:
                return False, "Позиция не найдена или уже закрыта"
            try:
                await self._close_real(pos, None, "manual")
                return True, f"Позиция #{real_position_id} закрывается (manual)"
            except Exception as exc:
                logger.error("Real-short: ручное закрытие — ошибка", error=str(exc))
                return False, f"Ошибка: {exc}"

    async def _close_real(
        self,
        pos: dict,
        exit_price: float | None,
        close_reason: str,
    ) -> None:
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        real_id = pos["id"]
        testnet = bool(pos["testnet"])

        client = await self._get_trade_client(testnet)

        # Отменить висящий SL-ордер (чтобы не задвоить закрытие)
        sl_order_id = pos.get("sl_order_id")
        if sl_order_id:
            await client.cancel_order(symbol, sl_order_id)

        close_link = f"rs_{real_id}_close"
        try:
            resp = await client.close_short_market(symbol, qty, close_link)
            await self._log_order(
                real_id, symbol, "close", "Buy", qty, exit_price,
                reduce_only=True, order_link_id=close_link,
                order_id=resp.get("orderId"), status="submitted", raw=resp,
            )
        except Exception as exc:
            await self._log_order(
                real_id, symbol, "close", "Buy", qty, exit_price,
                reduce_only=True, order_link_id=close_link, status="error", error=str(exc),
            )
            logger.error("Real-short: закрытие отклонено", symbol=symbol, error=str(exc))
            return

        # Реальный PnL из Bybit (closedPnl) — для сверки с бумажным
        pnl_usdt = None
        try:
            closed = await client.get_closed_pnl(symbol, limit=1)
            if closed:
                pnl_usdt = float(closed[0].get("closedPnl", 0) or 0)
        except Exception:
            pass

        entry_price = float(pos["entry_price"])
        leverage = float(pos["leverage"])
        eff_exit = exit_price
        if eff_exit is None and pnl_usdt is not None and qty > 0:
            # обратная оценка цены выхода из pnl (грубо)
            eff_exit = entry_price - (pnl_usdt / qty)
        if eff_exit:
            price_move_pct = ((entry_price - eff_exit) / entry_price) * 100.0
            pnl_pct = price_move_pct * leverage
        else:
            pnl_pct = None

        await self._mark_position_closed(real_id, eff_exit, pnl_pct, pnl_usdt, close_reason)
        logger.info(
            "Real-short: реальная позиция закрыта",
            real_id=real_id, symbol=symbol, close_reason=close_reason,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct,
        )
        await self._notify_closed(symbol, real_id, eff_exit, pnl_pct, pnl_usdt, close_reason)

    # ── Redis cooldown ─────────────────────────────────────────────

    async def _cooldown_active(self, symbol: str) -> bool:
        try:
            return bool(await self._redis.get(f"{COOLDOWN_REDIS_PREFIX}{symbol}"))
        except Exception:
            return False

    async def _set_cooldown(self, symbol: str, seconds: int) -> None:
        if seconds <= 0:
            return
        try:
            await self._redis.set(f"{COOLDOWN_REDIS_PREFIX}{symbol}", "1", ex=seconds)
        except Exception:
            pass

    # ── БД-операции ────────────────────────────────────────────────

    async def _has_real_for_signal(self, ml_signal_id: int) -> bool:
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                r = await session.execute(
                    text("SELECT 1 FROM real_short_positions WHERE ml_signal_id = :sid LIMIT 1"),
                    {"sid": ml_signal_id},
                )
                return r.fetchone() is not None
        except Exception as exc:
            logger.error("Real-short: проверка дубля — ошибка", error=str(exc))
            # При ошибке считаем что дубль ЕСТЬ (безопаснее не открывать)
            return True

    async def _count_open_real(self) -> int:
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                r = await session.execute(
                    text("SELECT COUNT(*) FROM real_short_positions WHERE status IN ('open','opening')")
                )
                return int(r.scalar_one())
        except Exception:
            return 0

    async def _realized_pnl_today(self) -> float:
        """Сумма реального PnL (USDT) по закрытым позициям с начала суток UTC."""
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal
            start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            async with AsyncSessionLocal() as session:
                r = await session.execute(
                    text("""
                        SELECT COALESCE(SUM(pnl_usdt), 0) FROM real_short_positions
                        WHERE status = 'closed' AND exit_ts >= :start AND pnl_usdt IS NOT NULL
                    """),
                    {"start": start},
                )
                return float(r.scalar_one() or 0.0)
        except Exception:
            return 0.0

    async def _insert_position(self, **kw) -> int | None:
        try:
            from app.db.models.real_short import RealShortPosition
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                pos = RealShortPosition(
                    ml_signal_id=kw["ml_signal_id"],
                    ml_position_id=kw["ml_position_id"],
                    symbol=kw["symbol"],
                    side="Sell",
                    entry_ts=datetime.now(timezone.utc),
                    entry_price=kw["entry_price"],
                    qty=kw["qty"],
                    leverage=kw["leverage"],
                    margin_usdt=kw["margin_usdt"],
                    notional_usdt=kw["notional_usdt"],
                    tp_pct=kw["tp_pct"],
                    sl_pct=kw["sl_pct"],
                    tp_price=kw["tp_price"],
                    sl_price=kw["sl_price"],
                    testnet=kw["testnet"],
                    status="opening",
                )
                session.add(pos)
                await session.commit()
                await session.refresh(pos)
                return pos.id
        except Exception as exc:
            # UNIQUE violation по ml_signal_id → идемпотентность сработала
            logger.info("Real-short: insert позиции не удался (возможно дубль)", error=str(exc))
            return None

    async def _set_sl_order_id(self, real_id: int, order_id: str | None) -> None:
        if not order_id:
            return
        await self._exec_update(
            "UPDATE real_short_positions SET sl_order_id = :oid WHERE id = :id",
            {"oid": order_id, "id": real_id}, ignore_missing_col=True,
        )

    async def _mark_position_open(self, real_id: int) -> None:
        await self._exec_update(
            "UPDATE real_short_positions SET status = 'open', updated_at = NOW() WHERE id = :id",
            {"id": real_id},
        )

    async def _mark_position_failed(self, real_id: int, error: str) -> None:
        await self._exec_update(
            "UPDATE real_short_positions SET status = 'failed', close_reason = :err, updated_at = NOW() WHERE id = :id",
            {"id": real_id, "err": error[:200]},
        )

    async def _mark_position_closed(
        self, real_id: int, exit_price, pnl_pct, pnl_usdt, reason: str
    ) -> None:
        await self._exec_update(
            """UPDATE real_short_positions
               SET status='closed', exit_ts=NOW(), exit_price=:ep,
                   pnl_pct=:pp, pnl_usdt=:pu, close_reason=:r, updated_at=NOW()
               WHERE id=:id""",
            {"id": real_id, "ep": exit_price, "pp": pnl_pct, "pu": pnl_usdt, "r": reason},
        )

    async def _exec_update(self, sql: str, params: dict, ignore_missing_col: bool = False) -> None:
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                await session.execute(text(sql), params)
                await session.commit()
        except Exception as exc:
            if ignore_missing_col:
                logger.debug("Real-short: update skipped", error=str(exc))
            else:
                logger.error("Real-short: update ошибка", error=str(exc))

    async def _log_order(
        self, position_id, symbol, order_type, side, qty, price,
        reduce_only=False, order_link_id=None, order_id=None,
        status="submitted", error=None, raw=None,
    ) -> None:
        try:
            from app.db.models.real_short import RealShortOrder
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                order = RealShortOrder(
                    position_id=position_id, symbol=symbol, order_type=order_type,
                    side=side, qty=qty, price=price, reduce_only=reduce_only,
                    order_id=order_id, order_link_id=order_link_id, status=status,
                    error=error[:500] if error else None,
                    raw_response=raw if isinstance(raw, dict) else None,
                )
                session.add(order)
                await session.commit()
        except Exception as exc:
            logger.debug("Real-short: лог ордера не записан", error=str(exc))

    async def _get_open_by_ml_position(self, ml_position_id: int) -> dict | None:
        return await self._fetch_position(
            "WHERE ml_position_id = :v AND status IN ('open','opening')",
            {"v": ml_position_id},
        )

    async def _get_open_by_id(self, real_id: int) -> dict | None:
        return await self._fetch_position(
            "WHERE id = :v AND status IN ('open','opening')", {"v": real_id}
        )

    async def _fetch_position(self, where: str, params: dict) -> dict | None:
        try:
            from sqlalchemy import text
            from app.db.session import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                r = await session.execute(
                    text(f"""
                        SELECT id, symbol, qty, entry_price, leverage, testnet,
                               tp_price, sl_price,
                               (SELECT order_id FROM real_short_orders o
                                WHERE o.position_id = p.id AND o.order_type='sl'
                                  AND o.status='submitted'
                                ORDER BY o.id DESC LIMIT 1) AS sl_order_id
                        FROM real_short_positions p {where} ORDER BY id DESC LIMIT 1
                    """),
                    params,
                )
                row = r.fetchone()
                if not row:
                    return None
                return {
                    "id": row[0], "symbol": row[1], "qty": row[2], "entry_price": row[3],
                    "leverage": row[4], "testnet": row[5], "tp_price": row[6],
                    "sl_price": row[7], "sl_order_id": row[8],
                }
        except Exception as exc:
            logger.error("Real-short: fetch позиции ошибка", error=str(exc))
            return None

    # ── TG уведомления ─────────────────────────────────────────────

    async def _users(self) -> list[int]:
        try:
            from app.bot.user_store import get_active_users
            user_ids = await get_active_users(self._redis)
            return user_ids or settings.allowed_user_ids
        except Exception:
            return settings.allowed_user_ids

    async def _send(self, text: str) -> None:
        if not self._bot:
            return
        for uid in await self._users():
            try:
                await self._bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            except Exception:
                pass

    async def _notify_opened(self, symbol, entry, qty, lev, tp, sl, testnet, real_id) -> None:
        mode = "🧪 TESTNET" if testnet else "🔴 MAINNET"
        await self._send(
            f"💵 <b>Real-Short: позиция ОТКРЫТА</b>\n\n"
            f"📌 #{real_id} <b>{symbol}</b> ({mode})\n"
            f"💰 Вход: <b>${entry:.6g}</b>\n"
            f"📦 Кол-во: <b>{qty:g}</b> | ⚖️ {lev}x\n"
            f"🎯 TP: ${tp:.6g}\n"
            f"🛑 SL: ${sl:.6g} (limit reduce-only)"
        )

    async def _notify_closed(self, symbol, real_id, exit_price, pnl_pct, pnl_usdt, reason) -> None:
        labels = {"tp": "🎯 TP", "sl": "🛑 SL", "timeout": "⏰ Timeout", "manual": "✋ Вручную"}
        pnl_em = "🟢" if (pnl_usdt or 0) > 0 else "🔴" if (pnl_usdt or 0) < 0 else "⚪"
        exit_str = f"${exit_price:.6g}" if exit_price else "—"
        pnl_pct_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
        pnl_usdt_str = f"{pnl_usdt:+.4f} USDT" if pnl_usdt is not None else "—"
        await self._send(
            f"💵 <b>Real-Short: позиция ЗАКРЫТА</b>\n\n"
            f"📌 #{real_id} <b>{symbol}</b>\n"
            f"💹 Выход: <b>{exit_str}</b>\n"
            f"{pnl_em} PnL: <b>{pnl_pct_str}</b> ({pnl_usdt_str})\n"
            f"📋 Причина: {labels.get(reason, reason)}"
        )

    async def _notify_kill_switch(self, realized_today: float, cfg: dict) -> None:
        await self._send(
            f"🚨 <b>Real-Short: KILL-SWITCH</b>\n\n"
            f"Дневной убыток достиг лимита.\n"
            f"📉 PnL за сегодня: <b>{realized_today:+.2f} USDT</b>\n"
            f"🛑 Лимит: <b>-{float(cfg.get('real_max_daily_loss_usdt', 0)):.2f} USDT</b>\n\n"
            f"⛔️ Реальная торговля АВТО-ВЫКЛЮЧЕНА."
        )
