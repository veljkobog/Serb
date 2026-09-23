"""Named 0DTE setups: the market-structure conditions behind a spike.

Each setup is an explicit rule over the tape and the chain, so a rating can
always be explained as "these five things were true at 10:31."

Honest framing: these encode widely documented 0DTE market-structure behavior
(dealer gamma regime, walls, max-pain drift, opening-range and VWAP mechanics,
classified flow confirmation). They are *priors*, not parameters fitted to a
backtest — no backtest has been run here. The snapshot/side-tape replay path
exists so you can validate and retune them on your own tape before leaning on
them. Strengths are deliberately coarse for the same reason.

Sides: +1 bullish, -1 bearish, 0 blocker (it drags any rating down).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .option_metrics import OptionMetrics
from .price_metrics import PriceMetrics


@dataclass(frozen=True)
class Setup:
    key: str
    label: str
    side: int          # +1 bull, -1 bear, 0 blocker
    strength: float    # rating points it moves, before the confidence haircut
    why: str           # the mechanism, one line


@dataclass
class Hit:
    setup: Setup
    detail: str        # the numbers that fired it, for the card

    @property
    def key(self) -> str:
        return self.setup.key

    @property
    def side(self) -> int:
        return self.setup.side

    @property
    def strength(self) -> float:
        return self.setup.strength


SETUPS = {
    s.key: s
    for s in [
        # --- bullish ------------------------------------------------------
        Setup("short_gamma_breakout", "Short-gamma breakout", +1, 1.5,
              "Dealers short gamma have to buy into strength, so breaks extend instead of fading."),
        Setup("call_wall_break", "Pressing the call wall", +1, 1.2,
              "Through the heaviest call-gamma strike, hedging flips from selling rallies to chasing them."),
        Setup("magnet_up", "Max-pain magnet above", +1, 1.0,
              "Long-gamma tape drifts toward the strike that expires the most open interest worthless."),
        Setup("vwap_reclaim", "VWAP reclaim", +1, 1.0,
              "Session spent below VWAP then reclaimed: trapped sellers cover into the afternoon."),
        Setup("gap_and_go", "Gap and go", +1, 1.2,
              "Gap held, opening range broken up, no fill: the day's sellers never showed."),
        Setup("put_unwind", "Put unwind", +1, 1.0,
              "Customers writing puts: dealers buy stock back as those hedges come off."),
        # --- bearish ------------------------------------------------------
        Setup("short_gamma_breakdown", "Short-gamma breakdown", -1, 1.5,
              "Dealers short gamma sell into weakness, so breaks accelerate."),
        Setup("put_wall_break", "Pressing the put wall", -1, 1.2,
              "Through the heaviest put-gamma strike, hedging turns into selling pressure."),
        Setup("magnet_down", "Max-pain magnet below", -1, 1.0,
              "Long-gamma tape drifts down toward max pain."),
        Setup("vwap_rejection", "VWAP rejection", -1, 1.0,
              "Session spent above VWAP then lost it: late longs are offside."),
        Setup("gap_fade", "Gap fade", -1, 1.2,
              "Gap up filled and price red on the day: the open was the high."),
        Setup("call_unwind", "Call unwind", -1, 1.0,
              "Customers writing calls: dealers sell stock as those hedges come off."),
        # --- blockers -----------------------------------------------------
        Setup("pin_lock", "Pinned", 0, 1.5,
              "Long gamma, sitting on the wall and near max pain: a range day, both sides bleed."),
        Setup("em_exhausted", "Move already used", 0, 1.0,
              "Most of a normal day's range is already spent; continuation pays too much."),
        Setup("flow_divergence", "Flow disagrees with tape", 0, 1.0,
              "Price is going one way and classified option flow the other."),
        Setup("proxy_only", "No trade side", 0, 0.5,
              "Flow is an unsigned proxy: nothing confirms who was the aggressor."),
        Setup("illiquid", "Illiquid chain", 0, 2.0,
              "Spreads and volume make the entry cost more than the edge."),
    ]
}


def _hit(key: str, detail: str) -> Hit:
    return Hit(SETUPS[key], detail)


def detect(
    price: PriceMetrics,
    options: Optional[OptionMetrics],
    flags: Optional[List[str]] = None,
) -> List[Hit]:
    """Every setup that fires on this symbol right now, strongest first."""
    flags = flags or []
    hits: List[Hit] = []
    p, o = price, options

    above_vwap = p.last > p.vwap
    broke_up = p.or_high > 0 and p.last > p.or_high
    broke_down = p.or_low > 0 and p.last < p.or_low
    em = o.em_dollars if o else None
    flow = o.flow_tilt if o else None
    confirmed_flow = bool(o and o.flow_source == "side")

    # --- gamma regime -------------------------------------------------------
    if o and o.net_gex < 0:
        if broke_up and p.vwap_z > 0.5 and (flow is None or flow > -0.1):
            hits.append(_hit(
                "short_gamma_breakout",
                f"net GEX {o.net_gex / 1e6:+.0f}mm, above OR high {p.or_high:,.2f}, "
                f"{p.vwap_z:+.1f}σ over VWAP",
            ))
        if broke_down and p.vwap_z < -0.5 and (flow is None or flow < 0.1):
            hits.append(_hit(
                "short_gamma_breakdown",
                f"net GEX {o.net_gex / 1e6:+.0f}mm, below OR low {p.or_low:,.2f}, "
                f"{p.vwap_z:+.1f}σ under VWAP",
            ))

    # --- walls --------------------------------------------------------------
    if o and em:
        if o.call_wall and abs(o.spot - o.call_wall) < 0.4 * em and (flow or 0) > 0.1:
            hits.append(_hit(
                "call_wall_break",
                f"spot {o.spot:,.2f} into call wall {o.call_wall:,.2f}, flow {flow:+.2f}",
            ))
        if o.put_wall and abs(o.spot - o.put_wall) < 0.4 * em and (flow or 0) < -0.1:
            hits.append(_hit(
                "put_wall_break",
                f"spot {o.spot:,.2f} into put wall {o.put_wall:,.2f}, flow {flow:+.2f}",
            ))

    # --- max pain -----------------------------------------------------------
    if o and em and o.max_pain is not None and o.net_gex > 0:
        dist = (o.max_pain - o.spot) / em
        if dist > 0.5:
            hits.append(_hit("magnet_up", f"max pain {o.max_pain:,.2f} is {dist:+.1f} EM above"))
        elif dist < -0.5:
            hits.append(_hit("magnet_down", f"max pain {o.max_pain:,.2f} is {dist:+.1f} EM below"))

    # --- VWAP ---------------------------------------------------------------
    if above_vwap and p.persistence < 0.45 and p.vwap_z > 0.3:
        hits.append(_hit(
            "vwap_reclaim",
            f"only {p.persistence:.0%} of bars closed above VWAP, now {p.vwap_z:+.1f}σ over",
        ))
    if not above_vwap and p.persistence > 0.55 and p.vwap_z < -0.3:
        hits.append(_hit(
            "vwap_rejection",
            f"{p.persistence:.0%} of bars closed above VWAP, now {p.vwap_z:+.1f}σ under",
        ))

    # --- the open -----------------------------------------------------------
    if p.gap_pct is not None and abs(p.gap_pct) > 0.2:
        gapped_up = p.gap_pct > 0
        if gapped_up and broke_up and p.last > p.open_px:
            hits.append(_hit("gap_and_go", f"gapped {p.gap_pct:+.2f}% and held, OR high taken"))
        if gapped_up and p.ret_prev_close_pct is not None and p.ret_prev_close_pct < 0:
            hits.append(_hit(
                "gap_fade",
                f"gapped {p.gap_pct:+.2f}% and filled, now {p.ret_prev_close_pct:+.2f}% on the day",
            ))
        if not gapped_up and broke_down and p.last < p.open_px:
            hits.append(_hit(
                "short_gamma_breakdown" if o and o.net_gex < 0 else "vwap_rejection",
                f"gapped {p.gap_pct:+.2f}% and kept going, OR low taken",
            ))

    # --- who is writing what ------------------------------------------------
    if o and confirmed_flow:
        if o.put_flow_tilt is not None and o.put_flow_tilt > 0.25:
            hits.append(_hit("put_unwind", f"put flow tilt {o.put_flow_tilt:+.2f} (customers selling puts)"))
        if o.call_flow_tilt is not None and o.call_flow_tilt < -0.25:
            hits.append(_hit("call_unwind", f"call flow tilt {o.call_flow_tilt:+.2f} (customers writing calls)"))

    # --- blockers -----------------------------------------------------------
    if o and em and o.net_gex > 0 and o.gamma_wall is not None:
        near_wall = abs(o.spot - o.gamma_wall) < 0.25 * em
        near_pain = o.max_pain is not None and abs(o.spot - o.max_pain) < 0.5 * em
        if near_wall and near_pain:
            hits.append(_hit(
                "pin_lock",
                f"long gamma, {abs(o.spot - o.gamma_wall):,.2f} from the wall at {o.gamma_wall:,.2f}",
            ))
    if p.move_vs_adr is not None and p.move_vs_adr > 1.2:
        hits.append(_hit("em_exhausted", f"{p.move_vs_adr:.1f}x the 14-day average daily range"))
    tape_dir = 1 if (p.vwap_z + p.or_pos) > 0 else -1
    if flow is not None and abs(flow) > 0.2 and (1 if flow > 0 else -1) != tape_dir:
        hits.append(_hit(
            "flow_divergence",
            f"tape {'up' if tape_dir > 0 else 'down'}, flow {flow:+.2f}",
        ))
    if o and o.flow_source != "side":
        hits.append(_hit("proxy_only", "no classified prints merged for this symbol"))
    if "thin-chain" in flags or "wide-spreads" in flags:
        hits.append(_hit("illiquid", ", ".join(f for f in flags if f in ("thin-chain", "wide-spreads"))))

    # De-duplicate (a couple of rules can fire the same setup) and rank.
    seen = {}
    for h in hits:
        if h.key not in seen:
            seen[h.key] = h
    return sorted(seen.values(), key=lambda h: (-h.strength, h.key))
