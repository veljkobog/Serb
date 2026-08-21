"""US equity-market session helpers: holidays, trading days, DTE, session progress."""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo always present on 3.9+
    ET = dt.timezone(dt.timedelta(hours=-5))

# NYSE full-day closures. Extend as new years are published.
HOLIDAYS = {
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

# 1:00pm ET early closes.
HALF_DAYS = {
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
    "2027-11-26",
}

OPEN_MIN = 9 * 60 + 30
CLOSE_MIN = 16 * 60
HALF_CLOSE_MIN = 13 * 60


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=ET)


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_trading_day(d: dt.date) -> dt.date:
    cur = d - dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= dt.timedelta(days=1)
    return cur


def next_trading_day(d: dt.date) -> dt.date:
    cur = d + dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur += dt.timedelta(days=1)
    return cur


def recent_trading_days(n: int, end: Optional[dt.date] = None) -> List[dt.date]:
    """The ``n`` most recent trading days at or before ``end``, oldest first."""
    cur = end or now_et().date()
    out: List[dt.date] = []
    while len(out) < n:
        if is_trading_day(cur):
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return list(reversed(out))


def trading_days_between(start: dt.date, end: dt.date) -> int:
    """Trading sessions strictly after ``start`` up to and including ``end``."""
    if end <= start:
        return 0
    n, cur = 0, start
    while cur < end:
        cur += dt.timedelta(days=1)
        if is_trading_day(cur):
            n += 1
    return n


def session_close_minute(d: dt.date) -> int:
    return HALF_CLOSE_MIN if d.isoformat() in HALF_DAYS else CLOSE_MIN


def session_progress(ts: Optional[dt.datetime] = None) -> float:
    """Fraction of the regular session elapsed (0.0 pre-open .. 1.0 at/after close)."""
    ts = ts or now_et()
    d = ts.date()
    if not is_trading_day(d):
        return 1.0
    minute = ts.hour * 60 + ts.minute
    close = session_close_minute(d)
    if minute <= OPEN_MIN:
        return 0.0
    if minute >= close:
        return 1.0
    return (minute - OPEN_MIN) / (close - OPEN_MIN)


def market_is_open(ts: Optional[dt.datetime] = None) -> bool:
    ts = ts or now_et()
    if not is_trading_day(ts.date()):
        return False
    minute = ts.hour * 60 + ts.minute
    return OPEN_MIN <= minute < session_close_minute(ts.date())


def parse_date(value) -> Optional[dt.date]:
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc).astimezone(ET).date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%Y"):
            try:
                return dt.datetime.strptime(value[:10], fmt).date()
            except ValueError:
                continue
    return None
