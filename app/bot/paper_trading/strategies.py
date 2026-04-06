"""
Стратегии paper trading — уровни TP и SL.
"""
from dataclasses import dataclass


@dataclass
class Strategy:
    name: str
    label: str
    description: str
    tp1_pct: float
    tp2_pct: float
    tp3_pct: float
    sl_pct: float


STRATEGIES = {
    "conservative": Strategy(
        name="conservative",
        label="🎯 Консервативная",
        description="TP: 5% / 10% / 15% | SL: 5%",
        tp1_pct=5.0,
        tp2_pct=10.0,
        tp3_pct=15.0,
        sl_pct=5.0,
    ),
    "moderate": Strategy(
        name="moderate",
        label="⚡ Средняя",
        description="TP: 10% / 30% / 50% | SL: 10%",
        tp1_pct=10.0,
        tp2_pct=30.0,
        tp3_pct=50.0,
        sl_pct=10.0,
    ),
    "aggressive": Strategy(
        name="aggressive",
        label="🚀 Агрессивная",
        description="TP: 20% / 50% / 100% | SL: 15%",
        tp1_pct=20.0,
        tp2_pct=50.0,
        tp3_pct=100.0,
        sl_pct=15.0,
    ),
}


def calculate_levels(entry_price: float, strategy: Strategy) -> dict:
    """Рассчитать цены TP и SL от цены входа."""
    return {
        "tp1_price": entry_price * (1 + strategy.tp1_pct / 100),
        "tp2_price": entry_price * (1 + strategy.tp2_pct / 100),
        "tp3_price": entry_price * (1 + strategy.tp3_pct / 100),
        "sl_price":  entry_price * (1 - strategy.sl_pct / 100),
    }