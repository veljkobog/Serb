#!/usr/bin/env python3
"""Start the press-the-button scanner UI.

  ./serve.py                     # opens http://127.0.0.1:8765 in your browser
  ./serve.py --auto 09:35        # also scans automatically at 9:35 ET each trading day
  ./serve.py --provider tradier  # use a real-time chain instead of delayed Yahoo
  ./serve.py --demo              # synthetic data, works any time, no network

Press SCAN in the page. It pulls live data at that moment (bypassing the cache),
shows progress, and renders the top setups with a full strike ladder for each.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odte.config import Config
from odte.providers import PROVIDERS
from odte.webapp import serve


def _valid_time(value: str) -> str:
    try:
        hour, minute = (int(x) for x in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected HH:MM in ET, got {value!r}")
    return f"{hour:02d}:{minute:02d}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default localhost; only widen deliberately)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--provider", default=None, choices=list(PROVIDERS))
    p.add_argument("--config", default=None, help="JSON config file")
    p.add_argument("--auto", type=_valid_time, default=None, metavar="HH:MM",
                   help="also scan automatically at this ET time on trading days, "
                        "e.g. --auto 09:35 for just after the open")
    p.add_argument("--universe", default=None, help="universe preset used by --auto")
    p.add_argument("--demo", action="store_true", help="synthetic data — no network, no keys")
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    args = p.parse_args(argv)

    cfg = Config.load(args.config)
    if args.provider:
        cfg.provider = args.provider

    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    auto_settings = {"universe": args.universe} if args.universe else {}
    serve(cfg, host=args.host, port=args.port, auto_at=args.auto, demo=args.demo,
          auto_settings=auto_settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
