"""Tradier provider: real-time quotes, chains with greeks, and trade side.

Two halves:

* `TradierClient` / `TradierProvider` — REST. Quotes, 5-minute time & sales,
  today's option chain with the broker's own greeks and IV.
* `TradierTapeStream` — HTTP streaming time & sales for the 0DTE contracts,
  classified into buyer- and seller-initiated volume (see `odte.sides`). This
  is what the free feeds cannot give you: exchange volume carries no side, so
  without a live stream "flow" is only a positioning proxy.

Stdlib only (urllib): no requests/websocket dependency.

Auth: put a token in TRADIER_TOKEN. TRADIER_ENV=sandbox points at the sandbox
host (delayed data, useful for wiring); production needs a brokerage account
with market data enabled for real-time quotes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as dtime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import Bar, OptionQuote, SymbolSnapshot
from ..session import ET, session_open, to_et
from ..sides import SideTape
from .base import Provider

PROD_API = "https://api.tradier.com/v1"
SANDBOX_API = "https://sandbox.tradier.com/v1"
STREAM_API = "https://stream.tradier.com/v1"

# Tradier caps streamed symbols per session; chains are trimmed to the strikes
# that actually matter before subscribing.
MAX_STREAM_SYMBOLS = 500


class TradierError(RuntimeError):
    pass


def _as_list(node: Any, key: str) -> List[Dict[str, Any]]:
    """Tradier returns a bare object for one result and a list for many."""
    if not node:
        return []
    inner = node.get(key) if isinstance(node, dict) else node
    if inner is None:
        return []
    return inner if isinstance(inner, list) else [inner]


def _num(d: Dict[str, Any], key: str) -> Optional[float]:
    v = d.get(key)
    if v in (None, "", "NaN"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


class TradierClient:
    """Minimal REST client with rate-limit backoff."""

    def __init__(
        self,
        token: Optional[str] = None,
        env: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.token = token or os.environ.get("TRADIER_TOKEN") or ""
        if not self.token:
            raise TradierError(
                "no Tradier token: set TRADIER_TOKEN (or pass --tradier-token)"
            )
        env = (env or os.environ.get("TRADIER_ENV") or "production").lower()
        self.env = env
        self.base = SANDBOX_API if env.startswith("sand") else PROD_API
        self.timeout = timeout
        self.max_retries = max_retries
        self._lock = threading.Lock()
        self._next_ok = 0.0
        # Market-data endpoints allow ~120 req/min; keep a floor between calls
        # so a wide universe scan doesn't trip the limiter.
        self._min_interval = 0.5 if env.startswith("sand") else 0.25

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "odte-scanner/0.2",
        }

    def _throttle(self) -> None:
        with self._lock:
            wait = self._next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._next_ok = time.monotonic() + self._min_interval

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            self._throttle()
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise TradierError(
                        f"Tradier rejected the token ({exc.code}). Check TRADIER_TOKEN "
                        f"and that TRADIER_ENV matches where it was issued."
                    ) from exc
                if exc.code == 429 or exc.code >= 500:
                    last_exc = exc
                    time.sleep(2 ** attempt)
                    continue
                raise TradierError(f"GET {path} failed: {exc.code} {exc.reason}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        raise TradierError(f"GET {path} failed after {self.max_retries} tries: {last_exc}")

    def post(self, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._throttle()
        body = urllib.parse.urlencode(data or {}).encode()
        req = urllib.request.Request(
            f"{self.base}{path}", data=body, headers=self._headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    # --- market data -------------------------------------------------------
    def quotes(self, symbols: Sequence[str], greeks: bool = False) -> Dict[str, Dict]:
        if not symbols:
            return {}
        payload = self.get(
            "/markets/quotes",
            {"symbols": ",".join(symbols), "greeks": "true" if greeks else "false"},
        )
        return {
            q.get("symbol"): q for q in _as_list(payload.get("quotes"), "quote")
        }

    def timesales(
        self, symbol: str, start: datetime, end: datetime, interval: str = "5min"
    ) -> List[Dict]:
        payload = self.get(
            "/markets/timesales",
            {
                "symbol": symbol,
                "interval": interval,
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M"),
                "session_filter": "open",  # regular session only
            },
        )
        return _as_list(payload.get("series"), "data")

    def daily_history(self, symbol: str, start: datetime, end: datetime) -> List[Dict]:
        payload = self.get(
            "/markets/history",
            {
                "symbol": symbol,
                "interval": "daily",
                "start": start.strftime("%Y-%m-%d"),
                "end": end.strftime("%Y-%m-%d"),
            },
        )
        return _as_list(payload.get("history"), "day")

    def expirations(self, symbol: str) -> List[str]:
        payload = self.get(
            "/markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
        )
        node = payload.get("expirations") or {}
        dates = node.get("date") or []
        return dates if isinstance(dates, list) else [dates]

    def chain(self, symbol: str, expiration: str) -> List[Dict]:
        payload = self.get(
            "/markets/options/chains",
            {"symbol": symbol, "expiration": expiration, "greeks": "true"},
        )
        return _as_list(payload.get("options"), "option")

    def stream_session(self) -> Dict[str, str]:
        payload = self.post("/markets/events/session")
        return payload.get("stream") or {}


def parse_chain(rows: Iterable[Dict[str, Any]]) -> List[OptionQuote]:
    """Tradier chain rows -> OptionQuote, keeping the broker's greeks."""
    out: List[OptionQuote] = []
    for row in rows:
        strike = _num(row, "strike")
        kind = (row.get("option_type") or "").upper()[:1]
        if strike is None or kind not in ("C", "P"):
            continue
        greeks = row.get("greeks") or {}
        iv = None
        for key in ("mid_iv", "smv_vol", "ask_iv", "bid_iv"):
            iv = _num(greeks, key)
            if iv:
                break
        out.append(
            OptionQuote(
                strike=strike,
                right=kind,
                bid=_num(row, "bid"),
                ask=_num(row, "ask"),
                last=_num(row, "last"),
                volume=_num(row, "volume") or 0.0,
                open_interest=_num(row, "open_interest") or 0.0,
                iv=iv,
                occ=row.get("symbol"),
                delta=_num(greeks, "delta"),
                gamma=_num(greeks, "gamma"),
            )
        )
    return out


