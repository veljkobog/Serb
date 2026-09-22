"""Composite bias scoring.

Each component is mapped to [-1, +1] where negative means downside. The
composite is a weighted sum scaled to -100..+100, reported next to a separate
confidence score so a strong-looking bias on a thin, contradictory chain
doesn't read the same as a clean one.

Three components (relative strength, skew, OI tilt) are standardized *across
the scanned universe* rather than against absolute thresholds: equity skew is
structurally put-bid and OI is structurally call-heavy above spot, so the
tradeable information is which names are unusual today, not the sign itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import fmean, pstdev
from typing import Dict, List, Optional, Sequence

from .config import Gates, ScanConfig, Weights
from .option_metrics import OptionMetrics
from .price_metrics import PriceMetrics


@dataclass
class SymbolData:
    """One symbol's raw inputs, assembled by the CLI before scoring."""

    symbol: str
    price: PriceMetrics
    options: Optional[OptionMetrics]
    minutes_to_close: float
    expiry: Optional[str] = None
    is_benchmark: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass
class TradePlan:
    direction: str
    atm_strike: Optional[float]
    otm_1em_strike: Optional[float]
    target: Optional[float]
    invalidation: Optional[float]


@dataclass
class SymbolScore:
    symbol: str
    bias: float                       # -100 (downside) .. +100 (upside)
    confidence: float                 # 0..100
    verdict: str
    components: Dict[str, Optional[float]]
    contributions: Dict[str, float]
    flags: List[str]
    price: PriceMetrics
    options: Optional[OptionMetrics]
    plan: TradePlan
    expiry: Optional[str] = None


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _tanh(x: Optional[float], scale: float) -> Optional[float]:
    if x is None or scale == 0:
        return None
    return math.tanh(x / scale)


