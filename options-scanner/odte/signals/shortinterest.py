"""Short interest and squeeze fuel.

Exchange short interest settles twice a month and publishes with roughly an eight-day
lag, so treat it as slow-moving fuel, never as a trigger. The fast-moving cousin is
the daily off-exchange short ratio in ``darkpool.py``.
"""
from __future__ import annotations

from typing import List, Optional

from ..indicators import clamp, ramp
from ..providers.base import Bar, Fundamentals
from . import DIRECTIONAL, QUALITY, Block, Signal, blend


def analyse(fundamentals: Optional[Fundamentals], bars: List[Bar]) -> Block:
    block = Block(name="short_interest")
    if fundamentals is None:
        block.available = False
        block.notes.append("no fundamentals available")
        return block

    vols = [b.volume for b in bars[-21:-1]] or [b.volume for b in bars[-20:]]
    adv = sum(vols) / len(vols) if vols else 0.0

    pct_float = fundamentals.short_pct_float
    if pct_float is None and fundamentals.shares_short and fundamentals.float_shares:
        pct_float = fundamentals.shares_short / fundamentals.float_shares
    if pct_float is not None and pct_float > 1.5:   # some feeds report percent, not fraction
        pct_float /= 100.0

    dtc = None
    if fundamentals.shares_short and adv > 0:
        dtc = fundamentals.shares_short / adv
    elif fundamentals.short_ratio:
        dtc = fundamentals.short_ratio

    if pct_float is None and dtc is None:
        block.available = False
        block.notes.append("no short interest reported")
        return block

    si_score = ramp(pct_float, 0.03, 0.20)
    block.add(Signal("short_pct_float", "Short interest % of float", pct_float, si_score, QUALITY,
                     f"{pct_float*100:.1f}% of float" if pct_float is not None else "n/a",
                     {"as_of": str(fundamentals.short_interest_date or "")}))

    dtc_score = ramp(dtc, 1.0, 6.0)
    block.add(Signal("days_to_cover", "Days to cover", dtc, dtc_score, QUALITY,
                     f"{dtc:.1f} days to cover" if dtc is not None else "n/a"))

    # Rising short interest into a rising tape is the classic squeeze setup; rising
    # short interest into a falling tape is just correct positioning.
    change = None
    if fundamentals.shares_short and fundamentals.shares_short_prior:
        change = fundamentals.shares_short / fundamentals.shares_short_prior - 1.0
    block.add(Signal("si_change", "Short interest change vs prior", change,
                     clamp((change or 0.0) * 2.0), DIRECTIONAL,
                     f"{change*100:+.0f}% vs prior settlement" if change is not None else "n/a"))

    block.quality = blend([(si_score, 2.0), (dtc_score, 1.5)])
    block.direction = 0.0   # fuel, not direction: score.py applies it as a squeeze multiplier
    block.detail = {"short_pct_float": pct_float, "days_to_cover": dtc,
                    "shares_short": fundamentals.shares_short,
                    "si_change": change, "as_of": str(fundamentals.short_interest_date or "")}
    return block
