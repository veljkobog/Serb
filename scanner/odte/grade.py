"""`odte grade` — did the reads pay?

The run log already holds every press: side, rating, band, setups, spot and the
expected move at that moment. Grading needs nothing else. For each read, the
score is how far price travelled *in the rated direction* by the last press of
the day, measured in expected moves:

    progress = (final spot - spot at the read) / expected move,  signed by side

A hit is progress > 0. "Full EM" means the read paid a whole expected move,
which for a 0DTE entry is roughly the difference between a scratch and a real
winner. Broken out by rating band and by setup, this is how the priors in
setups.py stop being priors.

One session of this proves nothing. A month of it tells you which bands and
which setups deserve their weights.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .runs import Entry, Run, RunLog


@dataclass
class Result:
    """One read, graded against the end of its session."""

    date: str
    symbol: str
    press: int
    side: str
    score: float
    band: str
    setups: List[str]
    spot: float
    final_spot: float
    progress_em: Optional[float]
    minutes_held: float

    @property
    def hit(self) -> Optional[bool]:
        return None if self.progress_em is None else self.progress_em > 0

    @property
    def full_em(self) -> Optional[bool]:
        return None if self.progress_em is None else self.progress_em >= 1.0


@dataclass
class Bucket:
    label: str
    reads: int = 0
    hits: int = 0
    full: int = 0
    progress: List[float] = field(default_factory=list)

    def add(self, r: Result) -> None:
        if r.progress_em is None:
            return
        self.reads += 1
        self.hits += 1 if r.hit else 0
        self.full += 1 if r.full_em else 0
        self.progress.append(r.progress_em)

    @property
    def hit_rate(self) -> Optional[float]:
        return (self.hits / self.reads) if self.reads else None

    @property
    def full_rate(self) -> Optional[float]:
        return (self.full / self.reads) if self.reads else None

    @property
    def mean_em(self) -> Optional[float]:
        return statistics.fmean(self.progress) if self.progress else None

    @property
    def median_em(self) -> Optional[float]:
        return statistics.median(self.progress) if self.progress else None

    @property
    def worst_em(self) -> Optional[float]:
        return min(self.progress) if self.progress else None

    @property
    def best_em(self) -> Optional[float]:
        return max(self.progress) if self.progress else None


def grade_run_log(log: RunLog, date: str = "") -> List[Result]:
    """Grade every press except the last, which has nothing to settle against."""
    if len(log.runs) < 2:
        return []
    final = log.runs[-1]
    out: List[Result] = []
    for run in log.runs[:-1]:
        held = (final.when - run.when).total_seconds() / 60.0
        for symbol, e in run.entries.items():
            settled = final.entries.get(symbol)
            progress = None
            final_spot = e.spot
            if settled is not None and e.em:
                final_spot = settled.spot
                move = (final_spot - e.spot) / e.em
                progress = round(move if e.side == "BULL" else -move, 3)
            out.append(
                Result(
                    date=date or run.ts[:10],
                    symbol=symbol,
                    press=run.press,
                    side=e.side,
                    score=e.score,
                    band=e.band,
                    setups=list(e.setups),
                    spot=e.spot,
                    final_spot=final_spot,
                    progress_em=progress,
                    minutes_held=round(held, 1),
                )
            )
    return out


def bucket_by(results: Sequence[Result], key) -> Dict[str, Bucket]:
    buckets: Dict[str, Bucket] = {}
    for r in results:
        for label in key(r):
            buckets.setdefault(label, Bucket(label)).add(r)
    return buckets


BAND_ORDER = ["MAX CONVICTION", "STRONG", "MODERATE", "LEAN", "STAND DOWN"]


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _em(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:+.2f}"


def _table(title: str, buckets: Dict[str, Bucket], order: Optional[List[str]] = None) -> List[str]:
    rows = [buckets[k] for k in (order or sorted(buckets)) if k in buckets]
    rows = [b for b in rows if b.reads]
    if not rows:
        return []
    width = max(len(b.label) for b in rows)
    lines = [
        "",
        title,
        f"  {'':<{width}}  {'reads':>5}{'hit':>6}{'1 EM':>6}{'mean':>7}{'med':>7}{'worst':>7}{'best':>7}",
    ]
    for b in rows:
        lines.append(
            f"  {b.label:<{width}}  {b.reads:>5}{_pct(b.hit_rate):>6}{_pct(b.full_rate):>6}"
            f"{_em(b.mean_em):>7}{_em(b.median_em):>7}{_em(b.worst_em):>7}{_em(b.best_em):>7}"
        )
    return lines


def render(results: Sequence[Result], dates: Sequence[str]) -> str:
    graded = [r for r in results if r.progress_em is not None]
    if not graded:
        return (
            "Nothing to grade yet. Grading needs at least two presses in a day — "
            "run `./bullbear live` (or press the button twice) and try again."
        )
    overall = Bucket("all reads")
    for r in graded:
        overall.add(r)

    lines = [
        "",
        f"0DTE READ GRADE  |  {len(dates)} session(s): {', '.join(dates)}",
        "=" * 78,
        f"  {len(graded)} graded reads, held {statistics.fmean(r.minutes_held for r in graded):.0f} min"
        f" on average, settled against each session's last press",
    ]
    lines += _table("BY BAND", bucket_by(graded, lambda r: [r.band]), BAND_ORDER)
    lines += _table("OVERALL", {"all reads": overall})
    lines += _table("BY SETUP", bucket_by(graded, lambda r: r.setups or ["(none)"]))
    lines += _table("BY SYMBOL", bucket_by(graded, lambda r: [r.symbol]))
    lines += [
        "",
        "  hit  = price moved in the rated direction by the last press",
        "  1 EM = it moved a full expected move that way",
        "  mean/med/worst/best = expected moves captured, signed for the side",
        "",
        "  Small samples lie. Weigh a band or setup only once it has 30+ reads,",
        "  and remember every read here is graded against a same-session close,",
        "  not against an entry and exit you actually took.",
    ]
    return "\n".join(lines)


def collect(out_dir: Path, date: Optional[str]) -> tuple[List[Result], List[str]]:
    runs_dir = Path(out_dir) / "runs"
    paths: Iterable[Path]
    if date:
        paths = [runs_dir / f"runs-{date}.json"]
    else:
        paths = sorted(runs_dir.glob("runs-*.json"))
    results: List[Result] = []
    dates: List[str] = []
    for path in paths:
        if not path.exists():
            continue
        day = path.stem.replace("runs-", "")
        log = RunLog(path)
        graded = grade_run_log(log, day)
        if graded:
            dates.append(day)
        results.extend(graded)
    return results, dates


def run_grade(args: argparse.Namespace) -> int:
    results, dates = collect(Path(args.out_dir), args.date)
    if not dates:
        where = args.date or "out/runs/"
        print(
            f"No gradeable sessions in {where}. Grading needs a day with at least "
            f"two presses.",
        )
        return 1
    print(render(results, dates))
    if args.csv:
        import csv as _csv

        path = Path(args.csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            writer = _csv.DictWriter(
                fh,
                fieldnames=[
                    "date", "symbol", "press", "side", "score", "band", "setups",
                    "spot", "final_spot", "progress_em", "hit", "full_em", "minutes_held",
                ],
            )
            writer.writeheader()
            for r in results:
                writer.writerow(
                    {
                        "date": r.date, "symbol": r.symbol, "press": r.press,
                        "side": r.side, "score": r.score, "band": r.band,
                        "setups": "|".join(r.setups), "spot": r.spot,
                        "final_spot": r.final_spot, "progress_em": r.progress_em,
                        "hit": r.hit, "full_em": r.full_em,
                        "minutes_held": r.minutes_held,
                    }
                )
        print(f"\n  per-read detail -> {path}")
    return 0
