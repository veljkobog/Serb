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
    p.add_argument("--output", default=None, help="CSV path (default: <category>-<location>.csv)")

    flt = p.add_argument_group(
        "size / quality filters",
        "Unknown values PASS by default -- a missing field is not evidence of a small shop. "
        "Pass --drop-unknown to make every active filter strict instead.",
    )
    flt.add_argument("--min-years", type=int, default=0,
                     help="years in business (default: 0; 10+ is the established-shop signal)")
    flt.add_argument("--min-employees", type=int, default=0,
                     help="headcount from the BBB profile; ranges count as their low end")
    flt.add_argument("--min-bbb-reviews", type=int, default=0, help="BBB customer review count")
    flt.add_argument("--max-bbb-complaints", type=int, default=None,
                     help="drop listings with more than N BBB complaints")
    flt.add_argument("--min-google-reviews", type=int, default=0,
                     help="Google review count (requires --google-key)")
    flt.add_argument("--min-google-rating", type=float, default=0.0,
                     help="Google star rating (requires --google-key)")
    flt.add_argument("--allow-low-match", action="store_true",
                     help="trust low-confidence Google matches when filtering (default: treat as unknown)")
    flt.add_argument("--drop-unknown", action="store_true",
                     help="drop listings whose value for an active filter is unknown")
    flt.add_argument("--rejects", default=None,
                     help="write filtered-out listings here instead of discarding them")

    goo = p.add_argument_group("google enrichment (billed per lookup by Google)")
    goo.add_argument("--google-key", default=None,
                     help="Places API key; falls back to $GOOGLE_MAPS_API_KEY / $GOOGLE_PLACES_API_KEY")
    goo.add_argument("--google-cache", default=".google-places-cache.json",
                     help="lookup cache, so re-runs and resumes cost nothing twice")
    goo.add_argument("--google-cache-ttl", type=int, default=30,
                     help="days before cached Google content is refetched (default: 30)")
    goo.add_argument("--max-google-lookups", type=int, default=500,
                     help="hard cap on billed lookups per run (default: 500)")
    goo.add_argument("--google-endpoint", default=None,
                     help="override the Places endpoint (enterprise proxy, or a test double)")
    goo.add_argument("--google-delay", type=float, default=0.0,
                     help="seconds between Google lookups (default: 0)")

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

class Filter:
    """One named threshold, plus how to read its value off a Listing."""

    def __init__(self, name, getter, predicate, detail_field=None):
        self.name = name
        self.getter = getter
        self.predicate = predicate
        self.detail_field = detail_field
        self.dropped = 0
        self.unknown_dropped = 0

    def keep(self, listing, drop_unknown: bool) -> bool:
        value = self.getter(listing)
        if value is None:
            if drop_unknown:
                self.unknown_dropped += 1
                self.dropped += 1
                return False
            return True          # unknown is not evidence of "too small"
        if self.predicate(value):
            return True
        self.dropped += 1
        return False


def _google_value(field, allow_low_match):
    """Read a google_* field, treating a low-confidence match as unknown.

    A wrong match inflates a business's numbers with someone else's reviews, so
    by default a weak match is 'we don't know' rather than a number to filter on.
    """
    def getter(listing):
        if not listing.google_match:
            return None
        if listing.google_match == "low" and not allow_low_match:
            return None
        return getattr(listing, field)
    return getter


def build_filters(args):
    filters = []
    if args.min_years > 0:
        filters.append(Filter("min-years", lambda l: l.years_in_business,
                              lambda v: v >= args.min_years, "years_in_business"))
    if args.min_employees > 0:
        filters.append(Filter("min-employees", lambda l: l.employees,
                              lambda v: v >= args.min_employees, "employees"))
    if args.min_bbb_reviews > 0:
        filters.append(Filter("min-bbb-reviews", lambda l: l.bbb_reviews,
                              lambda v: v >= args.min_bbb_reviews, "bbb_reviews"))
    if args.max_bbb_complaints is not None:
        filters.append(Filter("max-bbb-complaints", lambda l: l.bbb_complaints,
                              lambda v: v <= args.max_bbb_complaints, "bbb_complaints"))
    if args.min_google_reviews > 0:
        filters.append(Filter("min-google-reviews",
                              _google_value("google_reviews", args.allow_low_match),
                              lambda v: v >= args.min_google_reviews))
    if args.min_google_rating > 0:
        filters.append(Filter("min-google-rating",
                              _google_value("google_rating", args.allow_low_match),
                              lambda v: v >= args.min_google_rating))
    return filters


