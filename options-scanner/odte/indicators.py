"""Technical indicators, implemented on plain Python lists (no numpy/pandas).

Every function takes oldest-first sequences and returns either a single float or a
list aligned to the input (with ``None`` where the indicator is not yet defined).
"""
from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence


def _clean(xs: Iterable[Optional[float]]) -> List[float]:
    return [float(x) for x in xs if x is not None]


def sma(values: Sequence[float], n: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    run = sum(values[:n])
    out[n - 1] = run / n
    for i in range(n, len(values)):
        run += values[i] - values[i - n]
        out[i] = run / n
    return out


def ema(values: Sequence[float], n: int) -> List[Optional[float]]:
    """EMA seeded with an SMA of the first ``n`` values (standard charting behaviour)."""
    out: List[Optional[float]] = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(high: Sequence[float], low: Sequence[float], close: Sequence[float]) -> List[Optional[float]]:
    out: List[Optional[float]] = [None]
    for i in range(1, len(close)):
        out.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return out


def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14) -> List[Optional[float]]:
    """Wilder's ATR."""
    tr = true_range(high, low, close)
    out: List[Optional[float]] = [None] * len(close)
    vals = [t for t in tr if t is not None]
    if len(vals) < n:
        return out
    prev = sum(vals[:n]) / n
    out[n] = prev
    for i in range(n + 1, len(close)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def adx(high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = 14):
    """Wilder's ADX. Returns (adx, plus_di, minus_di) as aligned lists."""
    size = len(close)
    blank: List[Optional[float]] = [None] * size
    if size < 2 * n + 2:
        return blank, list(blank), list(blank)

    plus_dm, minus_dm, tr = [0.0], [0.0], [0.0]
    for i in range(1, size):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))

    # Wilder smoothing
    str_ = sum(tr[1:n + 1])
    spdm = sum(plus_dm[1:n + 1])
    smdm = sum(minus_dm[1:n + 1])
    pdi: List[Optional[float]] = [None] * size
    mdi: List[Optional[float]] = [None] * size
    dx: List[Optional[float]] = [None] * size

    def _fill(idx: int) -> None:
        if str_ <= 0:
            return
        p = 100.0 * spdm / str_
        m = 100.0 * smdm / str_
        pdi[idx], mdi[idx] = p, m
        if p + m > 0:
            dx[idx] = 100.0 * abs(p - m) / (p + m)

    _fill(n)
    for i in range(n + 1, size):
        str_ = str_ - str_ / n + tr[i]
        spdm = spdm - spdm / n + plus_dm[i]
        smdm = smdm - smdm / n + minus_dm[i]
        _fill(i)

    out: List[Optional[float]] = [None] * size
    dxv = [(i, v) for i, v in enumerate(dx) if v is not None]
    if len(dxv) >= n:
        start = dxv[n - 1][0]
        prev = sum(v for _, v in dxv[:n]) / n
        out[start] = prev
        for i in range(start + 1, size):
            if dx[i] is None:
                continue
            prev = (prev * (n - 1) + dx[i]) / n
            out[i] = prev
    return out, pdi, mdi


def rsi(values: Sequence[float], n: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(values)):
        ch = values[i] - values[i - 1]
        ag = (ag * (n - 1) + max(ch, 0.0)) / n
        al = (al * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def obv(close: Sequence[float], volume: Sequence[float]) -> List[float]:
    out = [0.0]
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            out.append(out[-1] + volume[i])
        elif close[i] < close[i - 1]:
            out.append(out[-1] - volume[i])
        else:
            out.append(out[-1])
    return out


def cmf(high, low, close, volume, n: int = 20) -> Optional[float]:
    """Chaikin Money Flow over the last ``n`` bars: -1 (distribution) .. +1 (accumulation)."""
    if len(close) < n:
        return None
    mfv = 0.0
    vol = 0.0
    for i in range(len(close) - n, len(close)):
        rng = high[i] - low[i]
        mult = 0.0 if rng <= 0 else ((close[i] - low[i]) - (high[i] - close[i])) / rng
        mfv += mult * volume[i]
        vol += volume[i]
    return None if vol <= 0 else mfv / vol


def linreg_slope(values: Sequence[float]) -> Optional[float]:
    """Least-squares slope per bar."""
    n = len(values)
    if n < 2:
        return None
    xbar = (n - 1) / 2.0
    ybar = sum(values) / n
    num = sum((i - xbar) * (values[i] - ybar) for i in range(n))
    den = sum((i - xbar) ** 2 for i in range(n))
    return None if den == 0 else num / den


def stdev(values: Sequence[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def zscore(value: Optional[float], history: Sequence[float]) -> Optional[float]:
    hist = _clean(history)
    if value is None or len(hist) < 5:
        return None
    m = sum(hist) / len(hist)
    sd = stdev(hist)
    if not sd:
        return None
    return (value - m) / sd


def pct_rank(value: Optional[float], history: Sequence[float]) -> Optional[float]:
    """Fraction of ``history`` at or below ``value`` (0..1)."""
    hist = _clean(history)
    if value is None or not hist:
        return None
    return sum(1 for h in hist if h <= value) / len(hist)


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def squash(x: Optional[float], scale: float) -> float:
    """Map an unbounded value onto -1..1 with tanh; ``scale`` is the value that maps to ~0.76."""
    if x is None or scale == 0:
        return 0.0
    return math.tanh(x / scale)


def ramp(x: Optional[float], lo: float, hi: float) -> float:
    """Linear 0..1 ramp between ``lo`` and ``hi`` (handles hi < lo for inverted ramps)."""
    if x is None:
        return 0.0
    if hi == lo:
        return 0.0
    return clamp((x - lo) / (hi - lo), 0.0, 1.0)
