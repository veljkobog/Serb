"""Plain data containers shared by providers, metrics and scoring.

Stdlib only on purpose: pandas/numpy stay confined to the provider adapters so
the scoring engine can run (and be tested) anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Bar:
    """One intraday OHLCV bar (regular-session only)."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def typical(self) -> float:
        """HLC/3 — used as the VWAP price proxy for a bar."""
        return (self.high + self.low + self.close) / 3.0


@dataclass
class OptionQuote:
    strike: float
    right: str  # "C" or "P"
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    volume: float = 0.0
    open_interest: float = 0.0
    iv: Optional[float] = None  # provider IV, as a decimal (0.23 == 23%)
    occ: Optional[str] = None   # OCC symbol, e.g. SPY260922C00590000
    # Provider greeks, when the feed supplies them (Tradier does). Used in
    # preference to re-deriving them from the mid.
    delta: Optional[float] = None
    gamma: Optional[float] = None
    # Signed volume from classified time & sales. Zeros mean "not sampled",
    # which is different from "no trades" — check `sampled` before reading.
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    mid_volume: float = 0.0
    buy_premium: float = 0.0
    sell_premium: float = 0.0

    @property
    def sampled_volume(self) -> float:
        return self.buy_volume + self.sell_volume + self.mid_volume

    @property
    def sampled(self) -> bool:
        return self.sampled_volume > 0

    @property
    def net_volume(self) -> float:
        """Buyer-initiated minus seller-initiated contracts."""
        return self.buy_volume - self.sell_volume

    @property
    def mid(self) -> Optional[float]:
        """Mid price, falling back to last when one side of the book is missing."""
        if self.bid is not None and self.ask is not None and self.ask > 0:
            if self.bid <= 0:
                # Bid-less penny options: the ask alone is the only real info.
                return self.ask / 2.0
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread_pct(self) -> Optional[float]:
        """Bid/ask spread as a fraction of mid. None when unquotable."""
        if self.bid is None or self.ask is None:
            return None
        mid = self.mid
        if not mid or mid <= 0 or self.ask <= 0:
            return None
        return (self.ask - self.bid) / mid


@dataclass
class SymbolSnapshot:
    """Everything a single symbol contributes, as captured at scan time."""

    symbol: str
    spot: float
    prev_close: Optional[float]
    bars: List[Bar] = field(default_factory=list)
    chain: List[OptionQuote] = field(default_factory=list)
    expiry: Optional[str] = None  # YYYY-MM-DD of the 0DTE expiry actually used
    adr_pct: Optional[float] = None  # 14-day average daily range, % of price
    avg_share_volume: Optional[float] = None
    is_benchmark: bool = False
    notes: List[str] = field(default_factory=list)
    # Seconds of classified time & sales merged into `chain`, when any.
    side_window_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["bars"] = [
            {
                "ts": b.ts.isoformat(),
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in self.bars
        ]
        return d
