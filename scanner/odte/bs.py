"""Black-Scholes greeks and implied-vol solving, stdlib math only.

0DTE lives in the corner of the model where time-to-expiry is measured in
minutes, so everything here is written to stay finite as T -> 0.
"""

from __future__ import annotations

import math
from typing import Optional

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
# One minute, in years. Used as the T floor so gamma/vega stay finite into the
# close instead of blowing up or dividing by zero.
MIN_T = 60.0 / SECONDS_PER_YEAR


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t: float, vol: float, r: float, q: float):
    vs = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / vs
    return d1, d1 - vs


def price(
    spot: float,
    strike: float,
    t: float,
    vol: float,
    right: str,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    """European option price. Returns intrinsic when T or vol collapse."""
    right = right.upper()[0]
    if spot <= 0 or strike <= 0:
        return 0.0
    if t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    df, dq = math.exp(-r * t), math.exp(-q * t)
    if right == "C":
        return spot * dq * norm_cdf(d1) - strike * df * norm_cdf(d2)
    return strike * df * norm_cdf(-d2) - spot * dq * norm_cdf(-d1)


def delta(
    spot: float,
    strike: float,
    t: float,
    vol: float,
    right: str,
    r: float = 0.0,
    q: float = 0.0,
) -> float:
    right = right.upper()[0]
    if spot <= 0 or strike <= 0:
        return 0.0
    if t <= 0 or vol <= 0:
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    d1, _ = _d1_d2(spot, strike, t, vol, r, q)
    dq = math.exp(-q * t)
    return dq * norm_cdf(d1) if right == "C" else -dq * norm_cdf(-d1)


def gamma(
    spot: float, strike: float, t: float, vol: float, r: float = 0.0, q: float = 0.0
) -> float:
    """Gamma per $1 move. Same for calls and puts."""
    if spot <= 0 or strike <= 0 or vol <= 0:
        return 0.0
    t = max(t, MIN_T)
    d1, _ = _d1_d2(spot, strike, t, vol, r, q)
    return math.exp(-q * t) * norm_pdf(d1) / (spot * vol * math.sqrt(t))


def vega(
    spot: float, strike: float, t: float, vol: float, r: float = 0.0, q: float = 0.0
) -> float:
    """Vega per 1.00 (100 vol points) change in vol."""
    if spot <= 0 or strike <= 0 or vol <= 0:
        return 0.0
    t = max(t, MIN_T)
    d1, _ = _d1_d2(spot, strike, t, vol, r, q)
    return spot * math.exp(-q * t) * norm_pdf(d1) * math.sqrt(t)


def implied_vol(
    target: float,
    spot: float,
    strike: float,
    t: float,
    right: str,
    r: float = 0.0,
    q: float = 0.0,
    lo: float = 0.01,
    hi: float = 5.0,
    tol: float = 1e-5,
) -> Optional[float]:
    """Solve IV by bisection. None when the price is unsolvable (at/below
    intrinsic, above the no-arb bound, or T already expired).

    Bisection rather than Newton: 0DTE vegas get small enough near the close
    that a derivative step regularly overshoots out of the bracket.
    """
    right = right.upper()[0]
    t = max(t, MIN_T)
    if target is None or target <= 0 or spot <= 0 or strike <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if target <= intrinsic + 1e-9:
        return None
    if price(spot, strike, t, hi, right, r, q) < target:
        return None  # above the highest vol we're willing to quote
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if price(spot, strike, t, mid, right, r, q) > target:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)
