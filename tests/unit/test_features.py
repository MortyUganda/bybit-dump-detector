"""
Unit tests for FeatureCalculator.
"""
import time
import pytest
from app.analytics.features import CandleData, FeatureCalculator, TradeTick


@pytest.fixture
def calc():
    return FeatureCalculator("TESTUSDT")


def make_trade(side="Buy", price=1.0, qty=100.0, ts_offset=0) -> TradeTick:
    return TradeTick(
        ts=time.time() - ts_offset,
        price=price,
        qty=qty,
        side=side,
        usdt_value=price * qty,
    )


def make_candle(close=1.0, high=None, low=None, volume=1000.0) -> CandleData:
    return CandleData(
        ts=int(time.time() * 1000),
        open=close * 0.99,
        high=high or close * 1.01,
        low=low or close * 0.98,
        close=close,
        volume=volume,
        turnover=close * volume,
    )


class TestTradeFeatures:
    def test_buy_imbalance(self, calc):
        for i in range(20):
            calc.update_trade(make_trade("Buy", price=1.0, qty=100))
        for i in range(5):
            calc.update_trade(make_trade("Sell", price=1.0, qty=100))

        features = calc.compute()
        assert features.trade_imbalance_5m > 0, "Buy-heavy should show positive imbalance"

    def test_large_trade_threshold(self, calc):
        # Add many small trades + a few large ones
        for i in range(50):
            calc.update_trade(make_trade("Buy", qty=10))  # Small
        for i in range(5):
            calc.update_trade(make_trade("Buy", qty=10000))  # Large

        features = calc.compute()
        assert features.large_trade_threshold > 100, "Threshold should be above small trades"

    def test_sell_imbalance(self, calc):
        for i in range(5):
            calc.update_trade(make_trade("Buy", qty=10))
        for i in range(30):
            calc.update_trade(make_trade("Sell", qty=100))

        features = calc.compute()
        assert features.trade_imbalance_5m < 0, "Sell-heavy should show negative imbalance"


class TestCandleFeatures:
    def test_rsi_overbought(self, calc):
        # 14 rising candles should give high RSI
        candles = []
        for i in range(30):
            price = 1.0 + i * 0.05  # Steadily rising
            candles.append(make_candle(close=price))

        calc.update_candles(candles, "1")
        features = calc.compute()
        assert features.rsi_14_1m > 70, f"Rising candles should give RSI > 70, got {features.rsi_14_1m}"

    def test_consecutive_greens(self, calc):
        # Use distinct timestamps so candles are not deduplicated
        base_ts = int(time.time()) * 1000
        candles = []
        for i in range(15):
            c = CandleData(
                ts=base_ts + i * 60000,
                open=1.0 + i * 0.01,
                high=1.0 + i * 0.01 + 0.005,
                low=1.0 + i * 0.01 - 0.002,
                close=1.0 + (i + 1) * 0.01,  # close > open = green
                volume=1000.0,
                turnover=1010.0,
            )
            candles.append(c)
        calc.update_candles(candles, "1")
        features = calc.compute()
        assert features.consecutive_green_candles >= 5

    def test_insufficient_data_returns_defaults(self, calc):
        # Only 5 candles — insufficient for RSI
        candles = [make_candle() for _ in range(5)]
        calc.update_candles(candles, "1")
        features = calc.compute()
        assert features.rsi_14_1m == 50.0  # Default

    def test_vwap_extension_positive_when_rising(self, calc):
        candles = []
        for i in range(20):
            price = 1.0 + i * 0.05
            candles.append(make_candle(close=price, high=price * 1.01, low=price * 0.99))
        calc.update_candles(candles, "1")
        features = calc.compute()
        # Price at end is above VWAP of last 15 candles
        assert features.vwap_extension_pct >= 0


class TestOrderbookFeatures:
    def test_ob_imbalance_bid_heavy(self, calc):
        ob = {
            "bids": [[1.0, 10000.0], [0.99, 8000.0]],  # Deep bids
            "asks": [[1.01, 100.0], [1.02, 100.0]],     # Thin asks
        }
        calc.update_orderbook(ob)
        features = calc.compute()
        assert features.ob_imbalance > 0, "Bid-heavy OB should have positive imbalance"

    def test_spread_calculation(self, calc):
        ob = {
            "bids": [[1.00, 1000.0]],
            "asks": [[1.02, 1000.0]],
        }
        calc.update_orderbook(ob)
        features = calc.compute()
        # Spread = (1.02 - 1.00) / 1.01 * 100 ≈ 1.98%
        assert abs(features.spread_pct - 1.98) < 0.1, f"Expected ~1.98% spread, got {features.spread_pct}"
