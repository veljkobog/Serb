#!/usr/bin/env python3
"""0DTE / 1DTE options scanner — command line entry point.

Examples
--------
  ./scan.py                                   # default universe, both sides
  ./scan.py --universe movers --max-dte 0     # same-day expiries only
  ./scan.py --symbols NVDA,AMD,SPY --explain NVDA
  ./scan.py --provider tradier --min-score 55 --top 10
  ./scan.py --min-cap 10e9 --min-dollar-volume 250e6   # only very large, very liquid
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odte import report, universe as universe_mod
from odte.config import Config
from odte.engine import Scanner
from odte.providers import PROVIDERS


def _num(value: str) -> float:
    """Accept 2e9, 2_000_000_000, 2B, 250M."""
    v = value.strip().replace("_", "").replace("$", "").replace(",", "")
    mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get(v[-1:].upper())
    return float(v[:-1]) * mult if mult else float(v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scan liquid, optionable names for 0DTE/1DTE setups.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    src = p.add_argument_group("data")
    src.add_argument("--provider", default=None, choices=list(PROVIDERS),
                     help="market data source (default: yahoo, no API key needed)")
    src.add_argument("--config", default=None, help="JSON config file")
    src.add_argument("--cache-dir", default=None, help="HTTP cache directory")
    src.add_argument("--no-cache", action="store_true", help="bypass the HTTP cache")
    src.add_argument("--darkpool-days", type=int, default=None,
                     help="sessions of FINRA off-exchange history to load (default 25)")
    src.add_argument("--no-darkpool", action="store_true",
                     help="skip the FINRA download entirely")
    src.add_argument("--doctor", action="store_true",
                     help="run preflight checks against the live data path and exit — "
                          "use this first if a scan comes back empty")
    src.add_argument("--demo", action="store_true",
                     help="run against deterministic synthetic data — no network, no keys. "
                          "Use it to see the output shape and tune gates off-hours.")

    uni = p.add_argument_group("universe")
    uni.add_argument("--universe", default=None,
                     help=f"preset ({', '.join(universe_mod.PRESETS)}) or comma-separated symbols")
    uni.add_argument("--symbols", default=None, help="explicit comma-separated symbol list")
    uni.add_argument("--universe-file", default=None, help="file with one symbol per line")
    uni.add_argument("--list-presets", action="store_true", help="print the presets and exit")

    flt = p.add_argument_group("filters")
    flt.add_argument("--max-dte", type=int, default=None, choices=[0, 1, 2, 3, 4, 5],
                     help="0 = same-day only, 1 = include the next session (default). "
                          "Higher reaches this week's Friday on a Mon-Wed, when single "
                          "names have nothing near-dated — a swing trade, not a 0DTE trade")
    flt.add_argument("--min-score", type=float, default=None, help="score floor, 0-100")
    flt.add_argument("--min-cap", type=_num, default=None, help="minimum market cap, e.g. 2e9 or 2B")
    flt.add_argument("--min-dollar-volume", type=_num, default=None,
                     help="minimum 20d average dollar volume, e.g. 100M")
    flt.add_argument("--min-option-volume", type=_num, default=None,
                     help="minimum contracts traded on the near expiry")
    flt.add_argument("--max-spread", type=float, default=None,
                     help="max ATM bid/ask spread as a fraction of mid, e.g. 0.10")
    flt.add_argument("--side", default="both", choices=["both", "calls", "puts"])
    flt.add_argument("--no-etf", action="store_true", help="exclude ETFs")
    flt.add_argument("--require-darkpool", action="store_true",
                     help="drop names with no FINRA off-exchange record")
    flt.add_argument("--exclude-earnings", action="store_true",
                     help="drop names reporting today or tomorrow")

    out = p.add_argument_group("output")
    out.add_argument("--top", type=int, default=None, help="how many names to show (default 20)")
    out.add_argument("--out-dir", default=None, help="directory for json/csv/html output")
    out.add_argument("--no-files", action="store_true", help="terminal output only")
    out.add_argument("--open", dest="open_html", action="store_true",
                     help="open the HTML dashboard when the scan finishes")
    out.add_argument("--explain", default=None, metavar="SYM",
                     help="dump every signal for one symbol as JSON")
    out.add_argument("--json", dest="json_only", action="store_true",
                     help="print the full result as JSON to stdout and nothing else")
    out.add_argument("--verbose", "-v", action="store_true", help="also list rejected names")
    out.add_argument("--quiet", "-q", action="store_true", help="suppress progress output")
    out.add_argument("--no-color", action="store_true")
    out.add_argument("--workers", type=int, default=None)
    return p


def apply_args(cfg: Config, args) -> Config:
    if args.provider:
        cfg.provider = args.provider
    if args.cache_dir:
        cfg.cache_dir = args.cache_dir
    if args.no_cache:
        cfg.cache_dir = ""
    if args.darkpool_days is not None:
        cfg.darkpool_days = args.darkpool_days
    if args.no_darkpool:
        cfg.darkpool_days = 0
    if args.workers:
        cfg.workers = args.workers
    if args.top:
        cfg.top = args.top
    if args.out_dir:
        cfg.out_dir = args.out_dir
    if args.max_dte is not None:
        cfg.gates.max_dte = args.max_dte
    if args.min_score is not None:
        cfg.gates.min_score = args.min_score
    if args.min_cap is not None:
        cfg.gates.min_market_cap = args.min_cap
    if args.min_dollar_volume is not None:
        cfg.gates.min_avg_dollar_volume = args.min_dollar_volume
    if args.min_option_volume is not None:
        cfg.gates.min_option_volume = args.min_option_volume
    if args.max_spread is not None:
        cfg.gates.max_atm_spread_pct = args.max_spread
    if args.no_etf:
        cfg.gates.allow_etf = False
    if args.require_darkpool:
        cfg.gates.require_darkpool = True
    if args.exclude_earnings:
        cfg.gates.exclude_earnings_today = True
    return cfg


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_presets:
        for name, syms in universe_mod.PRESETS.items():
            print(f"{name:<12} {len(syms):>4} symbols   {', '.join(syms[:8])}...")
        return 0

    cfg = apply_args(Config.load(args.config), args)

    if args.doctor:
        from odte import doctor
        checks = doctor.run_checks(cfg)
        if args.json_only:
            print(json.dumps({"verdict": doctor.verdict(checks),
                              "checks": [c.as_dict() for c in checks]}, indent=2))
        else:
            print(doctor.render_terminal(
                checks, colour=not args.no_color and sys.stdout.isatty()))
        return 0 if doctor.verdict(checks) != "fail" else 2

    symbols = universe_mod.load(
        spec=args.universe,
        explicit=args.symbols.split(",") if args.symbols else (cfg.universe or None),
        path=args.universe_file or cfg.universe_file,
    )
    if args.explain and args.explain.upper() not in symbols:
        symbols.append(args.explain.upper())

    quiet = args.quiet or args.json_only

    def progress(symbol: str, done: str) -> None:
        if not quiet:
            print(f"\r  scanning {done:<12} {symbol:<8}", end="", file=sys.stderr, flush=True)

    if args.demo:
        from odte.synthetic import FakeProvider, make_offex
        from odte.calendar_utils import now_et
        today = now_et().date()
        symbols = symbols[:12]
        provider = FakeProvider(symbols, today, drift=0.004, vol=0.009, mixed=True)
        bars = {s: provider.daily_bars(s) for s in symbols}
        scanner = Scanner(cfg, provider=provider,
                          offex=make_offex(symbols, bars, short_ratio=0.40),
                          progress_cb=progress)
        if not quiet:
            print("DEMO MODE: synthetic data, not the market.", file=sys.stderr)
    else:
        scanner = Scanner(cfg, progress_cb=progress)
    if cfg.darkpool_days <= 0 and not args.demo:
        from odte.providers import FinraOffExchange
        scanner.offex = FinraOffExchange(days=0)   # loaded-but-empty: signals disabled

    if not quiet:
        print(f"Scanning {len(symbols)} symbols via {scanner.provider.name} "
              f"(max {cfg.gates.max_dte}DTE)...", file=sys.stderr)
    result = scanner.run(symbols)
    if not quiet:
        print("\r" + " " * 40 + "\r", end="", file=sys.stderr)

    if args.side != "both":
        want = "CALLS" if args.side == "calls" else "PUTS"
        result.candidates = [c for c in result.candidates if c.side == want]

    if args.explain:
        target = args.explain.upper()
        match = next((c for c in result.candidates + result.rejected if c.symbol == target), None)
        print(json.dumps(match.as_dict() if match else {"error": f"{target} not evaluated"},
                         indent=2, default=str))
        return 0

    if args.json_only:
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0

    print(report.render_terminal(result, top=cfg.top,
                                 colour=not args.no_color and sys.stdout.isatty(),
                                 verbose=args.verbose))

    if not args.no_files:
        stamp = result.generated_at.strftime("%Y%m%d-%H%M")
        base = os.path.join(cfg.out_dir, f"scan-{stamp}")
        paths = [report.write_json(result, base + ".json"),
                 report.write_csv(result, base + ".csv"),
                 report.write_html(result, base + ".html", top=cfg.top),
                 report.write_html(result, os.path.join(cfg.out_dir, "latest.html"), top=cfg.top)]
        if cfg.journal:
            paths.append(report.append_journal(result, os.path.join(cfg.out_dir, "journal.jsonl")))
        print("Wrote: " + ", ".join(paths))
        if args.open_html:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(paths[2]))

    return 0 if result.candidates else 1


if __name__ == "__main__":
    raise SystemExit(main())