def apply_filters(listings, filters, drop_unknown: bool):
    """Split into (kept, rejected). Each filter tallies its own drops."""
    kept, rejected = [], []
    for listing in listings:
        if all(f.keep(listing, drop_unknown) for f in filters):
            kept.append(listing)
        else:
            rejected.append(listing)
    return kept, rejected


def detail_fields_for(args, filters) -> set:
    """Which fields this run needs, so detail visits are driven by the filters.

    Without this, --min-employees would silently drop everything (or keep
    everything) because the search card never carries a headcount.
    """
    required = {"years_in_business", "accredited"}
    for f in filters:
        if f.detail_field:
            required.add(f.detail_field)
    return required & parse.DETAIL_FIELDS


def uses_google(args) -> bool:
    return bool(args.google_key or args.min_google_reviews or args.min_google_rating)


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


def collect_via_api(args, checkpoint: Checkpoint, collector: Collector, required_fields=None) -> bool:
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

        if not args.no_detail:
            enrich_details(_http_detail_fetcher(client), collector.listings, args,
                           required=required_fields)
        return True

    except api_client.BlockedError as exc:
        print(f"[run] blocked: {exc} -- keeping partial results", file=sys.stderr)
        return bool(collector.listings)
    except api_client.EndpointUnavailable as exc:
        print(f"[run] Approach A unavailable: {exc}", file=sys.stderr)
        return False
    finally:
        client.close()


def _http_detail_fetcher(client):
    """Detail-page fetcher for Approach A: plain HTTP + the shared DOM parser."""
    from browser_client import listing_from_detail_html

    def fetch(url):
        html = client.fetch_detail_html(url)
        return listing_from_detail_html(html) if html else None

    return fetch


def collect_via_browser(args, checkpoint: Checkpoint, collector: Collector, required_fields=None) -> bool:
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
                enrich_details(browser.fetch_detail, collector.listings, args,
                               required=required_fields)
        return True
    except browser_client.BrowserUnavailable as exc:
        print(f"[run] Approach B unavailable: {exc}", file=sys.stderr)
        return False


def enrich_details(fetch, listings: List[parse.Listing], args, required=None) -> int:
    """Visit detail pages only for records still missing the fields we need.

    `fetch` takes a profile URL and returns a Listing of whatever it found.
    Request volume is the scarce resource, so complete records are never
    re-fetched and the visit count is capped by --max-detail.
    """
    pending = [l for l in listings if l.needs_detail(required) and l.profile_url][: args.max_detail]
    if not pending:
        return 0
    print(f"[run] visiting {len(pending)} detail page(s) for missing fields")
    filled = 0
    for listing in pending:
        try:
            detail = fetch(listing.profile_url)
        except api_client.BlockedError as exc:
            print(f"[run] detail pass stopped: {exc}", file=sys.stderr)
            break
        except Exception as exc:
            if args.verbose:
                print(f"[run] detail failed for {listing.profile_url}: {exc}", file=sys.stderr)
            continue
        if detail is not None:
            listing.merge(detail)
            filled += 1
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
    filters = build_filters(args)
    required_fields = detail_fields_for(args, filters)

    used_browser = False
    if args.browser:
        used_browser = True
        collect_via_browser(args, checkpoint, collector, required_fields)
    else:
        ok = collect_via_api(args, checkpoint, collector, required_fields)
        if not ok and not args.no_fallback:
            print("[run] falling back to Approach B (Playwright)")
            used_browser = True
            collect_via_browser(args, checkpoint, collector, required_fields)

    if args.no_detail:
        blind = {f.detail_field for f in filters if f.detail_field} & parse.DETAIL_FIELDS
        if blind:
            print(f"[run] warning: --no-detail with filters on {', '.join(sorted(blind))} -- "
                  f"those values stay unknown for any listing whose search card omits them",
                  file=sys.stderr)

    google_stats = None
    if uses_google(args) and collector.listings:
        google_stats = run_google_enrichment(args, collector.listings)

    return finish(args, collector, output, checkpoint, used_browser, filters, google_stats)


