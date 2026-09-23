"""`odte live` — the whole session in one command.

Start it in the morning and leave it:

    09:25  ./bullbear live
           -> records and classifies 0DTE prints from the open
    10:05  -> prints the opening read
    10:50  -> re-confirms it
    11:35  -> re-confirms again ... until you stop it

Everything the other commands do separately (record, go, go again) on one
timer, so a live day is one command and no clock-watching.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from . import session
from .sides import SideTape

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]

TICK = 2.0  # how often the wait loop wakes to flush the tape / catch ctrl-c


def parse_clock_time(now: datetime, text: str) -> datetime:
    """'10:05' -> today at 10:05 ET. 'now' starts immediately. Past stays past."""
    if text.strip().lower() in ("now", "asap"):
        return now
    hh, _, mm = text.partition(":")
    return now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)


def press_times(
    now: datetime, first: str, every_minutes: float, until: str, max_presses: int
) -> List[datetime]:
    """When the button gets pressed today, first press included."""
    start = parse_clock_time(now, first)
    if start < now:
        start = now
    stop = parse_clock_time(now, until)
    if stop < start:
        # Asking for "now" is an explicit instruction, so honour it even past
        # the stop time. A schedule that has simply run out returns nothing.
        return [start] if first.strip().lower() in ("now", "asap") else []
    times: List[datetime] = []
    t = start
    while t <= stop and len(times) < max_presses:
        times.append(t)
        if every_minutes <= 0:
            break
        t = t + timedelta(minutes=every_minutes)
    return times


def _wait_until(
    target: datetime, clock: Clock, sleeper: Sleeper, on_tick: Optional[Callable] = None
) -> None:
    announced = None
    while True:
        now = clock()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            return
        if on_tick:
            on_tick()
        minutes = int(remaining // 60)
        if minutes != announced and (minutes % 5 == 0 or minutes < 5):
            announced = minutes
            print(
                f"  ... {target:%H:%M} ET in {minutes}m{int(remaining % 60):02d}s",
                file=sys.stderr,
            )
        sleeper(min(TICK, remaining))


def run_live(
    args: argparse.Namespace,
    clock: Clock = session.now_et,
    sleeper: Sleeper = time.sleep,
) -> int:
    from .cli import ScanFailed, press_once

    now = clock()
    out_dir = Path(args.out_dir)
    schedule = press_times(now, args.first, args.every, args.until, args.presses)
    if not schedule:
        print(
            f"Nothing to do: {args.first}-{args.until} ET has already passed "
            f"(it is {now:%H:%M}). Press the button directly with ./bullbear.",
            file=sys.stderr,
        )
        return 2

    print(
        f"live session  |  {len(schedule)} press(es): "
        + ", ".join(f"{t:%H:%M}" for t in schedule)
        + f"  |  out: {out_dir}"
    )

    stream = None
    tape_path = out_dir / f"sides-{now:%Y-%m-%d}.json"
    if args.record and args.provider == "tradier":
        stream = _start_recording(args, now, tape_path)
    elif args.record:
        print(
            f"  (trade-side recording needs the tradier provider; "
            f"running {args.provider} without it)",
            file=sys.stderr,
        )

    def flush() -> None:
        if stream is not None and stream.tape.trades:
            stream.tape.save(tape_path)

    code = 0
    try:
        for i, when in enumerate(schedule, start=1):
            _wait_until(when, clock, sleeper, on_tick=flush)
            flush()
            if stream is not None and stream.error:
                print(f"  ! stream stopped: {stream.error}", file=sys.stderr)
            try:
                press_once(
                    args, clock(), out_dir, top=args.top,
                    save=True, fresh=args.fresh and i == 1,
                )
            except ScanFailed as exc:
                print(f"  ! press {i}: {exc}", file=sys.stderr)
                code = exc.code
                if exc.code == 2:  # configuration, not data: stop trying
                    break
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
    finally:
        if stream is not None:
            stream.stop()
            flush()
            print(
                f"  side tape: {stream.tape.trades:,} prints across "
                f"{len(stream.tape.contracts)} contracts -> {tape_path}",
                file=sys.stderr,
            )
    return code


def _start_recording(args: argparse.Namespace, now: datetime, tape_path: Path):
    """Begin classifying 0DTE prints in the background. None if it can't start."""
    from .cli import fetch_all
    from .providers.tradier import (
        TradierClient,
        TradierProvider,
        TradierTapeStream,
        liquid_contracts,
    )
    from .cli import resolve_symbols

    try:
        client = TradierClient(token=args.tradier_token, env=args.tradier_env)
        provider = TradierProvider(client=client)
        snaps = fetch_all(provider, resolve_symbols(args), now, workers=4)
        contracts: List[str] = []
        for snap in snaps.values():
            contracts.extend(liquid_contracts(snap, args.stream_contracts))
        if not contracts:
            print("  (no 0DTE contracts to record yet)", file=sys.stderr)
            return None
        tape = SideTape.load(tape_path) if tape_path.exists() else SideTape()
        stream = TradierTapeStream(client, tape=tape)
        stream.start(contracts)
        print(
            f"  recording trade side on {len(contracts)} contracts "
            f"across {len(snaps)} symbols",
            file=sys.stderr,
        )
        return stream
    except Exception as exc:
        print(f"  ! could not start recording: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
