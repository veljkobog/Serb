"""Fallback provider built on yfinance (Yahoo Finance). Needs no account.

Tradier is the default; this exists for wiring, weekends and anyone without a
brokerage token. What you give up:
  * quotes are delayed ~15 minutes on many symbols,
  * no trade side at all, so `flow` stays an unsigned positioning proxy,
  * no broker greeks — everything is re-derived from the mid,
  * the IV field is occasionally stale or zero (see option_metrics._resolve_iv).
Open interest is the prior session's official figure here as everywhere.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import List, Optional

from ..models import Bar, OptionQuote, SymbolSnapshot
from ..session import ET, session_open, to_et
from .base import Provider


class YahooProvider(Provider):
    name = "yahoo"

    def __init__(self, bar_interval: str = "5m"):
        try:
            import yfinance  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "yfinance is required for the yahoo provider: pip install -r requirements.txt"
            ) from exc
        self.bar_interval = bar_interval

    def snapshot(self, symbol: str, now: datetime) -> Optional[SymbolSnapshot]:
        import yfinance as yf

        now = to_et(now)
        t = yf.Ticker(symbol)
        notes: List[str] = []

        bars = self._bars(t, now)
        if not bars:
            return None

        daily = t.history(period="1mo", interval="1d", auto_adjust=False)
        prev_close = None
        adr_pct = None
        avg_vol = None
        if daily is not None and not daily.empty:
            today = now.date()
            prior = daily[[d.date() < today for d in daily.index]]
            if not prior.empty:
                prev_close = float(prior["Close"].iloc[-1])
                tail = prior.tail(14)
                ranges = (tail["High"] - tail["Low"]) / tail["Close"] * 100.0
                adr_pct = float(ranges.mean())
                avg_vol = float(tail["Volume"].mean())

        spot = bars[-1].close
        expiry = now.date().isoformat()
        try:
            expiries = list(t.options or [])
        except Exception as exc:  # pragma: no cover - network dependent
            notes.append(f"chain-error:{type(exc).__name__}")
            expiries = []
        if expiry not in expiries:
            notes.append("no-0dte")
            return SymbolSnapshot(
                symbol=symbol, spot=spot, prev_close=prev_close, bars=bars,
                chain=[], expiry=None, adr_pct=adr_pct,
                avg_share_volume=avg_vol, notes=notes,
            )

        chain: List[OptionQuote] = []
        try:
            oc = t.option_chain(expiry)
            chain = self._quotes(oc.calls, "C") + self._quotes(oc.puts, "P")
        except Exception as exc:  # pragma: no cover - network dependent
            notes.append(f"chain-error:{type(exc).__name__}")

        return SymbolSnapshot(
            symbol=symbol,
            spot=spot,
            prev_close=prev_close,
            bars=bars,
            chain=chain,
            expiry=expiry,
            adr_pct=adr_pct,
            avg_share_volume=avg_vol,
            notes=notes,
        )

    def _bars(self, ticker, now: datetime) -> List[Bar]:
        hist = ticker.history(
            period="1d", interval=self.bar_interval, prepost=False, auto_adjust=False
        )
        if hist is None or hist.empty:
            return []
        open_ts = session_open(now.date())
        out: List[Bar] = []
        for ts, row in hist.iterrows():
            ts = ts.to_pydatetime()
            ts = ts.astimezone(ET) if ts.tzinfo else ts.replace(tzinfo=ET)
            # Regular session only: pre/post bars wreck VWAP and the open range.
            if ts < open_ts or ts.time() >= time(16, 0) or ts.date() != now.date():
                continue
            out.append(
                Bar(
                    ts=ts,
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row.get("Volume") or 0.0),
                )
            )
        return out

    @staticmethod
    def _quotes(df, right: str) -> List[OptionQuote]:
        if df is None or df.empty:
            return []

        def num(row, key) -> Optional[float]:
            v = row.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else f  # drop NaN

        out: List[OptionQuote] = []
        for _, row in df.iterrows():
            strike = num(row, "strike")
            if strike is None:
                continue
            out.append(
                OptionQuote(
                    strike=strike,
                    right=right,
                    bid=num(row, "bid"),
                    ask=num(row, "ask"),
                    last=num(row, "lastPrice"),
                    volume=num(row, "volume") or 0.0,
                    open_interest=num(row, "openInterest") or 0.0,
                    iv=num(row, "impliedVolatility"),
                )
            )
        return out
