"""Deterministic synthetic market data.

Used for tests, for demoing the scanner outside market hours, and for sanity
checks: the generator takes an explicit drift so the expected sign of the
composite bias is known in advance.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .. import bs
from ..models import Bar, OptionQuote, SymbolSnapshot
from ..session import session_open, t_years_to_close, to_et
from .base import Provider

# symbol -> (reference price, session drift in %, annualized vol, skew in vol pts)
DEFAULT_SCENARIO: Dict[str, tuple] = {
    "SPY": (585.0, 0.55, 0.13, -2.0),
    "QQQ": (505.0, 0.85, 0.17, -1.5),
    "IWM": (222.0, -0.35, 0.20, -3.5),
    "DIA": (430.0, 0.10, 0.12, -2.5),
    "NVDA": (178.0, 1.90, 0.45, 1.5),
    "TSLA": (410.0, -1.60, 0.55, -5.0),
    "AAPL": (245.0, 0.20, 0.24, -1.0),
    "AMZN": (232.0, 0.60, 0.28, -1.2),
    "META": (742.0, -0.80, 0.30, -3.0),
    "MSFT": (512.0, 0.25, 0.22, -1.4),
    "AMD": (164.0, 1.20, 0.48, 0.5),
    "GOOGL": (248.0, 0.45, 0.26, -1.0),
}


class SyntheticProvider(Provider):
    name = "synthetic"

    def __init__(self, seed: int = 7, scenario: Optional[Dict[str, tuple]] = None):
        self.seed = seed
        self.scenario = scenario or DEFAULT_SCENARIO

    def _params(self, symbol: str) -> tuple:
        if symbol in self.scenario:
            return self.scenario[symbol]
        rng = random.Random(f"{self.seed}:{symbol}")
        return (
            rng.uniform(50, 600),
            rng.uniform(-1.5, 1.5),
            rng.uniform(0.18, 0.5),
            rng.uniform(-4.0, 1.0),
        )

    def snapshot(self, symbol: str, now: datetime) -> Optional[SymbolSnapshot]:
        now = to_et(now)
        ref, drift_pct, vol, skew_pts = self._params(symbol)
        rng = random.Random(f"{self.seed}:{symbol}:bars")

        open_ts = session_open(now.date())
        n_bars = max(1, int((now - open_ts).total_seconds() // 300))
        prev_close = round(ref, 2)
        gap = ref * rng.uniform(-0.002, 0.002)
        open_px = prev_close + gap

        bars: List[Bar] = []
        px = open_px
        per_bar_drift = (drift_pct / 100.0) * ref / max(n_bars, 1)
        bar_sigma = ref * vol * math.sqrt(5.0 / (252.0 * 390.0))
        base_vol = 400_000 if symbol in ("SPY", "QQQ") else 120_000
        for i in range(n_bars):
            o = px
            px = o + per_bar_drift + rng.gauss(0, bar_sigma)
            hi = max(o, px) + abs(rng.gauss(0, bar_sigma * 0.4))
            lo = min(o, px) - abs(rng.gauss(0, bar_sigma * 0.4))
            shares = base_vol * rng.uniform(0.6, 1.6) * (2.2 if i < 6 else 1.0)
            bars.append(
                Bar(open_ts + timedelta(minutes=5 * i), o, hi, lo, px, round(shares))
            )

        spot = round(px, 2)
        chain = self._chain(symbol, spot, t_years_to_close(now), vol, skew_pts, drift_pct, rng)
        return SymbolSnapshot(
            symbol=symbol,
            spot=spot,
            prev_close=prev_close,
            bars=bars,
            chain=chain,
            expiry=now.date().isoformat(),
            adr_pct=round(vol / math.sqrt(252) * 100 * 1.6, 3),
            avg_share_volume=base_vol * 78,
            notes=["synthetic"],
        )

    def _chain(
        self,
        symbol: str,
        spot: float,
        t: float,
        vol: float,
        skew_pts: float,
        drift_pct: float,
        rng: random.Random,
    ) -> List[OptionQuote]:
        step = 1.0 if spot < 100 else (5.0 if spot > 300 else 2.5)
        atm = round(spot / step) * step
        quotes: List[OptionQuote] = []
        # Intraday 0DTE ATM vol runs well under the annualized 20-day number.
        atm_iv = max(0.05, vol * 0.75)
        for k in range(-12, 13):
            strike = atm + k * step
            if strike <= 0:
                continue
            moneyness = (strike - spot) / spot
            for right in ("C", "P"):
                # Linear skew in moneyness, signed so negative skew_pts means
                # downside strikes carry the higher vol.
                iv = max(0.03, atm_iv - (skew_pts / 100.0) * (moneyness / 0.02) * 0.05)
                px = bs.price(spot, strike, t, iv, right)
                tick = 0.01 if px < 3 else 0.05
                mid = max(0.01, round(px / tick) * tick)
                half = max(tick, mid * 0.02)
                dist = abs(k)
                # Volume concentrates near the money and leans with the drift.
                lean = 1.0 + 0.5 * math.copysign(1.0, drift_pct) * (
                    1.0 if right == "C" else -1.0
                )
                vol_c = max(0.0, (3000.0 / (1 + dist ** 1.6)) * lean * rng.uniform(0.6, 1.4))
                oi = max(0.0, (9000.0 / (1 + dist ** 1.2)) * rng.uniform(0.5, 1.5))
                quotes.append(
                    OptionQuote(
                        strike=round(strike, 2),
                        right=right,
                        bid=round(max(0.0, mid - half), 2),
                        ask=round(mid + half, 2),
                        last=round(mid, 2),
                        volume=round(vol_c),
                        open_interest=round(oi),
                        iv=round(iv, 4),
                    )
                )
        return quotes
