"""Run log: what the button showed, and whether the next press confirmed it.

Every press appends a run to out/runs/<date>.json. A later press compares each
symbol against the first read of the day, which is what makes re-confirmation
mean something: same side, better score and price progress in your favour is a
different situation from the same side with the score bleeding out.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

CONFIRMED, HOLDING, FADED, FLIPPED, NEW = (
    "CONFIRMED", "HOLDING", "FADED", "FLIPPED", "NEW"
)


@dataclass
class Entry:
    symbol: str
    side: str
    score: float
    bias: float
    confidence: float
    spot: float
    vwap: float
    em: Optional[float]
    flow_tilt: Optional[float]
    flow_source: str
    verdict: str
    band: str
    setups: List[str] = field(default_factory=list)


@dataclass
class Run:
    ts: str
    press: int          # 1 = the open read, 2+ = re-confirmations
    provider: str
    entries: Dict[str, Entry] = field(default_factory=dict)

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.ts)


@dataclass
class Change:
    symbol: str
    status: str
    d_score: float
    d_bias: float
    progress_em: Optional[float]   # price move since the first read, in EMs, signed for the side
    minutes: float
    baseline_side: str
    note: str


def entry_from_score(score) -> Entry:
    o = score.options
    return Entry(
        symbol=score.symbol,
        side=score.rating.side,
        score=score.rating.score,
        bias=round(score.bias, 1),
        confidence=round(score.confidence, 0),
        spot=round(score.price.last, 4),
        vwap=round(score.price.vwap, 4),
        em=round(o.em_dollars, 4) if o and o.em_dollars else None,
        flow_tilt=round(o.flow_tilt, 3) if o and o.flow_tilt is not None else None,
        flow_source=o.flow_source if o else "none",
        verdict=score.verdict,
        band=score.rating.band,
        setups=[h.key for h in score.setups],
    )


class RunLog:
    """One JSON file per session date."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.runs: List[Run] = []
        if self.path.exists():
            payload = json.loads(self.path.read_text())
            for r in payload.get("runs", []):
                self.runs.append(
                    Run(
                        ts=r["ts"],
                        press=r.get("press", 1),
                        provider=r.get("provider", "?"),
                        entries={k: Entry(**v) for k, v in (r.get("entries") or {}).items()},
                    )
                )

    @classmethod
    def for_date(cls, directory: str | Path, day: datetime) -> "RunLog":
        return cls(Path(directory) / f"runs-{day:%Y-%m-%d}.json")

    @property
    def next_press(self) -> int:
        return len(self.runs) + 1

    @property
    def first(self) -> Optional[Run]:
        return self.runs[0] if self.runs else None

    @property
    def last(self) -> Optional[Run]:
        return self.runs[-1] if self.runs else None

    def append(self, run: Run) -> Run:
        self.runs.append(run)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "date": run.ts[:10],
                    "runs": [
                        {
                            "ts": r.ts,
                            "press": r.press,
                            "provider": r.provider,
                            "entries": {k: asdict(v) for k, v in r.entries.items()},
                        }
                        for r in self.runs
                    ],
                },
                indent=2,
            )
        )
        return run

    def record(self, scores: Sequence, now: datetime, provider: str) -> Run:
        return self.append(
            Run(
                ts=now.isoformat(),
                press=self.next_press,
                provider=provider,
                entries={s.symbol: entry_from_score(s) for s in scores},
            )
        )


def compare(current: Run, baseline: Optional[Run]) -> Dict[str, Change]:
    """Grade each of the current run's symbols against the first read."""
    out: Dict[str, Change] = {}
    if baseline is None or baseline is current:
        return out
    minutes = max(0.0, (current.when - baseline.when).total_seconds() / 60.0)

    for symbol, now_e in current.entries.items():
        was = baseline.entries.get(symbol)
        if was is None:
            out[symbol] = Change(symbol, NEW, 0.0, 0.0, None, minutes, now_e.side,
                                 "not in the first read")
            continue

        d_score = round(now_e.score - was.score, 1)
        d_bias = round(now_e.bias - was.bias, 1)
        progress = None
        if was.em:
            move = (now_e.spot - was.spot) / was.em
            progress = round(move if was.side == "BULL" else -move, 2)

        if now_e.side != was.side:
            status = FLIPPED
            note = f"was {was.side} {was.score:.0f}/10"
        elif d_score <= -1.5 or (progress is not None and progress <= -0.35):
            status = FADED
            note = "score and price both gave back" if d_score <= -1.5 and (
                progress is not None and progress <= -0.35
            ) else ("score bleeding" if d_score <= -1.5 else "price went the other way")
        elif (
            (progress is not None and progress >= 0.15 and d_score >= -0.5)
            or d_score >= 1.0
        ):
            status = CONFIRMED
            note = "price progressed and the read held"
        else:
            status = HOLDING
            note = "unchanged: same side, no follow-through yet"

        # Flow turning against a still-standing read is worth saying out loud.
        if (
            status in (CONFIRMED, HOLDING)
            and now_e.flow_tilt is not None
            and was.flow_tilt is not None
            and now_e.flow_source == "side"
            and (now_e.flow_tilt > 0) != (was.flow_tilt > 0)
        ):
            status = FADED if status == HOLDING else status
            note += "; flow flipped side"

        out[symbol] = Change(symbol, status, d_score, d_bias, progress, minutes,
                             was.side, note)
    return out
