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
RSI overbought (>75)       |  15%   | ↑ risk when high
VWAP extension (>3%)       |  12%   | ↑ risk when above vwap
Volume z-score (>2σ)       |  12%   | ↑ risk when spiking
Trade imbalance (buy-heavy)|  10%   | ↑ risk when buy-dominated
Large buy cluster (5m)     |  10%   | ↑ risk when clustered
Price acceleration         |  10%   | ↑ risk when accelerating
Consecutive green candles  |   8%   | ↑ risk when 5+
OB imbalance (bid removal) |   8%   | ↑ risk when bids thin
Spread expansion           |   5%   | ↑ risk when wide
Momentum loss after spike  |   5%   | ↑ risk when stalling
Upper wick rejection       |   3%   | ↑ risk on long upper wick
Funding rate (perp only)   |   2%   | ↑ risk when extreme
═══════════════════════════════════════════════════════════════

Anti-noise mechanisms:
- Minimum 100 trades in buffer before scoring
- score only increases if >= 3 factors triggered
- cooldown: same symbol re-scored at most every 30s

TODO: Calibrate thresholds after 1 week of live data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    EARLY_WARNING = "early_warning"    # Score 30–49, 2+ factors
    OVERHEATED = "overheated"          # Score 50–74, RSI + VWAP + volume all high
    REVERSAL_RISK = "reversal_risk"    # Momentum loss + wick + OB thinning
    DUMP_STARTED = "dump_started"      # Price already -3% from recent high + OB collapse


@dataclass
class FactorResult:
    name: str
    raw_value: float
    normalized: float   # 0–1
    weight: float
    contribution: float  # normalized * weight * 100
    triggered: bool      # normalized >= 0.5


