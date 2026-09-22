"""Command line entry point: `python -m odte [options]`."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional

from . import __version__, report, session
from .config import (
    EQUITY_UNIVERSE,
    INDEX_UNIVERSE,
    Gates,
    ScanConfig,
    Weights,
    OPENING_RANGE_MINUTES,
)
from .models import SymbolSnapshot
from .option_metrics import compute_option_metrics
from .price_metrics import compute_price_metrics
from .providers import get_provider
from .providers.snapshot import save_snapshots
from .scoring import SymbolData, score_universe


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odte",
        description="0DTE options bias scanner for index ETFs and heavy-volume names.",
    )
    p.add_argument("--provider", default="yahoo", choices=["yahoo", "synthetic", "snapshot"])
    p.add_argument("--snapshot-path", help="snapshot file to replay (--provider snapshot)")
    p.add_argument("--universe", default="all", choices=["index", "equity", "all"])
    p.add_argument("--symbols", help="comma-separated override, e.g. SPY,QQQ,NVDA")
    p.add_argument("--benchmark", default="SPY", help="relative-strength benchmark")
    p.add_argument("--interval", default="5m", help="intraday bar interval (1m/2m/5m)")
    p.add_argument("--as-of", help="ISO timestamp to score as (replay/testing)")
    p.add_argument("--force", action="store_true", help="scan outside the advised window")
    p.add_argument("--top", type=int, default=0, help="only print the N strongest setups")
    p.add_argument("--detail", type=int, default=3, help="detail blocks to print")
    p.add_argument("--only-tradeable", action="store_true", help="drop NO TRADE / AVOID rows")
    p.add_argument("--min-bias", type=float, default=Gates.min_bias_to_trade)
    p.add_argument("--min-confidence", type=float, default=Gates.min_confidence_to_trade)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--csv", help="write a flat CSV of every metric here")
    p.add_argument("--json", dest="json_path", help="write full JSON output here")
    p.add_argument("--html", help="write a standalone HTML table here")
    p.add_argument("--save-snapshot", help="write the raw pulled data here for replay")
    p.add_argument("--version", action="version", version=f"odte {__version__}")
    return p


def resolve_symbols(args: argparse.Namespace) -> List[str]:
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe == "index":
        syms = list(INDEX_UNIVERSE)
    elif args.universe == "equity":
        syms = list(EQUITY_UNIVERSE)
    else:
        syms = INDEX_UNIVERSE + EQUITY_UNIVERSE
    bench = args.benchmark.upper()
    if bench not in syms:
        syms.append(bench)
    return list(dict.fromkeys(syms))


def fetch_all(provider, symbols: List[str], now: datetime, workers: int) -> Dict[str, SymbolSnapshot]:
    out: Dict[str, SymbolSnapshot] = {}

    def one(sym: str):
        try:
            return sym, provider.snapshot(sym, now)
        except Exception as exc:  # keep one bad symbol from killing the scan
            print(f"  ! {sym}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return sym, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for sym, snap in pool.map(one, symbols):
            if snap is not None:
                out[sym] = snap
    return out


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    now = session.to_et(datetime.fromisoformat(args.as_of)) if args.as_of else session.now_et()
    if not session.in_scan_window(now) and not args.force:
        print(
            f"Outside the advised scan window ({session.window_label()}); "
            f"now {now:%H:%M} ET. Re-run with --force to scan anyway.",
            file=sys.stderr,
        )
        return 2

    if args.provider == "snapshot" and not args.snapshot_path:
        print("--provider snapshot requires --snapshot-path", file=sys.stderr)
        return 2

    kwargs = {}
    if args.provider == "yahoo":
        kwargs["bar_interval"] = args.interval
    if args.provider == "snapshot":
        kwargs["path"] = args.snapshot_path
    provider = get_provider(args.provider, **kwargs)

    symbols = resolve_symbols(args)
    if args.provider == "snapshot" and not args.symbols:
        symbols = [s for s in provider.symbols]
    bench = args.benchmark.upper()

    cfg = ScanConfig(
        symbols=symbols,
        benchmark=bench,
        bar_interval=args.interval,
        weights=Weights(),
        gates=Gates(
            min_bias_to_trade=args.min_bias,
            min_confidence_to_trade=args.min_confidence,
        ),
    )

    snaps = fetch_all(provider, symbols, now, args.workers)
    if not snaps:
        print("No data returned for any symbol.", file=sys.stderr)
        return 1
    if args.save_snapshot:
        path = save_snapshots(args.save_snapshot, list(snaps.values()), now)
        print(f"snapshot -> {path}", file=sys.stderr)

    bench_snap = snaps.get(bench)
    bench_bars = bench_snap.bars if bench_snap else None
    t_years = session.t_years_to_close(now)
    mins_left = session.minutes_to_close(now)

    data: List[SymbolData] = []
    for sym in symbols:
        snap = snaps.get(sym)
        if snap is None:
            continue
        snap.is_benchmark = sym == bench
        pm = compute_price_metrics(snap, OPENING_RANGE_MINUTES, bench_bars)
        if pm is None:
            continue
        om = None
        notes = list(snap.notes)
        if snap.chain:
            om = compute_option_metrics(
                snap.chain, snap.spot, t_years, cfg.risk_free, cfg.chain_em_window
            )
        if om is None:
            notes.append("no-chain")
        data.append(
            SymbolData(
                symbol=sym,
                price=pm,
                options=om,
                minutes_to_close=mins_left,
                expiry=snap.expiry,
                is_benchmark=snap.is_benchmark,
                notes=notes,
            )
        )

    scores = score_universe(data, cfg)
    if args.only_tradeable:
        scores = [s for s in scores if not s.verdict.startswith(("NO TRADE", "AVOID"))]
    if args.top:
        scores = scores[: args.top]
    if not scores:
        print("Nothing cleared the filters.", file=sys.stderr)
        return 0

    print(report.render_console(scores, now, provider.name, mins_left, args.detail))

    meta = {
        "version": __version__,
        "scanned_at": now.isoformat(),
        "provider": provider.name,
        "benchmark": bench,
        "bar_interval": args.interval,
        "minutes_to_close": round(mins_left),
        "weights": cfg.weights.as_dict(),
    }
    if args.csv:
        print(f"csv  -> {report.write_csv(args.csv, scores)}", file=sys.stderr)
    if args.json_path:
        print(f"json -> {report.write_json(args.json_path, scores, meta)}", file=sys.stderr)
    if args.html:
        print(f"html -> {report.write_html(args.html, scores, meta)}", file=sys.stderr)
    return 0


def main() -> None:  # pragma: no cover
    sys.exit(run())
