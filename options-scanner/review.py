#!/usr/bin/env python3
"""Forward-test the scan journal against what the underlyings actually did.

  ./review.py                       # review out/journal.jsonl
  ./review.py --detail              # ...and list every entry
  ./review.py --since 2026-08-01    # only expiries on or after this date
  ./review.py --json                # machine-readable

Run the scanner daily for a few weeks first — a handful of entries tells you nothing.
This is how you find out whether the weights are worth trading before you size up.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from odte import review as review_mod
from odte.config import Config
from odte.providers import PROVIDERS, resolve


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--journal", default=None, help="path to journal.jsonl (default out/journal.jsonl)")
    p.add_argument("--provider", default=None, choices=list(PROVIDERS))
    p.add_argument("--config", default=None)
    p.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                   help="only evaluate expiries on or after this date")
    p.add_argument("--detail", action="store_true", help="list every entry, not just the summary")
    p.add_argument("--json", dest="as_json", action="store_true")
    p.add_argument("--min-entries", type=int, default=1,
                   help="refuse to summarise fewer than this many entries (default 1)")
    args = p.parse_args(argv)

    cfg = Config.load(args.config)
    if args.provider:
        cfg.provider = args.provider
    journal = args.journal or os.path.join(cfg.out_dir, "journal.jsonl")

    since = None
    if args.since:
        try:
            since = dt.datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            p.error(f"--since expects YYYY-MM-DD, got {args.since!r}")

    try:
        outcomes, summary = review_mod.review(journal, resolve(cfg.provider), since=since)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if summary["evaluated"] < args.min_entries:
        print(f"Only {summary['evaluated']} entries could be evaluated "
              f"(minimum {args.min_entries}). Keep scanning.", file=sys.stderr)
        if not args.as_json:
            return 1

    if args.as_json:
        print(json.dumps({"summary": summary,
                          "outcomes": [o.as_dict() for o in outcomes]}, indent=2, default=str))
    else:
        print(review_mod.render_terminal(outcomes, summary, detail=args.detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