def _zscores(values: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Cross-sectional z-scores, None-safe.

    Returns all-None when the universe is too small or too uniform to
    standardize, which tells the caller to fall back to absolute scaling
    instead of scoring everything at zero.
    """
    present = [v for v in values if v is not None]
    if len(present) < 3:
        return [None] * len(values)
    mu = fmean(present)
    sd = pstdev(present)
    if sd <= 1e-12:
        return [None] * len(values)
    return [None if v is None else (v - mu) / sd for v in values]


def _score_tape(p: PriceMetrics) -> Dict[str, Optional[float]]:
    vwap = 0.75 * math.tanh(p.vwap_z / 1.5) + 0.25 * math.tanh(p.vwap_slope_bps / 15.0)
    orb = _clamp(p.or_pos / 1.5)
    persistence = 0.6 * _clamp((p.persistence - 0.5) * 2.0) + 0.4 * _clamp(p.clv)
    return {"vwap": _clamp(vwap), "orb": orb, "persistence": _clamp(persistence)}


def _score_options(o: Optional[OptionMetrics], p: PriceMetrics) -> Dict[str, Optional[float]]:
    if o is None:
        return {"flow": None, "pcr": None, "gamma_pull": None, "stretch": None}

    flow = _tanh(o.flow_tilt, 0.30)

    pcr = None
    if o.pc_volume_ratio and o.pc_volume_ratio > 0:
        # More put volume than call volume reads bearish. Log-scaled so 1.0 is
        # neutral and the tails compress.
        pcr = -math.tanh(math.log(o.pc_volume_ratio) / 0.5)

    # Max pain acts as a magnet, but only while dealers are net long gamma
    # (positive GEX = pinning). Short-gamma tape chases instead of pinning, so
    # the pull is heavily discounted there.
    gamma_pull = None
    if o.max_pain is not None and o.em_dollars:
        raw = (o.max_pain - o.spot) / o.em_dollars
        gamma_pull = math.tanh(raw)
        if o.net_gex < 0:
            gamma_pull *= 0.4

    # Counter-trend brake: a move that has already used up most of a normal
    # day's range (or several expected moves) is a bad continuation entry.
    stretch = None
    move = p.ret_prev_close_pct if p.ret_prev_close_pct is not None else p.ret_open_pct
    used = None
    if p.move_vs_adr is not None:
        used = (p.move_vs_adr - 0.6) / 0.8
    elif o.em_pct:
        used = (abs(move) / (2.0 * o.em_pct) - 0.6) / 0.8
    if used is not None and move != 0:
        stretch = -math.copysign(_clamp(used, 0.0, 1.0), move)

    return {"flow": flow, "pcr": pcr, "gamma_pull": gamma_pull, "stretch": stretch}


def _confidence(
    d: SymbolData,
    components: Dict[str, Optional[float]],
    weights: Weights,
    gates: Gates,
) -> tuple[float, List[str]]:
    flags: List[str] = []
    o, p = d.options, d.price

    # Liquidity: how far above the gate is the traded 0DTE volume.
    liq = 0.0
    if o:
        liq = _clamp(math.log10(max(o.chain_volume, 1) / gates.min_chain_volume) / 1.0, 0.0, 1.0)
        if o.chain_volume < gates.min_chain_volume:
            flags.append("thin-chain")
        if o.avg_atm_spread_pct is not None:
            if o.avg_atm_spread_pct > gates.max_atm_spread_pct:
                flags.append("wide-spreads")
            liq *= _clamp(
                1.0 - (o.avg_atm_spread_pct / max(gates.max_atm_spread_pct, 1e-6) - 1.0),
                0.3,
                1.0,
            )
    if p.share_volume < gates.min_share_volume:
        flags.append("thin-tape")

    # Trade-side quality: how much of the day's volume in the measured window
    # came with a buyer/seller label. Zero means `flow` is only a proxy.
    side = 0.0
    if o:
        if o.flow_source == "side" and o.side_coverage:
            side = _clamp(o.side_coverage / 0.5, 0.0, 1.0)
        else:
            flags.append("proxy-flow")

    # Freshness: today's volume relative to standing OI. Low = stale book.
    fresh = 0.5
    if o and o.vol_oi_ratio is not None:
        fresh = _clamp(o.vol_oi_ratio / 0.5, 0.0, 1.0)
        if o.vol_oi_ratio < 0.10:
            flags.append("stale-oi")

    # Agreement: weighted dispersion of the signed components.
    w = weights.as_dict()
    present = [(w[k], v) for k, v in components.items() if v is not None and k in w]
    agree = 0.0
    if present:
        tw = sum(x[0] for x in present)
        mean = sum(x[0] * x[1] for x in present) / tw
        var = sum(x[0] * (x[1] - mean) ** 2 for x in present) / tw
        agree = _clamp(1.0 - math.sqrt(var) / 0.8, 0.0, 1.0)

    coverage = len(present) / max(len(w), 1)
    if coverage < 0.7:
        flags.append("partial-data")

    # Pin risk: sitting on a long-gamma wall inside a quarter expected move.
    if o and o.gamma_wall is not None and o.em_dollars and o.net_gex > 0:
        if abs(o.spot - o.gamma_wall) < 0.25 * o.em_dollars:
            flags.append("pin-risk")

    conf = 100.0 * (
        0.32 * liq + 0.27 * agree + 0.18 * fresh + 0.13 * coverage + 0.10 * side
    )
    if "pin-risk" in flags:
        conf *= 0.75
    if "stale-oi" in flags:
        conf *= 0.9
    return _clamp(conf, 0.0, 100.0), flags


def _verdict(bias: float, conf: float, flags: List[str], gates: Gates) -> str:
    if "thin-chain" in flags or "wide-spreads" in flags:
        return "AVOID (illiquid)"
    if conf < gates.min_confidence_to_trade or abs(bias) < gates.min_bias_to_trade:
        return "NO TRADE"
    if bias >= 55:
        return "LONG (calls)"
    if bias >= gates.min_bias_to_trade:
        return "LEAN LONG"
    if bias <= -55:
        return "SHORT (puts)"
    return "LEAN SHORT"


def _plan(d: SymbolData, bias: float) -> TradePlan:
    o, p = d.options, d.price
    up = bias >= 0
    direction = "calls" if up else "puts"
    atm = o.atm_strike if o else None
    em = o.em_dollars if o else None
    otm = None
    if atm is not None and em:
        step = _strike_step(o)
        raw = atm + em if up else atm - em
        if step:
            otm = round(raw / step) * step
            # An expected move smaller than one strike increment would round
            # straight back to ATM; step out one strike instead.
            if abs(otm - atm) < step / 2:
                otm = atm + step if up else atm - step
        else:
            otm = raw
    target = None
    if em:
        target = p.last + em if up else p.last - em
    # Invalidation: the level that kills the thesis — VWAP, or the far side of
    # the opening range when price has already left it.
    if up:
        invalidation = max(p.vwap, p.or_low) if p.last > p.or_high else p.vwap
    else:
        invalidation = min(p.vwap, p.or_high) if p.last < p.or_low else p.vwap
    return TradePlan(direction, atm, otm, target, invalidation)


def _strike_step(o: Optional[OptionMetrics]) -> Optional[float]:
    if not o or not o.gamma_profile or len(o.gamma_profile) < 2:
        return None
    diffs = sorted(
        o.gamma_profile[i + 1].strike - o.gamma_profile[i].strike
        for i in range(len(o.gamma_profile) - 1)
    )
    step = diffs[len(diffs) // 2]
    return step if step > 0 else None


def score_universe(data: Sequence[SymbolData], cfg: ScanConfig) -> List[SymbolScore]:
    w = cfg.weights.as_dict()

    rs_z = _zscores([d.price.rs_resid_pct if not d.is_benchmark else None for d in data])
    skew_z = _zscores([d.options.skew_norm if d.options else None for d in data])
    oi_z = _zscores([d.options.oi_tilt if d.options else None for d in data])

    out: List[SymbolScore] = []
    for i, d in enumerate(data):
        comps: Dict[str, Optional[float]] = {}
        comps.update(_score_tape(d.price))
        comps.update(_score_options(d.options, d.price))

        # Relative strength: cross-sectional when the universe supports it.
        # The benchmark has no RS against itself, so it falls back to its own
        # drift; a too-small universe falls back to absolute scaling (0.6% of
        # beta-adjusted outperformance ~ one unit).
        if rs_z[i] is not None:
            comps["rs"] = _tanh(rs_z[i], 1.2)
        elif d.is_benchmark:
            comps["rs"] = _tanh(d.price.ret_open_pct, 0.6)
        else:
            comps["rs"] = _tanh(d.price.rs_resid_pct, 0.6)

        skew_norm = d.options.skew_norm if d.options else None
        if skew_z[i] is not None:
            comps["skew"] = _tanh(skew_z[i], 1.2)
        elif skew_norm is not None:
            # Absolute fallback: equity 25d skew normally sits near -0.10 of
            # ATM vol, so that is the neutral point, not zero.
            comps["skew"] = _tanh(skew_norm + 0.10, 0.12)
        else:
            comps["skew"] = None

        oi_tilt = d.options.oi_tilt if d.options else None
        if oi_z[i] is not None:
            comps["oi_tilt"] = _tanh(oi_z[i], 1.2)
        else:
            comps["oi_tilt"] = _tanh(oi_tilt, 0.5) if oi_tilt is not None else None

        # Renormalize over the components we actually have, so a missing input
        # dilutes nothing and the scale stays -100..100.
        usable = {k: v for k, v in comps.items() if v is not None and k in w}
        tw = sum(w[k] for k in usable) or 1.0
        contributions = {k: 100.0 * w[k] * v / tw for k, v in usable.items()}
        bias = _clamp(sum(contributions.values()), -100.0, 100.0)

        conf, flags = _confidence(d, comps, cfg.weights, cfg.gates)
        flags = list(dict.fromkeys(flags + list(d.notes)))
        out.append(
            SymbolScore(
                symbol=d.symbol,
                bias=bias,
                confidence=conf,
                verdict=_verdict(bias, conf, flags, cfg.gates),
                components=comps,
                contributions=contributions,
                flags=flags,
                price=d.price,
                options=d.options,
                plan=_plan(d, bias),
                expiry=d.expiry,
            )
        )
    out.sort(key=lambda s: abs(s.bias) * (s.confidence / 100.0), reverse=True)
    return out
