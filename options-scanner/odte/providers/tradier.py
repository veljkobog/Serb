"""Tradier provider — the best free-tier option chain (real OI, greeks, IV, tight quotes).

Get a token at https://developer.tradier.com (sandbox is free; a funded brokerage
account gets you real-time data). Set ``TRADIER_TOKEN`` and, for sandbox,
``TRADIER_BASE=https://sandbox.tradier.com``.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any, Dict, List, Optional

from ..calendar_utils import ET, parse_date
from ..http import Http
from .base import (Bar, Fundamentals, IntradayBar, MarketDataProvider, OptionChain,
                   OptionContract, Quote)

DEFAULT_BASE = "https://api.tradier.com"


def _f(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v


def _listify(node: Any, key: str) -> List[Dict[str, Any]]:
    """Tradier collapses single-element arrays into an object."""
    if not node or node in ("null", "nil"):
        return []
    inner = node.get(key) if isinstance(node, dict) else node
    if inner is None:
        return []
    return inner if isinstance(inner, list) else [inner]


class TradierProvider(MarketDataProvider):
    name = "tradier"
    supports_greeks = True

    def __init__(self, http: Optional[Http] = None, token: Optional[str] = None, base: Optional[str] = None):
        self.token = token or os.environ.get("TRADIER_TOKEN", "")
        if not self.token:
            raise RuntimeError("TRADIER_TOKEN is not set")
        self.base = (base or os.environ.get("TRADIER_BASE") or DEFAULT_BASE).rstrip("/")
        self.http = http or Http(min_interval=0.12)

    def _get(self, path: str, params: Dict[str, Any], cache_ttl: int) -> Any:
        return self.http.get_json(
            f"{self.base}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            cache_ttl=cache_ttl,
        )

    def daily_bars(self, symbol: str, lookback: int = 260) -> List[Bar]:
        end = dt.date.today()
        start = end - dt.timedelta(days=int(lookback * 1.6) + 10)
        data = self._get("/v1/markets/history",
                         {"symbol": symbol, "interval": "daily",
                          "start": start.isoformat(), "end": end.isoformat()}, 900)
        bars = []
        for raw in _listify(data.get("history"), "day"):
            d = parse_date(raw.get("date"))
            if d is None:
                continue
            bars.append(Bar(d, _f(raw.get("open")) or 0.0, _f(raw.get("high")) or 0.0,
                            _f(raw.get("low")) or 0.0, _f(raw.get("close")) or 0.0,
                            _f(raw.get("volume")) or 0.0))
        return [b for b in bars if b.close > 0][-lookback:]

    def intraday_bars(self, symbol: str, interval: str = "5m") -> List[IntradayBar]:
        today = dt.date.today().isoformat()
        step = {"1m": "1min", "5m": "5min", "15m": "15min"}.get(interval, "5min")
        data = self._get("/v1/markets/timesales",
                         {"symbol": symbol, "interval": step, "start": f"{today} 09:30",
                          "end": f"{today} 16:00", "session_filter": "open"}, 60)
        out: List[IntradayBar] = []
        for raw in _listify(data.get("series"), "data"):
            stamp = raw.get("time") or raw.get("timestamp")
            try:
                ts = (dt.datetime.fromisoformat(str(stamp)).replace(tzinfo=ET)
                      if not isinstance(stamp, (int, float))
                      else dt.datetime.fromtimestamp(float(stamp), tz=ET))
            except (TypeError, ValueError):
                continue
            out.append(IntradayBar(ts, _f(raw.get("open")) or 0.0, _f(raw.get("high")) or 0.0,
                                   _f(raw.get("low")) or 0.0, _f(raw.get("close")) or 0.0,
                                   _f(raw.get("volume")) or 0.0))
        return [b for b in out if b.close > 0]

    def quote(self, symbol: str) -> Optional[Quote]:
        data = self._get("/v1/markets/quotes", {"symbols": symbol, "greeks": "false"}, 30)
        rows = _listify(data.get("quotes"), "quote")
        if not rows:
            return None
        r = rows[0]
        last = _f(r.get("last")) or _f(r.get("close"))
        if last is None:
            return None
        return Quote(symbol=symbol, last=last, prev_close=_f(r.get("prevclose")),
                     day_open=_f(r.get("open")), day_high=_f(r.get("high")), day_low=_f(r.get("low")),
                     day_volume=_f(r.get("volume")), bid=_f(r.get("bid")), ask=_f(r.get("ask")))

    def fundamentals(self, symbol: str) -> Fundamentals:
        # Tradier's fundamentals endpoints are a paid add-on; the scanner falls back
        # to the Yahoo provider for cap / float / short interest (see providers.resolve).
        data = self._get("/v1/markets/quotes", {"symbols": symbol}, 3600)
        rows = _listify(data.get("quotes"), "quote")
        kind = (rows[0].get("type") if rows else "") or ""
        return Fundamentals(symbol=symbol, name=(rows[0].get("description") if rows else None),
                            is_etf=kind.lower() in {"etf", "index"})

    def expirations(self, symbol: str) -> List[dt.date]:
        data = self._get("/v1/markets/options/expirations",
                         {"symbol": symbol, "includeAllRoots": "true", "strikes": "false"}, 3600)
        node = data.get("expirations")
        raw = _listify(node, "date") if isinstance(node, dict) else []
        out = [parse_date(x) for x in raw]
        return sorted(d for d in out if d)

    def chain(self, symbol: str, expiry: dt.date) -> Optional[OptionChain]:
        data = self._get("/v1/markets/options/chains",
                         {"symbol": symbol, "expiration": expiry.isoformat(), "greeks": "true"}, 120)
        rows = _listify(data.get("options"), "option")
        if not rows:
            return None
        chain = OptionChain(underlying=symbol, expiry=expiry)
        for r in rows:
            greeks = r.get("greeks") or {}
            right = "C" if (r.get("option_type") or "").lower().startswith("c") else "P"
            strike = _f(r.get("strike"))
            if strike is None:
                continue
            chain.contracts.append(OptionContract(
                symbol=r.get("symbol") or "", underlying=symbol,
                expiry=parse_date(r.get("expiration_date")) or expiry,
                strike=strike, right=right,
                bid=_f(r.get("bid")), ask=_f(r.get("ask")), last=_f(r.get("last")),
                volume=_f(r.get("volume")) or 0.0,
                open_interest=_f(r.get("open_interest")) or 0.0,
                iv=_f(greeks.get("mid_iv")) or _f(greeks.get("smv_vol")),
                delta=_f(greeks.get("delta")), gamma=_f(greeks.get("gamma")),
            ))
        return chain
