"""Provider registry.

``resolve()`` returns the requested market-data provider. Tradier and Polygon are
better at options but thin on float/short-interest, so they are wrapped to fall back
to Yahoo for fundamentals — the scanner always gets a full record.
"""
from __future__ import annotations

from typing import Optional

from ..http import Http
from .base import (Bar, Fundamentals, IntradayBar, MarketDataProvider, OptionChain,
                   OptionContract, Quote)
from .finra import FinraOffExchange, OffExDay

__all__ = [
    "Bar", "Fundamentals", "IntradayBar", "MarketDataProvider", "OptionChain",
    "OptionContract", "Quote",
    "FinraOffExchange", "OffExDay", "resolve", "PROVIDERS",
]

PROVIDERS = ("yahoo", "tradier", "polygon")


class _FundamentalsFallback(MarketDataProvider):
    """Delegates everything to ``primary`` except fundamentals, which come from Yahoo."""

    def __init__(self, primary: MarketDataProvider, fallback: MarketDataProvider):
        self.primary = primary
        self.fallback = fallback
        self.name = primary.name
        self.supports_greeks = primary.supports_greeks

    def daily_bars(self, symbol, lookback=260):
        return self.primary.daily_bars(symbol, lookback)

    def intraday_bars(self, symbol, interval="5m"):
        return self.primary.intraday_bars(symbol, interval)

    def quote(self, symbol):
        return self.primary.quote(symbol)

    def expirations(self, symbol):
        return self.primary.expirations(symbol)

    def chain(self, symbol, expiry):
        return self.primary.chain(symbol, expiry)

    def fundamentals(self, symbol) -> Fundamentals:
        base = self.primary.fundamentals(symbol)
        try:
            extra = self.fallback.fundamentals(symbol)
        except Exception:
            return base
        for field in ("name", "market_cap", "shares_out", "float_shares", "shares_short",
                      "shares_short_prior", "short_pct_float", "short_ratio",
                      "short_interest_date", "earnings_date"):
            if getattr(base, field, None) in (None, 0):
                setattr(base, field, getattr(extra, field, None))
        base.is_etf = base.is_etf or extra.is_etf
        return base


def resolve(name: str = "yahoo", http: Optional[Http] = None) -> MarketDataProvider:
    name = (name or "yahoo").lower()
    from .yahoo import YahooProvider

    if name == "yahoo":
        return YahooProvider(http=http)
    if name == "tradier":
        from .tradier import TradierProvider
        return _FundamentalsFallback(TradierProvider(http=http), YahooProvider())
    if name == "polygon":
        from .polygon import PolygonProvider
        return _FundamentalsFallback(PolygonProvider(http=http), YahooProvider())
    raise ValueError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
