"""
Feature Calculator — computes all analytical signals from raw market data.

Features are split into:
  A. Real-time (from trade tick stream): trade imbalance, large trade detection, volume burst
  B. Candle-based (1m/5m/15m OHLCV): RSI, ATR, VWAP extension, wick patterns, momentum
  C. Orderbook-based: imbalance ratio, bid support thinning, spread expansion

All features are stored as a CoinFeatures dataclass per symbol.
The ScoringEngine reads CoinFeatures and produces a RiskScore.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque

import numpy as np

from app.utils.logging import get_logger
from app.utils.time_utils import utcnow_ts

logger = get_logger(__name__)


# ─── Data containers ──────────────────────────────────────────────────────────

@dataclass
class TradeTick:
    ts: float          # Unix timestamp (seconds)
    price: float
    qty: float
    side: str          # "Buy" | "Sell"
    usdt_value: float  # price * qty


@dataclass
class CandleData:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass
class CoinFeatures:
    """
    All computed features for one symbol at one point in time.
    Used as input to ScoringEngine.
    """
    symbol: str
    ts: float

    # ── Trade imbalance (5m) ──────────────────────────────────────
    buy_volume_5m: float = 0.0
    sell_volume_5m: float = 0.0
    trade_imbalance_5m: float = 0.0   # (buy - sell) / (buy + sell) in [-1, 1]

    # ── Large trade detection ─────────────────────────────────────
    large_buy_count_5m: int = 0       # trades > large_trade_threshold in last 5m
    large_sell_count_5m: int = 0
    large_buy_usdt_5m: float = 0.0    # total USDT of large buys
    large_sell_usdt_5m: float = 0.0
    large_trade_threshold: float = 0.0  # dynamic: 95th percentile of recent trades

    # ── Volume anomaly ────────────────────────────────────────────
    volume_1m: float = 0.0            # current 1m volume (USDT turnover)
    volume_zscore_1m: float = 0.0     # z-score vs rolling 60-period mean (trade-based)
    volume_zscore_candle: float = 0.0 # z-score from candle turnover (candle-based)
    volume_ratio_5m: float = 0.0      # 5m volume / avg 5m volume (rolling 12 periods)

    # ── Price momentum ────────────────────────────────────────────
    price_change_1m: float = 0.0      # % price change last 1m
    price_change_5m: float = 0.0      # % price change last 5m
    price_change_15m: float = 0.0     # % price change last 15m
    price_acceleration: float = 0.0   # 1m change - avg 1m change (speed-up signal)

    # ── VWAP extension ────────────────────────────────────────────
    vwap_15m: float = 0.0
    vwap_extension_pct: float = 0.0   # (price - vwap) / vwap * 100 — positive = overextended

    # ── RSI ───────────────────────────────────────────────────────
    rsi_14_1m: float = 50.0           # RSI(14) on 1m candles
    rsi_14_5m: float = 50.0           # RSI(14) on 5m candles

    # ── ATR / Realized Volatility ─────────────────────────────────
    atr_14_1m: float = 0.0            # ATR(14) on 1m — measures intraday volatility
    realized_vol_1h: float = 0.0      # std dev of 1m returns over 60 periods

    # ── Candle patterns ───────────────────────────────────────────
    consecutive_green_candles: int = 0  # how many green 1m candles in a row
    upper_wick_ratio: float = 0.0       # upper_wick / body — rejection signal
    lower_wick_ratio: float = 0.0

    # ── Momentum loss ─────────────────────────────────────────────
    momentum_loss_signal: bool = False  # price stalls after impulsive move
    volume_decline_after_spike: bool = False

    # ── Orderbook features ────────────────────────────────────────
    ob_imbalance: float = 0.0         # (bid_depth - ask_depth) / total — in [-1, 1]
    bid_depth_usdt: float = 0.0       # total bid depth in USDT (top 10 levels)
    ask_depth_usdt: float = 0.0
    spread_pct: float = 0.0           # (best_ask - best_bid) / mid_price * 100
    bid_depth_change_5m: float = 0.0  # % change in bid depth (negative = thinning)

    # ── Open Interest (perpetual only, None for spot) ─────────────
    oi_change_pct_1h: float | None = None  # % OI change in last hour
    funding_rate: float | None = None       # latest funding rate

    # ── Metadata ─────────────────────────────────────────────────
    last_price: float = 0.0
    market_cap_proxy: float = 0.0     # price * circulating_supply (if available)
    volume_24h_usdt: float = 0.0


# ─── Feature Calculator ───────────────────────────────────────────────────────

class FeatureCalculator:
    """
    Stateful per-symbol feature calculator.
    Maintains rolling buffers of trade ticks and candles.
    Call update_trade() for each incoming WS trade tick.
    Call update_candles() when new candles arrive from REST/WS.
    Call update_orderbook() when orderbook snapshot arrives.
    Call compute() to get latest CoinFeatures.
    """

    # Rolling buffer sizes
    TRADE_BUFFER_SIZE = 2000    # ~last 2k trades per symbol
    CANDLE_BUFFER_1M = 120      # 2 hours of 1m candles
    CANDLE_BUFFER_5M = 144      # 12 hours of 5m candles
    OB_HISTORY_SLOTS = 12       # 12 * 5s = 60s of OB snapshots (5s interval assumed)

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._trades: Deque[TradeTick] = deque(maxlen=self.TRADE_BUFFER_SIZE)
        self._candles_1m: Deque[CandleData] = deque(maxlen=self.CANDLE_BUFFER_1M)
        self._candles_5m: Deque[CandleData] = deque(maxlen=self.CANDLE_BUFFER_5M)
        self._ob_snapshots: Deque[dict] = deque(maxlen=self.OB_HISTORY_SLOTS)
        self._last_ob: dict | None = None
        self._volume_24h: float = 0.0
        self._last_price: float = 0.0

    def update_trade(self, tick: TradeTick) -> None:
        self._trades.append(tick)
        self._last_price = tick.price

    def update_candles(self, candles: list[CandleData], interval: str) -> None:
        if interval == "1":
            self._candles_1m.clear()
            self._candles_1m.extend(sorted(candles, key=lambda c: c.ts))
        elif interval == "5":
            self._candles_5m.clear()
            self._candles_5m.extend(sorted(candles, key=lambda c: c.ts))

    def update_orderbook(self, ob: dict) -> None:
        self._last_ob = ob
        self._ob_snapshots.append({**ob, "ts": utcnow_ts()})

    def update_24h_volume(self, volume_usdt: float) -> None:
        self._volume_24h = volume_usdt

    def compute(self) -> CoinFeatures:
        now = utcnow_ts()
        features = CoinFeatures(symbol=self.symbol, ts=now, last_price=self._last_price)
        features.volume_24h_usdt = self._volume_24h

        self._compute_trade_features(features, now)
        self._compute_candle_features(features)
        self._compute_ob_features(features)

        return features

    async def save_to_redis(self, redis) -> None:
        """Сохранить свечи в Redis для восстановления при перезапуске."""
        try:
            import json
            key = f"candles:{self.symbol}"
            data = {
                "candles_1m": [
                    {"ts": c.ts, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                    "volume": c.volume, "turnover": c.turnover}
                    for c in self._candles_1m
                ],
                "candles_5m": [
                    {"ts": c.ts, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close,
                    "volume": c.volume, "turnover": c.turnover}
                    for c in self._candles_5m
                ],
                "volume_24h": self._volume_24h,
                "last_price": self._last_price,
            }
            await redis.setex(key, 3600, json.dumps(data))  # TTL 1 час
        except Exception as e:
            logger.debug("Candle save failed", symbol=self.symbol, error=str(e))


    async def restore_from_redis(self, redis) -> bool:
        """Восстановить свечи из Redis. Возвращает True если данные найдены."""
        try:
            import json
            key = f"candles:{self.symbol}"
            raw = await redis.get(key)
            if not raw:
                return False

            data = json.loads(raw)

            candles_1m = [CandleData(**c) for c in data.get("candles_1m", [])]
            candles_5m = [CandleData(**c) for c in data.get("candles_5m", [])]

            if candles_1m:
                self._candles_1m.clear()
                self._candles_1m.extend(candles_1m)

            if candles_5m:
                self._candles_5m.clear()
                self._candles_5m.extend(candles_5m)

            self._volume_24h = data.get("volume_24h", 0.0)
            self._last_price = data.get("last_price", 0.0)

            logger.debug(
                "Candles restored from Redis",
                symbol=self.symbol,
                candles_1m=len(candles_1m),
                candles_5m=len(candles_5m),
            )
            return True

        except Exception as e:
            logger.debug("Candle restore failed", symbol=self.symbol, error=str(e))
            return False

    # ── Trade features ────────────────────────────────────────────

    def _compute_trade_features(self, f: CoinFeatures, now: float) -> None:
        window_5m = now - 300
        window_1m = now - 60

        trades_5m = [t for t in self._trades if t.ts >= window_5m]
        trades_1m = [t for t in self._trades if t.ts >= window_1m]

        if not trades_5m:
            return

        # --- Large trade threshold (dynamic: 95th percentile) ---
        all_values = [t.usdt_value for t in self._trades]
        if len(all_values) >= 20:
            f.large_trade_threshold = float(np.percentile(all_values, 95))
        else:
            f.large_trade_threshold = float(np.mean(all_values) * 3) if all_values else 1000.0

        # --- Buy/sell volumes 5m ---
        buys_5m = [t for t in trades_5m if t.side == "Buy"]
        sells_5m = [t for t in trades_5m if t.side == "Sell"]

        f.buy_volume_5m = sum(t.usdt_value for t in buys_5m)
        f.sell_volume_5m = sum(t.usdt_value for t in sells_5m)

        total_vol = f.buy_volume_5m + f.sell_volume_5m
        if total_vol > 0:
            f.trade_imbalance_5m = (f.buy_volume_5m - f.sell_volume_5m) / total_vol

        # --- Large trades 5m ---
        threshold = f.large_trade_threshold
        large_buys = [t for t in buys_5m if t.usdt_value >= threshold]
        large_sells = [t for t in sells_5m if t.usdt_value >= threshold]
        f.large_buy_count_5m = len(large_buys)
        f.large_sell_count_5m = len(large_sells)
        f.large_buy_usdt_5m = sum(t.usdt_value for t in large_buys)
        f.large_sell_usdt_5m = sum(t.usdt_value for t in large_sells)

        # --- Volume 1m ---
        f.volume_1m = sum(t.usdt_value for t in trades_1m)

        # --- Volume z-score ---
        if len(self._candles_1m) >= 10:
            candle_vols = [c.turnover for c in self._candles_1m]
            mu = np.mean(candle_vols)
            sigma = np.std(candle_vols)
            if sigma > 0:
                f.volume_zscore_1m = (f.volume_1m - mu) / sigma
            else:
                f.volume_zscore_1m = 0.0

        # --- Price changes (используем last_price и ближайший трейд) ---
        price_now = self._last_price if self._last_price else (
            trades_5m[-1].price if trades_5m else 0.0
        )

        # Ищем ближайший трейд не позднее чем 1/5 минут назад
        trades_before_1m = [t for t in self._trades if t.ts <= now - 55]
        trades_before_5m = [t for t in self._trades if t.ts <= now - 295]

        price_1m_ago = trades_before_1m[-1].price if trades_before_1m else None
        price_5m_ago = trades_before_5m[-1].price if trades_before_5m else None

        if price_1m_ago and price_now:
            f.price_change_1m = (price_now - price_1m_ago) / price_1m_ago * 100
        if price_5m_ago and price_now:
            f.price_change_5m = (price_now - price_5m_ago) / price_5m_ago * 100
    # ── Candle features ───────────────────────────────────────────


    def _compute_candle_features(self, f: CoinFeatures) -> None:
        candles = list(self._candles_1m)
        if len(candles) < 15:
            return

        closes = np.array([c.close for c in candles])
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        opens = np.array([c.open for c in candles])
        volumes = np.array([c.turnover for c in candles])

        # --- Price change 15m ---
        if len(closes) >= 15:
            f.price_change_15m = (closes[-1] - closes[-15]) / closes[-15] * 100

        # --- RSI 1m ---
        f.rsi_14_1m = self._calc_rsi(closes, 14)

        # --- RSI 5m ---
        candles_5m = list(self._candles_5m)
        if len(candles_5m) >= 15:
            closes_5m = np.array([c.close for c in candles_5m])
            f.rsi_14_5m = self._calc_rsi(closes_5m, 14)

        # --- ATR(14) ---
        f.atr_14_1m = self._calc_atr(highs, lows, closes, 14)

        # --- Realized volatility (std of 1m returns, 60 periods) ---
        if len(closes) >= 2:
            returns = np.diff(closes) / closes[:-1]
            window = min(60, len(returns))
            f.realized_vol_1h = float(np.std(returns[-window:]) * 100)

        # --- VWAP (15m) ---
        if len(candles) >= 15:
            c15 = candles[-15:]
            typical = np.array([(c.high + c.low + c.close) / 3 for c in c15])
            vol15 = np.array([c.volume for c in c15])
            if vol15.sum() > 0:
                f.vwap_15m = float(np.dot(typical, vol15) / vol15.sum())
                if f.vwap_15m > 0:
                    f.vwap_extension_pct = (closes[-1] - f.vwap_15m) / f.vwap_15m * 100

        # --- Consecutive green candles ---
        count = 0
        for c in reversed(candles):
            if c.close > c.open:
                count += 1
            else:
                break
        f.consecutive_green_candles = count

        # --- Latest candle wick analysis ---
        last = candles[-1]
        body = abs(last.close - last.open)
        if body > 0:
            upper_wick = last.high - max(last.close, last.open)
            lower_wick = min(last.close, last.open) - last.low
            f.upper_wick_ratio = upper_wick / body
            f.lower_wick_ratio = lower_wick / body

        # --- Price acceleration ---
        if len(closes) >= 6:
            recent_change = (closes[-1] - closes[-2]) / closes[-2] * 100
            prior_changes = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                            for i in range(-6, -1)]
            avg_prior = np.mean(prior_changes)
            f.price_acceleration = recent_change - avg_prior

        # --- Volume z-score from candles (separate field to avoid overwriting trade-based) ---
        if len(volumes) >= 10:
            mu = np.mean(volumes[:-1])
            sigma = np.std(volumes[:-1])
            if sigma > 0:
                f.volume_zscore_candle = (volumes[-1] - mu) / sigma

        # --- Volume decline after spike (исправлено) ---
        if len(volumes) >= 5:
            if len(volumes) >= 10:
                peak_slice = volumes[-10:-2]
            else:
                peak_slice = volumes[:-2]

            if len(peak_slice) > 0:
                peak_vol = np.max(peak_slice)
                current_vol = volumes[-1]
                if current_vol < peak_vol * 0.5 and f.price_change_5m > 2.0:
                    f.volume_decline_after_spike = True

        # --- Momentum loss ---
        if len(closes) >= 10:
            impulse = (closes[-5] - closes[-10]) / closes[-10] * 100
            stall = (closes[-1] - closes[-5]) / closes[-5] * 100
            if impulse > 3.0 and abs(stall) < 0.5:
                f.momentum_loss_signal = True


    # ── Orderbook features ────────────────────────────────────────

    def _compute_ob_features(self, f: CoinFeatures) -> None:
        ob = self._last_ob
        if not ob or not ob.get("bids") or not ob.get("asks"):
            return

        bids = ob["bids"]    # [[price, qty], ...]
        asks = ob["asks"]

        if not bids or not asks:
            return

        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid = (best_bid + best_ask) / 2

        # --- Spread ---
        if mid > 0:
            f.spread_pct = (best_ask - best_bid) / mid * 100

        # --- Depth (top 10 levels) ---
        top10_bids = bids[:10]
        top10_asks = asks[:10]

        f.bid_depth_usdt = sum(p * q for p, q in top10_bids)
        f.ask_depth_usdt = sum(p * q for p, q in top10_asks)

        total_depth = f.bid_depth_usdt + f.ask_depth_usdt
        if total_depth > 0:
            f.ob_imbalance = (f.bid_depth_usdt - f.ask_depth_usdt) / total_depth

        # --- Bid depth change (thinning detection) ---
        if len(self._ob_snapshots) >= 2:
            old_snap = self._ob_snapshots[0]
            old_bids = old_snap.get("data", {}).get("b") or old_snap.get("bids", [])
            if old_bids:
                try:
                    old_bid_depth = sum(float(p) * float(q) for p, q in old_bids[:10])
                    if old_bid_depth > 0:
                        f.bid_depth_change_5m = (f.bid_depth_usdt - old_bid_depth) / old_bid_depth * 100
                except (TypeError, ValueError):
                    pass

    # ── Math helpers ──────────────────────────────────────────────

    @staticmethod
    def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        # Wilder's exponential smoothing
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100 - (100 / (1 + rs)))

    @staticmethod
    def _calc_atr(
        highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14
    ) -> float:
        if len(closes) < 2:
            return 0.0
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )
        if len(tr) < period:
            return float(np.mean(tr)) if len(tr) > 0 else 0.0
        return float(np.mean(tr[-period:]))
