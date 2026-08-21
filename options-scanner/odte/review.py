"""Forward-test the journal: did the scanner's picks actually do anything?

Every scan appends its candidates to ``out/journal.jsonl`` *before* the outcome is
known. This replays those entries against what the underlying subsequently did and
reports hit rates, returns, and — the part that matters — whether each signal family
earned its weight.

Two honest limits, stated up front because they bound every number below:

  * Daily bars cannot say whether the stop or the target was touched first. Sessions
    that touched both are counted separately as ``ambiguous`` rather than being
    silently scored as wins.
  * Two returns are reported, and the difference between them is the point:

      ``expiry``   intrinsic value at the close. The pessimistic bound — an ATM 0DTE
                   long expires worthless unless the stock *closes* through the strike,
                   even if it traded well past it intraday.
      ``managed``  what the plan would have produced if you actually traded it: exit at
                   T1 when T1 was reached, take the premium stop when the stop was hit,
                   otherwise hold to expiry. Exits are priced by delta, so this is an
                   estimate, not a fill.

    Sessions that touched both the stop and the target are excluded from ``managed``
    rather than being resolved by assumption in the flattering direction.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .calendar_utils import parse_date
from .providers.base import Bar, MarketDataProvider

BUCKETS: Tuple[Tuple[float, float, str], ...] = (
    (0, 50, "<50"), (50, 60, "50-60"), (60, 70, "60-70"), (70, 200, "70+"),
)
BLOCKS = ("trend", "volume", "darkpool", "options", "short_interest")


@dataclass
class Outcome:
    symbol: str
    scanned_at: str
    side: str
    score: float
    spot: float
    expiry: str
    dte: int
    close_at_expiry: Optional[float] = None
    mfe_pct: Optional[float] = None       # best excursion in the trade's favour
    mae_pct: Optional[float] = None       # worst excursion against it
    hit_t1: Optional[bool] = None
    hit_t2: Optional[bool] = None
    hit_stop: Optional[bool] = None
    ambiguous: bool = False               # both stop and target touched the same session
    rungs: List[Dict[str, Any]] = field(default_factory=list)
    blocks: Dict[str, float] = field(default_factory=dict)
    exit_reason: Optional[str] = None     # target | stop | expiry | ambiguous
    skipped: Optional[str] = None

    def _rung(self, name: str = "core") -> Optional[Dict[str, Any]]:
        for rung in self.rungs:
            if rung["rung"] == name:
                return rung
        return self.rungs[0] if self.rungs else None

    @property
    def core_return(self) -> Optional[float]:
        rung = self._rung()
        return rung["return_pct"] if rung else None

    @property
    def managed_return(self) -> Optional[float]:
        rung = self._rung()
        return rung.get("managed_return_pct") if rung else None

    def as_dict(self) -> Dict[str, Any]:
        out = dict(self.__dict__)
        out["core_return"] = self.core_return
        return out


def load_journal(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no journal at {path} — run a scan first")
    entries: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue      # a partial final line from an interrupted scan
    return entries


def dedupe(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One entry per symbol per expiry — the first time the scanner flagged it.

    Re-scanning the same morning would otherwise let a name that kept ranking count
    three or four times and quietly dominate the statistics.
    """
    seen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in sorted(entries, key=lambda e: e.get("ts", "")):
        key = (entry.get("symbol", ""), str(entry.get("expiry", "")))
        seen.setdefault(key, entry)
    return list(seen.values())


def _levels_touched(bar: Bar, sign: int, stop: Optional[float], t1: Optional[float],
                    t2: Optional[float]) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
    hit = lambda level: None if level is None else (  # noqa: E731
        bar.high >= level if sign > 0 else bar.low <= level)
    stopped = None if stop is None else (bar.low <= stop if sign > 0 else bar.high >= stop)
    return stopped, hit(t1), hit(t2)


