"""Command line entry point.

    python -m odte                 the button: BULL/BEAR out of ten per symbol
    python -m odte                 (press again later to re-confirm the read)
    python -m odte scan            the full board, every metric
    python -m odte record ...      stream classified time & sales to a side tape
    python -m odte serve           the same button, in a browser

`go` is the default, so the subcommand can be left off.
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
from .runs import Run, RunLog, compare, entry_from_score
from .scoring import SymbolData, score_universe
from .sides import SideTape

SUBCOMMANDS = ("go", "scan", "record", "serve")


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


def _add_scan_core_args(p: argparse.ArgumentParser) -> None:
    """Everything both `go` and `scan` need to pull and score the data."""
    p.add_argument(
        "--provider", default="tradier", choices=["tradier", "yahoo", "synthetic", "snapshot"]
    )
    p.add_argument("--snapshot-path", help="snapshot file to replay (--provider snapshot)")
    p.add_argument("--interval", default="5m", help="intraday bar interval (1m/5m/15m)")
    p.add_argument("--as-of", help="ISO timestamp to score as (replay/testing)")
    p.add_argument("--force", action="store_true", help="run outside the advised window")
    p.add_argument("--sides", help="side tape from `odte record` to merge in")
    p.add_argument(
        "--stream-seconds", type=float, default=0.0,
        help="sample classified time & sales inline for N seconds before scoring",
    )
    p.add_argument(
        "--stream-contracts", type=int, default=40,
        help="contracts per symbol to subscribe to when streaming",
    )
    p.add_argument("--min-bias", type=float, default=Gates.min_bias_to_trade)
    p.add_argument("--min-confidence", type=float, default=Gates.min_confidence_to_trade)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--save-snapshot", help="write the raw pulled data here for replay")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odte",
        description="0DTE options bias scanner for index ETFs and heavy-volume names.",
    )
    p.add_argument("--version", action="version", version=f"odte {__version__}")
    subs = p.add_subparsers(dest="command")

    go = subs.add_parser(
        "go", help="the button: one press, BULL/BEAR out of ten, press again to re-confirm"
    )
    _add_universe_args(go)
    _add_tradier_args(go)
    _add_scan_core_args(go)
    go.add_argument("--top", type=int, default=5, help="cards to print")
    go.add_argument("--out-dir", default="out", help="where runs and outputs are kept")
    go.add_argument("--fresh", action="store_true", help="start a new press chain today")
    go.add_argument("--no-save", action="store_true", help="don't record this press")

    scan = subs.add_parser("scan", help="the full board: every metric, one row per symbol")
    _add_universe_args(scan)
    _add_tradier_args(scan)
    _add_scan_core_args(scan)
    scan.add_argument("--top", type=int, default=0, help="only print the N strongest setups")
    scan.add_argument("--detail", type=int, default=3, help="detail blocks to print")
    scan.add_argument("--only-tradeable", action="store_true", help="drop NO TRADE / AVOID rows")
    scan.add_argument("--csv", help="write a flat CSV of every metric here")
    scan.add_argument("--json", dest="json_path", help="write full JSON output here")
    scan.add_argument("--html", help="write a standalone HTML table here")

    serve = subs.add_parser("serve", help="the same button, in a browser")
    _add_universe_args(serve)
    _add_tradier_args(serve)
    _add_scan_core_args(serve)
    serve.add_argument("--host", default="127.0.0.1", help="loopback by default, on purpose")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--top", type=int, default=8, help="cards to show")
    serve.add_argument("--out-dir", default="out")
    serve.add_argument("--open", action="store_true", help="open a browser tab on start")

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


class ScanFailed(Exception):
    """Raised with an exit code when a scan can't be produced."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def resolve_now(args: argparse.Namespace) -> datetime:
    return (
        session.to_et(datetime.fromisoformat(args.as_of))
        if getattr(args, "as_of", None)
        else session.now_et()
    )


