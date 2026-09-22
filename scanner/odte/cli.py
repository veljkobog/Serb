"""Command line entry point.

    python -m odte                 scan the universe and print the board
    python -m odte record ...      stream classified time & sales to a side tape

`scan` is the default, so the subcommand can be left off.
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

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
from .sides import SideTape

SUBCOMMANDS = ("scan", "record")


def _add_universe_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--universe", default="all", choices=["index", "equity", "all"])
    p.add_argument("--symbols", help="comma-separated override, e.g. SPY,QQQ,NVDA")
    p.add_argument("--benchmark", default="SPY", help="relative-strength benchmark")


def _add_tradier_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tradier-token", help="defaults to $TRADIER_TOKEN")
    p.add_argument(
        "--tradier-env",
        choices=["production", "sandbox"],
        help="defaults to $TRADIER_ENV, else production",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odte",
        description="0DTE options bias scanner for index ETFs and heavy-volume names.",
    )
    p.add_argument("--version", action="version", version=f"odte {__version__}")
    subs = p.add_subparsers(dest="command")

    scan = subs.add_parser("scan", help="score the universe (default)")
    _add_universe_args(scan)
    _add_tradier_args(scan)
    scan.add_argument(
        "--provider", default="tradier", choices=["tradier", "yahoo", "synthetic", "snapshot"]
    )
    scan.add_argument("--snapshot-path", help="snapshot file to replay (--provider snapshot)")
    scan.add_argument("--interval", default="5m", help="intraday bar interval (1m/5m/15m)")
    scan.add_argument("--as-of", help="ISO timestamp to score as (replay/testing)")
    scan.add_argument("--force", action="store_true", help="scan outside the advised window")
    scan.add_argument("--sides", help="side tape from `odte record` to merge in")
    scan.add_argument(
        "--stream-seconds", type=float, default=0.0,
        help="sample classified time & sales inline for N seconds before scoring",
    )
    scan.add_argument(
        "--stream-contracts", type=int, default=40,
        help="contracts per symbol to subscribe to when streaming",
    )
    scan.add_argument("--top", type=int, default=0, help="only print the N strongest setups")
    scan.add_argument("--detail", type=int, default=3, help="detail blocks to print")
    scan.add_argument("--only-tradeable", action="store_true", help="drop NO TRADE / AVOID rows")
    scan.add_argument("--min-bias", type=float, default=Gates.min_bias_to_trade)
    scan.add_argument("--min-confidence", type=float, default=Gates.min_confidence_to_trade)
    scan.add_argument("--workers", type=int, default=8)
    scan.add_argument("--csv", help="write a flat CSV of every metric here")
    scan.add_argument("--json", dest="json_path", help="write full JSON output here")
    scan.add_argument("--html", help="write a standalone HTML table here")
    scan.add_argument("--save-snapshot", help="write the raw pulled data here for replay")

    rec = subs.add_parser(
        "record", help="stream and classify 0DTE time & sales into a side tape"
    )
    _add_universe_args(rec)
    _add_tradier_args(rec)
    rec.add_argument("--out", required=True, help="side tape file to write")
    rec.add_argument("--minutes", type=float, default=60.0, help="how long to record")
    rec.add_argument("--until", help="stop at this ET clock time, e.g. 10:30")
    rec.add_argument("--contracts", type=int, default=40, help="contracts per symbol")
    rec.add_argument("--append", action="store_true", help="fold into an existing tape")
    rec.add_argument("--flush-seconds", type=float, default=30.0, help="how often to save")
    return p


def resolve_symbols(args: argparse.Namespace) -> List[str]:
    if getattr(args, "symbols", None):
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.universe == "index":
        syms = list(INDEX_UNIVERSE)
    elif args.universe == "equity":
        syms = list(EQUITY_UNIVERSE)
    else:
        syms = INDEX_UNIVERSE + EQUITY_UNIVERSE
    bench = getattr(args, "benchmark", "SPY").upper()
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


def _merge_sides(
    snaps: Dict[str, SymbolSnapshot], tape: SideTape, source: str
) -> int:
    hits = 0
    for snap in snaps.values():
        touched = tape.merge_into(snap.chain)
        if touched:
            hits += touched
            snap.side_window_seconds = tape.window_seconds
            snap.notes.append(f"sides:{source}")
    return hits


def _sample_sides_inline(provider, snaps: Dict[str, SymbolSnapshot], args) -> None:
    """Stream time & sales for `--stream-seconds`, then merge what was classified."""
    from .providers.tradier import TradierProvider, TradierTapeStream, liquid_contracts

    if not isinstance(provider, TradierProvider):
        print("--stream-seconds needs the tradier provider; skipping.", file=sys.stderr)
        return
    contracts: List[str] = []
    for snap in snaps.values():
        contracts.extend(liquid_contracts(snap, args.stream_contracts))
    if not contracts:
        print("no 0DTE contracts to stream.", file=sys.stderr)
        return
    print(
        f"streaming {len(contracts)} contracts for {args.stream_seconds:.0f}s...",
        file=sys.stderr,
    )
    stream = TradierTapeStream(provider.client)
    tape = stream.run_for(contracts, args.stream_seconds)
    if stream.error:
        print(f"  ! stream: {type(stream.error).__name__}: {stream.error}", file=sys.stderr)
    _merge_sides(snaps, tape, "inline")
    print(f"classified {tape.trades:,} prints", file=sys.stderr)


def run_scan(args: argparse.Namespace) -> int:
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

    try:
        provider = get_provider(
            args.provider,
            bar_interval=args.interval,
            path=args.snapshot_path,
            token=args.tradier_token,
            env=args.tradier_env,
        )
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    symbols = resolve_symbols(args)
    if args.provider == "snapshot" and not args.symbols:
        symbols = list(provider.symbols)
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

    if args.sides:
        tape = SideTape.load(args.sides)
        hits = _merge_sides(snaps, tape, Path(args.sides).name)
        print(
            f"side tape: {tape.trades:,} prints over "
            f"{(tape.window_seconds or 0) / 60:.0f} min -> {hits} contracts",
            file=sys.stderr,
        )
    if args.stream_seconds > 0:
        _sample_sides_inline(provider, snaps, args)

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

    label = provider.name
    if args.provider == "tradier":
        label += f":{provider.env}"
    print(report.render_console(scores, now, label, mins_left, args.detail))

    meta = {
        "version": __version__,
        "scanned_at": now.isoformat(),
        "provider": label,
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


def _record_deadline(args: argparse.Namespace, now: datetime) -> datetime:
    if args.until:
        hh, mm = (int(x) for x in args.until.split(":"))
        stop = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if stop <= now:
            stop += timedelta(days=1)
        return stop
    return now + timedelta(minutes=args.minutes)


def run_record(args: argparse.Namespace) -> int:
    from .providers.tradier import (
        TradierClient,
        TradierProvider,
        TradierTapeStream,
        liquid_contracts,
    )

    try:
        client = TradierClient(token=args.tradier_token, env=args.tradier_env)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    provider = TradierProvider(client=client)

    now = session.now_et()
    deadline = _record_deadline(args, now)
    symbols = resolve_symbols(args)
    snaps = fetch_all(provider, symbols, now, workers=4)
    contracts: List[str] = []
    for snap in snaps.values():
        contracts.extend(liquid_contracts(snap, args.contracts))
    if not contracts:
        print("No 0DTE contracts found to record.", file=sys.stderr)
        return 1

    tape = SideTape.load(args.out) if args.append and Path(args.out).exists() else SideTape()
    stream = TradierTapeStream(client, tape=tape)
    print(
        f"recording {len(contracts)} contracts across {len(snaps)} symbols "
        f"until {deadline:%H:%M} ET -> {args.out}",
        file=sys.stderr,
    )
    stream.start(contracts)
    try:
        while session.now_et() < deadline and not stream.error:
            time.sleep(min(args.flush_seconds, 5.0))
            tape.save(args.out)
    except KeyboardInterrupt:
        print("interrupted; saving what was recorded.", file=sys.stderr)
    finally:
        stream.stop()
        tape.save(args.out)
    if stream.error:
        print(f"stream ended early: {stream.error}", file=sys.stderr)
    print(
        f"{tape.trades:,} prints across {len(tape.contracts)} contracts -> {args.out}",
        file=sys.stderr,
    )
    return 0 if tape.trades else 1


def run(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `scan` is the default subcommand, so plain flags still work.
    if not argv or argv[0] not in SUBCOMMANDS + ("--version", "-h", "--help"):
        argv = ["scan"] + argv
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return run_record(args)
    return run_scan(args)


def main() -> None:  # pragma: no cover
    sys.exit(run())