def parse_bars(rows: Iterable[Dict[str, Any]], day: datetime) -> List[Bar]:
    open_ts = session_open(day.date())
    bars: List[Bar] = []
    for row in rows:
        stamp = row.get("time") or row.get("date")
        if not stamp:
            continue
        try:
            ts = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        ts = ts.replace(tzinfo=ET) if ts.tzinfo is None else ts.astimezone(ET)
        if ts < open_ts or ts.time() >= dtime(16, 0) or ts.date() != day.date():
            continue
        close = _num(row, "close")
        if close is None:
            continue
        bars.append(
            Bar(
                ts=ts,
                open=_num(row, "open") or close,
                high=_num(row, "high") or close,
                low=_num(row, "low") or close,
                close=close,
                volume=_num(row, "volume") or 0.0,
            )
        )
    return bars


class TradierProvider(Provider):
    name = "tradier"

    def __init__(
        self,
        token: Optional[str] = None,
        env: Optional[str] = None,
        bar_interval: str = "5m",
        client: Optional[TradierClient] = None,
    ):
        self.client = client or TradierClient(token=token, env=env)
        self.interval = {"1m": "1min", "5m": "5min", "15m": "15min"}.get(
            bar_interval, "5min"
        )

    @property
    def env(self) -> str:
        return self.client.env

    def snapshot(self, symbol: str, now: datetime) -> Optional[SymbolSnapshot]:
        now = to_et(now)
        notes: List[str] = []
        if self.client.env.startswith("sand"):
            notes.append("sandbox-delayed")

        bars = parse_bars(
            self.client.timesales(
                symbol, session_open(now.date()), now, interval=self.interval
            ),
            now,
        )
        if not bars:
            return None

        quote = self.client.quotes([symbol]).get(symbol, {})
        spot = _num(quote, "last") or bars[-1].close
        prev_close = _num(quote, "prevclose")

        adr_pct = None
        avg_volume = _num(quote, "average_volume")
        days = self.client.daily_history(
            symbol, now - timedelta(days=30), now - timedelta(days=1)
        )
        usable = [d for d in days if _num(d, "close")][-14:]
        if usable:
            ranges = [
                (_num(d, "high") - _num(d, "low")) / _num(d, "close") * 100.0
                for d in usable
                if _num(d, "high") is not None and _num(d, "low") is not None
            ]
            if ranges:
                adr_pct = sum(ranges) / len(ranges)
            if prev_close is None:
                prev_close = _num(usable[-1], "close")

        expiry = now.date().isoformat()
        chain: List[OptionQuote] = []
        if expiry in self.client.expirations(symbol):
            chain = parse_chain(self.client.chain(symbol, expiry))
        else:
            notes.append("no-0dte")
            expiry = None

        return SymbolSnapshot(
            symbol=symbol,
            spot=spot,
            prev_close=prev_close,
            bars=bars,
            chain=chain,
            expiry=expiry,
            adr_pct=adr_pct,
            avg_share_volume=avg_volume,
            notes=notes,
        )