def _managed_return(rung: Dict[str, Any], plan: Dict[str, Any], spot: float, sign: int,
                    hit_t1: Optional[bool], hit_stop: Optional[bool], ambiguous: bool,
                    close: float, stop_pct: float) -> Tuple[Optional[float], str]:
    """What the plan would have returned if you traded it, rather than held to expiry."""
    premium = rung.get("premium") or 0.0
    if premium <= 0:
        return None, "no premium"
    if ambiguous:
        # Daily bars cannot order the two touches. Refusing to guess is the whole point.
        return None, "ambiguous"
    if hit_t1:
        target = plan.get("target_1")
        if target is None:
            return None, "no target"
        delta = rung.get("delta")
        if delta is None:
            # Without greeks, approximate the exit at intrinsic-plus-half-the-time-value.
            intrinsic = max(0.0, (target - rung["strike"]) * sign)
            value = intrinsic + 0.5 * premium
        else:
            move = (target - spot)
            value = premium + abs(delta) * move * sign + \
                0.5 * (rung.get("gamma") or 0.0) * move * move
        return round((max(0.0, value) / premium - 1.0) * 100.0, 1), "target"
    if hit_stop:
        return round(-stop_pct * 100.0, 1), "stop"
    intrinsic = max(0.0, (close - rung["strike"]) * sign)
    return round((intrinsic / premium - 1.0) * 100.0, 1), "expiry"


def evaluate_entry(entry: Dict[str, Any], bars_by_date: Dict[dt.date, Bar]) -> Outcome:
    plan = entry.get("plan") or {}
    side = entry.get("side", "")
    sign = 1 if side == "CALLS" else -1 if side == "PUTS" else 0
    out = Outcome(symbol=entry.get("symbol", "?"), scanned_at=entry.get("ts", ""),
                  side=side, score=float(entry.get("score") or 0.0),
                  spot=float(entry.get("spot") or 0.0),
                  expiry=str(entry.get("expiry") or ""), dte=int(entry.get("dte") or 0),
                  blocks={k: (v or {}).get("direction", 0.0)
                          for k, v in (entry.get("blocks") or {}).items()})
    if not sign:
        out.skipped = "no directional side"
        return out

    expiry = parse_date(out.expiry)
    bar = bars_by_date.get(expiry) if expiry else None
    if bar is None:
        out.skipped = f"no bar for expiry {out.expiry}"
        return out

    out.close_at_expiry = bar.close
    if out.spot:
        best = (bar.high - out.spot) if sign > 0 else (out.spot - bar.low)
        worst = (bar.low - out.spot) if sign > 0 else (out.spot - bar.high)
        out.mfe_pct = round(best / out.spot * 100.0, 2)
        out.mae_pct = round(worst / out.spot * 100.0, 2)

    out.hit_stop, out.hit_t1, out.hit_t2 = _levels_touched(
        bar, sign, plan.get("underlying_stop"), plan.get("target_1"), plan.get("target_2"))
    out.ambiguous = bool(out.hit_stop and out.hit_t1)

    stop_pct = float(plan.get("premium_stop_pct") or 0.35)
    for raw in plan.get("ladder") or []:
        premium = raw.get("mid") or 0.0
        strike = raw.get("strike")
        if not premium or strike is None:
            continue
        intrinsic = max(0.0, (bar.close - strike) * sign)
        rung = {
            "rung": raw.get("rung"), "strike": strike, "premium": premium,
            "delta": raw.get("delta"), "gamma": raw.get("gamma"),
            "value_at_expiry": round(intrinsic, 2),
            "pnl_per_contract": round((intrinsic - premium) * 100.0, 2),
            "return_pct": round((intrinsic / premium - 1.0) * 100.0, 1),
            "expired_worthless": intrinsic <= 0.0,
        }
        managed, reason = _managed_return(rung, plan, out.spot, sign, out.hit_t1,
                                          out.hit_stop, out.ambiguous, bar.close, stop_pct)
        rung["managed_return_pct"] = managed
        rung["exit_reason"] = reason
        if raw.get("rung") == "core" or out.exit_reason is None:
            out.exit_reason = reason
        out.rungs.append(rung)
    return out


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "win_rate": None,
                "best": None, "worst": None}
    ordered = sorted(values)
    mid = len(ordered) // 2
    median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 1),
        "median": round(median, 1),
        "win_rate": round(100.0 * sum(1 for v in values if v > 0) / len(values), 1),
        "best": round(max(values), 1),
        "worst": round(min(values), 1),
    }


