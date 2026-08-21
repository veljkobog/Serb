"""Yahoo Finance provider — free, no API key, good enough to run the scanner today.

Caveats, so nobody is surprised in production:
  * Option quotes are delayed (typically 15 min) and the chain has no greeks.
  * Yahoo rate-limits aggressively; the scanner throttles and caches.
  * Endpoints are unofficial and do change. If Yahoo breaks, switch providers with
    ``--provider tradier`` / ``--provider polygon``.
"""
from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Dict, List, Optional

from ..calendar_utils import ET, parse_date
from ..http import Http, HttpError
from .base import (Bar, Fundamentals, IntradayBar, MarketDataProvider, OptionChain,
                   OptionContract, Quote)

# Yahoo runs the same API on two hosts and they fail independently — one will 403 or
# rate-limit while the other answers. Every request tries them in order.
HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
CHART = "/v8/finance/chart/{sym}"
OPTIONS = "/v7/finance/options/{sym}"
SUMMARY = "/v10/finance/quoteSummary/{sym}"
CRUMB = "/v1/test/getcrumb"
COOKIE_SEED = "https://fc.yahoo.com"
# Statuses that mean "your crumb/cookie went stale", as opposed to "no such symbol".
STALE_AUTH = (401, 403)


def _f(node: Any) -> Optional[float]:
    """Yahoo returns either a raw number or {'raw': n, 'fmt': '...'}."""
    if node is None:
        return None
    if isinstance(node, dict):
        node = node.get("raw")
    try:
        val = float(node)
    except (TypeError, ValueError):
        return None
    return None if val != val else val  # drop NaN


