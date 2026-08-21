"""Preflight checks: prove the live data path works before you trust a scan.

An empty result table has two very different causes — "nothing set up today" and
"the data never arrived" — and they look identical. This tells them apart, and every
failure carries the fix rather than just the error.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .calendar_utils import (is_trading_day, market_is_open, now_et, prev_trading_day,
                             recent_trading_days, session_progress)
from .config import Config
from .engine import pick_expiry
from .http import Http, HttpError
from .providers import FinraOffExchange, resolve

OK, WARN, FAIL = "ok", "warn", "fail"
PROBE = "SPY"        # listed everywhere, options every session, never thin


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""
    ms: int = 0
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "fix": self.fix, "ms": self.ms, "data": self.data}


class _Timer:
    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.ms = int((time.time() - self.t0) * 1000)


def _env(config: Config) -> List[Check]:
    checks: List[Check] = []
    version = ".".join(str(x) for x in sys.version_info[:3])
    checks.append(Check(
        "Python", OK if sys.version_info >= (3, 9) else FAIL,
        f"Python {version}",
        "" if sys.version_info >= (3, 9) else "Python 3.9+ is required (zoneinfo).",
    ))

    cache = config.cache_dir or "(disabled)"
    if not config.cache_dir:
        checks.append(Check("Cache", WARN, "cache disabled",
                            "Every run re-downloads everything. Set cache_dir to speed it up."))
    else:
        try:
            os.makedirs(config.cache_dir, exist_ok=True)
            probe = os.path.join(config.cache_dir, ".writetest")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
            checks.append(Check("Cache", OK, f"writable at {cache}"))
        except OSError as exc:
            checks.append(Check("Cache", WARN, f"{cache} is not writable: {exc}",
                                "Point cache_dir somewhere writable, or scans will be slow."))

    now = now_et()
    today = now.date()
    if not is_trading_day(today):
        detail = f"{now:%a %H:%M} ET — market closed (not a trading day)"
        fix = ("Quotes and chains will be stale. Run --demo to exercise the tool, or "
               "scan on a session day.")
        status = WARN
    elif market_is_open(now):
        detail = f"{now:%a %H:%M} ET — market open, {session_progress(now)*100:.0f}% through the session"
        fix, status = "", OK
    else:
        detail = f"{now:%a %H:%M} ET — trading day, but outside 09:30-16:00"
        fix = "Option day-volume will read near zero until the open."
        status = WARN
    checks.append(Check("Session", status, detail, fix,
                        data={"session_progress": round(session_progress(now), 3)}))
    return checks


PROVIDER_HOSTS = {
    "yahoo": "https://query2.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=1d",
    "tradier": "https://api.tradier.com/v1/markets/clock",
    "polygon": "https://api.polygon.io/v1/marketstatus/now",
}


def _network(config: Config, http: Http) -> List[Check]:
    """Probe raw reachability first, so later failures read as consequences."""
    url = PROVIDER_HOSTS.get(config.provider)
    if not url:
        return []
    host = url.split("/")[2]
    with _Timer() as t:
        try:
            http.get_text(url, cache_ttl=0)
            check = Check("Network", OK, f"{host} reachable")
        except HttpError as exc:
            # An HTTP status means the host answered — that is reachability, whatever
            # the status says about credentials.
            check = Check("Network", OK if exc.status else WARN,
                          f"{host} answered HTTP {exc.status}",
                          "Reachable, but rejecting the request — usually a missing or "
                          "expired API key." if exc.status in (401, 403) else "")
        except Exception as exc:
            check = Check(
                "Network", FAIL, f"{host} unreachable — {type(exc).__name__}: {exc}",
                "Nothing below can succeed until this does. Check your proxy, VPN, DNS "
                "or firewall; corporate networks commonly block market-data hosts.")
    check.ms = t.ms
    return [check]


def _provider(config: Config, http: Http,
              provider: Optional[Any] = None) -> List[Check]:
    checks: List[Check] = []
    today = now_et().date()
    if provider is None:
        try:
            provider = resolve(config.provider, http=http)
        except Exception as exc:
            return [Check(f"Provider ({config.provider})", FAIL, str(exc),
                          "Set the provider's API key, or use --provider yahoo "
                          "(no key needed).")]

    # --- daily bars ------------------------------------------------------
    bars = []
    with _Timer() as t:
        try:
            bars = provider.daily_bars(PROBE, 260)
            if not bars:
                checks.append(Check("Price history", FAIL, f"no bars returned for {PROBE}",
                                    "The provider answered but had no data. Try the other "
                                    "provider, or check network/proxy egress."))
            else:
                latest = bars[-1].date
                lag = (today - latest).days
                expected = prev_trading_day(today) if not market_is_open() else today
                stale = latest < expected
                checks.append(Check(
                    "Price history", WARN if stale else OK,
                    f"{len(bars)} daily bars for {PROBE}, latest {latest} "
                    f"(close {bars[-1].close:,.2f})",
                    f"Latest bar is {lag}d old — expected {expected}. Data may be delayed."
                    if stale else "",
                    data={"bars": len(bars), "latest": str(latest)}))
        except Exception as exc:
            checks.append(Check("Price history", FAIL, f"{type(exc).__name__}: {exc}",
                                "This is the core feed — nothing works without it. If Yahoo "
                                "is 403ing, try --provider tradier."))
    checks[-1].ms = t.ms

    # --- quote -----------------------------------------------------------
    with _Timer() as t:
        try:
            quote = provider.quote(PROBE)
            if quote and quote.last:
                checks.append(Check("Live quote", OK,
                                    f"{PROBE} {quote.last:,.2f} "
                                    f"({quote.change_pct:+.2f}%)" if quote.change_pct is not None
                                    else f"{PROBE} {quote.last:,.2f}"))
            else:
                checks.append(Check("Live quote", WARN, "no quote returned",
                                    "The scanner will fall back to the last daily close."))
        except Exception as exc:
            checks.append(Check("Live quote", WARN, f"{type(exc).__name__}: {exc}",
                                "Falls back to the last daily close; scores still work."))
    checks[-1].ms = t.ms

    # --- fundamentals ----------------------------------------------------
    with _Timer() as t:
        try:
            fund = provider.fundamentals("AAPL")
            if fund.market_cap:
                bits = [f"AAPL cap ${fund.market_cap/1e9:,.0f}B"]
                if fund.short_pct_float:
                    bits.append(f"short {fund.short_pct_float*100:.1f}% of float")
                checks.append(Check("Fundamentals", OK, ", ".join(bits)))
            else:
                checks.append(Check(
                    "Fundamentals", FAIL, "no market cap returned for AAPL",
                    "Without market cap every non-ETF fails the size gate and the scan "
                    "comes back empty. Yahoo gates this behind a cookie+crumb — if it "
                    "keeps failing, clear the cache dir and retry."))
        except Exception as exc:
            checks.append(Check("Fundamentals", FAIL, f"{type(exc).__name__}: {exc}",
                                "Non-ETFs will all fail the market cap gate."))
    checks[-1].ms = t.ms

    # --- expiries and chain ----------------------------------------------
    expiry: Optional[dt.date] = None
    with _Timer() as t:
        try:
            expiries = provider.expirations(PROBE)
            expiry = pick_expiry(expiries, today, config.gates.max_dte)
            if not expiries:
                checks.append(Check("Option expiries", FAIL, f"none listed for {PROBE}",
                                    "No chain means no scan. Check the provider's options "
                                    "entitlement."))
            elif expiry:
                dte = 0 if expiry == today else "1+"
                checks.append(Check("Option expiries", OK,
                                    f"{len(expiries)} listed, nearest usable {expiry} ({dte}DTE)",
                                    data={"expiry": str(expiry)}))
            else:
                checks.append(Check(
                    "Option expiries", WARN,
                    f"{len(expiries)} listed, nearest {expiries[0]} — outside "
                    f"{config.gates.max_dte}DTE",
                    "Nothing to trade at this DTE today. Raise max_dte, or scan on a day "
                    "with a nearer expiry."))
        except Exception as exc:
            checks.append(Check("Option expiries", FAIL, f"{type(exc).__name__}: {exc}",
                                "No chain means no scan."))
    checks[-1].ms = t.ms

    if expiry:
        with _Timer() as t:
            try:
                chain = provider.chain(PROBE, expiry)
                if not chain or not chain.contracts:
                    checks.append(Check("Option chain", FAIL, f"empty chain for {PROBE} {expiry}",
                                        "Every name will fail the option gates."))
                else:
                    spot = bars[-1].close if bars else 0.0
                    volume = sum(c.volume for c in chain.contracts)
                    oi = sum(c.open_interest for c in chain.contracts)
                    greeks = sum(1 for c in chain.contracts if c.delta is not None)
                    quoted = sum(1 for c in chain.contracts if c.spread_pct is not None)
                    detail = (f"{len(chain.contracts)} contracts, {volume:,.0f} volume, "
                              f"{oi:,.0f} OI, {quoted} two-sided quotes")
                    fix = ""
                    status = OK
                    if not quoted:
                        status, fix = FAIL, ("No bid/ask anywhere — the spread gate will "
                                             "reject everything. This is normal outside "
                                             "market hours.")
                    elif not oi:
                        status, fix = WARN, "No open interest reported; the OI gate will reject."
                    checks.append(Check("Option chain", status, detail, fix,
                                        data={"contracts": len(chain.contracts),
                                              "volume": volume, "open_interest": oi,
                                              "greeks": greeks, "spot": spot}))
                    checks.append(Check(
                        "Greeks", OK if greeks else WARN,
                        f"{greeks}/{len(chain.contracts)} contracts carry delta"
                        if greeks else "not provided by this source",
                        "" if greeks else ("Strike ladders fall back to expected-move offsets. "
                                           "Tradier or Polygon give real deltas.")))
            except Exception as exc:
                checks.append(Check("Option chain", FAIL, f"{type(exc).__name__}: {exc}",
                                    "Every name will fail the option gates."))
        checks[-2 if len(checks) > 1 and checks[-1].name == "Greeks" else -1].ms = t.ms
    return checks


def _finra(config: Config, http: Http,
           offex: Optional[FinraOffExchange] = None) -> List[Check]:
    if config.darkpool_days <= 0:
        return [Check("Dark pool (FINRA)", WARN, "disabled by config",
                      "Dark pool signals are skipped; the other four blocks still score.")]
    with _Timer() as t:
        if offex is not None:
            feed = offex
        else:
            feed = FinraOffExchange(http=http, days=min(config.darkpool_days, 5))
            feed.load()
    if feed.available:
        latest = feed.loaded_dates[-1]
        rows = len(feed.by_symbol)
        age = len(recent_trading_days(6, now_et().date())) - 1
        sample = feed.latest(PROBE)
        detail = f"{len(feed.loaded_dates)} sessions, {rows:,} symbols, latest {latest}"
        if sample and sample.dpi is not None:
            detail += f" ({PROBE} DPI {sample.dpi*100:.1f}%)"
        stale = (now_et().date() - latest).days > 4
        check = Check("Dark pool (FINRA)", WARN if stale else OK, detail,
                      "Latest file is several sessions old — FINRA may be late publishing."
                      if stale else "",
                      data={"latest": str(latest), "symbols": rows, "age_probe": age})
    else:
        first = feed.errors[0] if feed.errors else "no files fetched"
        check = Check("Dark pool (FINRA)", WARN, f"unavailable — {first[:160]}",
                      "cdn.finra.org is unreachable (proxy or firewall). The scan still "
                      "runs; the dark pool block is marked unavailable and drops out of "
                      "the weighting.")
    check.ms = t.ms
    return [check]


def run_checks(config: Config, provider: Optional[Any] = None,
               offex: Optional[FinraOffExchange] = None) -> List[Check]:
    """Run every check. ``provider``/``offex`` are injection points for tests."""
    # Bounded on purpose: a diagnostic that takes four minutes to tell you the network
    # is down is not a diagnostic. Fail fast, report, move on.
    http = Http(cache_dir=config.cache_dir, cache_ttl=120, timeout=8, retries=1,
                min_interval=0.05, trip_after=2, cooldown=5.0)
    Http.reset_breakers()          # a diagnostic should start from a clean slate
    checks = _env(config)
    network = [] if provider is not None else _network(config, http)
    checks.extend(network)
    offline = any(c.name == "Network" and c.status == FAIL for c in network)
    checks.extend(_provider(config, http, provider))
    checks.extend(_finra(config, http, offex))
    if offline:
        for c in checks:
            if c.status in (WARN, FAIL) and c.name not in ("Network", "Session", "Cache"):
                c.fix = "Consequence of the Network failure above — fix that first."
    Http.reset_breakers()          # don't leave a later scan inheriting these trips
    return checks


def verdict(checks: List[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK


def render_terminal(checks: List[Check], colour: bool = True) -> str:
    marks = {OK: ("PASS", "\033[32m"), WARN: ("WARN", "\033[33m"), FAIL: ("FAIL", "\033[31m")}
    reset, dim, bold = "\033[0m", "\033[2m", "\033[1m"
    lines = [f"{bold if colour else ''}Preflight{reset if colour else ''}", ""]
    for c in checks:
        label, code = marks[c.status]
        tag = f"{code}{label}{reset}" if colour else label
        timing = f" {c.ms}ms" if c.ms else ""
        lines.append(f"  [{tag}] {c.name:<20} {c.detail}{dim if colour else ''}{timing}"
                     f"{reset if colour else ''}")
        if c.fix:
            lines.append(f"         {dim if colour else ''}-> {c.fix}{reset if colour else ''}")
    overall = verdict(checks)
    label, code = marks[overall]
    summary = {OK: "Everything the scanner needs is answering.",
               WARN: "Usable, but read the warnings above — some signals will be degraded.",
               FAIL: "A scan will not produce meaningful results until the failures are fixed."}[overall]
    lines += ["", f"  {(code + label + reset) if colour else label}: {summary}", ""]
    return "\n".join(lines)
