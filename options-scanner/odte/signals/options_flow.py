"""Near-dated option flow, liquidity, and structure for the 0DTE/1DTE expiry.

This block does double duty: it produces a directional read from where premium is
being spent, and it produces the hard tradability gate. A perfect chart with a
40%-wide bid/ask on the front expiry is not a trade.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from ..indicators import clamp, ramp
from ..providers.base import OptionChain, OptionContract
from . import DIRECTIONAL, QUALITY, Block, Signal, blend


def _premium(contracts: List[OptionContract]) -> float:
    return sum(c.notional for c in contracts)


def _oi_wall(contracts: List[OptionContract]) -> Tuple[Optional[float], float]:
    best, best_oi = None, 0.0
    by_strike: Dict[float, float] = {}
    for c in contracts:
        by_strike[c.strike] = by_strike.get(c.strike, 0.0) + c.open_interest
    for strike, oi in by_strike.items():
        if oi > best_oi:
            best, best_oi = strike, oi
    return best, best_oi


def _atm_pair(chain: OptionChain, spot: float):
    strike = chain.nearest_strike(spot)
    if strike is None:
        return None, None, None
    return strike, chain.at(strike, "C"), chain.at(strike, "P")


def _gamma_profile(chain: OptionChain, spot: float) -> Dict[str, Optional[float]]:
    """Dealer gamma proxy. Assumes dealers are long calls / short puts against
    customer flow — the standard retail-facing convention. Sign is a proxy, not a
    measured dealer book."""
    have_gamma = any(c.gamma is not None for c in chain.contracts)
    per_strike: Dict[float, float] = {}
    for c in chain.contracts:
        if c.open_interest <= 0:
            continue
        if have_gamma:
            if c.gamma is None:
                continue
            gex = c.gamma * c.open_interest * 100.0 * spot * spot * 0.01
        else:
            # Without greeks, weight OI by proximity to spot as a crude gamma stand-in.
            width = max(spot * 0.03, 1e-6)
            gex = c.open_interest * 100.0 * spot * math.exp(-((c.strike - spot) / width) ** 2)
        per_strike[c.strike] = per_strike.get(c.strike, 0.0) + (gex if c.right == "C" else -gex)

    total = sum(per_strike.values())
    flip = None
    strikes = sorted(per_strike)
    cum = 0.0
    for s in strikes:
        prev = cum
        cum += per_strike[s]
        if prev < 0 <= cum or prev > 0 >= cum:
            flip = s
    return {"net_gex": total if per_strike else None, "flip_strike": flip,
            "measured": have_gamma}


def analyse(chain: Optional[OptionChain], spot: float, atr_pct: Optional[float] = None,
            dte: int = 0, band_pct: float = 0.06, min_unusual_volume: float = 500.0) -> Block:
    block = Block(name="options")
    if chain is None or not chain.contracts:
        block.available = False
        block.notes.append("no option chain for the near expiry")
        return block

    calls, puts = chain.calls, chain.puts
    call_vol = sum(c.volume for c in calls)
    put_vol = sum(c.volume for c in puts)
    call_oi = sum(c.open_interest for c in calls)
    put_oi = sum(c.open_interest for c in puts)
    total_vol, total_oi = call_vol + put_vol, call_oi + put_oi

    block.add(Signal("option_volume", "Near-expiry contract volume", total_vol,
                     ramp(total_vol, 2_000, 50_000), QUALITY,
                     f"{total_vol:,.0f} contracts on {chain.expiry}"))

    vol_oi = (total_vol / total_oi) if total_oi > 0 else None
    block.add(Signal("vol_oi", "Volume / open interest", vol_oi, ramp(vol_oi, 0.4, 1.5), QUALITY,
                     f"{vol_oi:.2f}x OI (new positioning)" if vol_oi else "n/a",
                     {"open_interest": total_oi}))

    # --- Where is premium actually going -----------------------------------
    lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
    otm_calls = [c for c in calls if spot <= c.strike <= hi]
    otm_puts = [p for p in puts if lo <= p.strike <= spot]
    call_prem, put_prem = _premium(otm_calls), _premium(otm_puts)
    prem_total = call_prem + put_prem
    prem_skew = ((call_prem - put_prem) / prem_total) if prem_total > 0 else 0.0
    block.add(Signal("premium_skew", "Near-money premium skew", prem_skew, clamp(prem_skew * 1.4),
                     DIRECTIONAL,
                     f"${call_prem/1e6:.1f}M calls vs ${put_prem/1e6:.1f}M puts",
                     {"call_premium": call_prem, "put_premium": put_prem}))

    pcr = (put_vol / call_vol) if call_vol > 0 else None
    pcr_dir = clamp((1.0 - (pcr or 1.0)) * 1.2)
    block.add(Signal("put_call_ratio", "Put/call volume", pcr, pcr_dir, DIRECTIONAL,
                     f"P/C {pcr:.2f}" if pcr is not None else "n/a"))

    # --- Expected move: for a 0DTE chain the ATM straddle IS the expected move ---
    strike, atm_c, atm_p = _atm_pair(chain, spot)
    straddle = None
    if atm_c and atm_p and atm_c.mid and atm_p.mid:
        straddle = atm_c.mid + atm_p.mid
    if straddle is None and atm_c and atm_c.iv:
        yrs = max(dte, 1) / 365.0
        straddle = 0.8 * spot * atm_c.iv * math.sqrt(yrs)
    em_pct = (straddle / spot * 100.0) if (straddle and spot) else None

    atm_ivs = [c.iv for c in (atm_c, atm_p) if c and c.iv]
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None
    block.add(Signal("atm_iv", "ATM implied volatility", atm_iv, 0.0, QUALITY,
                     f"IV {atm_iv*100:.0f}%" if atm_iv else "n/a"))

    # Cheap premium relative to how far this thing actually travels is the edge for
    # buying; rich premium argues for spreads instead of naked longs.
    em_vs_atr = (em_pct / atr_pct) if (em_pct and atr_pct) else None
    value_score = 1.0 - ramp(em_vs_atr, 0.6, 1.6)
    block.add(Signal("em_vs_atr", "Expected move vs ATR", em_vs_atr, value_score, QUALITY,
                     f"EM {em_pct:.2f}% vs ATR {atr_pct:.2f}%" if em_vs_atr else "n/a",
                     {"expected_move_pct": em_pct, "straddle": straddle, "atm_strike": strike}))

    # --- Liquidity gate ----------------------------------------------------
    atm_spread = None
    spreads = [c.spread_pct for c in (atm_c, atm_p) if c and c.spread_pct is not None]
    if spreads:
        atm_spread = sum(spreads) / len(spreads)
    tradable = [c for c in chain.contracts
                if lo <= c.strike <= hi and c.open_interest >= 100
                and c.spread_pct is not None and c.spread_pct <= 0.15]
    spread_score = 1.0 - ramp(atm_spread, 0.02, 0.15)
    block.add(Signal("atm_spread", "ATM bid/ask spread", atm_spread, spread_score, QUALITY,
                     f"{atm_spread*100:.1f}% wide" if atm_spread is not None else "n/a"))
    block.add(Signal("tradable_strikes", "Liquid near-money strikes", float(len(tradable)),
                     ramp(len(tradable), 4, 16), QUALITY,
                     f"{len(tradable)} strikes with tight quotes + OI"))

    # --- Structure: walls and gamma ---------------------------------------
    call_wall, call_wall_oi = _oi_wall(calls)
    put_wall, put_wall_oi = _oi_wall(puts)
    gamma = _gamma_profile(chain, spot)
    room_up = ((call_wall - spot) / spot * 100.0) if call_wall else None
    room_dn = ((spot - put_wall) / spot * 100.0) if put_wall else None
    block.add(Signal("call_wall", "Call wall (max call OI)", call_wall, 0.0, QUALITY,
                     f"{call_wall:g} ({call_wall_oi:,.0f} OI, {room_up:+.2f}% away)"
                     if call_wall and room_up is not None else "n/a"))
    block.add(Signal("put_wall", "Put wall (max put OI)", put_wall, 0.0, QUALITY,
                     f"{put_wall:g} ({put_wall_oi:,.0f} OI, {room_dn:.2f}% below)"
                     if put_wall and room_dn is not None else "n/a"))

    # Pinned into a wall with less room than the expected move = the move is capped.
    headroom = None
    if em_pct and room_up is not None and room_dn is not None:
        headroom = min(abs(room_up), abs(room_dn)) / em_pct
    block.add(Signal("wall_headroom", "Headroom to nearest wall (EM units)", headroom,
                     ramp(headroom, 0.5, 1.5), QUALITY,
                     f"{headroom:.2f}x expected move to nearest wall" if headroom else "n/a",
                     gamma))

    # --- Unusual activity --------------------------------------------------
    unusual = [c for c in chain.contracts
               if c.volume >= min_unusual_volume and c.open_interest > 0
               and c.volume > 3 * c.open_interest]
    unusual.sort(key=lambda c: c.notional, reverse=True)
    unusual_call = sum(c.notional for c in unusual if c.right == "C")
    unusual_put = sum(c.notional for c in unusual if c.right == "P")
    unusual_total = unusual_call + unusual_put
    unusual_dir = ((unusual_call - unusual_put) / unusual_total) if unusual_total > 0 else 0.0
    block.add(Signal("unusual", "Unusual contracts (vol > 3x OI)", float(len(unusual)),
                     clamp(unusual_dir), DIRECTIONAL,
                     f"{len(unusual)} sweeps, ${unusual_total/1e6:.1f}M premium",
                     {"top": [{"symbol": c.symbol, "strike": c.strike, "right": c.right,
                               "volume": c.volume, "open_interest": c.open_interest,
                               "premium": round(c.notional)} for c in unusual[:5]],
                      "call_premium": unusual_call, "put_premium": unusual_put}))

    block.direction = blend([
        (block.get("premium_skew").score, 3.0),
        (block.get("put_call_ratio").score, 1.5),
        (block.get("unusual").score, 2.0 if unusual_total > 0 else 0.0),
    ])
    block.quality = blend([
        (block.get("option_volume").score, 3.0),
        (block.get("vol_oi").score, 1.5),
        (spread_score, 3.0),
        (block.get("tradable_strikes").score, 2.0),
        (value_score, 1.5),
        (block.get("wall_headroom").score, 1.0),
    ])
    block.detail = {
        "expiry": str(chain.expiry), "dte": dte, "call_volume": call_vol, "put_volume": put_vol,
        "total_volume": total_vol, "open_interest": total_oi, "vol_oi": vol_oi,
        "put_call_ratio": pcr, "call_premium": call_prem, "put_premium": put_prem,
        "atm_strike": strike, "atm_iv": atm_iv, "straddle": straddle,
        "expected_move_pct": em_pct, "expected_move_dollars": straddle,
        "atm_spread": atm_spread, "tradable_strikes": len(tradable),
        "call_wall": call_wall, "put_wall": put_wall,
        "call_wall_oi": call_wall_oi, "put_wall_oi": put_wall_oi,
        "room_to_call_wall_pct": room_up, "room_to_put_wall_pct": room_dn,
        "net_gex": gamma.get("net_gex"), "gamma_flip": gamma.get("flip_strike"),
        "gamma_measured": gamma.get("measured"),
        "unusual_count": len(unusual), "unusual_premium": unusual_total,
    }
    return block
