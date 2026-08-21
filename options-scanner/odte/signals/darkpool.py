"""Off-exchange ("dark pool") participation and buy/sell pressure proxy.

See ``odte/providers/finra.py`` for exactly what the underlying file is and is not.
"""
from __future__ import annotations

from typing import Dict, List

from ..indicators import clamp, ramp, squash, zscore
from ..providers.base import Bar
from ..providers.finra import OffExDay
from . import DIRECTIONAL, QUALITY, Block, Signal, blend


def analyse(history: List[OffExDay], bars: List[Bar]) -> Block:
    block = Block(name="darkpool")
    if not history:
        block.available = False
        block.notes.append("no FINRA off-exchange data for this symbol")
        return block

    consolidated: Dict[object, float] = {b.date: b.volume for b in bars}

    shares: List[float] = []       # off-exchange share of consolidated volume, by day
    notionals: List[float] = []    # off-exchange $ notional, by day
    closes: Dict[object, float] = {b.date: b.close for b in bars}
    for row in history:
        total = consolidated.get(row.date)
        if total and total > 0 and row.total_volume > 0:
            shares.append(min(row.total_volume / total, 1.5))
        px = closes.get(row.date)
        if px:
            notionals.append(row.total_volume * px)

    latest = history[-1]
    dpis = [r.dpi for r in history if r.dpi is not None]

    # --- DPI: low off-exchange short ratio => market makers are selling into buys ---
    dpi = latest.dpi
    dpi_dir = clamp((dpi - 0.5) * 5.0) if dpi is not None else 0.0
    block.add(Signal("dpi", "Dark pool index (1 - offex short ratio)", dpi, dpi_dir, DIRECTIONAL,
                     f"DPI {dpi*100:.1f}%" if dpi is not None else "n/a",
                     {"date": str(latest.date), "short_volume": latest.short_volume,
                      "offex_volume": latest.total_volume}))

    dpi5 = sum(dpis[-5:]) / len(dpis[-5:]) if dpis else None
    block.add(Signal("dpi_5d", "DPI 5-day average", dpi5,
                     clamp(((dpi5 or 0.5) - 0.5) * 5.0), DIRECTIONAL,
                     f"5d DPI {dpi5*100:.1f}%" if dpi5 is not None else "n/a"))

    dpi_z = zscore(dpi, dpis[:-1][-20:]) if len(dpis) > 6 else None
    block.add(Signal("dpi_z", "DPI z-score vs 20d", dpi_z, squash(dpi_z, 1.6), DIRECTIONAL,
                     f"{dpi_z:+.1f}σ vs its own 20d" if dpi_z is not None else "n/a"))

    # --- Participation: an off-exchange share spike means size was worked quietly ---
    share = shares[-1] if shares else None
    share_z = zscore(share, shares[:-1][-20:]) if len(shares) > 6 else None
    share_q = ramp(share_z, 0.5, 2.5)
    block.add(Signal("offex_share", "Off-exchange share of volume", share, share_q, QUALITY,
                     f"{share*100:.1f}% off-exchange" if share is not None else "n/a",
                     {"zscore": share_z}))
    block.add(Signal("offex_share_z", "Off-exchange share z-score", share_z, share_q, QUALITY,
                     f"{share_z:+.1f}σ" if share_z is not None else "n/a"))

    notional = notionals[-1] if notionals else None
    notional_z = zscore(notional, notionals[:-1][-20:]) if len(notionals) > 6 else None
    block.add(Signal("offex_notional", "Off-exchange $ notional", notional,
                     ramp(notional_z, 0.5, 2.5), QUALITY,
                     f"${notional/1e6:.0f}M off-exchange" if notional else "n/a",
                     {"zscore": notional_z}))

    block.direction = blend([
        (block.get("dpi").score, 2.0),
        (block.get("dpi_5d").score, 1.5),
        (block.get("dpi_z").score, 1.5),
    ])
    block.quality = blend([(share_q, 2.0), (block.get("offex_notional").score, 1.5)])
    block.detail = {"as_of": str(latest.date), "dpi": dpi, "dpi_5d": dpi5,
                    "offex_share": share, "offex_share_z": share_z,
                    "offex_notional": notional, "days_of_history": len(history)}
    if len(history) < 10:
        block.notes.append(f"only {len(history)} days of off-exchange history")
    return block
