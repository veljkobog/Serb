"""Session VWAP, opening range, and where price sits inside today's session.

This is the block that matters most for a same-day trade and the one a daily-bar-only
scanner cannot produce. Session VWAP is the reference institutional algos are measured
against, so on a 0DTE timeframe it behaves as the line that decides who is in control:
above it, dip-buyers are defending; below it, rallies get sold into. The opening range
is the other level the whole session trades around.

Everything here is computed from today's regular-session bars only — no pre-market, no
carry-over from yesterday — because that is what "session VWAP" means.
"""
from __future__ import annotations

from typing import List, Optional

from ..indicators import clamp, linreg_slope, ramp, squash
from ..providers.base import IntradayBar
from . import DIRECTIONAL, QUALITY, Block, Signal, blend

OPENING_RANGE_MINUTES = 30


def vwap_series(bars: List[IntradayBar]) -> List[float]:
    """Cumulative session VWAP, one value per bar."""
    out: List[float] = []
    pv = vol = 0.0
    for bar in bars:
        pv += bar.typical * bar.volume
        vol += bar.volume
        out.append(pv / vol if vol > 0 else bar.close)
    return out


def opening_range(bars: List[IntradayBar], minutes: int = OPENING_RANGE_MINUTES):
    """(high, low) of the first ``minutes`` of the session, or (None, None)."""
    if not bars:
        return None, None
    start = bars[0].ts
    window = [b for b in bars if (b.ts - start).total_seconds() < minutes * 60]
    if not window:
        return None, None
    return max(b.high for b in window), min(b.low for b in window)


def analyse(bars: List[IntradayBar], spot: Optional[float] = None,
            atr: Optional[float] = None) -> Block:
    block = Block(name="intraday")
    if len(bars) < 3:
        block.available = False
        block.notes.append("no intraday bars from this provider")
        return block

    last = spot if spot else bars[-1].close
    vwaps = vwap_series(bars)
    vwap = vwaps[-1]
    session_high = max(b.high for b in bars)
    session_low = min(b.low for b in bars)
    unit = atr if atr and atr > 0 else max(session_high - session_low, last * 0.002)

    # --- price vs VWAP: the control line -----------------------------------
    vwap_dist = (last - vwap) / unit if unit else 0.0
    block.add(Signal("vwap", "Price vs session VWAP", vwap,
                     squash(vwap_dist, 0.5), DIRECTIONAL,
                     f"{'above' if last >= vwap else 'below'} VWAP {vwap:,.2f} "
                     f"({(last/vwap - 1)*100:+.2f}%)",
                     {"distance_atr": round(vwap_dist, 3)}))

    # A rising VWAP means the average fill is improving all session — real demand,
    # not a single spike that has already faded.
    vwap_slope = linreg_slope(vwaps[-12:]) if len(vwaps) >= 12 else None
    slope_norm = (vwap_slope / unit) if (vwap_slope is not None and unit) else None
    block.add(Signal("vwap_slope", "VWAP slope", slope_norm, squash(slope_norm, 0.05),
                     DIRECTIONAL,
                     f"VWAP {'rising' if (slope_norm or 0) > 0 else 'falling'}"
                     if slope_norm is not None else "n/a"))

    # How much of the session has price spent on the right side of VWAP?
    above = sum(1 for i, b in enumerate(bars) if b.close >= vwaps[i])
    share_above = above / len(bars)
    block.add(Signal("vwap_persistence", "Share of session above VWAP", share_above,
                     clamp((share_above - 0.5) * 2.0), DIRECTIONAL,
                     f"{share_above*100:.0f}% of bars above VWAP"))

    # --- opening range -----------------------------------------------------
    or_high, or_low = opening_range(bars)
    or_state = "inside"
    or_dir = 0.0
    if or_high is not None and or_low is not None:
        if last > or_high:
            or_state, or_dir = "broken up", 1.0
        elif last < or_low:
            or_state, or_dir = "broken down", -1.0
        else:
            span = or_high - or_low
            or_dir = clamp(((last - or_low) / span - 0.5) * 1.2) if span > 0 else 0.0
        block.add(Signal("opening_range", f"{OPENING_RANGE_MINUTES}m opening range", or_dir,
                         or_dir, DIRECTIONAL,
                         f"OR {or_low:,.2f}-{or_high:,.2f}, {or_state}",
                         {"high": or_high, "low": or_low, "state": or_state}))

    # --- position in the session's own range -------------------------------
    span = session_high - session_low
    pos = ((last - session_low) / span) if span > 0 else 0.5
    block.add(Signal("session_position", "Position in session range", pos,
                     clamp((pos - 0.5) * 2.0), DIRECTIONAL,
                     f"{pos*100:.0f}% of today's range",
                     {"high": session_high, "low": session_low}))

    # --- quality: is this an orderly trend or a chop-fest? -----------------
    # Net progress divided by the path walked to get it. High means directional;
    # low means the session is grinding sideways and 0DTE premium is bleeding.
    path = sum(abs(bars[i].close - bars[i - 1].close) for i in range(1, len(bars)))
    efficiency = (abs(bars[-1].close - bars[0].open) / path) if path > 0 else 0.0
    block.add(Signal("efficiency", "Trend efficiency", efficiency,
                     ramp(efficiency, 0.15, 0.55), QUALITY,
                     f"{efficiency*100:.0f}% of the path was net progress"))

    range_atr = (span / unit) if unit else None
    block.add(Signal("range_used", "Session range vs ATR", range_atr,
                     ramp(range_atr, 0.25, 1.0), QUALITY,
                     f"range is {range_atr:.2f} ATR so far" if range_atr else "n/a"))

    block.direction = blend([
        (block.get("vwap").score, 3.0),
        (block.get("vwap_persistence").score, 1.5),
        (block.get("vwap_slope").score, 1.5),
        (block.get("opening_range").score if block.get("opening_range") else 0.0,
         2.0 if block.get("opening_range") else 0.0),
        (block.get("session_position").score, 1.0),
    ])
    block.quality = blend([(block.get("efficiency").score, 2.0),
                           (block.get("range_used").score, 1.5)])
    block.detail = {
        "vwap": vwap, "vwap_slope": slope_norm, "share_above_vwap": share_above,
        "or_high": or_high, "or_low": or_low, "or_state": or_state,
        "session_high": session_high, "session_low": session_low,
        "efficiency": efficiency, "range_atr": range_atr, "bars": len(bars),
    }
    return block
