"""Scanner configuration: liquidity gates, signal weights, and output options."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Gates:
    """Hard filters. Anything that fails one of these is dropped, whatever it scores.

    These are the "no bullshit tiny caps" rules — tune them, but tune them knowingly.
    """
    min_price: float = 15.0
    max_price: float = 2000.0
    min_market_cap: float = 2_000_000_000.0        # $2B floor on the underlying
    min_avg_dollar_volume: float = 100_000_000.0   # $100M/day in the stock
    min_avg_share_volume: float = 1_000_000.0
    min_option_volume: float = 3_000.0             # contracts on the 0/1DTE expiry, full day
    min_option_open_interest: float = 2_000.0
    # Near the open the day's option volume is almost zero, so a flat contract-volume
    # gate rejects everything at 9:35. The requirement is scaled by how much of the
    # session has actually elapsed, with a floor so a 9:31 scan still demands a pulse.
    scale_option_volume_by_session: bool = True
    min_option_volume_floor: float = 0.04          # never require less than 4% of the target
    max_atm_spread_pct: float = 0.12               # 12% of mid, ATM
    min_tradable_strikes: int = 4
    max_dte: int = 1                               # 0 = same-day only, 1 = include next session
    allow_etf: bool = True
    require_darkpool: bool = False                 # drop names with no FINRA record
    exclude_earnings_today: bool = False           # flagged by default, not excluded
    min_score: float = 45.0


@dataclass
class Weights:
    """Block weights. ``direction`` decides the side; ``quality`` decides conviction."""
    direction: Dict[str, float] = field(default_factory=lambda: {
        "trend": 0.30, "volume": 0.20, "darkpool": 0.25, "options": 0.25,
    })
    quality: Dict[str, float] = field(default_factory=lambda: {
        "trend": 0.15, "volume": 0.25, "darkpool": 0.10, "options": 0.50,
    })
    squeeze_bonus: float = 8.0        # max points added to a long from short-interest fuel
    confluence_floor: float = 0.40    # score floor when blocks disagree on direction


@dataclass
class Config:
    provider: str = "yahoo"
    universe: Optional[List[str]] = None
    universe_file: Optional[str] = None
    lookback_days: int = 260
    darkpool_days: int = 25
    workers: int = 8
    top: int = 20
    gates: Gates = field(default_factory=Gates)
    weights: Weights = field(default_factory=Weights)
    cache_dir: str = ".cache"
    force_fresh: bool = False         # Scan button: bypass the cache for live quotes
    out_dir: str = "out"
    journal: bool = True
    band_pct: float = 0.06            # near-money window used for flow and liquidity
    min_unusual_volume: float = 500.0
    premium_stop_pct: float = 0.35    # suggested option-premium stop in the trade plan
    risk_per_trade_pct: float = 1.0   # % of account risked per idea, used for sizing math
    account_size: float = 25_000.0    # only used to turn risk % into a contract count

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg = cls()
        if not path:
            return cfg
        if not os.path.exists(path):
            raise FileNotFoundError(f"config file not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cfg.merge(raw)

    def merge(self, raw: Dict[str, Any]) -> "Config":
        for key, value in (raw or {}).items():
            if key == "gates" and isinstance(value, dict):
                for gk, gv in value.items():
                    if hasattr(self.gates, gk):
                        setattr(self.gates, gk, gv)
            elif key == "weights" and isinstance(value, dict):
                for wk, wv in value.items():
                    if wk in ("direction", "quality") and isinstance(wv, dict):
                        getattr(self.weights, wk).update(wv)
                    elif hasattr(self.weights, wk):
                        setattr(self.weights, wk, wv)
            elif hasattr(self, key):
                setattr(self, key, value)
        return self

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)
