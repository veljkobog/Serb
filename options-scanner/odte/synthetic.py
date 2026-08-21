"""Deterministic synthetic market data — powers ``scan.py --demo`` and the test suite.

Seeds come from ``zlib.crc32`` rather than ``hash()`` — Python randomises string
hashing per process, which would make these fixtures (and any test built on them)
silently non-reproducible between runs.
"""
from __future__ import annotations

import datetime as dt
import math
import random
import zlib
from typing import Dict, List, Optional

from .calendar_utils import recent_trading_days
from .providers.base import (Bar, Fundamentals, MarketDataProvider, OptionChain,
                             OptionContract, Quote)
from .providers.finra import FinraOffExchange, OffExDay


def _seed(symbol: str) -> int:
    return zlib.crc32(symbol.encode()) & 0xFFFF


def make_bars(symbol: str, days: int = 260, start: float = 100.0, drift: float = 0.0015,
              vol: float = 0.014, base_volume: float = 8_000_000.0,
              end: Optional[dt.date] = None) -> List[Bar]:
    rng = random.Random(_seed(symbol))
    dates = recent_trading_days(days, end)
    price = start
    bars: List[Bar] = []
    for i, d in enumerate(dates):
        shock = rng.gauss(0, 1)
        price = max(1.0, price * (1 + drift + vol * shock))
        rng_pct = abs(rng.gauss(0, 1)) * vol + 0.004
        high = price * (1 + rng_pct / 2)
        low = price * (1 - rng_pct / 2)
        open_ = low + (high - low) * rng.random()
        volume = base_volume * (0.7 + 0.6 * rng.random()) * (1.6 if i == len(dates) - 1 else 1.0)
        bars.append(Bar(d, open_, high, low, price, volume))
    return bars


def make_chain(symbol: str, expiry: dt.date, spot: float, *, skew: float = 1.0,
               spread: float = 0.02, base_oi: float = 4_000.0,
               base_volume: float = 3_000.0, greeks: bool = True) -> OptionChain:
    """A realistic-looking chain: OI peaks near the money, IV smiles, spreads widen OTM."""
    chain = OptionChain(underlying=symbol, expiry=expiry)
    # Real listed increments, so demo ladders pick realistic neighbouring strikes.
    step = 0.5 if spot < 25 else 1.0 if spot < 200 else 2.5
    atm = round(spot / step) * step
    for k in range(-16, 17):
        strike = round(atm + k * step, 2)
        if strike <= 0:
            continue
        moneyness = (strike - spot) / spot
        decay = math.exp(-((moneyness / 0.035) ** 2))
        # Volume concentrates at the money; open interest peaks out of the money, where
        # the call/put walls actually sit in a real chain.
        call_oi_decay = math.exp(-(((moneyness - 0.018) / 0.030) ** 2))
        put_oi_decay = math.exp(-(((moneyness + 0.018) / 0.030) ** 2))
        iv = 0.28 + 0.9 * moneyness ** 2 - 0.15 * moneyness
        for right in ("C", "P"):
            intrinsic = max(0.0, (spot - strike) if right == "C" else (strike - spot))
            extrinsic = max(0.02, 1.2 * spot * 0.01 * decay)
            mid = intrinsic + extrinsic
            width = max(0.01, mid * spread * (2.0 - decay))
            tilt = skew if right == "C" else 1.0 / skew
            delta = 0.5 + (0.5 * math.tanh(-moneyness / 0.02)) if right == "C" else \
                -(0.5 + 0.5 * math.tanh(moneyness / 0.02))
            chain.contracts.append(OptionContract(
                symbol=f"{symbol}{expiry:%y%m%d}{right}{int(strike*1000):08d}",
                underlying=symbol, expiry=expiry, strike=strike, right=right,
                bid=round(max(0.01, mid - width / 2), 2), ask=round(mid + width / 2, 2),
                last=round(mid, 2),
                volume=round(base_volume * decay * tilt),
                open_interest=round(base_oi * ((call_oi_decay if right == "C"
                                                else put_oi_decay) + 0.15)),
                iv=iv, delta=delta if greeks else None,
                gamma=(0.02 * decay / max(spot * 0.01, 0.01)) if greeks else None,
            ))
    return chain


