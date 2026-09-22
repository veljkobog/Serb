"""Intraday tape metrics: VWAP, opening range, persistence, relative strength."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from .models import Bar, SymbolSnapshot


@dataclass
class PriceMetrics:
    last: float
    open_px: float
    prev_close: Optional[float]
    gap_pct: Optional[float]          # open vs prior close
    ret_open_pct: float               # last vs session open
    ret_prev_close_pct: Optional[float]
    vwap: float
    vwap_z: float                     # (last - vwap) / vwap sigma
    vwap_slope_bps: float             # VWAP drift over the last 3 bars, bps
    or_high: float
    or_low: float
    or_pos: float                     # -1 at OR low, +1 at OR high, beyond = breakout
    clv: float                        # close location in the session range, -1..1
    persistence: float                # share of bars closing above VWAP, 0..1
    session_range_pct: float
    move_vs_adr: Optional[float]      # |move| as a fraction of 14d avg daily range
    share_volume: float
    beta: float = 1.0
    rs_resid_pct: float = 0.0         # return minus beta * benchmark return


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def vwap_and_sigma(bars: Sequence[Bar]) -> tuple[float, float]:
    """Volume-weighted average price and the volume-weighted dispersion of
    typical price around it. Falls back to unweighted stats on zero volume."""
    tot_v = sum(b.volume for b in bars)
    if tot_v <= 0:
        closes = [b.close for b in bars] or [0.0]
        mean = sum(closes) / len(closes)
        var = sum((c - mean) ** 2 for c in closes) / len(closes)
        return mean, math.sqrt(var)
    vwap = sum(b.typical * b.volume for b in bars) / tot_v
    var = sum(((b.typical - vwap) ** 2) * b.volume for b in bars) / tot_v
    return vwap, math.sqrt(var)


def vwap_series(bars: Sequence[Bar]) -> List[float]:
    """Running (anchored-at-open) VWAP, one value per bar."""
    out: List[float] = []
    pv = v = 0.0
    for b in bars:
        pv += b.typical * b.volume
        v += b.volume
        out.append(pv / v if v > 0 else b.close)
    return out


def opening_range(bars: Sequence[Bar], minutes: int) -> tuple[float, float]:
    if not bars:
        return 0.0, 0.0
    cutoff = bars[0].ts + timedelta(minutes=minutes)
    window = [b for b in bars if b.ts < cutoff] or [bars[0]]
    return max(b.high for b in window), min(b.low for b in window)


def beta_from_bars(sym: Sequence[Bar], bench: Sequence[Bar], default: float = 1.0) -> float:
    """OLS beta of bar-to-bar returns against the benchmark.

    Intraday samples are noisy, so the result is clamped to a sane band and
    falls back to `default` when there aren't enough paired bars.
    """
    n = min(len(sym), len(bench))
    if n < 8:
        return default
    sr, br = [], []
    for i in range(1, n):
        if sym[i - 1].close and bench[i - 1].close:
            sr.append(sym[i].close / sym[i - 1].close - 1.0)
            br.append(bench[i].close / bench[i - 1].close - 1.0)
    if len(br) < 6:
        return default
    mb = sum(br) / len(br)
    ms = sum(sr) / len(sr)
    var = sum((x - mb) ** 2 for x in br)
    if var <= 0:
        return default
    cov = sum((br[i] - mb) * (sr[i] - ms) for i in range(len(br)))
    return max(0.2, min(4.0, cov / var))


def compute_price_metrics(
    snap: SymbolSnapshot,
    opening_range_minutes: int,
    bench_bars: Optional[Sequence[Bar]] = None,
) -> Optional[PriceMetrics]:
    bars = snap.bars
    if not bars:
        return None

    last = snap.spot or bars[-1].close
    open_px = bars[0].open or bars[0].close
    vwap, sigma = vwap_and_sigma(bars)
    running = vwap_series(bars)
    slope_window = running[-4:]
    slope_bps = 0.0
    if len(slope_window) >= 2 and slope_window[0]:
        slope_bps = (slope_window[-1] / slope_window[0] - 1.0) * 10_000

    or_high, or_low = opening_range(bars, opening_range_minutes)
    or_width = or_high - or_low
    or_mid = (or_high + or_low) / 2.0
    or_pos = _safe_div(last - or_mid, or_width / 2.0) if or_width > 0 else 0.0

    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    clv = _safe_div((last - lo) - (hi - last), hi - lo) if hi > lo else 0.0

    above = sum(1 for i, b in enumerate(bars) if b.close > running[i])
    persistence = above / len(bars)

    ret_open = _safe_div(last - open_px, open_px) * 100.0
    gap = None
    ret_prev = None
    if snap.prev_close:
        gap = _safe_div(open_px - snap.prev_close, snap.prev_close) * 100.0
        ret_prev = _safe_div(last - snap.prev_close, snap.prev_close) * 100.0

    range_pct = _safe_div(hi - lo, open_px) * 100.0
    move_vs_adr = None
    if snap.adr_pct and snap.adr_pct > 0:
        move_vs_adr = abs(ret_prev if ret_prev is not None else ret_open) / snap.adr_pct

    beta = 1.0
    rs = 0.0
    if bench_bars and not snap.is_benchmark:
        beta = beta_from_bars(bars, bench_bars)
        b_open = bench_bars[0].open or bench_bars[0].close
        b_ret = _safe_div(bench_bars[-1].close - b_open, b_open) * 100.0
        rs = ret_open - beta * b_ret

    return PriceMetrics(
        last=last,
        open_px=open_px,
        prev_close=snap.prev_close,
        gap_pct=gap,
        ret_open_pct=ret_open,
        ret_prev_close_pct=ret_prev,
        vwap=vwap,
        vwap_z=_safe_div(last - vwap, sigma) if sigma > 0 else 0.0,
        vwap_slope_bps=slope_bps,
        or_high=or_high,
        or_low=or_low,
        or_pos=or_pos,
        clv=clv,
        persistence=persistence,
        session_range_pct=range_pct,
        move_vs_adr=move_vs_adr,
        share_volume=sum(b.volume for b in bars),
        beta=beta,
        rs_resid_pct=rs,
    )
