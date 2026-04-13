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
FACTOR TABLE  (17 factors, weights sum to 1.00)
═══════════════════════════════════════════════════════════════
Factor                    | Weight | Direction
──────────────────────────┼────────┼────────────────────────────
RSI overbought 1m           |   9%   | ↑ risk when high (adaptive)
RSI overbought 5m           |   7%   | ↑ risk when high (adaptive)
VWAP extension              |   8%   | ↑ risk when above vwap (adaptive)
Volume z-score              |   8%   | ↑ risk when spiking (adaptive)
Trade imbalance (buy-heavy) |   5%   | ↑ risk when buy-dominated
Large buy cluster (5m)      |   8%   | ↑ risk when clustered
Large sell cluster (5m)     |   6%   | ↑ risk when big sells appear
Price acceleration          |   6%   | ↑ risk when accelerating
Consecutive green candles   |   4%   | ↑ risk when 5+
OB imbalance (bid removal)  |   7%   | ↑ risk when bids thin
Spread expansion            |   3%   | ↑ risk when wide
Momentum loss after spike   |   2%   | ↑ risk when stalling
Upper wick rejection        |   1%   | ↑ risk on long upper wick
Funding rate (perp only)    |   6%   | ↑ risk when extreme
OI spike                    |   5%   | ↑ risk when leveraged longs build
CVD divergence (bearish)    |   8%   | ↑ risk on price↑ CVD↓
Liquidation cascade         |   7%   | ↑ risk on forced liquidations
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
    ml_probability: Optional[float] = None
    trend_blocks_short: bool = False

    @property
    def is_actionable(self) -> bool:
        return self.signal_type in {
            SignalType.REVERSAL_RISK,
            SignalType.DUMP_STARTED,
        }

    @property
    def is_alertable(self) -> bool:
        return self.score >= 45 and self.triggered_count >= 2

    def to_dict(self) -> dict:
        d = {
            "symbol": self.symbol,
            "ts": self.ts,
            "score": round(self.score, 1),
            "level": self.level.value,
            "signal_type": self.signal_type.value if self.signal_type else None,
            "triggered_count": self.triggered_count,
            "trend_blocks_short": self.trend_blocks_short,
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
        if self.features_snapshot:
            d["features_snapshot"] = {"last_price": self.features_snapshot.last_price}
        return d


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

    WEIGHTS = {
        "rsi_1m": 0.09,
        "rsi_5m": 0.07,
        "vwap_extension": 0.08,
        "volume_zscore": 0.08,
        "trade_imbalance": 0.05,
        "large_buy_cluster": 0.08,
        "large_sell_cluster": 0.06,
        "price_acceleration": 0.06,
        "consecutive_greens": 0.04,
        "ob_bid_thinning": 0.07,
        "spread_expansion": 0.03,
        "momentum_loss": 0.02,
        "upper_wick": 0.01,
        "funding_rate": 0.06,
        "oi_spike": 0.05,
        "cvd_divergence": 0.08,
        "liquidation_cascade": 0.07,
    }

    RSI_LOW = 55.0
    RSI_HIGH = 72.0

    RSI_5M_LOW = 58.0
    RSI_5M_HIGH = 75.0

    VWAP_EXT_LOW = 1.5
    VWAP_EXT_HIGH = 3.5

    VOLUME_ZSCORE_LOW = 1.0
    VOLUME_ZSCORE_HIGH = 2.2

    IMBALANCE_LOW = 0.2
    IMBALANCE_HIGH = 0.55

    LARGE_BUY_LOW = 2
    LARGE_BUY_HIGH = 5

    LARGE_SELL_LOW = 1
    LARGE_SELL_HIGH = 4

    ACCEL_LOW = 0.3
    ACCEL_HIGH = 1.5

    GREEN_LOW = 3
    GREEN_HIGH = 6

    BID_THIN_LOW = -20.0
    BID_THIN_HIGH = -50.0

    SPREAD_LOW = 0.15
    SPREAD_HIGH = 0.5

    WICK_LOW = 1.0
    WICK_HIGH = 3.0

    FUNDING_LOW = 0.0003
    FUNDING_HIGH = 0.001

    OI_ZSCORE_LOW = 1.0
    OI_ZSCORE_HIGH = 2.5

    def _adaptive_thresholds(self, features: CoinFeatures) -> dict:
        vol = max(0.1, features.realized_vol_1h)
        vol_ratio = vol / 1.0

        rsi_adj = min(8.0, max(-8.0, (vol_ratio - 1.0) * 5.0))
        vwap_adj = min(1.5, max(-0.5, (vol_ratio - 1.0) * 0.8))
        vol_adj = min(0.5, max(-0.3, (vol_ratio - 1.0) * 0.3))

        return {
            "RSI_LOW": self.RSI_LOW + rsi_adj,
            "RSI_HIGH": self.RSI_HIGH + rsi_adj,
            "RSI_5M_LOW": self.RSI_5M_LOW + rsi_adj,
            "RSI_5M_HIGH": self.RSI_5M_HIGH + rsi_adj,
            "VWAP_LOW": self.VWAP_EXT_LOW + vwap_adj,
            "VWAP_HIGH": self.VWAP_EXT_HIGH + vwap_adj,
            "VOL_LOW": self.VOLUME_ZSCORE_LOW + vol_adj,
            "VOL_HIGH": self.VOLUME_ZSCORE_HIGH + vol_adj,
        }

    def score(self, features: CoinFeatures) -> RiskScore:
        factors: list[FactorResult] = []

        at = self._adaptive_thresholds(features)

        rsi_1m = features.rsi_14_1m
        factors.append(self._factor("rsi_1m", rsi_1m, at["RSI_LOW"], at["RSI_HIGH"]))

        rsi_5m = features.rsi_14_5m
        factors.append(self._factor("rsi_5m", rsi_5m, at["RSI_5M_LOW"], at["RSI_5M_HIGH"]))

        vwap_ext = max(features.vwap_extension_pct, 0.0)
        factors.append(self._factor("vwap_extension", vwap_ext, at["VWAP_LOW"], at["VWAP_HIGH"]))

        vz_trade = features.volume_zscore_1m
        vz_candle = features.volume_zscore_candle
        if vz_trade > 0 and vz_candle > 0:
            vz = (vz_trade + vz_candle) / 2
        else:
            vz = max(vz_trade, vz_candle, 0.0)
        factors.append(self._factor("volume_zscore", vz, at["VOL_LOW"], at["VOL_HIGH"]))

        imbalance = max(features.trade_imbalance_5m, 0.0)
        factors.append(self._factor("trade_imbalance", imbalance, self.IMBALANCE_LOW, self.IMBALANCE_HIGH))

        large_buys = float(features.large_buy_count_5m)
        factors.append(self._factor("large_buy_cluster", large_buys, float(self.LARGE_BUY_LOW), float(self.LARGE_BUY_HIGH)))

        large_sells = float(features.large_sell_count_5m)
        factors.append(self._factor("large_sell_cluster", large_sells, float(self.LARGE_SELL_LOW), float(self.LARGE_SELL_HIGH)))

        accel = features.price_acceleration
        factors.append(self._factor("price_acceleration", accel, self.ACCEL_LOW, self.ACCEL_HIGH))

        greens = float(features.consecutive_green_candles)
        factors.append(self._factor("consecutive_greens", greens, float(self.GREEN_LOW), float(self.GREEN_HIGH)))

        bid_change = features.bid_depth_change_5m
        bid_thin_norm = self._normalize(-bid_change, -self.BID_THIN_LOW, -self.BID_THIN_HIGH)
        factors.append(FactorResult(
            name="ob_bid_thinning",
            raw_value=bid_change,
            normalized=bid_thin_norm,
            weight=self.WEIGHTS["ob_bid_thinning"],
            contribution=bid_thin_norm * self.WEIGHTS["ob_bid_thinning"] * 100,
            triggered=bid_thin_norm >= 0.5,
        ))

        spread = features.spread_pct
        factors.append(self._factor("spread_expansion", spread, self.SPREAD_LOW, self.SPREAD_HIGH))

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

        wick = features.upper_wick_ratio
        factors.append(self._factor("upper_wick", wick, self.WICK_LOW, self.WICK_HIGH))

        if features.funding_rate is not None:
            fr = max(features.funding_rate, 0.0)
            factors.append(self._factor("funding_rate", fr, self.FUNDING_LOW, self.FUNDING_HIGH))
        else:
            factors.append(FactorResult(
                name="funding_rate",
                raw_value=0.0,
                normalized=0.0,
                weight=self.WEIGHTS["funding_rate"],
                contribution=0.0,
                triggered=False,
            ))

        oi_z = max(features.oi_zscore, 0.0)
        if features.oi_change_pct > 0 and features.price_change_5m > 0:
            oi_z = oi_z * 1.2
        factors.append(self._factor("oi_spike", oi_z, self.OI_ZSCORE_LOW, self.OI_ZSCORE_HIGH))

        cvd_div = features.cvd_divergence
        cvd_raw = -cvd_div
        factors.append(self._factor("cvd_divergence", cvd_raw, 0.3, 0.6))

        liq_score = features.liquidation_cascade_score
        factors.append(self._factor("liquidation_cascade", liq_score, 0.3, 0.6))

        total_score = sum(f.contribution for f in factors)
        total_score = max(0.0, min(100.0, total_score))

        triggered_count = sum(1 for f in factors if f.triggered)

        if triggered_count < 2:
            total_score = min(total_score, 30.0)

        # ── Multi-timeframe trend context (informational only) ───
        # AutoShortService has its own _check_trend_filter — do not cap score here.
        # Just record the flag so AutoShortService can use it.
        trend_blocks_short = (
            hasattr(features, "trend_context")
            and features.trend_context is not None
            and not features.trend_context.is_safe_to_short()
        )

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
            trend_blocks_short=trend_blocks_short,
        )

    def _classify_signal(
        self,
        f: CoinFeatures,
        score: float,
        factors: list[FactorResult],
    ) -> SignalType | None:
        factor_map = {fr.name: fr for fr in factors}

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

        if (
            f.momentum_loss_signal
            and f.upper_wick_ratio >= self.WICK_LOW
            and f.bid_depth_change_5m < -20.0
        ):
            return SignalType.REVERSAL_RISK

        rsi_1m_f = factor_map.get("rsi_1m")
        rsi_5m_f = factor_map.get("rsi_5m")
        vwap_f = factor_map.get("vwap_extension")
        vol_f = factor_map.get("volume_zscore")

        rsi_confirmed = rsi_1m_f and rsi_1m_f.triggered and rsi_5m_f and rsi_5m_f.triggered
        core_triggered = sum(1 for x in [vwap_f, vol_f] if x and x.triggered)

        if score >= 45 and rsi_confirmed and core_triggered >= 1:
            return SignalType.OVERHEATED

        all_core = sum(1 for x in [rsi_1m_f, vwap_f, vol_f] if x and x.triggered)
        if score >= 45 and all_core >= 2:
            return SignalType.OVERHEATED

        if 25 <= score < 50 and sum(1 for fr in factors if fr.triggered) >= 2:
            return SignalType.EARLY_WARNING

        # Universal fallback: any combination of 2+ factors with score >= 45
        # Catches cases where new features (CVD, OI, liquidation, funding) drive
        # the score high without RSI/VWAP specifically triggering.
        all_triggered = sum(1 for fr in factors if fr.triggered)
        if score >= 45 and all_triggered >= 2:
            return SignalType.OVERHEATED

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
        if value <= low:
            return 0.0
        if value >= high:
            return 1.0
        return (value - low) / (high - low)