"""Polygon.io provider — paid, but the cleanest options snapshots (OI, greeks, IV, day volume).

Set ``POLYGON_API_KEY``. The Options Starter plan covers everything used here.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Optional

from ..calendar_utils import parse_date
from ..http import Http, HttpError
from .base import Bar, Fundamentals, MarketDataProvider, OptionChain, OptionContract, Quote

BASE = "https://api.polygon.io"


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


class PolygonProvider(MarketDataProvider):
    name = "polygon"
    supports_greeks = True

    def __init__(self, http: Optional[Http] = None, api_key: Optional[str] = None):
        self.key = api_key or os.environ.get("POLYGON_API_KEY", "")
        if not self.key:
            raise RuntimeError("POLYGON_API_KEY is not set")
        self.http = http or Http(min_interval=0.08)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, cache_ttl: int = 300) -> Any:
        params = dict(params or {})
        params["apiKey"] = self.key
        return self.http.get_json(f"{BASE}{path}", params=params, cache_ttl=cache_ttl)

    def daily_bars(self, symbol: str, lookback: int = 260) -> List[Bar]:
        end = dt.date.today()
        start = end - dt.timedelta(days=int(lookback * 1.6) + 10)
        data = self._get(f"/v2/aggs/ticker/{symbol}/range/1/day/{start}/{end}",
                         {"adjusted": "true", "sort": "asc", "limit": 50000}, 900)
        bars = []
        for r in data.get("results") or []:
            d = parse_date((r.get("t") or 0) / 1000.0)
            if d is None:
                continue
            bars.append(Bar(d, _f(r.get("o")) or 0.0, _f(r.get("h")) or 0.0, _f(r.get("l")) or 0.0,
                            _f(r.get("c")) or 0.0, _f(r.get("v")) or 0.0))
        return [b for b in bars if b.close > 0][-lookback:]

    def quote(self, symbol: str) -> Optional[Quote]:
        try:
            data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}", cache_ttl=30)
        except HttpError:
            return None
        t = data.get("ticker") or {}
        day, prev = t.get("day") or {}, t.get("prevDay") or {}
        last = _f(day.get("c")) or _f((t.get("lastTrade") or {}).get("p")) or _f(prev.get("c"))
        if last is None:
            return None
        return Quote(symbol=symbol, last=last, prev_close=_f(prev.get("c")), day_open=_f(day.get("o")),
                     day_high=_f(day.get("h")), day_low=_f(day.get("l")), day_volume=_f(day.get("v")))

    def fundamentals(self, symbol: str) -> Fundamentals:
        out = Fundamentals(symbol=symbol)
        try:
            data = self._get(f"/v3/reference/tickers/{symbol}", cache_ttl=86400)
        except HttpError:
            return out
        r = data.get("results") or {}
        out.name = r.get("name")
        out.market_cap = _f(r.get("market_cap"))
        out.shares_out = _f(r.get("weighted_shares_outstanding")) or _f(r.get("share_class_shares_outstanding"))
        out.is_etf = (r.get("type") or "").upper() in {"ETF", "ETN", "ETV", "INDEX"}
        return out

    def expirations(self, symbol: str) -> List[dt.date]:
        today = dt.date.today()
        data = self._get("/v3/reference/options/contracts",
                         {"underlying_ticker": symbol, "expired": "false",
                          "expiration_date.gte": today.isoformat(),
                          "expiration_date.lte": (today + dt.timedelta(days=10)).isoformat(),
                          "limit": 1000}, 3600)
        seen = {parse_date(r.get("expiration_date")) for r in (data.get("results") or [])}
        return sorted(d for d in seen if d)

    def chain(self, symbol: str, expiry: dt.date) -> Optional[OptionChain]:
        chain = OptionChain(underlying=symbol, expiry=expiry)
        url = f"/v3/snapshot/options/{symbol}"
        params: Dict[str, Any] = {"expiration_date": expiry.isoformat(), "limit": 250}
        for _ in range(8):  # paginate
            try:
                data = self._get(url, params, 120)
            except HttpError:
                break
            for r in data.get("results") or []:
                det = r.get("details") or {}
                day = r.get("day") or {}
                greeks = r.get("greeks") or {}
                quote = r.get("last_quote") or {}
                strike = _f(det.get("strike_price"))
                if strike is None:
                    continue
                chain.contracts.append(OptionContract(
                    symbol=det.get("ticker") or "", underlying=symbol,
                    expiry=parse_date(det.get("expiration_date")) or expiry, strike=strike,
                    right="C" if (det.get("contract_type") or "").lower().startswith("c") else "P",
                    bid=_f(quote.get("bid")), ask=_f(quote.get("ask")),
                    last=_f((r.get("last_trade") or {}).get("price")) or _f(day.get("close")),
                    volume=_f(day.get("volume")) or 0.0,
                    open_interest=_f(r.get("open_interest")) or 0.0,
                    iv=_f(r.get("implied_volatility")),
                    delta=_f(greeks.get("delta")), gamma=_f(greeks.get("gamma")),
                ))
            nxt = data.get("next_url")
            if not nxt:
                break
            url, params = nxt.replace(BASE, ""), {}
        return chain if chain.contracts else None
