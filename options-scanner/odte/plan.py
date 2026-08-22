"""Turn a ranked candidate into a concrete, checkable trade plan.

Produces a three-rung strike ladder rather than a single strike, because "which
contract" is a risk decision the scanner should not make for you:

  ANCHOR   ~0.60 delta, in the money.  Costs the most, decays the least, moves most
                                       like the stock. The measured-move trade.
  CORE     ~0.45 delta, at the money.  The default. Best liquidity, balanced payoff.
  RUNNER   ~0.30 delta, out of money.  Cheapest and highest percentage upside; needs
                                       most of the expected move to pay, and is the
                                       one that goes to zero.

Every rung only comes from strikes with a real two-sided quote and real open
interest, and each carries its own breakeven, percentage move required, expiry
payoff at both targets, and position sizing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import Config
from .session import horizon_notes
from .providers.base import OptionChain, OptionContract
from .score import LONG, SHORT, Candidate

TARGET_DELTA = 0.45

# (label, target delta, offset from spot in expected-move units when greeks are absent)
RUNGS = (
    ("anchor", 0.60, -0.35),
    ("core", 0.45, 0.00),
    ("runner", 0.30, 0.45),
)


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


def _expiry_value(contract: OptionContract, underlying: float) -> float:
    """What the contract is worth at expiry if the stock is at ``underlying``.

    For 0DTE this is the honest number: every cent of extrinsic value is gone by the
    close, so the payoff is pure intrinsic.
    """
    if contract.right == "C":
        return max(0.0, underlying - contract.strike)
    return max(0.0, contract.strike - underlying)


def _rung_economics(contract: OptionContract, spot: float, sign: int, targets: List[float],
                    config: Config, label: str) -> Dict[str, Any]:
    # Round the premium once and derive everything from it, so the numbers on the card
    # are arithmetically consistent with each other (a displayed breakeven that does not
    # equal displayed strike + displayed mid reads as a bug, and is one).
    premium = round(contract.mid or 0.0, 2)
    cost = premium * 100.0
    breakeven = contract.strike + premium if sign > 0 else contract.strike - premium
    move_needed = ((breakeven - spot) / spot * 100.0) * sign if spot else None

    outcomes = []
    for i, target in enumerate(targets, 1):
        if target is None:
            continue
        value = _expiry_value(contract, target)
        pnl = (value - premium) * 100.0
        row = {
            "target": f"T{i}",
            "underlying": _round(target),
            "value_at_expiry": _round(value),
            "pnl_per_contract": _round(pnl),
            "return_pct": _round((value / premium - 1.0) * 100.0, 1) if premium > 0 else None,
        }
        # With greeks we can also say what it is worth if it gets there *now*, before
        # theta has eaten the extrinsic value. That number is always the friendlier one.
        if contract.delta is not None:
            move = target - spot
            est = premium + contract.delta * move + 0.5 * (contract.gamma or 0.0) * move * move
            row["value_if_immediate"] = _round(max(0.0, est))
        outcomes.append(row)

    risk_per_contract = cost * config.premium_stop_pct
    budget = config.account_size * config.risk_per_trade_pct / 100.0
    qty = int(budget // risk_per_contract) if risk_per_contract > 0 else 0

    return {
        "rung": label,
        "contract": contract.symbol,
        "strike": contract.strike,
        "right": contract.right,
        "moneyness": "ITM" if (contract.strike - spot) * sign < 0 else
                     ("ATM" if abs(contract.strike - spot) < 1e-9 else "OTM"),
        "bid": _round(contract.bid),
        "ask": _round(contract.ask),
        "mid": _round(premium),
        "spread_pct": _round(contract.spread_pct, 4),
        "delta": _round(contract.delta, 3),
        "gamma": _round(contract.gamma, 5),
        "iv": _round(contract.iv, 4),
        "open_interest": contract.open_interest,
        "volume": contract.volume,
        "vol_oi": _round(contract.volume / contract.open_interest, 2) if contract.open_interest else None,
        "cost_per_contract": _round(cost),
        "breakeven": _round(breakeven),
        "pct_move_to_breakeven": _round(move_needed, 2),
        "premium_stop": _round(premium * (1 - config.premium_stop_pct)),
        "risk_per_contract": _round(risk_per_contract),
        "suggested_contracts": max(qty, 0),
        "total_cost": _round(cost * qty) if qty else None,
        "total_risk": _round(risk_per_contract * qty) if qty else None,
        "outcomes": outcomes,
    }


def _ladder(contracts: List[OptionContract], spot: float, sign: int,
            expected_move: Optional[float], targets: List[float],
            config: Config) -> List[Dict[str, Any]]:
    """Pick three distinct strikes spanning ITM / ATM / OTM."""
    if not contracts:
        return []
    em = expected_move or spot * 0.005
    has_greeks = any(c.delta is not None and abs(c.delta) > 0.01 for c in contracts)
    pool = sorted(contracts, key=lambda c: c.strike)
    rungs: List[Dict[str, Any]] = []
    used: set = set()

    for label, target_delta, em_offset in RUNGS:
        available = [c for c in pool if c.strike not in used]
        if not available:
            break
        if has_greeks:
            pick = min(available, key=lambda c: abs(abs(c.delta or 0.0) - target_delta))
        else:
            want = spot + sign * em_offset * em
            pick = min(available, key=lambda c: abs(c.strike - want))
        used.add(pick.strike)
        rungs.append(_rung_economics(pick, spot, sign, targets, config, label))

    # Keep the ladder ordered ITM -> OTM regardless of how the picks landed.
    rungs.sort(key=lambda r: r["strike"] * sign)
    return rungs


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

    viable = _viable(chain, right, config)

    # Invalidation: the nearer of a half-ATR against you and the fast moving average.
    # On a same-day trade the session VWAP is the line that decides control, so when
    # it sits between price and the moving average it is the better invalidation.
    intra = candidate.blocks.get("intraday")
    vwap = intra.detail.get("vwap") if (intra and intra.available) else None
    structural = ema8 if (ema8 and (spot - ema8) * sign > 0) else ema21
    if vwap and (spot - vwap) * sign > 0:
        if structural is None or (vwap - structural) * sign > 0:
            structural = vwap
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

    targets = [t for t in (t1, t2_opt) if t is not None]
    ladder = _ladder(viable, spot, sign, em, targets, config)
    # The primary contract is the middle rung — the balanced default.
    core = next((r for r in ladder if r["rung"] == "core"), ladder[len(ladder) // 2] if ladder else None)
    contract = next((c for c in viable if core and c.symbol == core["contract"]), None)
    if contract is None:
        contract = _pick(viable, spot, sign, em)

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
        "vwap": _round(vwap),
        "target_1": _round(t1),
        "target_2": _round(t2_opt),
        "target_2_capped_at_wall": capped,
        "ladder": ladder,
        "expected_move": _round(em),
        "atr14": _round(atr),
        "premium_stop_pct": config.premium_stop_pct,
        "premium_stop": _round(premium * (1 - config.premium_stop_pct)) if premium else None,
        "risk_per_contract": _round(premium * 100 * config.premium_stop_pct) if premium else None,
    }

    notes: List[str] = []
    if vwap and candidate.dte <= 1:
        side_word = "above" if sign > 0 else "below"
        notes.append(f"Session VWAP {vwap:,.2f} — the trade is only valid while price "
                     f"holds {side_word} it.")
    notes.extend(horizon_notes(candidate.dte))
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