def perform_scan(args: argparse.Namespace, now: datetime):
    """Pull, merge side data, score. Shared by `go`, `scan` and `serve`."""
    if not session.in_scan_window(now) and not args.force:
        raise ScanFailed(
            f"Outside the advised scan window ({session.window_label()}); "
            f"now {now:%H:%M} ET. Re-run with --force to scan anyway.",
            2,
        )

    if args.provider == "snapshot" and not args.snapshot_path:
        raise ScanFailed("--provider snapshot requires --snapshot-path", 2)

    try:
        provider = get_provider(
            args.provider,
            bar_interval=args.interval,
            path=args.snapshot_path,
            token=args.tradier_token,
            env=args.tradier_env,
        )
    except Exception as exc:
        raise ScanFailed(f"{type(exc).__name__}: {exc}", 2) from exc

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
        raise ScanFailed("No data returned for any symbol.", 1)

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
    label = provider.name
    if args.provider == "tradier":
        label += f":{provider.env}"
    return scores, label, mins_left


def run_scan(args: argparse.Namespace) -> int:
    now = resolve_now(args)
    try:
        scores, label, mins_left = perform_scan(args, now)
    except ScanFailed as exc:
        print(str(exc), file=sys.stderr)
        return exc.code

    if args.only_tradeable:
        scores = [s for s in scores if not s.verdict.startswith(("NO TRADE", "AVOID"))]
    if args.top:
        scores = scores[: args.top]
    if not scores:
        print("Nothing cleared the filters.", file=sys.stderr)
        return 0

    print(report.render_console(scores, now, label, mins_left, args.detail))

    meta = {
        "version": __version__,
        "scanned_at": now.isoformat(),
        "provider": label,
        "benchmark": args.benchmark.upper(),
        "bar_interval": args.interval,
        "minutes_to_close": round(mins_left),
        "weights": Weights().as_dict(),
    }
    if args.csv:
        print(f"csv  -> {report.write_csv(args.csv, scores)}", file=sys.stderr)
    if args.json_path:
        print(f"json -> {report.write_json(args.json_path, scores, meta)}", file=sys.stderr)
    if args.html:
        print(f"html -> {report.write_html(args.html, scores, meta)}", file=sys.stderr)
    return 0


def run_go(args: argparse.Namespace) -> int:
    """The button. One press scores the day; the next press grades that read."""
    now = resolve_now(args)
    out_dir = Path(args.out_dir)

    # Use today's recorded side tape automatically when one is sitting there.
    if not args.sides:
        auto = out_dir / f"sides-{now:%Y-%m-%d}.json"
        if auto.exists():
            args.sides = str(auto)

    try:
        scores, label, mins_left = perform_scan(args, now)
    except ScanFailed as exc:
        print(str(exc), file=sys.stderr)
        return exc.code

    log = RunLog.for_date(out_dir / "runs", now)
    if args.fresh:
        log.runs.clear()
    baseline = log.first
    press = log.next_press
    changes = {}
    if not args.no_save:
        run = log.record(scores, now, label)
        changes = compare(run, baseline)
        press = run.press
    elif baseline is not None:
        changes = compare(
            Run(ts=now.isoformat(), press=press, provider=label,
                entries={s.symbol: entry_from_score(s) for s in scores}),
            baseline,
        )

    print(report.render_cards(scores, now, label, mins_left, press, changes, args.top))

    meta = {
        "version": __version__,
        "scanned_at": now.isoformat(),
        "provider": label,
        "press": press,
        "minutes_to_close": round(mins_left),
    }
    if not args.no_save:
        report.write_json(out_dir / "latest.json", scores, meta)
        report.write_csv(out_dir / "latest.csv", scores)
        report.write_html(out_dir / "latest.html", scores, meta)
        print(f"  saved: {out_dir}/latest.[json|csv|html]  runs: {log.path}", file=sys.stderr)
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
        argv = ["go"] + argv
    args = build_parser().parse_args(argv)
    if args.command == "record":
        return run_record(args)
    if args.command == "serve":
        from .server import run_serve

        return run_serve(args)
    if args.command == "scan":
        return run_scan(args)
    return run_go(args)


def main() -> None:  # pragma: no cover
    sys.exit(run())
