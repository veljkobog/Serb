"""FINRA off-exchange (a.k.a. "dark pool") volume — free, no key, daily.

What this actually is, stated plainly so the signal is used honestly:

FINRA publishes a daily consolidated file of every trade in an NMS stock that printed
*off-exchange* — ATSs (true dark pools) plus wholesaler/internalizer prints. That is
the same tape retail "dark pool" tools bill you for. Two things fall out of it:

  ``offex_share``  off-exchange volume / consolidated volume.
                   Structurally ~40-50% for large caps. A jump above its own 20-day
                   mean is the tell — a block desk worked size away from the lit book.

  ``dpi``          1 - (off-exchange short volume / off-exchange volume).
                   Off-exchange "short" volume is dominated by market makers hedging
                   the other side of *buy* orders, so a LOW short ratio (high DPI)
                   reads as accumulation. This is a proxy, not a position report,
                   and it is noisy on any single day — use the multi-day trend.

Limits: the file is end-of-day (published after the close for that session), so it is
a next-morning signal, not an intraday one. It has no trade size, no venue, and no
price. If you need block prints tick-by-tick you need a paid feed.

Docs: https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..calendar_utils import parse_date, recent_trading_days
from ..http import Http, HttpError

DAILY_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{ymd}.txt"


@dataclass
class OffExDay:
    date: dt.date
    short_volume: float
    short_exempt_volume: float
    total_volume: float

    @property
    def short_ratio(self) -> Optional[float]:
        return None if self.total_volume <= 0 else self.short_volume / self.total_volume

    @property
    def dpi(self) -> Optional[float]:
        r = self.short_ratio
        return None if r is None else 1.0 - r


class FinraOffExchange:
    """Loads and indexes the last N daily off-exchange files."""

    def __init__(self, http: Optional[Http] = None, days: int = 25):
        # Published files never change, so cache them effectively forever.
        self.http = http or Http(min_interval=0.1, timeout=45)
        self.days = days
        self.by_symbol: Dict[str, List[OffExDay]] = {}
        self.loaded_dates: List[dt.date] = []
        self.errors: List[str] = []

    def load(self, end: Optional[dt.date] = None) -> "FinraOffExchange":
        for day in recent_trading_days(self.days, end):
            try:
                text = self.http.get_text(DAILY_URL.format(ymd=day.strftime("%Y%m%d")), cache_ttl=30 * 86400)
            except HttpError as exc:
                # 404 is normal: today's file is not posted until after the close.
                if getattr(exc, "status", None) != 404:
                    self.errors.append(f"{day}: {exc}")
                continue
            except Exception as exc:  # network
                self.errors.append(f"{day}: {exc}")
                continue
            if self._parse(text, day):
                self.loaded_dates.append(day)
        self.loaded_dates.sort()
        for rows in self.by_symbol.values():
            rows.sort(key=lambda r: r.date)
        return self

    def _parse(self, text: str, fallback: dt.date) -> int:
        rows = 0
        for line in text.splitlines():
            parts = line.strip().split("|")
            if len(parts) < 5 or parts[0] in ("Date", "") or not parts[1]:
                continue
            try:
                rec = OffExDay(
                    date=parse_date(parts[0]) or fallback,
                    short_volume=float(parts[2] or 0),
                    short_exempt_volume=float(parts[3] or 0),
                    total_volume=float(parts[4] or 0),
                )
            except ValueError:
                continue
            self.by_symbol.setdefault(parts[1].upper(), []).append(rec)
            rows += 1
        return rows

    def history(self, symbol: str) -> List[OffExDay]:
        return self.by_symbol.get(symbol.upper(), [])

    def latest(self, symbol: str) -> Optional[OffExDay]:
        rows = self.history(symbol)
        return rows[-1] if rows else None

    @property
    def available(self) -> bool:
        return bool(self.by_symbol)
