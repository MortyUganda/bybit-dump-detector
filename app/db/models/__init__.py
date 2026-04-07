from app.db.models.base import Base
from app.db.models.symbol import Symbol
from app.db.models.signal import Signal
from app.db.models.overvalued import OvervaluedSnapshot
from app.db.models.candle import CandleFeatureRow
from app.db.models.user import UserSettings, Watchlist
from app.db.models.alert import AlertHistory
from app.db.models.paper_trade import PaperTrade
from app.db.models.auto_short import AutoShort  # ← новая таблица


__all__ = [
    "Base",
    "Symbol",
    "Signal",
    "OvervaluedSnapshot",
    "CandleFeatureRow",
    "UserSettings",
    "Watchlist",
    "AlertHistory",
    "PaperTrade",
    "AutoShort",
]