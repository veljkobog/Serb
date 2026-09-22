"""Market-session time helpers (all ET)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .bs import SECONDS_PER_YEAR
from .config import CLOSE_TIME, MARKET_TZ, OPEN_TIME, SCAN_WINDOW_ET

ET = ZoneInfo(MARKET_TZ)

# Full-day closures. Half days (early 13:00 close) are handled separately.
_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}
_HALF_DAYS_2026 = {"2026-11-27", "2026-12-24"}


def now_et() -> datetime:
    return datetime.now(ET)


def to_et(dt: datetime) -> datetime:
    return dt.astimezone(ET) if dt.tzinfo else dt.replace(tzinfo=ET)


def session_open(d: date) -> datetime:
    return datetime.combine(d, time(*OPEN_TIME), tzinfo=ET)


def session_close(d: date) -> datetime:
    if d.isoformat() in _HALF_DAYS_2026:
        return datetime.combine(d, time(13, 0), tzinfo=ET)
    return datetime.combine(d, time(*CLOSE_TIME), tzinfo=ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in _HOLIDAYS_2026


def minutes_since_open(now: Optional[datetime] = None) -> float:
    now = to_et(now or now_et())
    return (now - session_open(now.date())).total_seconds() / 60.0


def minutes_to_close(now: Optional[datetime] = None) -> float:
    now = to_et(now or now_et())
    return max(0.0, (session_close(now.date()) - now).total_seconds() / 60.0)


def t_years_to_close(now: Optional[datetime] = None) -> float:
    """Time to today's 4pm ET expiry, in years. Never negative."""
    return max(0.0, minutes_to_close(now) * 60.0 / SECONDS_PER_YEAR)


def in_scan_window(now: Optional[datetime] = None) -> bool:
    now = to_et(now or now_et())
    if not is_trading_day(now.date()):
        return False
    start, end = SCAN_WINDOW_ET
    return time(*start) <= now.time() <= time(*end)


def window_label() -> str:
    (sh, sm), (eh, em) = SCAN_WINDOW_ET
    return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d} ET"