class YahooProvider(MarketDataProvider):
    name = "yahoo"
    supports_greeks = False

    def __init__(self, http: Optional[Http] = None):
        self.http = http or Http(min_interval=0.35)
        self._crumb: Optional[str] = None
        self._crumb_tried = False
        self._crumb_lock = threading.Lock()   # the scanner fans out across threads

    # -- auth --------------------------------------------------------------
    def _get_crumb(self, refresh: bool = False) -> Optional[str]:
        """Yahoo gates some endpoints behind a cookie+crumb pair. Best effort.

        A crumb expires, and the endpoint answers 401/403 rather than saying so. When
        that happens callers re-enter here with ``refresh=True`` for exactly one retry.
        """
        with self._crumb_lock:
            if refresh:
                self._crumb, self._crumb_tried = None, False
            if self._crumb or self._crumb_tried:
                return self._crumb
            self._crumb_tried = True
            try:
                self.http.get_text(COOKIE_SEED, cache_ttl=0)
            except Exception:
                pass  # expected to 404; we only want the Set-Cookie
            for host in HOSTS:
                try:
                    crumb = self.http.get_text(host + CRUMB, cache_ttl=0).strip()
                except Exception:
                    continue
                if crumb and "<" not in crumb and len(crumb) < 32:
                    self._crumb = crumb
                    break
            return self._crumb

    def _params(self, extra: Optional[Dict[str, Any]] = None,
                crumb: Optional[str] = None) -> Dict[str, Any]:
        params = dict(extra or {})
        crumb = crumb if crumb is not None else self._get_crumb()
        if crumb:
            params["crumb"] = crumb
        return params

    # -- transport ---------------------------------------------------------
    def _fetch(self, path: str, params: Optional[Dict[str, Any]] = None,
               cache_ttl: int = 300, with_crumb: bool = False) -> Any:
        """GET a Yahoo path, trying both hosts and refreshing a stale crumb once."""
        attempts = ((False,), (False, True))[1 if with_crumb else 0]
        last: Optional[Exception] = None
        for refresh in attempts:
            query = self._params(params, crumb=self._get_crumb(refresh=refresh)) \
                if with_crumb else dict(params or {})
            for host in HOSTS:
                try:
                    return self.http.get_json(host + path, params=query, cache_ttl=cache_ttl)
                except HttpError as exc:
                    last = exc
                    if exc.status in STALE_AUTH and with_crumb:
                        break        # try the other host only after refreshing the crumb
                except Exception as exc:
                    last = exc
        if last:
            raise last
        raise HttpError(0, path, "no response")

    # -- prices ------------------------------------------------------------
    def daily_bars(self, symbol: str, lookback: int = 260) -> List[Bar]:
        rng = "2y" if lookback > 250 else "1y"
        data = self._fetch(CHART.format(sym=symbol),
                           {"range": rng, "interval": "1d", "includePrePost": "false",
                            "events": "div,split"}, cache_ttl=900)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return []
        res = result[0]
        stamps = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows, closes, vols = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        bars: List[Bar] = []
        for i, ts in enumerate(stamps):
            try:
                o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], vols[i]
            except IndexError:
                continue
            if None in (o, h, l, c) or c <= 0:
                continue
            d = parse_date(ts)
            if d is None:
                continue
            bars.append(Bar(d, float(o), float(h), float(l), float(c), float(v or 0.0)))
        return bars[-lookback:]

    def intraday_bars(self, symbol: str, interval: str = "5m") -> List[IntradayBar]:
        data = self._fetch(CHART.format(sym=symbol),
                           {"range": "1d", "interval": interval, "includePrePost": "false"},
                           cache_ttl=60)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return []
        res = result[0]
        stamps = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        opens, highs, lows, closes, vols = (q.get(k) or [] for k in
                                            ("open", "high", "low", "close", "volume"))
        out: List[IntradayBar] = []
        for i, ts in enumerate(stamps):
            try:
                o, h, l, c, v = opens[i], highs[i], lows[i], closes[i], vols[i]
            except IndexError:
                continue
            if None in (o, h, l, c):
                continue          # Yahoo pads gaps with nulls
            out.append(IntradayBar(
                dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(ET),
                float(o), float(h), float(l), float(c), float(v or 0.0)))
        return out

    def quote(self, symbol: str) -> Optional[Quote]:
        data = self._fetch(CHART.format(sym=symbol),
                           {"range": "1d", "interval": "1m", "includePrePost": "false"},
                           cache_ttl=60)
        result = (data.get("chart") or {}).get("result") or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        last = _f(meta.get("regularMarketPrice"))
        if last is None:
            return None
        return Quote(
            symbol=symbol,
            last=last,
            prev_close=_f(meta.get("chartPreviousClose")) or _f(meta.get("previousClose")),
            day_high=_f(meta.get("regularMarketDayHigh")),
            day_low=_f(meta.get("regularMarketDayLow")),
            day_volume=_f(meta.get("regularMarketVolume")),
        )

    # -- fundamentals ------------------------------------------------------
    def fundamentals(self, symbol: str) -> Fundamentals:
        out = Fundamentals(symbol=symbol)
        try:
            data = self._fetch(SUMMARY.format(sym=symbol),
                               {"modules": "price,summaryDetail,defaultKeyStatistics,"
                                           "calendarEvents"},
                               cache_ttl=21600, with_crumb=True)
        except Exception:
            return out
        results = ((data.get("quoteSummary") or {}).get("result")) or []
        if not results:
            return out
        node = results[0]
        price = node.get("price") or {}
        stats = node.get("defaultKeyStatistics") or {}
        detail = node.get("summaryDetail") or {}
        cal = (node.get("calendarEvents") or {}).get("earnings") or {}

        out.name = price.get("longName") or price.get("shortName")
        out.market_cap = _f(price.get("marketCap")) or _f(detail.get("marketCap"))
        out.shares_out = _f(stats.get("sharesOutstanding"))
        out.float_shares = _f(stats.get("floatShares"))
        out.shares_short = _f(stats.get("sharesShort"))
        out.shares_short_prior = _f(stats.get("sharesShortPriorMonth"))
        out.short_pct_float = _f(stats.get("shortPercentOfFloat"))
        out.short_ratio = _f(stats.get("shortRatio"))
        out.short_interest_date = parse_date(_f(stats.get("dateShortInterest")))
        out.is_etf = (price.get("quoteType") or "").upper() in {"ETF", "MUTUALFUND", "INDEX"}
        dates = cal.get("earningsDate") or []
        if dates:
            out.earnings_date = parse_date(_f(dates[0]))
        return out

    # -- options -----------------------------------------------------------
    def expirations(self, symbol: str) -> List[dt.date]:
        payload = self._options_payload(symbol)
        if not payload:
            return []
        out = [parse_date(ts) for ts in (payload.get("expirationDates") or [])]
        return sorted(d for d in out if d)

    def _options_payload(self, symbol: str, expiry: Optional[dt.date] = None) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if expiry:
            params["date"] = int(dt.datetime.combine(expiry, dt.time(0, 0), tzinfo=dt.timezone.utc).timestamp())
        try:
            data = self._fetch(OPTIONS.format(sym=symbol), params, cache_ttl=120, with_crumb=True)
        except Exception:
            return None
        results = ((data.get("optionChain") or {}).get("result")) or []
        return results[0] if results else None

    def chain(self, symbol: str, expiry: dt.date) -> Optional[OptionChain]:
        payload = self._options_payload(symbol, expiry)
        if not payload:
            return None
        groups = payload.get("options") or []
        if not groups:
            return None
        chain = OptionChain(underlying=symbol, expiry=expiry)
        for right, key in (("C", "calls"), ("P", "puts")):
            for raw in groups[0].get(key) or []:
                strike = _f(raw.get("strike"))
                if strike is None:
                    continue
                chain.contracts.append(OptionContract(
                    symbol=raw.get("contractSymbol") or f"{symbol}{expiry:%y%m%d}{right}{int(strike*1000):08d}",
                    underlying=symbol,
                    expiry=parse_date(_f(raw.get("expiration"))) or expiry,
                    strike=strike,
                    right=right,
                    bid=_f(raw.get("bid")),
                    ask=_f(raw.get("ask")),
                    last=_f(raw.get("lastPrice")),
                    volume=_f(raw.get("volume")) or 0.0,
                    open_interest=_f(raw.get("openInterest")) or 0.0,
                    iv=_f(raw.get("impliedVolatility")),
                ))
        return chain
