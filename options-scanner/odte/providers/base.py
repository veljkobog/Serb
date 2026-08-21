"""Provider-neutral data model and interface.

Adding a data source means implementing :class:`MarketDataProvider` — nothing in the
signal or scoring layers knows which vendor the numbers came from.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Quote:
    symbol: str
    last: float
    prev_close: Optional[float] = None
    day_open: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    day_volume: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None

    @property
    def change_pct(self) -> Optional[float]:
        if not self.prev_close:
            return None
        return (self.last / self.prev_close - 1.0) * 100.0


@dataclass
class Fundamentals:
    symbol: str
    name: Optional[str] = None
    market_cap: Optional[float] = None
    shares_out: Optional[float] = None
    float_shares: Optional[float] = None
    shares_short: Optional[float] = None
    shares_short_prior: Optional[float] = None
    short_pct_float: Optional[float] = None       # 0..1
    short_ratio: Optional[float] = None           # days to cover, as reported
    short_interest_date: Optional[dt.date] = None
    is_etf: bool = False
    earnings_date: Optional[dt.date] = None


@dataclass
class OptionContract:
    symbol: str
    underlying: str
    expiry: dt.date
    strike: float
    right: str                 # "C" or "P"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: float = 0.0
    open_interest: float = 0.0
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return self.last
        if self.bid <= 0:
            return self.ask / 2.0
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if m is None or m <= 0 or self.bid is None or self.ask is None or self.ask <= 0:
            return None
        return (self.ask - self.bid) / m

    @property
    def notional(self) -> float:
        """Premium traded today, in dollars."""
        m = self.mid or 0.0
        return self.volume * m * 100.0


@dataclass
class OptionChain:
    underlying: str
    expiry: dt.date
    contracts: List[OptionContract] = field(default_factory=list)

    @property
    def calls(self) -> List[OptionContract]:
        return [c for c in self.contracts if c.right == "C"]

    @property
    def puts(self) -> List[OptionContract]:
        return [c for c in self.contracts if c.right == "P"]

    def nearest_strike(self, spot: float) -> Optional[float]:
        strikes = sorted({c.strike for c in self.contracts})
        if not strikes:
            return None
        return min(strikes, key=lambda s: abs(s - spot))

    def at(self, strike: float, right: str) -> Optional[OptionContract]:
        for c in self.contracts:
            if c.right == right and abs(c.strike - strike) < 1e-9:
                return c
        return None


class MarketDataProvider:
    """Interface every data source implements."""

    name = "base"
    supports_greeks = False

    def daily_bars(self, symbol: str, lookback: int = 260) -> List[Bar]:
        raise NotImplementedError

    def quote(self, symbol: str) -> Optional[Quote]:
        raise NotImplementedError

    def fundamentals(self, symbol: str) -> Fundamentals:
        return Fundamentals(symbol=symbol)

    def expirations(self, symbol: str) -> List[dt.date]:
        raise NotImplementedError

    def chain(self, symbol: str, expiry: dt.date) -> Optional[OptionChain]:
        raise NotImplementedError
