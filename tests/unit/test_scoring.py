"""
Unit tests for ScoringEngine.
Tests: factor scoring, normalization, signal classification, anti-noise.
"""
import pytest
from app.analytics.features import CoinFeatures
from app.scoring.engine import RiskLevel, RiskScore, ScoringEngine, SignalType


@pytest.fixture
def engine():
    return ScoringEngine()


@pytest.fixture
def neutral_features():
    """Features of a perfectly calm coin — should score LOW."""
    return CoinFeatures(
        symbol="TESTUSDT",
        ts=1000.0,
        rsi_14_1m=50.0,
        vwap_extension_pct=0.0,
        volume_zscore_1m=0.0,
        trade_imbalance_5m=0.0,
        large_buy_count_5m=0,
        price_acceleration=0.0,
        consecutive_green_candles=0,
        ob_imbalance=0.0,
        bid_depth_change_5m=0.0,
        spread_pct=0.1,
        momentum_loss_signal=False,
        volume_decline_after_spike=False,
        upper_wick_ratio=0.0,
        funding_rate=None,
        last_price=1.0,
    )


@pytest.fixture
def overheated_features():
    """Features of a classically overheated coin."""
    return CoinFeatures(
        symbol="PUMPUSDT",
        ts=2000.0,
        rsi_14_1m=85.0,          # Overbought
        vwap_extension_pct=6.5,   # 6.5% above VWAP
        volume_zscore_1m=3.5,     # 3.5σ volume spike
        trade_imbalance_5m=0.75,  # 75% buys
        large_buy_count_5m=10,    # many large buys
        price_acceleration=2.5,
        consecutive_green_candles=8,
        ob_imbalance=0.3,
        bid_depth_change_5m=-10.0,
        spread_pct=0.15,
        momentum_loss_signal=False,
        volume_decline_after_spike=False,
        upper_wick_ratio=0.5,
        last_price=0.05,
    )


@pytest.fixture
def reversal_features():
    """Features of a coin about to reverse."""
    return CoinFeatures(
        symbol="DUMPERUSDT",
        ts=3000.0,
        rsi_14_1m=82.0,
        vwap_extension_pct=4.5,
        volume_zscore_1m=2.0,
        trade_imbalance_5m=0.1,
        large_buy_count_5m=2,
        price_acceleration=-0.2,
        consecutive_green_candles=2,
        ob_imbalance=-0.2,
        bid_depth_change_5m=-45.0,  # Bids thinning
        spread_pct=0.4,
        momentum_loss_signal=True,  # Stalling
        volume_decline_after_spike=True,
        upper_wick_ratio=2.5,        # Strong rejection wick
        last_price=0.1,
    )


class TestScoringNeutrality:
    def test_neutral_coin_low_score(self, engine, neutral_features):
        result = engine.score(neutral_features)
        assert result.score < 30, f"Neutral coin should score LOW, got {result.score}"
        assert result.level == RiskLevel.LOW

    def test_neutral_coin_no_signal(self, engine, neutral_features):
        result = engine.score(neutral_features)
        assert result.signal_type is None

    def test_neutral_coin_few_triggers(self, engine, neutral_features):
        result = engine.score(neutral_features)
        assert result.triggered_count <= 2


class TestOverheatedDetection:
    def test_overheated_score_high(self, engine, overheated_features):
        result = engine.score(overheated_features)
        assert result.score >= 50, f"Overheated coin should score HIGH, got {result.score}"

    def test_overheated_signal_type(self, engine, overheated_features):
        result = engine.score(overheated_features)
        assert result.signal_type in (SignalType.OVERHEATED, SignalType.EARLY_WARNING)

    def test_overheated_has_reasons(self, engine, overheated_features):
        result = engine.score(overheated_features)
        assert len(result.top_reasons) >= 1

    def test_overheated_is_alertable(self, engine, overheated_features):
        result = engine.score(overheated_features)
        assert result.is_alertable is True


class TestReversalDetection:
    def test_reversal_detects_momentum_loss(self, engine, reversal_features):
        result = engine.score(reversal_features)
        factor_map = {f.name: f for f in result.factors}
        assert factor_map["momentum_loss"].triggered

    def test_reversal_signal_type(self, engine, reversal_features):
        result = engine.score(reversal_features)
        # Should be REVERSAL_RISK or OVERHEATED
        assert result.signal_type in (
            SignalType.REVERSAL_RISK,
            SignalType.OVERHEATED,
            SignalType.EARLY_WARNING,
        )


class TestNormalization:
    def test_score_bounds(self, engine, overheated_features):
        result = engine.score(overheated_features)
        assert 0.0 <= result.score <= 100.0

    def test_all_factors_present(self, engine, neutral_features):
        result = engine.score(neutral_features)
        factor_names = {f.name for f in result.factors}
        required = {"rsi", "vwap_extension", "volume_zscore", "trade_imbalance"}
        assert required.issubset(factor_names)


class TestAntiNoise:
    def test_low_trigger_count_caps_score(self, engine):
        """Score should be capped at 30 if < 3 factors triggered."""
        # Only RSI elevated, everything else neutral
        features = CoinFeatures(
            symbol="ONLYRSIUSDT",
            ts=5000.0,
            rsi_14_1m=85.0,   # Only this factor is high
            vwap_extension_pct=0.0,
            volume_zscore_1m=0.0,
            trade_imbalance_5m=0.0,
            large_buy_count_5m=0,
            price_acceleration=0.0,
            consecutive_green_candles=0,
            spread_pct=0.1,
            bid_depth_change_5m=0.0,
            momentum_loss_signal=False,
            volume_decline_after_spike=False,
            upper_wick_ratio=0.0,
        )
        result = engine.score(features)
        assert result.score <= 30.0, f"Isolated factor should not score > 30, got {result.score}"


class TestDumpStarted:
    def test_dump_started_signal(self, engine):
        features = CoinFeatures(
            symbol="DUMPUSDT",
            ts=6000.0,
            price_change_5m=-4.0,        # Falling
            bid_depth_change_5m=-50.0,   # OB collapsed
            trade_imbalance_5m=-0.5,     # Sell-heavy
            rsi_14_1m=60.0,
            vwap_extension_pct=0.0,
            volume_zscore_1m=2.0,
        )
        result = engine.score(features)
        assert result.signal_type == SignalType.DUMP_STARTED