def summarise(outcomes: List[Outcome]) -> Dict[str, Any]:
    scored = [o for o in outcomes if o.skipped is None and o.core_return is not None]
    managed = [o for o in scored if o.managed_return is not None]

    by_bucket: Dict[str, Any] = {}
    for low, high, label in BUCKETS:
        vals = [o.managed_return for o in managed if low <= o.score < high]
        if vals:
            by_bucket[label] = _stats(vals)

    by_side = {side: _stats([o.managed_return for o in managed if o.side == side])
               for side in ("CALLS", "PUTS")
               if any(o.side == side for o in managed)}

    by_rung: Dict[str, Any] = {}
    for name in ("anchor", "core", "runner"):
        expiry_vals = [r["return_pct"] for o in scored for r in o.rungs if r["rung"] == name]
        managed_vals = [r["managed_return_pct"] for o in scored for r in o.rungs
                        if r["rung"] == name and r.get("managed_return_pct") is not None]
        if expiry_vals:
            by_rung[name] = {"expiry": _stats(expiry_vals), "managed": _stats(managed_vals)}
            worthless = sum(1 for o in scored for r in o.rungs
                            if r["rung"] == name and r["expired_worthless"])
            by_rung[name]["expired_worthless_pct"] = round(100.0 * worthless / len(expiry_vals), 1)

    # Does each block earn its weight? Compare outcomes when a block agreed with the
    # traded side against when it disagreed. A block whose "agree" and "disagree"
    # columns look the same is contributing noise, not signal.
    by_block: Dict[str, Any] = {}
    for block in BLOCKS:
        agree, disagree = [], []
        for o in managed:
            direction = o.blocks.get(block)
            if direction is None or abs(direction) < 0.05:
                continue
            want = 1 if o.side == "CALLS" else -1
            (agree if (direction > 0) == (want > 0) else disagree).append(o.managed_return)
        if agree or disagree:
            by_block[block] = {"agreed": _stats(agree), "disagreed": _stats(disagree),
                               "edge": (round(_stats(agree)["mean"] - _stats(disagree)["mean"], 1)
                                        if agree and disagree else None)}

    touched = [o for o in scored if o.hit_t1 is not None]
    return {
        "evaluated": len(scored),
        "managed_evaluated": len(managed),
        "skipped": len(outcomes) - len(scored),
        "skip_reasons": dict(_count(o.skipped for o in outcomes if o.skipped)),
        "exit_reasons": dict(_count(o.exit_reason for o in scored if o.exit_reason)),
        "overall": _stats([o.managed_return for o in managed]),
        "overall_expiry": _stats([o.core_return for o in scored]),
        "levels": {
            "reached_t1_pct": round(100.0 * sum(1 for o in touched if o.hit_t1) / len(touched), 1)
            if touched else None,
            "reached_t2_pct": round(100.0 * sum(1 for o in touched if o.hit_t2) / len(touched), 1)
            if touched else None,
            "hit_stop_pct": round(100.0 * sum(1 for o in touched if o.hit_stop) / len(touched), 1)
            if touched else None,
            "ambiguous_pct": round(100.0 * sum(1 for o in touched if o.ambiguous) / len(touched), 1)
            if touched else None,
        },
        "mfe": _stats([o.mfe_pct for o in scored if o.mfe_pct is not None]),
        "mae": _stats([o.mae_pct for o in scored if o.mae_pct is not None]),
        "by_score_bucket": by_bucket,
        "by_side": by_side,
        "by_rung": by_rung,
        "by_block": by_block,
    }


