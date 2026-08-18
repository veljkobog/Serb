#!/usr/bin/env python3
"""BBB scraper -- pull business listings by vertical + geography into a CSV.

Feeds the standard pipeline (clean -> Apollo enrich -> HubSpot dedupe ->
Smartlead). Emails are not on BBB; enrichment happens downstream.

    python scraper.py --category plumber --location wilmington-nc --max-results 100

Approach A (JSON API) is tried first and Approach B (Playwright) is the
fallback. The API endpoint is never hardcoded -- see api_client.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

import api_client
import parse
from checkpoint import Checkpoint, listing_id


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scraper.py",
        description="Pull BBB business listings by category + location into a CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "endpoint discovery:\n"
            "  --from-har search.har --save-endpoints endpoints.json    (recommended, one-time)\n"
            "  --endpoints endpoints.json                               (subsequent runs)\n"
            "  --discover                                               (best-effort probe)\n"
            "  --browser                                                (skip the API entirely)\n"
        ),
    )
    p.add_argument("--category", required=True,
                   help="BBB category slug, e.g. heating-and-air-conditioning, plumber, roofing-contractors")
    p.add_argument("--location", required=True, help="city+state or state, e.g. charlotte-nc or nc")
    p.add_argument("--max-results", type=int, default=500, help="cap per run (default: 500)")
    p.add_argument("--min-years", type=int, default=0,
                   help="drop listings with fewer years in business (default: 0; 10 is the useful signal)")
    p.add_argument("--output", default=None, help="CSV path (default: <category>-<location>.csv)")

    src = p.add_argument_group("endpoint source")
    src.add_argument("--endpoints", help="JSON config of candidate API endpoints")
    src.add_argument("--from-har", help="HAR exported from the browser Network tab; mine it for the search XHR")
    src.add_argument("--save-endpoints", help="write the endpoints discovered this run to this path")
    src.add_argument("--save-cookies", action="store_true",
                     help="include session cookies in --save-endpoints (they are session credentials)")
    src.add_argument("--discover", action="store_true", help="probe the live search page for API routes")
    src.add_argument("--browser", action="store_true", help="skip Approach A, go straight to Playwright")
    src.add_argument("--no-fallback", action="store_true", help="fail instead of falling back to Playwright")

    beh = p.add_argument_group("behavior")
    beh.add_argument("--skip", type=int, default=0, help="skip this many result pages (manual resume)")
    beh.add_argument("--checkpoint", default=None,
                     help="checkpoint file for resume (default: .checkpoint-<category>-<location>.json)")
    beh.add_argument("--no-resume", action="store_true", help="ignore any existing checkpoint")
    beh.add_argument("--no-detail", action="store_true",
                     help="never visit detail pages, even for missing years/accreditation")
    beh.add_argument("--max-detail", type=int, default=100,
                     help="cap detail-page visits per run (default: 100)")
    beh.add_argument("--min-delay", type=float, default=2.0, help="min seconds between requests (default: 2)")
    beh.add_argument("--max-delay", type=float, default=4.0, help="max seconds between requests (default: 4)")
    beh.add_argument("--headed", action="store_true", help="run Playwright headed (helps with challenges)")
    beh.add_argument("--profile-dir", default=".bbb-browser-profile",
                     help="persistent browser profile dir (keeps cookies between runs)")
    beh.add_argument("-v", "--verbose", action="store_true")
    return p


def default_output(category: str, location: str) -> str:
    return f"{category}-{location}.csv"


def lowconfidence_path(output: str) -> str:
    base, ext = os.path.splitext(output)
    return f"{base}_lowconfidence{ext or '.csv'}"


def default_checkpoint(category: str, location: str) -> str:
    return f".checkpoint-{category}-{location}.json"


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------

class Collector:
    """Accumulates listings, enforcing the run cap and the resume set."""

    def __init__(self, max_results: int, checkpoint: Checkpoint, verbose: bool = False):
        self.max_results = max_results
        self.checkpoint = checkpoint
        self.verbose = verbose
        self.listings: List[parse.Listing] = []
        self.pages_fetched = 0

    @property
    def full(self) -> bool:
        return len(self.listings) >= self.max_results

    def add_page(self, page: int, listings: List[parse.Listing]) -> None:
        fresh = []
        for listing in listings:
            if self.full:
                break
            if self.checkpoint.seen(listing_id(listing)):
                continue
            self.listings.append(listing)
            fresh.append(listing)
        self.pages_fetched += 1
        self.checkpoint.record(page, (listing_id(l) for l in fresh), count_delta=len(fresh))
        self.checkpoint.save()
        if self.verbose:
            print(f"[run] page {page}: +{len(fresh)} (total {len(self.listings)})", flush=True)


def collect_via_api(args, checkpoint: Checkpoint, collector: Collector) -> bool:
    """Approach A. Returns True if it produced anything, False to fall back."""
    limiter = api_client.RateLimiter(min_delay=args.min_delay, max_delay=args.max_delay)
    specs: List[api_client.EndpointSpec] = []

    if args.from_har:
        specs.extend(api_client.endpoints_from_har(args.from_har))
        if args.verbose:
            print(f"[run] {len(specs)} candidate endpoint(s) from HAR", flush=True)
    if args.endpoints:
        specs.extend(api_client.load_endpoints(args.endpoints))

    client = api_client.ApiClient(limiter=limiter, verbose=args.verbose)
    try:
        if args.discover or not specs:
            try:
                discovered = api_client.discover_endpoints(
                    client.client, args.category, args.location, limiter=limiter
                )
                if args.verbose:
                    print(f"[run] {len(discovered)} candidate endpoint(s) discovered", flush=True)
                specs.extend(discovered)
            except api_client.BlockedError as exc:
                print(f"[run] discovery blocked: {exc}", file=sys.stderr)

        if not specs:
            raise api_client.EndpointUnavailable(
                "no candidate endpoints -- capture a HAR (--from-har) or use --browser"
            )

        spec = client.select_endpoint(specs, args.category, args.location)
        print(f"[run] using endpoint {spec.url} (source: {spec.source})")

        if args.save_endpoints:
            api_client.save_endpoints(args.save_endpoints, [spec], include_cookies=args.save_cookies)
            print(f"[run] saved endpoint config -> {args.save_endpoints}")

        start_page = max(checkpoint.next_page(), args.skip + 1)
        for page, payload in client.iter_pages(args.category, args.location, start_page=start_page):
            listings = list(parse.iter_listings_from_payload(payload, default_category=args.category))
            collector.add_page(page, listings)
            if collector.full:
                break
        return True

    except api_client.BlockedError as exc:
        print(f"[run] blocked: {exc} -- keeping partial results", file=sys.stderr)
        return bool(collector.listings)
    except api_client.EndpointUnavailable as exc:
        print(f"[run] Approach A unavailable: {exc}", file=sys.stderr)
        return False
    finally:
        client.close()


def collect_via_browser(args, checkpoint: Checkpoint, collector: Collector) -> bool:
    """Approach B. Returns True if it produced anything."""
    import browser_client

    start_page = max(checkpoint.next_page(), args.skip + 1)
    try:
        with browser_client.BrowserClient(
            user_data_dir=args.profile_dir,
            headless=not args.headed,
            min_delay=args.min_delay,
            max_delay=args.max_delay,
            verbose=args.verbose,
        ) as browser:
            for page, listings in browser.iter_listings(
                args.category, args.location, start_page=start_page
            ):
                collector.add_page(page, listings)
                if collector.full:
                    break

            if not args.no_detail:
                enrich_details(browser, collector.listings, args)
        return True
    except browser_client.BrowserUnavailable as exc:
        print(f"[run] Approach B unavailable: {exc}", file=sys.stderr)
        return False


def enrich_details(browser, listings: List[parse.Listing], args) -> int:
    """Visit detail pages only for records missing years/accreditation.

    Request volume is the scarce resource here, so complete records are never
    re-fetched and the visit count is capped.
    """
    pending = [l for l in listings if l.needs_detail() and l.profile_url][: args.max_detail]
    if not pending:
        return 0
    print(f"[run] visiting {len(pending)} detail page(s) for missing fields")
    filled = 0
    for listing in pending:
        try:
            listing.merge(browser.fetch_detail(listing.profile_url))
            filled += 1
        except Exception as exc:
            if args.verbose:
                print(f"[run] detail failed for {listing.profile_url}: {exc}", file=sys.stderr)
    return filled


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args) -> int:
    output = args.output or default_output(args.category, args.location)
    ckpt_path = args.checkpoint or default_checkpoint(args.category, args.location)

    checkpoint = Checkpoint(category=args.category, location=args.location, path=ckpt_path)
    if not args.no_resume:
        checkpoint = Checkpoint.load(ckpt_path, args.category, args.location)
        if checkpoint.last_page:
            print(f"[run] resuming after page {checkpoint.last_page} "
                  f"({len(checkpoint.collected_ids)} ids already collected)")

    collector = Collector(args.max_results, checkpoint, verbose=args.verbose)

    used_browser = False
    if args.browser:
        used_browser = True
        collect_via_browser(args, checkpoint, collector)
    else:
        ok = collect_via_api(args, checkpoint, collector)
        if not ok and not args.no_fallback:
            print("[run] falling back to Approach B (Playwright)")
            used_browser = True
            collect_via_browser(args, checkpoint, collector)

    return finish(args, collector, output, checkpoint, used_browser)


def finish(args, collector: Collector, output: str, checkpoint: Checkpoint, used_browser: bool) -> int:
    pulled = len(collector.listings)
    unique, dupes = parse.dedupe(collector.listings)

    filtered = unique
    dropped_years = 0
    if args.min_years > 0:
        # Unknown years are not "fewer than N" -- keeping them beats guessing.
        filtered = [l for l in unique if l.years_in_business is None or l.years_in_business >= args.min_years]
        dropped_years = len(unique) - len(filtered)

    main_rows = [l for l in filtered if not l.is_low_confidence()]
    low_rows = [l for l in filtered if l.is_low_confidence()]

    parse.write_csv(output, main_rows)
    low_path = ""
    if low_rows:
        low_path = lowconfidence_path(output)
        parse.write_csv(low_path, low_rows)

    with_website = sum(1 for l in main_rows if l.website)
    pct = (with_website / len(main_rows) * 100) if main_rows else 0.0

    print("")
    print("run summary")
    print("-----------")
    print(f"  approach        : {'B (playwright)' if used_browser else 'A (json api)'}")
    print(f"  pages fetched   : {collector.pages_fetched}")
    print(f"  pulled          : {pulled}")
    print(f"  deduped         : {len(dupes)}")
    if args.min_years > 0:
        print(f"  below min-years : {dropped_years}")
    print(f"  low-confidence  : {len(low_rows)}")
    print(f"  written         : {len(main_rows)} -> {output}")
    if low_path:
        print(f"                    {len(low_rows)} -> {low_path}")
    print(f"  website coverage: {with_website}/{len(main_rows)} ({pct:.0f}%)")

    if pulled and not main_rows:
        return 1
    if not pulled:
        print("  (nothing collected -- see errors above)")
        return 1

    if not collector.full:
        # Results exhausted rather than capped; the checkpoint has nothing left
        # to resume and would only cause a later run to skip real pages.
        checkpoint.clear()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.min_delay > args.max_delay:
        print("--min-delay cannot exceed --max-delay", file=sys.stderr)
        return 2
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\ninterrupted -- checkpoint saved, rerun the same command to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
