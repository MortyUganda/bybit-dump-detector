"""
Risk Scoring Engine — Rule-based scoring model (MVP).

Score = weighted sum of normalized factor scores, clipped to [0, 100].
Each factor contributes a partial score based on its weight.

Risk levels:
  0–24   LOW       — no action
  25–49  MODERATE  — monitor
  50–74  HIGH      — alert
  75–100 CRITICAL  — urgent alert

═══════════════════════════════════════════════════════════════
FACTOR TABLE
═══════════════════════════════════════════════════════════════
Factor                    | Weight | Direction
──────────────────────────┼────────┼────────────────────────────
RSI overbought 1m (>75)    |  13%   | ↑ risk when high
RSI overbought 5m (>70)    |   8%   | ↑ risk when high (confirmation)
VWAP extension (>3%)       |  11%   | ↑ risk when above vwap
Volume z-score (>2σ)       |  11%   | ↑ risk when spiking
Trade imbalance (buy-heavy)|   9%   | ↑ risk when buy-dominated
Large buy cluster (5m)     |   9%   | ↑ risk when clustered
Large sell cluster (5m)    |   6%   | ↑ risk when big sells appear
Price acceleration         |   9%   | ↑ risk when accelerating
Consecutive green candles  |   7%   | ↑ risk when 5+
OB imbalance (bid removal) |   7%   | ↑ risk when bids thin
Spread expansion           |   4%   | ↑ risk when wide
Momentum loss after spike  |   4%   | ↑ risk when stalling
Upper wick rejection       |   2%   | ↑ risk on long upper wick
Funding rate (perp only)   |   0%   | ↑ risk when extreme
═══════════════════════════════════════════════════════════════

TODO: Calibrate thresholds after 1 week of live data.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.analytics.features import CoinFeatures
from app.utils.logging import get_logger
from app.utils.time_utils import utcnow_ts

logger = get_logger(__name__)


class RiskLevel(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"


class SignalType(str, Enum):
    EARLY_WARNING = "early_warning"
    OVERHEATED = "overheated"
    REVERSAL_RISK = "reversal_risk"
    DUMP_STARTED = "dump_started"


@dataclass
class FactorResult:
    name: str
    raw_value: float
    normalized: float
    weight: float
    contribution: float
    triggered: bool


@dataclass
class RiskScore:
    symbol: str
    ts: float
    score: float
    level: RiskLevel
    factors: list[FactorResult]
    signal_type: Optional[SignalType]
    triggered_count: int
    top_reasons: list[str]
    features_snapshot: Optional[CoinFeatures] = None

    @property
    def is_actionable(self) -> bool:
        return self.score >= 45 and self.triggered_count >= 2

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "ts": self.ts,
            "score": round(self.score, 1),
            "level": self.level.value,
            "signal_type": self.signal_type.value if self.signal_type else None,
            "triggered_count": self.triggered_count,
            "top_reasons": self.top_reasons,
            "factors": [
                {
                    "name": f.name,
                    "raw_value": round(f.raw_value, 4),
                    "normalized": round(f.normalized, 3),
                    "contribution": round(f.contribution, 2),
                }
                for f in sorted(self.factors, key=lambda x: -x.contribution)
            ],
        }


def _level_from_score(score: float) -> RiskLevel:
    if score < 25:
        return RiskLevel.LOW
    elif score < 50:
        return RiskLevel.MODERATE
    elif score < 75:
        return RiskLevel.HIGH
    else:
        return RiskLevel.CRITICAL


class ScoringEngine:
    """
    Stateless rule-based scoring engine.
    Takes a CoinFeatures snapshot, returns a RiskScore.
    """

    # ── Factor weights (must sum to 1.0) ─────────────────────────
    WEIGHTS = {
        "rsi_1m":               0.12,   # reduced from 0.13
        "rsi_5m":               0.07,   # reduced from 0.08
        "vwap_extension":       0.10,   # reduced from 0.11
        "volume_zscore":        0.10,   # reduced from 0.11
        "trade_imbalance":      0.09,
        "large_buy_cluster":    0.08,   # reduced from 0.09
        "large_sell_cluster":   0.06,
        "price_acceleration":   0.08,   # reduced from 0.09
        "consecutive_greens":   0.04,
        "ob_bid_thinning":      0.07,
        "spread_expansion":     0.03,
        "momentum_loss":        0.04,
        "upper_wick":           0.01,
        "funding_rate":         0.06,   # increased from 0.05
        "oi_spike":             0.05,   # NEW: open interest spike detection
    }

    # ── RSI 1m thresholds ─────────────────────────────────────────
    RSI_LOW = 55.0
    RSI_HIGH = 72.0

    # ── RSI 5m thresholds (чуть ниже — 5m медленнее реагирует) ───
    RSI_5M_LOW = 58.0
    RSI_5M_HIGH = 75.0

    # ── VWAP extension ────────────────────────────────────────────
    VWAP_EXT_LOW = 1.5
    VWAP_EXT_HIGH = 3.5

    # ── Volume z-score ────────────────────────────────────────────
    VOLUME_ZSCORE_LOW = 1.0
    VOLUME_ZSCORE_HIGH = 2.2

    # ── Trade imbalance ───────────────────────────────────────────
    IMBALANCE_LOW = 0.2
    IMBALANCE_HIGH = 0.55

    # ── Large buy cluster ─────────────────────────────────────────
    LARGE_BUY_LOW = 2
    LARGE_BUY_HIGH = 5

    # ── Large sell cluster (крупные продажи = риск слива) ─────────
    LARGE_SELL_LOW = 1    # даже 1 крупная продажа — сигнал
    LARGE_SELL_HIGH = 4

    # ── Price acceleration ────────────────────────────────────────
    ACCEL_LOW = 0.3
    ACCEL_HIGH = 1.5

    # ── Consecutive green candles ─────────────────────────────────
    GREEN_LOW = 3
    GREEN_HIGH = 6

    # ── OB bid thinning ───────────────────────────────────────────
    BID_THIN_LOW = -20.0
    BID_THIN_HIGH = -50.0

    # ── Spread expansion ──────────────────────────────────────────
    SPREAD_LOW = 0.15
    SPREAD_HIGH = 0.5

    # ── Upper wick ────────────────────────────────────────────────
    WICK_LOW = 1.0
    WICK_HIGH = 3.0

    # ── Funding rate ──────────────────────────────────────────────
    FUNDING_LOW = 0.0003   # 0.03% = slightly elevated
    FUNDING_HIGH = 0.001   # 0.1% = very crowded longs

    # ── Open Interest spike ───────────────────────────────────────
    OI_ZSCORE_LOW = 1.0    # slightly elevated OI growth
    OI_ZSCORE_HIGH = 2.5   # very strong OI spike

    def score(self, features: CoinFeatures) -> RiskScore:
        factors: list[FactorResult] = []

        # ── 1. RSI 1m overbought ──────────────────────────────────
        rsi_1m = features.rsi_14_1m
        factors.append(self._factor(
            "rsi_1m",
            rsi_1m,
            self.RSI_LOW, self.RSI_HIGH,
        ))

        # ── 2. RSI 5m overbought (подтверждение) ─────────────────
        rsi_5m = features.rsi_14_5m
        factors.append(self._factor(
            "rsi_5m",
            rsi_5m,
            self.RSI_5M_LOW, self.RSI_5M_HIGH,
        ))

        # ── 3. VWAP extension ─────────────────────────────────────
        vwap_ext = max(features.vwap_extension_pct, 0.0)
        factors.append(self._factor(
            "vwap_extension",
            vwap_ext,
            self.VWAP_EXT_LOW, self.VWAP_EXT_HIGH,
        ))

        # ── 4. Volume z-score (average trade-based and candle-based) ─
        vz_trade = features.volume_zscore_1m
        vz_candle = features.volume_zscore_candle
        if vz_trade > 0 and vz_candle > 0:
            vz = (vz_trade + vz_candle) / 2
        else:
            vz = max(vz_trade, vz_candle, 0.0)
        factors.append(self._factor(
            "volume_zscore",
            vz,
            self.VOLUME_ZSCORE_LOW, self.VOLUME_ZSCORE_HIGH,
        ))

        # ── 5. Trade imbalance (buy-dominated) ────────────────────
        imbalance = max(features.trade_imbalance_5m, 0.0)
        factors.append(self._factor(
            "trade_imbalance",
            imbalance,
            self.IMBALANCE_LOW, self.IMBALANCE_HIGH,
        ))

        # ── 6. Large buy cluster ──────────────────────────────────
        large_buys = float(features.large_buy_count_5m)
        factors.append(self._factor(
            "large_buy_cluster",
            large_buys,
            float(self.LARGE_BUY_LOW), float(self.LARGE_BUY_HIGH),
        ))

        # ── 7. Large sell cluster (NEW) ───────────────────────────
        # Крупные продажи после памп-сигнала = умные деньги выходят
        large_sells = float(features.large_sell_count_5m)
        factors.append(self._factor(
            "large_sell_cluster",
            large_sells,
            float(self.LARGE_SELL_LOW), float(self.LARGE_SELL_HIGH),
        ))

        # ── 8. Price acceleration ─────────────────────────────────
        accel = features.price_acceleration
        factors.append(self._factor(
            "price_acceleration",
            accel,
            self.ACCEL_LOW, self.ACCEL_HIGH,
        ))

        # ── 9. Consecutive green candles ──────────────────────────
        greens = float(features.consecutive_green_candles)
        factors.append(self._factor(
            "consecutive_greens",
            greens,
            float(self.GREEN_LOW), float(self.GREEN_HIGH),
        ))

        # ── 10. OB bid thinning ───────────────────────────────────
        bid_change = features.bid_depth_change_5m
        bid_thin_norm = self._normalize(
            -bid_change,
            -self.BID_THIN_LOW,
            -self.BID_THIN_HIGH,
        )
        factors.append(FactorResult(
            name="ob_bid_thinning",
            raw_value=bid_change,
            normalized=bid_thin_norm,
            weight=self.WEIGHTS["ob_bid_thinning"],
            contribution=bid_thin_norm * self.WEIGHTS["ob_bid_thinning"] * 100,
            triggered=bid_thin_norm >= 0.5,
        ))

        # ── 11. Spread expansion ──────────────────────────────────
        spread = features.spread_pct
        factors.append(self._factor(
            "spread_expansion",
            spread,
            self.SPREAD_LOW, self.SPREAD_HIGH,
        ))

        # ── 12. Momentum loss ─────────────────────────────────────
        mom_loss = 1.0 if features.momentum_loss_signal else 0.0
        vol_decline = 1.0 if features.volume_decline_after_spike else 0.0
        momentum_val = max(mom_loss, vol_decline * 0.7)
        factors.append(FactorResult(
            name="momentum_loss",
            raw_value=momentum_val,
            normalized=momentum_val,
            weight=self.WEIGHTS["momentum_loss"],
            contribution=momentum_val * self.WEIGHTS["momentum_loss"] * 100,
            triggered=momentum_val >= 0.5,
        ))

        # ── 13. Upper wick rejection ──────────────────────────────
        wick = features.upper_wick_ratio
        factors.append(self._factor(
            "upper_wick",
            wick,
            self.WICK_LOW, self.WICK_HIGH,
        ))

        # ── 14. Funding rate (optional) ───────────────────────────
        if features.funding_rate is not None:
            fr = max(features.funding_rate, 0.0)
            factors.append(self._factor(
                "funding_rate",
                fr,
                self.FUNDING_LOW, self.FUNDING_HIGH,
            ))
        else:
            factors.append(FactorResult(
                name="funding_rate",
                raw_value=0.0,
                normalized=0.0,
                weight=self.WEIGHTS["funding_rate"],
                contribution=0.0,
                triggered=False,
            ))

        # ── 15. Open Interest spike ───────────────────────────────
        # Rising OI + rising price = leveraged longs building up = stronger short signal
        oi_z = max(features.oi_zscore, 0.0)
        # Boost if OI rising AND price rising (convergent signal)
        if features.oi_change_pct > 0 and features.price_change_5m > 0:
            oi_z = oi_z * 1.2  # 20% boost for convergent signal
        factors.append(self._factor(
            "oi_spike",
            oi_z,
            self.OI_ZSCORE_LOW, self.OI_ZSCORE_HIGH,
        ))

        # ── Aggregate score ───────────────────────────────────────
        total_score = sum(f.contribution for f in factors)
        total_score = max(0.0, min(100.0, total_score))

        triggered_count = sum(1 for f in factors if f.triggered)

        # ── Anti-noise ────────────────────────────────────────────
        if triggered_count < 2:
            total_score = min(total_score, 30.0)

        # ── Multi-timeframe trend filter ─────────────────────────
        if hasattr(features, 'trend_context') and features.trend_context:
            if not features.trend_context.is_safe_to_short():
                total_score = min(total_score, 30.0)

        level = _level_from_score(total_score)

        signal_type = self._classify_signal(features, total_score, factors)

        top_reasons = [
            f.name
            for f in sorted(factors, key=lambda x: -x.contribution)
            if f.triggered
        ][:3]

        return RiskScore(
            symbol=features.symbol,
            ts=features.ts,
            score=total_score,
            level=level,
            factors=factors,
            signal_type=signal_type,
            triggered_count=triggered_count,
            top_reasons=top_reasons,
            features_snapshot=features,
        )

    def _classify_signal(
        self,
        f: CoinFeatures,
        score: float,
        factors: list[FactorResult],
    ) -> SignalType | None:
        factor_map = {fr.name: fr for fr in factors}

        # DUMP_STARTED: цена уже падает + OB collapse + крупные продажи
        large_sell_f = factor_map.get("large_sell_cluster")
        if (
            f.price_change_5m < -3.0
            and f.bid_depth_change_5m < -40.0
            and f.trade_imbalance_5m < -0.3
        ) or (
            f.price_change_5m < -2.0
            and large_sell_f and large_sell_f.triggered
            and f.bid_depth_change_5m < -30.0
        ):
            return SignalType.DUMP_STARTED

        # REVERSAL_RISK: стагнация + wick + bid thinning
        if (
            f.momentum_loss_signal
            and f.upper_wick_ratio >= self.WICK_LOW
            and f.bid_depth_change_5m < -20.0
        ):
            return SignalType.REVERSAL_RISK

        # OVERHEATED: RSI 1m + RSI 5m + (VWAP или volume) — подтверждение
        rsi_1m_f = factor_map.get("rsi_1m")
        rsi_5m_f = factor_map.get("rsi_5m")
        vwap_f = factor_map.get("vwap_extension")
        vol_f = factor_map.get("volume_zscore")

        # Оба RSI сработали + хотя бы один из VWAP/volume
        rsi_confirmed = rsi_1m_f and rsi_1m_f.triggered and rsi_5m_f and rsi_5m_f.triggered
        core_triggered = sum(1 for x in [vwap_f, vol_f] if x and x.triggered)

        if score >= 45 and rsi_confirmed and core_triggered >= 1:
            return SignalType.OVERHEATED

        # Fallback OVERHEATED: 2 из 3 базовых факторов (как раньше)
        all_core = sum(1 for x in [rsi_1m_f, vwap_f, vol_f] if x and x.triggered)
        if score >= 45 and all_core >= 2:
            return SignalType.OVERHEATED

        # EARLY_WARNING
        if 25 <= score < 50 and sum(1 for fr in factors if fr.triggered) >= 2:
            return SignalType.EARLY_WARNING

        return None

    def _factor(
        self,
        name: str,
        value: float,
        low_thresh: float,
        high_thresh: float,
    ) -> FactorResult:
        norm = self._normalize(value, low_thresh, high_thresh)
        weight = self.WEIGHTS.get(name, 0.0)
        return FactorResult(
            name=name,
            raw_value=value,
            normalized=norm,
            weight=weight,
            contribution=norm * weight * 100,
            triggered=norm >= 0.5,
        )

    @staticmethod
    def _normalize(value: float, low: float, high: float) -> float:
        """Linear normalization: value < low → 0, value > high → 1."""
        if high <= low:
            return 0.0
        clipped = max(low, min(high, value))
        return (clipped - low) / (high - low)