def run_google_enrichment(args, listings):
    """Look up Google rating + review count. Opt-in, cached, capped."""
    import enrich_google

    key = enrich_google.resolve_api_key(args.google_key)
    if not key:
        print("[run] Google filters requested but no API key -- skipping enrichment "
              "(pass --google-key or set GOOGLE_MAPS_API_KEY)", file=sys.stderr)
        return None

    cache = enrich_google.PlacesCache(args.google_cache, ttl_days=args.google_cache_ttl)
    try:
        client = enrich_google.PlacesClient(
            key, cache=cache, min_delay=args.google_delay, verbose=args.verbose,
            endpoint=args.google_endpoint or enrich_google.PLACES_URL,
        )
    except enrich_google.GoogleUnavailable as exc:
        print(f"[run] Google enrichment unavailable: {exc}", file=sys.stderr)
        return None

    print(f"[run] enriching {len(listings)} listing(s) from Google Places "
          f"(cap {args.max_google_lookups} billed lookups)")
    try:
        return enrich_google.enrich_listings(listings, client, max_lookups=args.max_google_lookups)
    finally:
        client.close()


def finish(args, collector: Collector, output: str, checkpoint: Checkpoint, used_browser: bool,
           filters=None, google_stats=None) -> int:
    pulled = len(collector.listings)
    unique, dupes = parse.dedupe(collector.listings)

    filters = filters or []
    filtered, rejected = apply_filters(unique, filters, args.drop_unknown)

    main_rows = [l for l in filtered if not l.is_low_confidence()]
    low_rows = [l for l in filtered if l.is_low_confidence()]

    parse.write_csv(output, main_rows)
    low_path = ""
    if low_rows:
        low_path = lowconfidence_path(output)
        parse.write_csv(low_path, low_rows)
    if args.rejects and rejected:
        parse.write_csv(args.rejects, rejected)

    with_website = sum(1 for l in main_rows if l.website)
    pct = (with_website / len(main_rows) * 100) if main_rows else 0.0

    print("")
    print("run summary")
    print("-----------")
    print(f"  approach        : {'B (playwright)' if used_browser else 'A (json api)'}")
    print(f"  pages fetched   : {collector.pages_fetched}")
    print(f"  pulled          : {pulled}")
    print(f"  deduped         : {len(dupes)}")
    if google_stats:
        print(f"  google lookups  : {google_stats.looked_up} billed, "
              f"{google_stats.cached} cached, {google_stats.matched} matched "
              f"({google_stats.low_match} low-confidence)")
        if google_stats.no_result or google_stats.errors or google_stats.capped:
            print(f"                    {google_stats.no_result} no result, "
                  f"{google_stats.errors} errors, {google_stats.capped} skipped at cap")
    for f in filters:
        detail = f" ({f.unknown_dropped} unknown)" if f.unknown_dropped else ""
        print(f"  filtered {f.name:<18}: {f.dropped}{detail}")
    if filters:
        print(f"  filtered total  : {len(rejected)}"
              + (f" -> {args.rejects}" if args.rejects and rejected else ""))
    print(f"  low-confidence  : {len(low_rows)}")
    print(f"  written         : {len(main_rows)} -> {output}")
    if low_path:
        print(f"                    {len(low_rows)} -> {low_path}")
    print(f"  website coverage: {with_website}/{len(main_rows)} ({pct:.0f}%)")

    if pulled and not main_rows:
        if rejected:
            print("  (every listing was filtered out -- loosen the thresholds)")
            return 0
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
