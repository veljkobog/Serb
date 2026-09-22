"""0DTE chain metrics: expected move, skew, flow, positioning, gamma."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import bs
from .models import OptionQuote

CONTRACT_MULT = 100.0

# Classified prints only override the unsigned proxy once they account for at
# least this share of the day's traded volume in the measured strike window.
MIN_SIDE_COVERAGE = 0.05


@dataclass
class StrikeGamma:
    strike: float
    net_gex: float    # calls minus puts, $ per 1% move (regime / flip)
    total_gex: float  # calls plus puts, $ per 1% move (magnet / wall)


@dataclass
class OptionMetrics:
    spot: float
    t_years: float
    atm_strike: float
    atm_iv: Optional[float]
    atm_straddle: Optional[float]
    em_pct: Optional[float]           # expected remaining move, % of spot
    em_dollars: Optional[float]
    rr25: Optional[float]             # 25d call IV - 25d put IV (vol points)
    skew_norm: Optional[float]        # rr25 / atm_iv
    pc_volume_ratio: Optional[float]  # put volume / call volume (window)
    pc_oi_ratio: Optional[float]
    vol_oi_ratio: Optional[float]     # freshness of today's positioning
    oi_tilt: Optional[float]          # call OI above spot vs put OI below, -1..1
    flow_tilt: Optional[float]        # directional flow, -1..1 (see flow_source)
    flow_source: str                  # "side" (classified prints) or "proxy"
    proxy_flow_tilt: Optional[float]  # delta-$ premium, calls vs puts, unsigned volume
    signed_flow_tilt: Optional[float] # buyer- minus seller-initiated, delta-signed
    net_delta_contracts: float        # net delta bought, in contract deltas
    net_delta_dollars: float          # same, times spot * 100
    net_premium_dollars: float        # bought minus sold premium, delta-signed
    sampled_volume: float             # contracts with a classified side
    side_coverage: Optional[float]    # sampled / traded volume in the window
    call_delta_dollars: float
    put_delta_dollars: float
    max_pain: Optional[float]
    gamma_wall: Optional[float]       # strike holding the most total gamma
    gamma_flip: Optional[float]       # strike where cumulative net GEX crosses 0
    net_gex: float                    # chain-wide net gamma exposure
    chain_volume: float
    window_volume: float
    total_oi: float
    avg_atm_spread_pct: Optional[float]
    strikes_used: int
    gamma_profile: List[StrikeGamma] = field(default_factory=list)


def _by_right(chain: Sequence[OptionQuote]) -> Tuple[List[OptionQuote], List[OptionQuote]]:
    calls = sorted((o for o in chain if o.right.upper().startswith("C")), key=lambda o: o.strike)
    puts = sorted((o for o in chain if o.right.upper().startswith("P")), key=lambda o: o.strike)
    return calls, puts


def _delta_of(o: OptionQuote, spot: float, t: float, iv: Optional[float], r: float) -> Optional[float]:
    """Signed delta: the broker's when the feed supplies it, else Black-Scholes.

    Put deltas are negative, which is what makes delta-signing of classified
    flow work: buying puts and selling calls both come out bearish.
    """
    if o.delta is not None and -1.0001 <= o.delta <= 1.0001:
        return o.delta
    if iv is None:
        return None
    return bs.delta(spot, o.strike, t, iv, o.right, r=r)


def _gamma_of(o: OptionQuote, spot: float, t: float, iv: Optional[float], r: float) -> Optional[float]:
    if o.gamma is not None and o.gamma >= 0:
        return o.gamma
    if iv is None:
        return None
    return bs.gamma(spot, o.strike, t, iv, r=r)


def _resolve_iv(o: OptionQuote, spot: float, t: float, r: float) -> Optional[float]:
    """Provider IV when it looks sane, otherwise solve it from the mid."""
    if o.iv and 0.01 < o.iv < 5.0:
        return o.iv
    mid = o.mid
    if mid is None:
        return None
    return bs.implied_vol(mid, spot, o.strike, t, o.right, r=r)


def _nearest(values: Sequence[float], target: float) -> Optional[float]:
    if not values:
        return None
    return min(values, key=lambda v: abs(v - target))


def _ratio_to_signed(ratio: Optional[float], scale: float = 0.5) -> Optional[float]:
    """Map a positive ratio to [-1, 1] through log space: 1.0 -> 0."""
    if ratio is None or ratio <= 0:
        return None
    return math.tanh(math.log(ratio) / scale)


def max_pain(calls: Sequence[OptionQuote], puts: Sequence[OptionQuote]) -> Optional[float]:
    """Strike that expires the most open interest worthless (classic max pain)."""
    strikes = sorted({o.strike for o in list(calls) + list(puts)})
    if not strikes:
        return None
    best, best_val = None, None
    for s in strikes:
        pain = sum(c.open_interest * max(0.0, s - c.strike) for c in calls)
        pain += sum(p.open_interest * max(0.0, p.strike - s) for p in puts)
        if best_val is None or pain < best_val:
            best, best_val = s, pain
    return best


def _strike_gex(
    chain: Sequence[OptionQuote],
    strike: float,
    spot: float,
    t: float,
    ivs: Dict[Tuple[float, str], float],
    atm_iv: Optional[float],
    r: float,
) -> Tuple[float, float]:
    """Gamma exposure held at one strike, in $ per 1% spot move: (net, total).

    Net signs calls positive and puts negative — the usual published "net GEX"
    construction, standing in for dealers being short customer options. Total
    ignores sign and measures how much gamma is parked at the strike, which is
    what makes a level act as a magnet. Both are proxies: real dealer
    positioning is not public, so read the sign and the levels, not the size.
    """
    net = total = 0.0
    for o in chain:
        if o.strike != strike:
            continue
        iv = ivs.get((strike, o.right.upper()[0])) or atm_iv
        if not iv:
            continue
        g = _gamma_of(o, spot, t, iv, r)
        if g is None:
            continue
        gex = g * o.open_interest * CONTRACT_MULT * spot * spot * 0.01
        net += gex if o.right.upper().startswith("C") else -gex
        total += gex
    return net, total


def _zero_gamma_level(
    chain: Sequence[OptionQuote],
    strikes: Sequence[float],
    t: float,
    ivs: Dict[Tuple[float, str], float],
    atm_iv: Optional[float],
    r: float,
    spot: float,
) -> Optional[float]:
    """Spot level where chain-wide net gamma flips sign (the "gamma flip").

    Re-prices the whole surface at each strike used as a hypothetical spot,
    then returns the sign-change nearest to the current spot. Above the flip
    dealers are long gamma (moves get sold into / pinned); below it they are
    short gamma (moves get chased).
    """
    totals = []
    for cand in strikes:
        tot = sum(
            _strike_gex(chain, k, cand, t, ivs, atm_iv, r)[0] for k in strikes
        )
        totals.append((cand, tot))
    crossings = []
    for i in range(1, len(totals)):
        (k0, g0), (k1, g1) = totals[i - 1], totals[i]
        if g0 == 0.0:
            crossings.append(k0)
        elif (g0 < 0) != (g1 < 0):
            # linear interpolation between the bracketing strikes
            span = g1 - g0
            crossings.append(k0 + (k1 - k0) * (-g0 / span) if span else k0)
    if not crossings:
        return None
    return round(min(crossings, key=lambda k: abs(k - spot)), 2)


def compute_option_metrics(
    chain: Sequence[OptionQuote],
    spot: float,
    t_years: float,
    risk_free: float = 0.04,
    em_window: float = 2.0,
) -> Optional[OptionMetrics]:
    """Collapse a same-day chain into the directional and positioning numbers.

    `em_window` limits flow/skew/spread measurement to strikes within that many
    expected moves of spot — the only part of a 0DTE book that is really live.
    """
    calls, puts = _by_right(chain)
    if not calls or not puts or spot <= 0:
        return None
    t = max(t_years, bs.MIN_T)

    strikes = sorted({o.strike for o in chain})
    atm = _nearest(strikes, spot)
    ivs: Dict[Tuple[float, str], float] = {}
    for o in chain:
        iv = _resolve_iv(o, spot, t, risk_free)
        if iv:
            ivs[(o.strike, o.right.upper()[0])] = iv

    atm_ivs = [ivs[k] for k in ((atm, "C"), (atm, "P")) if k in ivs]
    atm_iv = sum(atm_ivs) / len(atm_ivs) if atm_ivs else None

    atm_call = next((c for c in calls if c.strike == atm), None)
    atm_put = next((p for p in puts if p.strike == atm), None)
    straddle = None
    if atm_call and atm_put and atm_call.mid and atm_put.mid:
        straddle = atm_call.mid + atm_put.mid

    # Expected remaining move = the ATM straddle: under a normal, the ATM
    # straddle equals E|move| = 0.798 sigma, which is the +/- band 0DTE desks
    # quote. Fall back to ATM IV over the remaining session on the same basis.
    em_dollars = None
    if straddle:
        em_dollars = straddle
    elif atm_iv:
        em_dollars = 0.7979 * spot * atm_iv * math.sqrt(t)
    em_pct = (em_dollars / spot * 100.0) if em_dollars else None

    band = (em_window * em_dollars) if em_dollars else 0.02 * spot
    in_window = [o for o in chain if abs(o.strike - spot) <= band]
    if not in_window:
        in_window = list(chain)

    # --- 25-delta risk reversal ---------------------------------------------
    rr25 = skew_norm = None
    def _delta_gap(o: OptionQuote, target: float) -> Optional[float]:
        d = _delta_of(o, spot, t, ivs.get((o.strike, o.right.upper()[0])), risk_free)
        return None if d is None else abs(d - target)

    call_cands = [
        (gap, c) for c in calls
        if (c.strike, "C") in ivs and (gap := _delta_gap(c, 0.25)) is not None
    ]
    put_cands = [
        (gap, p) for p in puts
        if (p.strike, "P") in ivs and (gap := _delta_gap(p, -0.25)) is not None
    ]
    if call_cands and put_cands:
        c25 = min(call_cands, key=lambda x: x[0])[1]
        p25 = min(put_cands, key=lambda x: x[0])[1]
        rr25 = (ivs[(c25.strike, "C")] - ivs[(p25.strike, "P")]) * 100.0  # vol points
        if atm_iv:
            skew_norm = rr25 / (atm_iv * 100.0)

    # --- volume / OI positioning --------------------------------------------
    call_vol = sum(o.volume for o in in_window if o.right.upper().startswith("C"))
    put_vol = sum(o.volume for o in in_window if o.right.upper().startswith("P"))
    call_oi = sum(o.open_interest for o in in_window if o.right.upper().startswith("C"))
    put_oi = sum(o.open_interest for o in in_window if o.right.upper().startswith("P"))
    chain_volume = sum(o.volume for o in chain)
    total_oi = sum(o.open_interest for o in chain)

    pc_vol = (put_vol / call_vol) if call_vol > 0 else None
    pc_oi = (put_oi / call_oi) if call_oi > 0 else None
    vol_oi = (chain_volume / total_oi) if total_oi > 0 else None

    oi_above = sum(c.open_interest for c in calls if c.strike > spot)
    oi_below = sum(p.open_interest for p in puts if p.strike < spot)
    oi_tilt = None
    if oi_above + oi_below > 0:
        oi_tilt = (oi_above - oi_below) / (oi_above + oi_below)

    # --- directional flow ----------------------------------------------------
    # Preferred: classified time & sales. Each print is labelled buyer- or
    # seller-initiated, and signing by the contract's own delta collapses all
    # four cases (buy calls / sell calls / buy puts / sell puts) into one
    # number: net delta the customer side lifted.
    #
    # Fallback when no side data was streamed: total traded premium, delta-
    # weighted, calls vs puts. That is positioning, not pressure — it cannot
    # tell a call buyer from a call seller.
    call_dd = put_dd = 0.0
    net_delta = gross_delta = 0.0
    net_premium = 0.0
    sampled = 0.0
    for o in in_window:
        iv = ivs.get((o.strike, o.right.upper()[0]))
        d = _delta_of(o, spot, t, iv, risk_free)
        mid = o.mid
        if d is None or not mid:
            continue
        if o.volume > 0:
            dollars = o.volume * mid * CONTRACT_MULT * abs(d)
            if o.right.upper().startswith("C"):
                call_dd += dollars
            else:
                put_dd += dollars
        if o.sampled:
            sampled += o.sampled_volume
            net_delta += o.net_volume * d
            gross_delta += (o.buy_volume + o.sell_volume) * abs(d)
            net_premium += (o.buy_premium - o.sell_premium) * math.copysign(1.0, d)

    proxy_tilt = None
    if call_dd + put_dd > 0:
        proxy_tilt = (call_dd - put_dd) / (call_dd + put_dd)

    signed_tilt = (net_delta / gross_delta) if gross_delta > 0 else None
    window_vol = call_vol + put_vol
    coverage = (sampled / window_vol) if window_vol > 0 else None

    # Classified flow wins once it has seen a meaningful slice of the day's
    # volume; below that the sample is too small to outvote the proxy.
    if signed_tilt is not None and coverage is not None and coverage >= MIN_SIDE_COVERAGE:
        flow_tilt, flow_source = signed_tilt, "side"
    else:
        flow_tilt, flow_source = proxy_tilt, "proxy"

    # --- gamma structure ----------------------------------------------------
    # Dealer convention: customers buy calls and puts, dealers are short both,
    # signed so that calls contribute positive and puts negative GEX.
    profile = []
    for k in strikes:
        net_k, total_k = _strike_gex(chain, k, spot, t, ivs, atm_iv, risk_free)
        profile.append(StrikeGamma(strike=k, net_gex=net_k, total_gex=total_k))
    net_gex = sum(g.net_gex for g in profile)
    wall = max(profile, key=lambda g: g.total_gex).strike if profile else None
    flip = _zero_gamma_level(chain, strikes, t, ivs, atm_iv, risk_free, spot)

    spreads = [o.spread_pct for o in in_window if o.spread_pct is not None and o.volume > 0]
    avg_spread = sum(spreads) / len(spreads) if spreads else None

    return OptionMetrics(
        spot=spot,
        t_years=t,
        atm_strike=atm or spot,
        atm_iv=atm_iv,
        atm_straddle=straddle,
        em_pct=em_pct,
        em_dollars=em_dollars,
        rr25=rr25,
        skew_norm=skew_norm,
        pc_volume_ratio=pc_vol,
        pc_oi_ratio=pc_oi,
        vol_oi_ratio=vol_oi,
        oi_tilt=oi_tilt,
        flow_tilt=flow_tilt,
        flow_source=flow_source,
        proxy_flow_tilt=proxy_tilt,
        signed_flow_tilt=signed_tilt,
        net_delta_contracts=net_delta,
        net_delta_dollars=net_delta * CONTRACT_MULT * spot,
        net_premium_dollars=net_premium,
        sampled_volume=sampled,
        side_coverage=coverage,
        call_delta_dollars=call_dd,
        put_delta_dollars=put_dd,
        max_pain=max_pain(calls, puts),
        gamma_wall=wall,
        gamma_flip=flip,
        net_gex=net_gex,
        chain_volume=chain_volume,
        window_volume=call_vol + put_vol,
        total_oi=total_oi,
        avg_atm_spread_pct=avg_spread,
        strikes_used=len(strikes),
        gamma_profile=profile,
    )
