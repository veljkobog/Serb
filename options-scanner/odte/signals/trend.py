"""Moving-average structure, trend strength, and extension."""
from __future__ import annotations

from typing import List, Optional

from ..indicators import adx, atr, clamp, ema, ramp, rsi, squash
from ..providers.base import Bar, Quote
from . import DIRECTIONAL, QUALITY, Block, Signal, blend


def analyse(bars: List[Bar], quote: Optional[Quote] = None) -> Block:
    block = Block(name="trend")
    if len(bars) < 60:
        block.available = False
        block.notes.append("not enough history for trend structure")
        return block

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    last = quote.last if quote and quote.last else closes[-1]

    e8, e21, e50 = (ema(closes, n)[-1] for n in (8, 21, 50))
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    a = atr(highs, lows, closes, 14)[-1] or 0.0
    atr_pct = (a / last * 100.0) if last else 0.0

    # --- MA stack: four independent alignment checks, each worth 0.25 -------
    checks = [
        (last, e8, "price>8EMA"),
        (e8, e21, "8>21EMA"),
        (e21, e50, "21>50EMA"),
        (e50, e200, "50>200EMA"),
    ]
    stack, live = 0.0, 0
    labels = []
    for lhs, rhs, name in checks:
        if lhs is None or rhs is None:
            continue
        live += 1
        stack += 1.0 if lhs > rhs else -1.0
        labels.append(name if lhs > rhs else name.replace(">", "<"))
    stack_score = stack / live if live else 0.0
    block.add(Signal("ma_stack", "MA stack", stack_score, stack_score, DIRECTIONAL,
                     " ".join(labels), {"ema8": e8, "ema21": e21, "ema50": e50, "ema200": e200}))

    # --- Slope of the 21EMA in ATR/day -------------------------------------
    e21_series = [v for v in ema(closes, 21) if v is not None]
    slope_atr = None
    if len(e21_series) >= 6 and a > 0:
        slope_atr = (e21_series[-1] - e21_series[-6]) / 5.0 / a
    block.add(Signal("ema21_slope", "21EMA slope (ATR/day)", slope_atr,
                     squash(slope_atr, 0.12), DIRECTIONAL,
                     f"{slope_atr:+.2f} ATR/day" if slope_atr is not None else "n/a"))

    # --- ADX: is there a trend at all --------------------------------------
    adx_series, pdi, mdi = adx(highs, lows, closes, 14)
    adx_v, p_v, m_v = adx_series[-1], pdi[-1], mdi[-1]
    adx_q = ramp(adx_v, 15.0, 35.0)
    di_dir = 0.0
    if p_v is not None and m_v is not None and (p_v + m_v) > 0:
        di_dir = clamp((p_v - m_v) / (p_v + m_v))
    block.add(Signal("adx", "ADX(14)", adx_v, adx_q, QUALITY,
                     f"ADX {adx_v:.1f}" if adx_v is not None else "n/a",
                     {"plus_di": p_v, "minus_di": m_v}))
    block.add(Signal("di_bias", "DI bias", di_dir, di_dir, DIRECTIONAL))

    # --- Extension: chasing 3 ATR above the 21EMA is how 0DTE calls die -----
    ext = ((last - e21) / a) if (e21 and a > 0) else None
    ext_penalty = ramp(abs(ext) if ext is not None else None, 1.5, 3.5)
    block.add(Signal("extension", "Extension vs 21EMA (ATR)", ext, 1.0 - ext_penalty, QUALITY,
                     f"{ext:+.2f} ATR from 21EMA" if ext is not None else "n/a"))

    # --- Position in the 20-day range --------------------------------------
    hi20, lo20 = max(highs[-20:]), min(lows[-20:])
    pos = None
    if hi20 > lo20:
        pos = (last - lo20) / (hi20 - lo20)
    range_dir = clamp((pos - 0.5) * 2.0) if pos is not None else 0.0
    block.add(Signal("range_pos", "20d range position", pos, range_dir, DIRECTIONAL,
                     f"{pos*100:.0f}% of 20d range" if pos is not None else "n/a",
                     {"high20": hi20, "low20": lo20}))

    r = rsi(closes, 14)[-1]
    rsi_dir = clamp((r - 50.0) / 25.0) if r is not None else 0.0
    block.add(Signal("rsi", "RSI(14)", r, rsi_dir, DIRECTIONAL,
                     f"RSI {r:.0f}" if r is not None else "n/a"))

    # --- Today's own move --------------------------------------------------
    gap_atr = None
    if quote and quote.prev_close and a > 0:
        gap_atr = (last - quote.prev_close) / a
    block.add(Signal("day_move", "Today's move (ATR)", gap_atr, squash(gap_atr, 0.8), DIRECTIONAL,
                     f"{gap_atr:+.2f} ATR today" if gap_atr is not None else "n/a"))

    block.direction = blend([
        (block.get("ma_stack").score, 3.0),
        (block.get("ema21_slope").score, 2.0),
        (block.get("di_bias").score, 1.5),
        (block.get("range_pos").score, 1.5),
        (block.get("rsi").score, 1.0),
        (block.get("day_move").score, 1.0),
    ])
    block.quality = blend([(adx_q, 2.0), (1.0 - ext_penalty, 1.5)])
    block.detail = {"atr14": a, "atr_pct": atr_pct, "ema8": e8, "ema21": e21,
                    "ema50": e50, "ema200": e200, "high20": hi20, "low20": lo20,
                    "last": last}
    return block
