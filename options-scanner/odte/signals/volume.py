"""Relative volume, dollar liquidity, and accumulation/distribution."""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..calendar_utils import session_progress
from ..indicators import clamp, cmf, linreg_slope, obv, ramp, squash
from ..providers.base import Bar, Quote
from . import DIRECTIONAL, QUALITY, Block, Signal, blend

# Empirical cumulative share of a US equity session's volume by elapsed session
# fraction. Front-loaded open, dead midday, closing-auction spike. Used to project a
# full-day RVOL from partial-session volume instead of comparing 10am to a full day.
VOLUME_CURVE: Tuple[Tuple[float, float], ...] = (
    (0.00, 0.000), (0.05, 0.075), (0.10, 0.130), (0.15, 0.176), (0.20, 0.218),
    (0.30, 0.293), (0.40, 0.362), (0.50, 0.432), (0.60, 0.508), (0.70, 0.596),
    (0.80, 0.700), (0.90, 0.822), (0.96, 0.910), (1.00, 1.000),
)


def expected_volume_fraction(progress: float) -> float:
    """Interpolate the share of the day's volume expected by ``progress`` (0..1)."""
    progress = clamp(progress, 0.0, 1.0)
    prev_p, prev_v = VOLUME_CURVE[0]
    for p, v in VOLUME_CURVE[1:]:
        if progress <= p:
            span = p - prev_p
            return prev_v if span == 0 else prev_v + (v - prev_v) * (progress - prev_p) / span
        prev_p, prev_v = p, v
    return 1.0


def analyse(bars: List[Bar], quote: Optional[Quote] = None, progress: Optional[float] = None) -> Block:
    block = Block(name="volume")
    if len(bars) < 25:
        block.available = False
        block.notes.append("not enough history for volume baselines")
        return block

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume for b in bars]

    # The final bar may be today's partial session; baseline off completed days only.
    hist = vols[-21:-1] if len(vols) >= 21 else vols[:-1]
    avg20 = sum(hist) / len(hist) if hist else 0.0
    avg5 = sum(vols[-6:-1]) / 5.0 if len(vols) >= 6 else avg20
    last_close = quote.last if quote and quote.last else closes[-1]

    prog = session_progress() if progress is None else clamp(progress, 0.0, 1.0)
    today_vol = (quote.day_volume if quote and quote.day_volume else vols[-1]) or 0.0
    frac = max(expected_volume_fraction(prog), 0.04)
    projected = today_vol / frac if prog < 0.999 else today_vol

    rvol = (projected / avg20) if avg20 > 0 else None
    rvol_score = ramp(rvol, 1.0, 3.0)
    block.add(Signal("rvol", "RVOL (projected)", rvol, rvol_score, QUALITY,
                     f"{rvol:.2f}x 20d avg" if rvol else "n/a",
                     {"today_volume": today_vol, "projected": projected,
                      "avg20": avg20, "session_progress": round(prog, 3)}))

    dollar_vol = avg20 * last_close
    block.add(Signal("dollar_volume", "20d avg $ volume", dollar_vol,
                     ramp(dollar_vol, 25e6, 500e6), QUALITY,
                     f"${dollar_vol/1e6:.0f}M/day"))

    vol_trend = (avg5 / avg20) if avg20 > 0 else None
    block.add(Signal("volume_trend", "5d vs 20d volume", vol_trend,
                     ramp(vol_trend, 0.9, 1.8), QUALITY,
                     f"{vol_trend:.2f}x" if vol_trend else "n/a"))

    money = cmf(highs, lows, closes, vols, 20)
    block.add(Signal("cmf", "Chaikin money flow (20)", money, clamp((money or 0.0) * 4.0), DIRECTIONAL,
                     f"CMF {money:+.2f}" if money is not None else "n/a"))

    obv_series = obv(closes, vols)
    obv_slope = linreg_slope(obv_series[-20:]) if len(obv_series) >= 20 else None
    obv_norm = (obv_slope / avg20) if (obv_slope is not None and avg20 > 0) else None
    block.add(Signal("obv_slope", "OBV slope (ADV/day)", obv_norm, squash(obv_norm, 0.35), DIRECTIONAL,
                     f"{obv_norm:+.2f} ADV/day" if obv_norm is not None else "n/a"))

    # A big-volume day that closes on its lows is distribution, not demand.
    close_loc = None
    if quote and quote.day_high and quote.day_low and quote.day_high > quote.day_low:
        close_loc = (last_close - quote.day_low) / (quote.day_high - quote.day_low)
    elif highs[-1] > lows[-1]:
        close_loc = (closes[-1] - lows[-1]) / (highs[-1] - lows[-1])
    block.add(Signal("close_location", "Close location in range", close_loc,
                     clamp(((close_loc or 0.5) - 0.5) * 2.0), DIRECTIONAL,
                     f"{close_loc*100:.0f}% of day range" if close_loc is not None else "n/a"))

    block.direction = blend([
        (block.get("cmf").score, 2.0),
        (block.get("obv_slope").score, 2.0),
        (block.get("close_location").score, 1.5),
    ])
    block.quality = blend([(rvol_score, 3.0),
                           (block.get("dollar_volume").score, 2.0),
                           (block.get("volume_trend").score, 1.0)])
    block.detail = {"avg20_volume": avg20, "avg5_volume": avg5, "dollar_volume": dollar_vol,
                    "rvol": rvol, "session_progress": prog}
    return block
