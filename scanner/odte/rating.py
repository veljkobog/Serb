"""BULL / BEAR out of ten.

The composite bias says which way and how hard; confidence says how much the
inputs can be trusted; the setups say whether a known structure is behind it.
The rating folds all three into one number a person can act on without reading
a table:

    score = (bias points + aligned setups - opposing setups - blockers)
            x confidence haircut

Bias alone tops out at 7/10 — the last three points have to be earned by
structure (short gamma, a wall, a magnet, confirmed flow). A blocker such as a
pin or an exhausted move drags the whole thing down regardless of how clean
the tape looks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .setups import Hit

MAX_SETUP_BONUS = 3.0
OPPOSING_WEIGHT = 0.7

BANDS = [
    (8.5, "MAX CONVICTION"),
    (6.5, "STRONG"),
    (4.5, "MODERATE"),
    (3.0, "LEAN"),
    (0.0, "STAND DOWN"),
]


@dataclass
class Rating:
    side: str            # BULL or BEAR
    score: float         # 0..10, one decimal
    stars: int           # score rounded, for the headline
    band: str
    headline: str        # "BULL 8/10 - STRONG"
    drivers: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    setup_bonus: float = 0.0
    blocker_penalty: float = 0.0

    @property
    def actionable(self) -> bool:
        return self.score >= 3.0

    @property
    def signed_score(self) -> float:
        """Score signed by side, so runs can be compared on one axis."""
        return self.score if self.side == "BULL" else -self.score


def band_for(score: float) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return "STAND DOWN"


def rate(bias: float, confidence: float, hits: Sequence[Hit]) -> Rating:
    side = "BULL" if bias >= 0 else "BEAR"
    direction = 1 if bias >= 0 else -1

    base = abs(bias) / 100.0 * 7.0
    aligned = sum(h.strength for h in hits if h.side == direction)
    opposed = sum(h.strength for h in hits if h.side == -direction)
    blocked = sum(h.strength for h in hits if h.side == 0)

    bonus = min(aligned, MAX_SETUP_BONUS)
    raw = base + bonus - OPPOSING_WEIGHT * opposed - blocked
    # A weak-confidence read never scores like a clean one, but a strong setup
    # on mediocre data still beats nothing: the haircut floors at 0.55.
    score = raw * (0.55 + 0.45 * max(0.0, min(confidence, 100.0)) / 100.0)
    score = max(0.0, min(10.0, score))

    stars = int(round(score))
    band = band_for(score)
    return Rating(
        side=side,
        score=round(score, 1),
        stars=stars,
        band=band,
        headline=f"{side} {stars}/10 - {band}",
        drivers=[h.setup.label for h in hits if h.side == direction],
        blockers=[h.setup.label for h in hits if h.side in (0, -direction)],
        setup_bonus=round(bonus - OPPOSING_WEIGHT * opposed, 2),
        blocker_penalty=round(blocked, 2),
    )
