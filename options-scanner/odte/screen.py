"""Hard liquidity and size gates — applied before anything is allowed to rank."""
from __future__ import annotations

import datetime as dt
from typing import List, Optional

from .config import Config
from .providers.base import Fundamentals, OptionChain
from .score import Candidate


def _fmt_money(x: float) -> str:
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x/div:.1f}{unit}"
    return f"${x:.0f}"


def required_option_volume(config: Config, session_progress: float) -> float:
    """The contract-volume bar for this moment in the session.

    Option volume accumulates through the day, so comparing 9:35 volume against a
    full-day threshold rejects every name near the open — exactly when a 0DTE scan is
    most useful. Scale the requirement by the share of the session's volume that has
    typically printed by now, with a floor so a pre-open scan still demands *some*
    interest rather than none.
    """
    g = config.gates
    if not g.scale_option_volume_by_session:
        return g.min_option_volume
    from .signals.volume import expected_volume_fraction
    share = max(expected_volume_fraction(session_progress), g.min_option_volume_floor)
    return g.min_option_volume * share


def apply(candidate: Candidate, fundamentals: Optional[Fundamentals],
          chain: Optional[OptionChain], config: Config,
          today: Optional[dt.date] = None, session_progress: float = 1.0) -> List[str]:
    """Return the list of failed gates (empty means the name is tradable)."""
    g = config.gates
    fails: List[str] = []
    spot = candidate.spot

    if spot < g.min_price:
        fails.append(f"price ${spot:.2f} < ${g.min_price:.0f}")
    if spot > g.max_price:
        fails.append(f"price ${spot:.2f} > ${g.max_price:.0f}")

    is_etf = bool(fundamentals and fundamentals.is_etf)
    if is_etf and not g.allow_etf:
        fails.append("ETF excluded")

    # Market cap is meaningless for an ETF, so only enforce it on operating companies.
    if not is_etf:
        cap = fundamentals.market_cap if fundamentals else None
        if cap is None:
            fails.append("no market cap available")
        elif cap < g.min_market_cap:
            fails.append(f"market cap {_fmt_money(cap)} < {_fmt_money(g.min_market_cap)}")

    vol_block = candidate.blocks.get("volume")
    if vol_block and vol_block.available:
        adv = vol_block.detail.get("avg20_volume") or 0.0
        dollar = vol_block.detail.get("dollar_volume") or 0.0
        if dollar < g.min_avg_dollar_volume:
            fails.append(f"${dollar/1e6:.0f}M/day < ${g.min_avg_dollar_volume/1e6:.0f}M")
        if adv < g.min_avg_share_volume:
            fails.append(f"{adv/1e6:.2f}M shares/day < {g.min_avg_share_volume/1e6:.2f}M")
    else:
        fails.append("insufficient price history")

    opt = candidate.blocks.get("options")
    if not opt or not opt.available or chain is None:
        fails.append("no near-dated option chain")
    else:
        d = opt.detail
        needed = required_option_volume(config, session_progress)
        if (d.get("total_volume") or 0) < needed:
            fails.append(f"{d.get('total_volume', 0):,.0f} contracts < {needed:,.0f} "
                         f"(session-adjusted)")
        if (d.get("open_interest") or 0) < g.min_option_open_interest:
            fails.append(f"{d.get('open_interest', 0):,.0f} OI < {g.min_option_open_interest:,.0f}")
        spread = d.get("atm_spread")
        if spread is None:
            fails.append("no two-sided ATM quote")
        elif spread > g.max_atm_spread_pct:
            fails.append(f"ATM spread {spread*100:.1f}% > {g.max_atm_spread_pct*100:.0f}%")
        if (d.get("tradable_strikes") or 0) < g.min_tradable_strikes:
            fails.append(f"only {d.get('tradable_strikes', 0)} liquid strikes")

    dp = candidate.blocks.get("darkpool")
    if g.require_darkpool and (not dp or not dp.available):
        fails.append("no off-exchange data")

    if fundamentals and fundamentals.earnings_date and today:
        if fundamentals.earnings_date <= today + dt.timedelta(days=1):
            candidate.flags.append(f"earnings {fundamentals.earnings_date}")
            if g.exclude_earnings_today:
                fails.append(f"earnings on {fundamentals.earnings_date}")
    return fails