class FakeProvider(MarketDataProvider):
    name = "fake"
    supports_greeks = True

    def __init__(self, symbols: List[str], today: dt.date, *, expiry_offset_days: int = 0,
                 market_cap: float = 500e9, spot_override: Optional[Dict[str, float]] = None,
                 skew: float = 1.6, spread: float = 0.02, chain_volume: float = 3_000.0,
                 drift: float = 0.0015, vol: float = 0.014, mixed: bool = False):
        self.symbols = symbols
        self.today = today
        self.expiry = today + dt.timedelta(days=expiry_offset_days)
        self.market_cap = market_cap
        self.spot_override = spot_override or {}
        self.skew = skew
        self.spread = spread
        self.chain_volume = chain_volume
        self.drift = drift
        self.vol = vol
        self.mixed = mixed
        self._bars: Dict[str, List[Bar]] = {}

    def _tilt(self, symbol: str) -> int:
        """Deterministic per-symbol bull/bear tilt, so a demo scan shows both sides."""
        return 1 if _seed(symbol) % 2 == 0 else -1

    def daily_bars(self, symbol: str, lookback: int = 260) -> List[Bar]:
        if symbol not in self._bars:
            drift = self.drift * (self._tilt(symbol) if self.mixed else 1)
            self._bars[symbol] = make_bars(symbol, lookback, drift=drift,
                                           vol=self.vol, end=self.today)
        return self._bars[symbol]

    def _spot(self, symbol: str) -> float:
        return self.spot_override.get(symbol, self.daily_bars(symbol)[-1].close)

    def quote(self, symbol: str) -> Quote:
        bars = self.daily_bars(symbol)
        last = self._spot(symbol)
        return Quote(symbol=symbol, last=last, prev_close=bars[-2].close,
                     day_open=bars[-1].open, day_high=bars[-1].high, day_low=bars[-1].low,
                     day_volume=bars[-1].volume)

    def fundamentals(self, symbol: str) -> Fundamentals:
        bars = self.daily_bars(symbol)
        shares = self.market_cap / bars[-1].close
        return Fundamentals(symbol=symbol, name=f"{symbol} Test Corp",
                            market_cap=self.market_cap, shares_out=shares,
                            float_shares=shares * 0.9, shares_short=shares * 0.08,
                            shares_short_prior=shares * 0.07, short_pct_float=0.089,
                            short_ratio=3.4, short_interest_date=self.today - dt.timedelta(days=9))

    def expirations(self, symbol: str) -> List[dt.date]:
        return [self.expiry, self.expiry + dt.timedelta(days=7)]

    def chain(self, symbol: str, expiry: dt.date) -> OptionChain:
        skew = self.skew if not self.mixed or self._tilt(symbol) > 0 else 1.0 / self.skew
        return make_chain(symbol, expiry, self._spot(symbol), skew=skew,
                          spread=self.spread, base_volume=self.chain_volume)


def make_offex(symbols: List[str], bars_by_symbol: Dict[str, List[Bar]],
               short_ratio: float = 0.38, share: float = 0.52,
               days: int = 20) -> FinraOffExchange:
    feed = FinraOffExchange(days=days)
    for symbol in symbols:
        rng = random.Random(_seed(symbol) + 7)
        rows = []
        for bar in bars_by_symbol[symbol][-days:]:
            total = bar.volume * (share + rng.uniform(-0.03, 0.03))
            rows.append(OffExDay(date=bar.date, short_volume=total * (short_ratio + rng.uniform(-0.02, 0.02)),
                                 short_exempt_volume=0.0, total_volume=total))
        feed.by_symbol[symbol] = rows
        feed.loaded_dates = [r.date for r in rows]
    return feed
