"""Timezone-aware time utilities."""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def utcnow_ts() -> float:
    """Return current UTC timestamp as float."""
    return utcnow().timestamp()


def ms_to_dt(ms: int) -> datetime:
    """Convert millisecond timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Convert datetime to millisecond timestamp."""
    return int(dt.timestamp() * 1000)
