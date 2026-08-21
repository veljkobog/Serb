"""Turn a ranked candidate into a concrete, checkable trade plan.

Strike selection favours a contract that still has real delta but is not so far out
that a normal expected move cannot reach it, and it will only pick a contract that
actually has a tight two-sided quote and open interest.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import Config
from .providers.base import OptionChain, OptionContract
from .score import LONG, SHORT, Candidate

TARGET_DELTA = 0.40


def _viable(chain: OptionChain, right: str, config: Config) -> List[OptionContract]:
    out = []
    for c in chain.contracts:
        if c.right != right or c.mid is None or c.mid <= 0.02:
            continue
        if c.open_interest < 100 and c.volume < 250:
            continue
        if c.spread_pct is not None and c.spread_pct > config.gates.max_atm_spread_pct * 1.5:
            continue
        out.append(c)
    return out


def _pick(contracts: List[OptionContract], spot: float, sign: int,
          expected_move: Optional[float]) -> Optional[OptionContract]:
    if not contracts:
        return None
    with_delta = [c for c in contracts if c.delta is not None and abs(c.delta) > 0.01]
    if with_delta:
        return min(with_delta, key=lambda c: abs(abs(c.delta) - TARGET_DELTA))
    # No greeks: aim a quarter of the expected move out of the money.
    offset = 0.25 * (expected_move or spot * 0.005)
    target = spot + sign * offset
    return min(contracts, key=lambda c: abs(c.strike - target))


def _round(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def build(candidate: Candidate, chain: Optional[OptionChain], config: Config) -> Dict[str, Any]:
    if candidate.side not in (LONG, SHORT) or chain is None:
        return {}

    sign = 1 if candidate.side == LONG else -1
    right = "C" if sign > 0 else "P"
    spot = candidate.spot
    trend = candidate.blocks.get("trend")
    opt = candidate.blocks.get("options")
    atr = (trend.detail.get("atr14") if trend else None) or spot * 0.01
    em = (opt.detail.get("expected_move_dollars") if opt else None) or atr * 0.6
    ema8 = trend.detail.get("ema8") if trend else None
    ema21 = trend.detail.get("ema21") if trend else None
    call_wall = opt.detail.get("call_wall") if opt else None
    put_wall = opt.detail.get("put_wall") if opt else None

    contract = _pick(_viable(chain, right, config), spot, sign, em)

    # Invalidation: the nearer of a half-ATR against you and the fast moving average.
    structural = ema8 if (ema8 and (spot - ema8) * sign > 0) else ema21
    atr_stop = spot - sign * 0.5 * atr
    if structural and (structural - atr_stop) * sign > 0:
        stop = structural
    else:
        stop = atr_stop
    # A moving average can sit on the wrong side of spot (price under both EMAs on a
    # long). A stop there is not a stop, so fall back to the ATR level.
    if (spot - stop) * sign <= 0:
        stop = atr_stop

    t1 = spot + sign * 0.5 * em
    t2 = spot + sign * 1.0 * em

    # An OI wall ahead of price is a magnet that caps the move. Only a wall that is
    # actually ahead of us counts — one at or behind spot is a level we have already
    # traded through, and using it would put a target on the wrong side of the entry.
    wall = call_wall if sign > 0 else put_wall
    wall_ahead = bool(wall) and (wall - spot) * sign > 0
    capped = False
    wall_inside_em = False
    t2_opt: Optional[float] = t2
    if wall_ahead:
        if (t2 - wall) * sign > 0:
            t2_opt, capped = wall, True
        if (t1 - t2_opt) * sign > 0:
            # The wall is nearer than the first target: there is one target, not two.
            t1, t2_opt = t2_opt, None
        wall_inside_em = abs(wall - spot) < 0.5 * em

    premium = contract.mid if contract else None
    plan: Dict[str, Any] = {
        "side": candidate.side,
        "expiry": str(chain.expiry),
        "dte": candidate.dte,
        "contract": contract.symbol if contract else None,
        "strike": contract.strike if contract else None,
        "right": right,
        "premium_mid": _round(premium),
        "delta": _round(contract.delta, 3) if contract else None,
        "bid": _round(contract.bid) if contract else None,
        "ask": _round(contract.ask) if contract else None,
        "open_interest": contract.open_interest if contract else None,
        "contract_volume": contract.volume if contract else None,
        "spread_pct": _round(contract.spread_pct, 4) if contract else None,
        "entry_trigger": _round(spot + sign * 0.1 * atr),
        "underlying_stop": _round(stop),
        "target_1": _round(t1),
        "target_2": _round(t2_opt),
        "target_2_capped_at_wall": capped,
        "expected_move": _round(em),
        "atr14": _round(atr),
        "premium_stop_pct": config.premium_stop_pct,
        "premium_stop": _round(premium * (1 - config.premium_stop_pct)) if premium else None,
        "risk_per_contract": _round(premium * 100 * config.premium_stop_pct) if premium else None,
    }

    notes: List[str] = []
    if candidate.dte == 0:
        notes.append("0DTE: theta accelerates hard after ~14:00 ET — take the trade in "
                     "the first half of the session or not at all.")
    else:
        notes.append("1DTE: overnight gap risk is the whole trade; size for a gap through "
                     "your stop, not to it.")
    if capped:
        notes.append(f"Target 2 is capped at the {wall:g} OI wall — that strike is a magnet, "
                     "not a level to trade through.")
    if wall_inside_em:
        notes.append(f"The {wall:g} wall sits inside one expected move — there is less room "
                     "than the premium implies; a debit spread to that strike fits better "
                     "than a naked long.")
    elif wall and not wall_ahead:
        notes.append(f"Price is already through the {wall:g} OI wall — that level flips to "
                     "support/resistance behind the trade.")
    em_vs_atr = opt.value("em_vs_atr") if opt else None
    if em_vs_atr and em_vs_atr > 1.3:
        notes.append("Premium is rich vs realised range — prefer a debit spread over a naked long.")
    if opt and (opt.detail.get("atm_spread") or 0) > 0.06:
        notes.append("Quotes are wide: work the mid with a limit, never market in.")
    if candidate.flags:
        notes.append("Flags: " + ", ".join(candidate.flags))
    plan["notes"] = notes

    if premium:
        risk = premium * 100 * config.premium_stop_pct
        plan["sizing"] = {
            "formula": "contracts = (account x risk_per_trade_pct) / (premium x 100 x premium_stop_pct)",
            "risk_per_trade_pct": config.risk_per_trade_pct,
            "risk_per_contract": _round(risk),
            "example_25k": max(1, int((25_000 * config.risk_per_trade_pct / 100.0) // risk)) if risk > 0 else None,
        }
    return plan
