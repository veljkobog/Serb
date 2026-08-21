"""Composite scoring: turn signal blocks into a side, a conviction, and a 0-100 score."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config
from .signals import Block

LONG, SHORT, FLAT = "CALLS", "PUTS", "NONE"


@dataclass
class Candidate:
    symbol: str
    name: Optional[str] = None
    spot: float = 0.0
    change_pct: Optional[float] = None
    market_cap: Optional[float] = None
    expiry: Optional[dt.date] = None
    dte: int = 0
    blocks: Dict[str, Block] = field(default_factory=dict)
    direction: float = 0.0
    confluence: float = 0.0
    quality: float = 0.0
    score: float = 0.0
    side: str = FLAT
    squeeze_bonus: float = 0.0
    gate_failures: List[str] = field(default_factory=list)
    stage: str = "scored"          # pre-gate | option-gate | scored
    fetched_chain: bool = False
    flags: List[str] = field(default_factory=list)
    plan: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        return not self.gate_failures and self.error is None

    def reasons(self, limit: int = 6) -> List[str]:
        """The strongest human-readable drivers behind this score."""
        scored = []
        for block in self.blocks.values():
            if not block.available:
                continue
            for sig in block.signals:
                if not sig.note or sig.note == "n/a":
                    continue
                weight = abs(sig.score) if sig.kind == "directional" else sig.score
                scored.append((weight, f"{sig.note}"))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [note for _, note in scored[:limit]]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "name": self.name, "spot": self.spot,
            "change_pct": self.change_pct, "market_cap": self.market_cap,
            "expiry": str(self.expiry) if self.expiry else None, "dte": self.dte,
            "score": round(self.score, 2), "side": self.side,
            "direction": round(self.direction, 4), "confluence": round(self.confluence, 4),
            "quality": round(self.quality, 4), "squeeze_bonus": round(self.squeeze_bonus, 2),
            "gate_failures": self.gate_failures, "stage": self.stage,
            "flags": self.flags,
            "reasons": self.reasons(), "plan": self.plan, "error": self.error,
            "blocks": {k: v.as_dict() for k, v in self.blocks.items()},
        }


def _weighted(blocks: Dict[str, Block], weights: Dict[str, float], attr: str):
    """Weighted mean over available blocks, renormalised so a missing block does not
    silently drag the composite toward zero."""
    num = den = 0.0
    used: Dict[str, float] = {}
    for name, weight in weights.items():
        block = blocks.get(name)
        if not block or not block.available or weight <= 0:
            continue
        num += getattr(block, attr) * weight
        den += weight
        used[name] = weight
    return (num / den if den else 0.0), used


def compose(candidate: Candidate, config: Config) -> Candidate:
    weights = config.weights
    direction, used = _weighted(candidate.blocks, weights.direction, "direction")
    quality, _ = _weighted(candidate.blocks, weights.quality, "quality")

    # Confluence: how much of the directional weight actually agrees on the side.
    agree = total = 0.0
    if direction != 0:
        want = 1 if direction > 0 else -1
        for name, weight in used.items():
            block_dir = candidate.blocks[name].direction
            total += weight
            if block_dir == 0:
                agree += weight * 0.5
            elif (1 if block_dir > 0 else -1) == want:
                agree += weight
    confluence = (agree / total) if total else 0.0

    # Short interest is squeeze fuel: it amplifies longs, it does not pick the side.
    squeeze = candidate.blocks.get("short_interest")
    bonus = 0.0
    if squeeze and squeeze.available and direction > 0.05:
        bonus = weights.squeeze_bonus * squeeze.quality
        if squeeze.quality > 0.5:
            candidate.flags.append("squeeze fuel")

    conviction = abs(direction) * (weights.confluence_floor + (1 - weights.confluence_floor) * confluence)
    raw = 100.0 * conviction * (0.35 + 0.65 * quality)

    candidate.direction = direction
    candidate.confluence = confluence
    candidate.quality = quality
    candidate.squeeze_bonus = bonus
    candidate.score = max(0.0, min(100.0, raw + bonus))
    if direction > 0.08:
        candidate.side = LONG
    elif direction < -0.08:
        candidate.side = SHORT
    else:
        candidate.side = FLAT
        candidate.score = min(candidate.score, 25.0)
    return candidate