class TradierTapeStream:
    """Streams option time & sales and classifies every print into a SideTape.

    Tradier's HTTP event stream delivers line-delimited JSON. `timesale` events
    carry the bid and ask standing at the print, which is exactly what trade
    classification needs; `trade` events do not, so they are ignored.
    """

    def __init__(self, client: TradierClient, tape: Optional[SideTape] = None):
        self.client = client
        self.tape = tape or SideTape()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[Exception] = None
        self.events = 0

    def start(self, symbols: Sequence[str]) -> None:
        symbols = list(dict.fromkeys(symbols))[:MAX_STREAM_SYMBOLS]
        if not symbols:
            raise ValueError("no symbols to stream")
        self._thread = threading.Thread(
            target=self._run, args=(symbols,), name="tradier-tape", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> SideTape:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        return self.tape

    def run_for(self, symbols: Sequence[str], seconds: float) -> SideTape:
        """Blocking convenience wrapper: stream for `seconds`, then stop."""
        self.start(symbols)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.25)
        return self.stop()

    def _run(self, symbols: Sequence[str]) -> None:
        try:
            stream = self.client.stream_session()
            session_id = stream.get("sessionid")
            url = stream.get("url") or f"{STREAM_API}/markets/events"
            if not session_id:
                raise TradierError("Tradier did not return a streaming session id")
            body = urllib.parse.urlencode(
                {
                    "sessionid": session_id,
                    "symbols": ",".join(symbols),
                    "filter": "timesale",
                    "linebreak": "true",
                    "validOnly": "true",
                }
            ).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.client.token}",
                    "Accept": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                for raw in resp:
                    if self._stop.is_set():
                        break
                    self.handle_line(raw.decode("utf-8", "replace"))
        except Exception as exc:  # surfaced by the caller, never crashes the scan
            self.error = exc

    def handle_line(self, line: str) -> Optional[str]:
        """Parse and record one stream line. Split out so it can be tested."""
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if event.get("type") != "timesale":
            return None
        occ = event.get("symbol")
        price = _num(event, "price") or _num(event, "last")
        size = _num(event, "size")
        if not occ or price is None or not size:
            return None
        when = None
        stamp = event.get("date") or event.get("time")
        if stamp:
            try:  # epoch milliseconds on the stream
                when = datetime.fromtimestamp(int(stamp) / 1000.0, ET)
            except (TypeError, ValueError):
                try:
                    when = to_et(datetime.fromisoformat(str(stamp)))
                except ValueError:
                    when = None
        self.events += 1
        return self.tape.record(
            occ, price, size, _num(event, "bid"), _num(event, "ask"), when
        )


def liquid_contracts(
    snapshot: SymbolSnapshot, per_symbol: int = 40
) -> List[str]:
    """The OCC symbols worth streaming: nearest-the-money, most-traded first."""
    quotes = [q for q in snapshot.chain if q.occ]
    quotes.sort(key=lambda q: (abs(q.strike - snapshot.spot), -q.volume))
    return [q.occ for q in quotes[:per_symbol]]
