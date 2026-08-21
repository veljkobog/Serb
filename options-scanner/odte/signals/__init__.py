"""Signal primitives shared by every detector."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DIRECTIONAL = "directional"   # score in -1..+1 (bearish .. bullish)
QUALITY = "quality"           # score in 0..1 (how tradable / how much conviction)


@dataclass
class Signal:
    key: str
    label: str
    value: Optional[float]
    score: float
    kind: str = DIRECTIONAL
    note: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "value": self.value,
                "score": round(self.score, 4), "kind": self.kind, "note": self.note,
                "detail": self.detail}


@dataclass
class Block:
    """One family of signals (trend, volume, dark pool, ...)."""
    name: str
    direction: float = 0.0    # -1..+1
    quality: float = 0.0      # 0..1
    available: bool = True
    signals: List[Signal] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def add(self, sig: Signal) -> Signal:
        self.signals.append(sig)
        return sig

    def get(self, key: str) -> Optional[Signal]:
        for s in self.signals:
            if s.key == key:
                return s
        return None

    def value(self, key: str) -> Optional[float]:
        s = self.get(key)
        return s.value if s else None

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "direction": round(self.direction, 4),
                "quality": round(self.quality, 4), "available": self.available,
                "notes": self.notes, "detail": self.detail,
                "signals": [s.as_dict() for s in self.signals]}


def blend(pairs) -> float:
    """Weighted mean of (score, weight) pairs, ignoring zero-weight entries."""
    num = den = 0.0
    for score, weight in pairs:
        if weight <= 0:
            continue
        num += score * weight
        den += weight
    return 0.0 if den == 0 else num / den
