"""Universe, weights and gates. Every number here is meant to be tuned."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

MARKET_TZ = "America/New_York"
OPEN_TIME = (9, 30)
CLOSE_TIME = (16, 0)

# Index / index-proxy ETFs. These carry the deepest 0DTE books and expire daily.
INDEX_UNIVERSE: List[str] = ["SPY", "QQQ", "IWM", "DIA"]

# Heavy-volume single names that list same-day expiries on M/W/F (and daily for
# the largest). Liquidity gates below drop whatever is thin on the day.
EQUITY_UNIVERSE: List[str] = [
    "NVDA", "TSLA", "AAPL", "AMZN", "META", "MSFT", "AMD", "GOOGL",
    "NFLX", "AVGO", "COIN", "PLTR", "MU", "MSTR", "SMCI",
]

BENCHMARK = "SPY"  # relative strength is measured against this

# --- session windows ---------------------------------------------------------
# The scan is built for the post-opening-range read: the first 30 minutes set
# the range, and by 10:30 the 0DTE book has real volume instead of stale OI.
OPENING_RANGE_MINUTES = 30
SCAN_WINDOW_ET = ((10, 0), (11, 30))  # advisory only; --force overrides

# --- liquidity gates ---------------------------------------------------------
@dataclass
class Gates:
    min_chain_volume: float = 2_000      # total 0DTE contracts traded, both sides
    min_atm_volume: float = 250          # traded contracts within +/-1 EM of spot
    max_atm_spread_pct: float = 0.12     # avg ATM bid/ask as a fraction of mid
    min_share_volume: float = 1_000_000  # shares traded in the session so far
    min_bias_to_trade: float = 25.0      # |bias| under this reads as NO TRADE
    min_confidence_to_trade: float = 45.0


# --- component weights ------------------------------------------------------
# Negative component score = downside bias. Weights sum to 1.0 so the composite
# is a clean -100..+100 scale. Tape ~42%, options ~58%.
@dataclass
class Weights:
    """Signed components each land in [-1, +1]; weights sum to 1.0."""

    rs: float = 0.17           # beta-adjusted relative strength vs benchmark
    vwap: float = 0.11         # price vs session VWAP, plus VWAP slope
    orb: float = 0.08          # position vs the opening range
    persistence: float = 0.06  # how cleanly the move is holding
    skew: float = 0.13         # 25-delta risk reversal (call IV - put IV)
    flow: float = 0.16         # delta-weighted 0DTE premium, calls vs puts
    pcr: float = 0.07          # put/call volume ratio
    oi_tilt: float = 0.05      # standing OI above vs below spot
    gamma_pull: float = 0.11   # spot vs max pain / gamma wall
    stretch: float = 0.06      # realized vs implied move (counter-trend brake)

    def as_dict(self) -> Dict[str, float]:
        return dict(self.__dict__)

    def total_abs(self) -> float:
        return sum(abs(v) for v in self.__dict__.values())


@dataclass
class ScanConfig:
    symbols: List[str] = field(
        default_factory=lambda: INDEX_UNIVERSE + EQUITY_UNIVERSE
    )
    benchmark: str = BENCHMARK
    bar_interval: str = "5m"
    weights: Weights = field(default_factory=Weights)
    gates: Gates = field(default_factory=Gates)
    risk_free: float = 0.04
    # Strikes within this many expected moves of spot are treated as the
    # "relevant" part of the chain for flow, skew and spread measurement.
    chain_em_window: float = 2.0
