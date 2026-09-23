"""`odte doctor` — prove the live wiring before you trust a number.

Walks the exact path a real scan takes (auth, quotes, today's expiry, greeks,
a streaming session, real prints) and says which step broke and what to do
about it, instead of leaving you to read a stack trace at 10:01.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional

from . import __version__, session
from .providers.tradier import (
    MAX_STREAM_SYMBOLS,
    TradierClient,
    TradierError,
    TradierTapeStream,
    parse_chain,
)

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARKS = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == FAIL


def _try(fn: Callable[[], Any]) -> tuple[Any, Optional[Exception]]:
    try:
        return fn(), None
    except Exception as exc:  # doctor reports failures, it never raises them
        return None, exc


def check_environment(now: datetime) -> List[Check]:
    checks = [
        Check(
            "python",
            OK if sys.version_info >= (3, 9) else FAIL,
            f"{platform.python_version()} on {platform.system()}",
            "" if sys.version_info >= (3, 9) else "Python 3.9 or newer is required.",
        )
    ]
    day_ok = session.is_trading_day(now.date())
    in_window = session.in_scan_window(now)
    checks.append(
        Check(
            "clock",
            OK if in_window else WARN,
            f"{now:%Y-%m-%d %H:%M} ET"
            + ("" if day_ok else " (not a trading day)")
            + ("" if in_window else f" — outside the {session.window_label()} scan window"),
            "" if in_window else "Scans still run with --force; the read is just less meaningful.",
        )
    )
    return checks


def check_tradier(
    client: TradierClient,
    now: datetime,
    symbol: str = "SPY",
    stream_seconds: float = 0.0,
) -> List[Check]:
    checks: List[Check] = []
    env = client.env
    checks.append(
        Check(
            "token",
            WARN if env.startswith("sand") else OK,
            f"{len(client.token)}-char token, env={env}",
            "Sandbox data is delayed and thin. Set TRADIER_ENV=production for a live read."
            if env.startswith("sand") else "",
        )
    )

    clock, exc = _try(client.clock)
    if exc:
        checks.append(
            Check("auth", FAIL, f"{type(exc).__name__}: {exc}",
                  "Check TRADIER_TOKEN, and that it was issued for the env you set.")
        )
        return checks  # nothing below can work
    state = clock.get("state", "?")
    checks.append(
        Check("auth", OK, f"market is {state} ({clock.get('description', '')})".strip())
    )

    quotes, exc = _try(lambda: client.quotes([symbol]))
    q = (quotes or {}).get(symbol, {})
    if exc or not q:
        checks.append(
            Check("quotes", FAIL, f"no quote for {symbol}" + (f": {exc}" if exc else ""),
                  "Market data may not be enabled on this account.")
        )
    else:
        last, bid, ask = q.get("last"), q.get("bid"), q.get("ask")
        age = ""
        stamp = q.get("trade_date") or q.get("bid_date")
        if stamp:
            try:
                secs = (now.timestamp() * 1000 - float(stamp)) / 1000.0
                age = f", last print {secs / 60:.0f} min ago"
            except (TypeError, ValueError):
                pass
        stale = state == "open" and "min ago" in age and not age.startswith(", last print 0")
        checks.append(
            Check(
                "quotes", WARN if (stale and env.startswith("sand")) else OK,
                f"{symbol} {last} ({bid}/{ask}){age}",
                "Delayed quotes — expected on sandbox." if stale and env.startswith("sand") else "",
            )
        )

    expirations, exc = _try(lambda: client.expirations(symbol))
    today = now.date().isoformat()
    if exc:
        checks.append(Check("expirations", FAIL, f"{type(exc).__name__}: {exc}"))
        return checks
    has_0dte = today in (expirations or [])
    checks.append(
        Check(
            "0DTE expiry", OK if has_0dte else WARN,
            f"{today} {'is' if has_0dte else 'is NOT'} listed for {symbol}"
            f" (next: {', '.join((expirations or [])[:3])})",
            "" if has_0dte else "No same-day expiry today — the scanner will flag it `no-0dte`.",
        )
    )

    if has_0dte:
        rows, exc = _try(lambda: client.chain(symbol, today))
        chain = parse_chain(rows or [])
        if exc or not chain:
            checks.append(
                Check("chain", FAIL, f"empty chain" + (f": {exc}" if exc else ""))
            )
        else:
            with_greeks = sum(1 for c in chain if c.delta is not None)
            traded = sum(1 for c in chain if c.volume > 0)
            checks.append(
                Check(
                    "chain", OK if with_greeks else WARN,
                    f"{len(chain)} contracts, {with_greeks} with greeks, {traded} traded today",
                    "" if with_greeks else
                    "No greeks in the response — the scanner will solve IV from the mid instead.",
                )
            )

    stream, exc = _try(client.stream_session)
    session_id = (stream or {}).get("sessionid")
    if exc or not session_id:
        checks.append(
            Check(
                "stream session", FAIL,
                f"no session id" + (f": {exc}" if exc else ""),
                "Streaming needs a brokerage account with market data. "
                "Without it, run the scan without --stream-seconds and expect `proxy-flow`.",
            )
        )
        return checks
    checks.append(Check("stream session", OK, f"session {session_id[:8]}... issued"))

    if stream_seconds <= 0:
        checks.append(Check("live prints", SKIP, "pass --stream-seconds to sample the tape"))
    elif state != "open":
        checks.append(Check("live prints", SKIP, f"market is {state}"))
    elif not has_0dte:
        checks.append(Check("live prints", SKIP, "no same-day contracts to listen to"))
    else:
        rows, _ = _try(lambda: client.chain(symbol, today))
        quotes_ = parse_chain(rows or [])
        spot = float(q.get("last") or 0) or (quotes_[0].strike if quotes_ else 0)
        near = sorted(
            (c for c in quotes_ if c.occ), key=lambda c: abs(c.strike - spot)
        )[: min(40, MAX_STREAM_SYMBOLS)]
        tape = TradierTapeStream(client)
        started = time.monotonic()
        tape.run_for([c.occ for c in near], stream_seconds)
        elapsed = time.monotonic() - started
        if tape.error:
            checks.append(
                Check("live prints", FAIL, f"{type(tape.error).__name__}: {tape.error}")
            )
        elif tape.tape.trades == 0:
            checks.append(
                Check("live prints", WARN, f"no prints in {elapsed:.0f}s",
                      "Quiet tape, or the stream is not entitled. Try a longer sample.")
            )
        else:
            sides = tape.tape.contracts
            buys = sum(c.buy for c in sides.values())
            sells = sum(c.sell for c in sides.values())
            checks.append(
                Check(
                    "live prints", OK,
                    f"{tape.tape.trades:,} prints in {elapsed:.0f}s across "
                    f"{len(sides)} contracts — {buys:,.0f} bought / {sells:,.0f} sold",
                )
            )
    return checks


def run_doctor(args: argparse.Namespace) -> int:
    now = (
        session.to_et(datetime.fromisoformat(args.as_of))
        if getattr(args, "as_of", None)
        else session.now_et()
    )
    print(f"odte {__version__} doctor\n")
    checks = check_environment(now)

    try:
        client = TradierClient(token=args.tradier_token, env=args.tradier_env)
    except TradierError as exc:
        checks.append(
            Check("token", FAIL, str(exc),
                  "export TRADIER_TOKEN=... (Tradier dashboard -> API Access)")
        )
        client = None
    if client is not None:
        checks.extend(
            check_tradier(client, now, args.symbol, args.stream_seconds)
        )

    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"  {MARKS[c.status]} {c.name.ljust(width)}  {c.detail}")
        if c.fix:
            print(f"         {' ' * width}  -> {c.fix}")

    failed = [c for c in checks if c.blocking]
    print()
    if failed:
        print(f"{len(failed)} blocking problem(s): " + ", ".join(c.name for c in failed))
        return 1
    warned = [c for c in checks if c.status == WARN]
    print(
        "Ready." if not warned
        else f"Ready, with {len(warned)} caveat(s): " + ", ".join(c.name for c in warned)
    )
    print("Next: ./bullbear record   (from 09:30)   then   ./bullbear   (after 10:00)")
    return 0