@dataclass
class RiskScore:
    symbol: str
    ts: float
    score: float                        # 0–100
    level: RiskLevel
    factors: list[FactorResult]
    signal_type: Optional[SignalType]
    triggered_count: int
    top_reasons: list[str]              # human-readable top 3 reasons
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
        "rsi":                  0.15,
        "vwap_extension":       0.12,
        "volume_zscore":        0.12,
        "trade_imbalance":      0.10,
        "large_buy_cluster":    0.10,
        "price_acceleration":   0.10,
        "consecutive_greens":   0.08,
        "ob_bid_thinning":      0.08,
        "spread_expansion":     0.05,
        "momentum_loss":        0.05,
        "upper_wick":           0.03,
        "funding_rate":         0.02,
    }

    # ── Threshold calibration defaults ────────────────────────────
    # RSI thresholds for normalization: below low_thresh → 0, above high_thresh → 1
    RSI_LOW = 60.0   # TODO: recalibrate after live data
    RSI_HIGH = 75.0

    VWAP_EXT_LOW = 1.5   # % — moderate overextension
    VWAP_EXT_HIGH = 2.5 # % — extreme overextension

    VOLUME_ZSCORE_LOW = 1.2
    VOLUME_ZSCORE_HIGH = 3.0

    # Imbalance: +1 = all buys, –1 = all sells. High buy imbalance = risk
    IMBALANCE_LOW = 0.3
    IMBALANCE_HIGH = 0.7

    # Large buy cluster (count 5m)
    LARGE_BUY_LOW = 3
    LARGE_BUY_HIGH = 8

    # Price acceleration (% vs prior baseline)
    ACCEL_LOW = 0.5
    ACCEL_HIGH = 2.0

    # Consecutive green candles
    GREEN_LOW = 4
    GREEN_HIGH = 8

    # OB bid depth change (negative = thinning = risk)
    BID_THIN_LOW = -20.0  # %
    BID_THIN_HIGH = -50.0

    # Spread expansion
    SPREAD_LOW = 0.2   # % — normal spread for shitcoin
    SPREAD_HIGH = 0.8

    # Upper wick (ratio to body)
    WICK_LOW = 1.0
    WICK_HIGH = 3.0

    # Funding rate (positive = longs paying = crowded long)
    FUNDING_LOW = 0.0005   # 0.05%
    FUNDING_HIGH = 0.002   # 0.2%

    def score(self, features: CoinFeatures) -> RiskScore:
        """
        Compute RiskScore from CoinFeatures.
        Returns LOW score with no signal if features are insufficient.
        """
        factors: list[FactorResult] = []

        # ── 1. RSI overbought ─────────────────────────────────────
        rsi = features.rsi_14_1m
        factors.append(self._factor(
            "rsi",
            rsi,
            self.RSI_LOW, self.RSI_HIGH,
            f"RSI={rsi:.1f} (overbought threshold {self.RSI_HIGH})",
        ))

        # ── 2. VWAP extension ─────────────────────────────────────
        vwap_ext = max(features.vwap_extension_pct, 0.0)  # only positive (price above VWAP)
        factors.append(self._factor(
            "vwap_extension",
            vwap_ext,
            self.VWAP_EXT_LOW, self.VWAP_EXT_HIGH,
            f"VWAP+{vwap_ext:.1f}% (price above VWAP)",
        ))

        # ── 3. Volume z-score ─────────────────────────────────────
        vz = max(features.volume_zscore_1m, 0.0)
        factors.append(self._factor(
            "volume_zscore",
            vz,
            self.VOLUME_ZSCORE_LOW, self.VOLUME_ZSCORE_HIGH,
            f"Vol z-score={vz:.1f}σ (abnormal volume spike)",
        ))

        # ── 4. Trade imbalance (buy-dominated) ────────────────────
        imbalance = max(features.trade_imbalance_5m, 0.0)  # only buy dominance counts
        factors.append(self._factor(
            "trade_imbalance",
            imbalance,
            self.IMBALANCE_LOW, self.IMBALANCE_HIGH,
            f"Buy imbalance={imbalance:.2f} (buy-heavy flow)",
        ))

        # ── 5. Large buy cluster ───────────────────────────────────
        large_buys = float(features.large_buy_count_5m)
        factors.append(self._factor(
            "large_buy_cluster",
            large_buys,
            float(self.LARGE_BUY_LOW), float(self.LARGE_BUY_HIGH),
            f"{int(large_buys)} large buys in 5m (cluster detected)",
        ))

        # ── 6. Price acceleration ─────────────────────────────────
        accel = features.price_acceleration
        factors.append(self._factor(
            "price_acceleration",
            accel,
            self.ACCEL_LOW, self.ACCEL_HIGH,
            f"Acceleration={accel:+.2f}% vs baseline",
        ))

        # ── 7. Consecutive green candles ──────────────────────────
        greens = float(features.consecutive_green_candles)
        factors.append(self._factor(
            "consecutive_greens",
            greens,
            float(self.GREEN_LOW), float(self.GREEN_HIGH),
            f"{int(greens)} consecutive green 1m candles",
        ))

        # ── 8. OB bid thinning ────────────────────────────────────
        # Negative change = bids are disappearing = risk
        bid_change = features.bid_depth_change_5m
        bid_thin_norm = self._normalize(
            -bid_change,  # invert: more negative = higher normalized
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

        # ── 9. Spread expansion ───────────────────────────────────
        spread = features.spread_pct
        factors.append(self._factor(
            "spread_expansion",
            spread,
            self.SPREAD_LOW, self.SPREAD_HIGH,
            f"Spread={spread:.3f}% (liquidity thinning)",
        ))

        # ── 10. Momentum loss ─────────────────────────────────────
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

        # ── 11. Upper wick rejection ──────────────────────────────
        wick = features.upper_wick_ratio
        factors.append(self._factor(
            "upper_wick",
            wick,
            self.WICK_LOW, self.WICK_HIGH,
            f"Upper wick={wick:.1f}x body (rejection signal)",
        ))

        # ── 12. Funding rate (optional) ───────────────────────────
        if features.funding_rate is not None:
            fr = max(features.funding_rate, 0.0)
            factors.append(self._factor(
                "funding_rate",
                fr,
                self.FUNDING_LOW, self.FUNDING_HIGH,
                f"Funding={fr*100:.3f}% (crowded longs)",
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

        # ── Aggregate score ───────────────────────────────────────
        total_score = sum(f.contribution for f in factors)
        total_score = max(0.0, min(100.0, total_score))

        triggered_count = sum(1 for f in factors if f.triggered)

        # ── Anti-noise: suppress weak signals ────────────────────
        # Only report if >= 3 factors triggered
        # Эксперимент: достаточно 2 триггеров, чтобы позволить score расти
        if triggered_count < 2:
            total_score = min(total_score, 30.0)

        level = _level_from_score(total_score)

        # ── Signal type classification ────────────────────────────
        signal_type = self._classify_signal(features, total_score, factors)

        # ── Top reasons ───────────────────────────────────────────
        top_reasons = [
            f.name
            for f in sorted(factors, key=lambda x: -x.contribution)
            if f.triggered
        ][:3]

        top_reason_messages = []
        for f in sorted(factors, key=lambda x: -x.contribution):
            if f.triggered:
                # Find the human label we embedded in the factor name
                top_reason_messages.append(f.name)
                if len(top_reason_messages) >= 3:
                    break

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

        # DUMP_STARTED: price already falling + OB collapse
        if (
            f.price_change_5m < -3.0
            and f.bid_depth_change_5m < -40.0
            and f.trade_imbalance_5m < -0.3
        ):
            return SignalType.DUMP_STARTED

        # REVERSAL_RISK: stalling momentum + wick rejection + bid thinning
        if (
            f.momentum_loss_signal
            and f.upper_wick_ratio >= self.WICK_LOW
            and f.bid_depth_change_5m < -20.0
        ):
            return SignalType.REVERSAL_RISK

        # OVERHEATED: RSI + VWAP + volume spike all elevated
        rsi_f = factor_map.get("rsi")
        vwap_f = factor_map.get("vwap_extension")
        vol_f = factor_map.get("volume_zscore")
        if (
            score >= 50
            and rsi_f and rsi_f.triggered
            and vwap_f and vwap_f.triggered
            and vol_f and vol_f.triggered
        ):
            return SignalType.OVERHEATED

        # EARLY_WARNING: score 30–49 with some factors triggering
        if 30 <= score < 50 and sum(1 for fr in factors if fr.triggered) >= 1:
            return SignalType.EARLY_WARNING

        return None

    def _factor(
        self,
        name: str,
        value: float,
        low_thresh: float,
        high_thresh: float,
        label: str = "",
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