def _count(items: Iterable[str]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for item in items:
        counts[item] += 1
    return counts


def review(journal_path: str, provider: MarketDataProvider, lookback: int = 260,
           since: Optional[dt.date] = None) -> Tuple[List[Outcome], Dict[str, Any]]:
    entries = dedupe(load_journal(journal_path))
    if since:
        entries = [e for e in entries
                   if (parse_date(str(e.get("expiry"))) or dt.date.min) >= since]

    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        by_symbol[entry.get("symbol", "?")].append(entry)

    outcomes: List[Outcome] = []
    for symbol, group in by_symbol.items():
        try:
            bars = {b.date: b for b in provider.daily_bars(symbol, lookback)}
        except Exception as exc:
            for entry in group:
                out = evaluate_entry(entry, {})
                out.skipped = f"price fetch failed: {type(exc).__name__}"
                outcomes.append(out)
            continue
        outcomes.extend(evaluate_entry(entry, bars) for entry in group)

    outcomes.sort(key=lambda o: (o.expiry, o.symbol))
    return outcomes, summarise(outcomes)


# --------------------------------------------------------------- rendering --
def _row(label: str, stats: Dict[str, Optional[float]], width: int = 16) -> str:
    if not stats or not stats.get("n"):
        return f"  {label:<{width}} —"
    return (f"  {label:<{width}} n={stats['n']:<4} win {str(stats['win_rate']) + '%':<7} "
            f"mean {stats['mean']:>7.1f}%  median {stats['median']:>7.1f}%  "
            f"best {stats['best']:>7.1f}%  worst {stats['worst']:>7.1f}%")


def render_terminal(outcomes: List[Outcome], summary: Dict[str, Any], detail: bool = False) -> str:
    lines = ["", "Journal review", "=" * 78, ""]
    lines.append(f"  {summary['evaluated']} entries evaluated, {summary['skipped']} skipped")
    for reason, count in summary.get("skip_reasons", {}).items():
        lines.append(f"    - {count} x {reason}")
    for reason, count in summary.get("exit_reasons", {}).items():
        lines.append(f"    exit {reason}: {count}")
    lines.append("")
    lines.append("  Core-rung return, managed (exit at T1, or the premium stop)")
    lines.append(_row("managed", summary["overall"]))
    lines.append("  ...and if you had instead held every one to expiry")
    lines.append(_row("expiry", summary["overall_expiry"]))
    lines.append("")

    if summary["by_score_bucket"]:
        lines.append("  By score bucket (managed) — does a higher score actually pay more?")
        for label, stats in summary["by_score_bucket"].items():
            lines.append(_row(label, stats))
        lines.append("")

    if summary["by_side"]:
        lines.append("  By side (managed)")
        for label, stats in summary["by_side"].items():
            lines.append(_row(label, stats))
        lines.append("")

    if summary["by_rung"]:
        lines.append("  By ladder rung")
        for label, stats in summary["by_rung"].items():
            lines.append(_row(f"{label} managed", stats["managed"]))
            lines.append(_row(f"{label} expiry", stats["expiry"])
                         + f"  worthless {stats.get('expired_worthless_pct')}%")
        lines.append("")

    lev = summary["levels"]
    if lev.get("reached_t1_pct") is not None:
        lines.append("  Underlying levels reached on the expiry session")
        lines.append(f"    T1 {lev['reached_t1_pct']}%   T2 {lev['reached_t2_pct']}%   "
                     f"stop {lev['hit_stop_pct']}%   both touched {lev['ambiguous_pct']}%")
        lines.append(f"    MFE mean {summary['mfe']['mean']}%   "
                     f"MAE mean {summary['mae']['mean']}%")
        lines.append("")

    if summary["by_block"]:
        lines.append("  Did each signal block earn its weight?")
        lines.append(f"    {'block':<16}{'agreed':>26}{'disagreed':>26}{'edge':>10}")
        for block, stats in summary["by_block"].items():
            a, d = stats["agreed"], stats["disagreed"]
            fmt = lambda x: (f"n={x['n']} mean {x['mean']:+.1f}%"  # noqa: E731
                             if x.get("n") else "—")
            edge = f"{stats['edge']:+.1f}%" if stats["edge"] is not None else "—"
            lines.append(f"    {block:<16}{fmt(a):>26}{fmt(d):>26}{edge:>10}")
        lines.append("")
        lines.append("    'edge' is mean return when the block agreed with the traded side")
        lines.append("    minus when it disagreed. Near zero means that block is adding")
        lines.append("    noise, not signal — reweight it.")
        lines.append("")

    if detail:
        lines.append("  Entries")
        for o in outcomes:
            if o.skipped:
                lines.append(f"    {o.expiry} {o.symbol:<6} skipped: {o.skipped}")
                continue
            managed = (f"{o.managed_return:+.1f}%" if o.managed_return is not None else "n/a")
            lines.append(f"    {o.expiry} {o.symbol:<6} {o.side:<5} score {o.score:>5.1f}  "
                         f"managed {managed:>8} ({o.exit_reason or '-':<9}) "
                         f"expiry {str(o.core_return) + '%':>9}  "
                         f"MFE {o.mfe_pct:>6.2f}%  MAE {o.mae_pct:>6.2f}%")
        lines.append("")

    lines.append("  Caveats: 'managed' prices the T1 exit by delta — an estimate, not a fill.")
    lines.append("  'expiry' is intrinsic at the close, the pessimistic bound. Daily bars cannot")
    lines.append("  order a stop against a target, so sessions touching both are excluded from")
    lines.append("  the managed numbers rather than resolved in the flattering direction.")
    lines.append("")
    return "\n".join(lines)
