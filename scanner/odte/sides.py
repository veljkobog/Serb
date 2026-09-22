"""Trade-side classification and the side tape.

Exchange time & sales carry no buy/sell label, so each print is classified
against the quote that was standing when it happened (Lee-Ready): lifting the
offer is buyer-initiated, hitting the bid is seller-initiated, and prints
inside the spread fall back to the midpoint comparison and then a tick test.

"Buyer-initiated" means the customer was the aggressor. Customer buys calls or
sells puts -> upside pressure; customer buys puts or sells calls -> downside.
Signing by the contract's own delta handles all four cases at once, since put
deltas are already negative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

from .models import OptionQuote

TICK = 0.005  # half a cent: options quote in 1c / 5c increments

BUY, SELL, MID = "buy", "sell", "mid"


def occ_symbol(root: str, expiry: str | date, right: str, strike: float) -> str:
    """Build an OCC symbol, e.g. SPY260922C00590000."""
    d = date.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    return (
        f"{root.upper()}{d:%y%m%d}{right.upper()[0]}"
        f"{int(round(strike * 1000)):08d}"
    )


def classify(
    price: float,
    bid: Optional[float],
    ask: Optional[float],
    prev_price: Optional[float] = None,
) -> str:
    """Label one print as buyer-initiated, seller-initiated, or neutral."""
    if bid is not None and ask is not None and ask > 0 and ask >= bid:
        if price >= ask - TICK:
            return BUY
        if price <= bid + TICK:
            return SELL
        mid = (bid + ask) / 2.0
        if price > mid + TICK:
            return BUY
        if price < mid - TICK:
            return SELL
    # Inside-the-spread or quote-less print: fall back to the tick test.
    if prev_price is not None:
        if price > prev_price:
            return BUY
        if price < prev_price:
            return SELL
    return MID


@dataclass
class ContractSides:
    buy: float = 0.0
    sell: float = 0.0
    mid: float = 0.0
    buy_premium: float = 0.0
    sell_premium: float = 0.0
    trades: int = 0
    last_price: Optional[float] = None


@dataclass
class SideTape:
    """Accumulated classified volume, keyed by OCC symbol."""

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    trades: int = 0
    contracts: Dict[str, ContractSides] = field(default_factory=dict)

    @property
    def window_seconds(self) -> Optional[float]:
        if not self.started_at or not self.ended_at:
            return None
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    def record(
        self,
        occ: str,
        price: float,
        size: float,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        when: Optional[datetime] = None,
    ) -> str:
        if size <= 0 or price <= 0:
            return MID
        c = self.contracts.setdefault(occ, ContractSides())
        side = classify(price, bid, ask, c.last_price)
        premium = size * price * 100.0
        if side == BUY:
            c.buy += size
            c.buy_premium += premium
        elif side == SELL:
            c.sell += size
            c.sell_premium += premium
        else:
            c.mid += size
        c.trades += 1
        c.last_price = price
        self.trades += 1
        if when:
            self.started_at = min(self.started_at or when, when)
            self.ended_at = max(self.ended_at or when, when)
        return side

    def merge_into(self, chain: Sequence[OptionQuote]) -> int:
        """Copy classified volume onto matching quotes. Returns quotes touched."""
        hits = 0
        for q in chain:
            c = self.contracts.get(q.occ or "")
            if not c:
                continue
            q.buy_volume = c.buy
            q.sell_volume = c.sell
            q.mid_volume = c.mid
            q.buy_premium = c.buy_premium
            q.sell_premium = c.sell_premium
            hits += 1
        return hits

    # --- persistence ------------------------------------------------------
    def to_dict(self) -> Dict:
        return {
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "trades": self.trades,
            "contracts": {
                k: {
                    "buy": v.buy, "sell": v.sell, "mid": v.mid,
                    "buy_premium": v.buy_premium, "sell_premium": v.sell_premium,
                    "trades": v.trades,
                }
                for k, v in self.contracts.items()
            },
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2))
        return p

    @classmethod
    def load(cls, path: str | Path) -> "SideTape":
        d = json.loads(Path(path).read_text())
        tape = cls(
            started_at=datetime.fromisoformat(d["started_at"]) if d.get("started_at") else None,
            ended_at=datetime.fromisoformat(d["ended_at"]) if d.get("ended_at") else None,
            trades=d.get("trades", 0),
        )
        for occ, c in (d.get("contracts") or {}).items():
            tape.contracts[occ] = ContractSides(
                buy=c.get("buy", 0.0), sell=c.get("sell", 0.0), mid=c.get("mid", 0.0),
                buy_premium=c.get("buy_premium", 0.0),
                sell_premium=c.get("sell_premium", 0.0),
                trades=c.get("trades", 0),
            )
        return tape

    def merge_tape(self, other: "SideTape") -> "SideTape":
        """Fold another tape in — lets a long recording be written in chunks."""
        for occ, c in other.contracts.items():
            mine = self.contracts.setdefault(occ, ContractSides())
            mine.buy += c.buy
            mine.sell += c.sell
            mine.mid += c.mid
            mine.buy_premium += c.buy_premium
            mine.sell_premium += c.sell_premium
            mine.trades += c.trades
        self.trades += other.trades
        starts = [t for t in (self.started_at, other.started_at) if t]
        ends = [t for t in (self.ended_at, other.ended_at) if t]
        self.started_at = min(starts) if starts else None
        self.ended_at = max(ends) if ends else None
        return self
