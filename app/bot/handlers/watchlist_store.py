"""
Общее MVP-хранилище watchlist в памяти процесса.
ВНИМАНИЕ: после перезапуска контейнера данные пропадут.
"""

WATCHLISTS: dict[int, set[str]] = {}


def normalize_symbol(raw: str) -> str:
    symbol = raw.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    return symbol