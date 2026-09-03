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
import re
import sys
from typing import List, Optional

import api_client
import checkpoint as checkpoint_mod
import metros
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
    p.add_argument("--category", default=None,
                   help="BBB category slug, e.g. heating-and-air-conditioning, plumber, "
                        "roofing-contractors. Comma-separate several to run them in turn.")
    p.add_argument("--categories-file", default=None,
                   help="file of category slugs, one per line, instead of --category")
    p.add_argument("--location", default=None,
                   help="city+state (charlotte-nc) or a whole state (nc, 'north carolina'), "
                        "which expands into a metro-by-metro pull")
    p.add_argument("--max-results", type=int, default=500,
                   help="cap per category, across every metro (default: 500)")
    p.add_argument("--max-per-metro", type=int, default=None,
                   help="cap per metro on a state pull (default: the whole --max-results budget)")

    geo = p.add_argument_group("full-state pulls")
    geo.add_argument("--metros", default=None,
                     help="explicit metro slugs, comma-separated; overrides the bundled list")
    geo.add_argument("--metros-file", default=None, help="file of metro slugs, one per line")
    geo.add_argument("--max-metros", type=int, default=None, help="only pull the first N metros")
    geo.add_argument("--list-metros", action="store_true",
                     help="print the metros that would be pulled, then exit without scraping")
    geo.add_argument("--overwrite", action="store_true",
                     help="start a fresh output file even when resuming a state pull")
    geo.add_argument("--append", action="store_true",
                     help="append to an existing output file (implied when resuming a state pull)")
    geo.add_argument("--progress", default=None,
                     help="cross-metro resume file (default: .progress-<category>-<location>.json)")
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
    flt.add_argument("--require-website", action="store_true",
                     help="drop listings with no company domain. NOTE: BBB publishes the website "
                          "only on profile pages, so this needs a working detail pass -- with "
                          "--no-detail (or profile pages blocked) it drops everything")
    flt.add_argument("--exclude-name", default=None,
                     help="drop listings whose name contains any of these (comma-separated, "
                          "case-insensitive) -- national chains, franchisors, supply houses")
    flt.add_argument("--exclude-domain", default=None,
                     help="drop listings on these domains (comma-separated); a bare domain "
                          "also matches its subdomains")
    flt.add_argument("--exclude-file", default=None,
                     help="file of exclusions, one per line; lines with a dot are treated as "
                          "domains, everything else as a name fragment")
    flt.add_argument("--drop-unknown", action="store_true",
                     help="drop listings whose value for an active filter is unknown")
    flt.add_argument("--target-rows", type=int, default=None,
                     help="trim the finished sheet to this many rows. Unlike "
                          "--max-results (which caps the RAW pull, before "
                          "filtering) this counts rows that actually survived, "
                          "so every sheet comes out the same size")
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
    goo.add_argument("--google-dry-run", action="store_true",
                     help="report how many lookups would be billed, then skip enrichment")
    goo.add_argument("--google-cost-per-lookup", type=float, default=None,
                     help="unit price, to turn the dry-run count into an estimate "
                          "(check current Places pricing -- nothing is assumed)")
    goo.add_argument("--google-endpoint", default=None,
                     help="override the Places endpoint (enterprise proxy, or a test double)")
    goo.add_argument("--google-delay", type=float, default=0.0,
                     help="seconds between Google lookups (default: 0)")

    apo = p.add_argument_group(
        "apollo lookup (free -- recovers the website BBB's profile page withholds)")
    apo.add_argument("--apollo", action="store_true",
                     help="look each company up in Apollo to fill a missing website")
    apo.add_argument("--apollo-key", default=None,
                     help="Apollo API key (default: $APOLLO_API_KEY)")
    apo.add_argument("--apollo-endpoint", default=None,
                     help="pin the lookup URL instead of using the discovered default")
    apo.add_argument("--apollo-probe", action="store_true",
                     help="report which Apollo lookup URLs answer, then exit "
                          "(uses a query that matches nothing, so it is free)")
    apo.add_argument("--apollo-cache", default=".apollo-cache.json",
                     help="lookup cache path (default: .apollo-cache.json)")
    apo.add_argument("--apollo-cache-ttl", type=int, default=30,
                     help="days before a cached lookup is refetched (default: 30)")
    apo.add_argument("--max-apollo-lookups", type=int, default=500,
                     help="cap lookups per run (default: 500)")
    apo.add_argument("--apollo-delay", type=float, default=0.0,
                     help="seconds between Apollo calls")

    src = p.add_argument_group("endpoint source")
    src.add_argument("--endpoints", help="JSON config of candidate API endpoints")
    src.add_argument("--from-har", help="HAR exported from the browser Network tab; mine it for the search XHR")
    src.add_argument("--save-endpoints", help="write the endpoints discovered this run to this path")
    src.add_argument("--save-cookies", action="store_true",
                     help="include session cookies in --save-endpoints (they are session credentials)")
    src.add_argument("--replay", default=None,
                     help="replay a captured HAR offline: parse its real payloads through the "
                          "whole pipeline with no network at all")
    src.add_argument("--base-url", default=None,
                     help="override the site root (an enterprise proxy, or a test double)")
    src.add_argument("--find-entity", default=None,
                     help="BBB's own category id (tob_id, e.g. 10113-000 for Plumber) to pin "
                          "the search to one category instead of matching on text")
    src.add_argument("--dump-sample", default=None,
                     help="print the raw JSON-LD and markup samples from a HAR, and write "
                          "sample-search.html / sample-profile.html for inspection")
    src.add_argument("--inspect-har", default=None,
                     help="report what a HAR contains (payloads, profile pages, endpoints), "
                          "then exit")
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
    beh.add_argument("--browser-executable", default=None,
                     help="path to an existing Chromium, when Playwright's bundled build "
                          "isn't installed (or set $BBB_BROWSER_EXECUTABLE)")
    beh.add_argument("--browser-no-sandbox", action="store_true",
                     help="pass --no-sandbox to Chromium (needed inside most containers)")
    beh.add_argument("--column-map", default=None,
                     help="JSON column map for the output CSV (see column-map.example.json), "
                          "so the file lands in the shape the next pipeline step expects")
    beh.add_argument("--report", default=None,
                     help="write a machine-readable JSON run report (counts, filters, "
                          "coverage, per-metro breakdown)")
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

    def __init__(self, name, getter, predicate, detail_field=None, needs_google=False):
        self.name = name
        self.getter = getter
        self.predicate = predicate
        self.detail_field = detail_field
        self.needs_google = needs_google
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


def load_exclusions(args) -> tuple:
    """(name fragments, domains) to drop, from the flags and any --exclude-file."""
    names = [n.strip().lower() for n in (args.exclude_name or "").split(",") if n.strip()]
    domains = [d.strip().lower().lstrip(".") for d in (args.exclude_domain or "").split(",")
               if d.strip()]
    if args.exclude_file:
        with open(args.exclude_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                # A dot means a domain; anything else is a name fragment.
                if "." in line and " " not in line:
                    domains.append(line.lower().lstrip("."))
                else:
                    names.append(line.lower())
    return names, domains


def excluded(listing, names, domains) -> bool:
    company = (listing.company_name or "").lower()
    if any(fragment in company for fragment in names):
        return True
    website = (listing.website or "").lower()
    if website and any(website == d or website.endswith("." + d) for d in domains):
        return True
    return False


def build_filters(args):
    filters = []
    if args.min_years > 0:
        filters.append(Filter("min-years", lambda l: l.years_in_business,
                              lambda v: v >= args.min_years, "years_in_business"))
    if args.min_employees > 0:
        filters.append(Filter("min-employees", lambda l: l.employees,
                              lambda v: v >= args.min_employees, "employees"))
    if args.require_website:
        # Returns "" (not None) when absent: a blank website is a KNOWN absence,
        # not an unknown, so it must fail rather than pass as "we can't tell".
        # A social page is not a company domain and does not satisfy this either.
        filters.append(Filter("require-website", lambda l: l.website, lambda v: bool(v)))
    if args.min_bbb_reviews > 0:
        filters.append(Filter("min-bbb-reviews", lambda l: l.bbb_reviews,
                              lambda v: v >= args.min_bbb_reviews, "bbb_reviews"))
    if args.max_bbb_complaints is not None:
        filters.append(Filter("max-bbb-complaints", lambda l: l.bbb_complaints,
                              lambda v: v <= args.max_bbb_complaints, "bbb_complaints"))
    if args.min_google_reviews > 0:
        filters.append(Filter("min-google-reviews",
                              _google_value("google_reviews", args.allow_low_match),
                              lambda v: v >= args.min_google_reviews, needs_google=True))
    if args.min_google_rating > 0:
        filters.append(Filter("min-google-rating",
                              _google_value("google_rating", args.allow_low_match),
                              lambda v: v >= args.min_google_rating, needs_google=True))
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


def _default_paths(args, category, locations):
    """Per-location checkpoint paths that don't collide across metros."""
    if args.checkpoint and len(locations) == 1:
        return {locations[0]: args.checkpoint}
    if args.checkpoint:
        base, ext = os.path.splitext(args.checkpoint)
        return {loc: f"{base}-{loc}{ext or '.json'}" for loc in locations}
    return {loc: default_checkpoint(category, loc) for loc in locations}


class ApiSession:
    """One HTTP session for the whole run, in one of two modes.

    Default is `html`: BBB renders search results into the page, so Approach A
    is a plain GET of the search URL, parsed via its schema.org JSON-LD. The
    `json` mode is kept for an explicit --endpoints/--from-har config, in case
    an XHR API exists on some pages or returns later.

    Either way it is resolved once for the whole run -- re-probing per metro
    would cost extra requests, and a client per metro would throw away the
    cookies and User-Agent that make a long run look like one person browsing.
    """

    def __init__(self, args):
        self.args = args
        self.limiter = api_client.RateLimiter(min_delay=args.min_delay, max_delay=args.max_delay)
        self.mode = "json" if (args.endpoints or args.from_har or args.discover) else "html"
        self.client = None
        self.search = None
        self.spec = None

    def close(self):
        for closer in (self.client, self.search):
            if closer is not None:
                closer.close()
        self.client = self.search = None

    # ------------------------------------------------------------------
    def resolve(self, location: str):
        if self.mode == "html":
            return self._resolve_html(location)
        return self._resolve_json(location)

    def _resolve_html(self, location: str):
        import search_client

        base_url = self.args.base_url or search_client.BBB_BASE
        self.search = search_client.SearchClient(
            limiter=self.limiter, entity=self.args.find_entity, base_url=base_url,
            verbose=self.args.verbose,
        )
        url = search_client.build_search_url(self.args.category or "", location,
                                             base_url=base_url, entity=self.args.find_entity)
        found = self.search.probe(self.args.category or "", location)
        if not found:
            self.search.close()
            self.search = None
            raise api_client.EndpointUnavailable(
                f"no listings on {url} -- Cloudflare may be challenging the request; "
                f"--browser runs the same search through Playwright"
            )
        print(f"[run] reading rendered search pages ({found} listings on page 1)")
        return self.search

    def iter_listings(self, category: str, location: str, start_page: int = 1):
        """Yield (page_number, listings) regardless of which mode is active."""
        if self.mode == "html":
            yield from self.search.iter_pages(category, location, start_page=start_page)
            return
        for page, payload in self.client.iter_pages(category, location, start_page=start_page):
            listings, skipped = parse.listings_from_payload(payload, default_category=category)
            warn_unnamed(skipped, len(listings))
            yield page, listings

    def fetch_detail(self, profile_url: str):
        if self.mode == "html":
            return self.search.fetch_detail(profile_url)
        return _http_detail_fetcher(self.client)(profile_url)

    # ------------------------------------------------------------------
    def _resolve_json(self, location: str):
        """Find and verify a working JSON endpoint. Raises EndpointUnavailable."""
        args = self.args
        self.client = api_client.ApiClient(limiter=self.limiter, verbose=args.verbose)
        specs = []
        if args.from_har:
            specs.extend(api_client.endpoints_from_har(args.from_har))
            if args.verbose:
                print(f"[run] {len(specs)} candidate endpoint(s) from HAR", flush=True)
        if args.endpoints:
            specs.extend(api_client.load_endpoints(args.endpoints))

        if args.discover or not specs:
            try:
                discovered = api_client.discover_endpoints(
                    self.client.client, args.category or "", location, limiter=self.limiter
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

        self.spec = self.client.select_endpoint(specs, args.category or "", location)
        print(f"[run] using endpoint {self.spec.url} (source: {self.spec.source})")
        if args.save_endpoints:
            api_client.save_endpoints(args.save_endpoints, [self.spec],
                                      include_cookies=args.save_cookies)
            print(f"[run] saved endpoint config -> {args.save_endpoints}")
        return self.spec


def collect_via_api(session: ApiSession, category: str, location: str, checkpoint: Checkpoint,
                    collector: Collector, required_fields=None) -> bool:
    """Approach A for one location. False means fall back to Approach B."""
    args = session.args
    try:
        start_page = max(checkpoint.next_page(), args.skip + 1)
        for page, listings in session.iter_listings(category, location, start_page=start_page):
            collector.add_page(page, listings)
            if collector.full:
                break

        if not args.no_detail:
            enrich_details(session.fetch_detail, collector.listings, args,
                           required=required_fields)
        return True

    except api_client.BlockedError as exc:
        print(f"[run] blocked: {exc} -- keeping partial results", file=sys.stderr)
        return bool(collector.listings)
    except api_client.EndpointUnavailable as exc:
        print(f"[run] Approach A unavailable: {exc}", file=sys.stderr)
        return False


def warn_unnamed(skipped: int, kept: int) -> None:
    """Records that parsed as businesses but had no readable name.

    Every one is a dropped lead, and a high count means the name key moved --
    which otherwise looks exactly like a thin result set.
    """
    if not skipped:
        return
    print(f"[run] warning: {skipped} record(s) had no readable company name and were "
          f"dropped ({kept} kept). If that's most of them, the name field moved -- "
          f"run --inspect-har on a capture to see the real keys", file=sys.stderr)


def _http_detail_fetcher(client):
    """Detail-page fetcher for Approach A: plain HTTP + the shared DOM parser."""
    from browser_client import listing_from_detail_html

    def fetch(url):
        html = client.fetch_detail_html(url)
        return listing_from_detail_html(html) if html else None

    return fetch


def collect_via_browser(browser, args, category: str, location: str, checkpoint: Checkpoint,
                        collector: Collector, required_fields=None) -> bool:
    """Approach B for one location."""
    start_page = max(checkpoint.next_page(), args.skip + 1)
    for page, listings in browser.iter_listings(category, location, start_page=start_page):
        collector.add_page(page, listings)
        if collector.full:
            break
    if not args.no_detail:
        enrich_details(browser.fetch_detail, collector.listings, args, required=required_fields)
    return True


def open_browser(args):
    import browser_client
    browser = browser_client.BrowserClient(
        user_data_dir=args.profile_dir,
        headless=not args.headed,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        verbose=args.verbose,
        executable_path=args.browser_executable,
        extra_args=["--no-sandbox"] if args.browser_no_sandbox else None,
    )
    browser.start()
    return browser


def enrich_details(fetch, listings: List[parse.Listing], args, required=None) -> int:
    """Visit detail pages only for records still missing the fields we need.

    `fetch` takes a profile URL and returns a Listing of whatever it found.
    Request volume is the scarce resource, so complete records are never
    re-fetched and the visit count is capped by --max-detail.
    """
    candidates = [l for l in listings if l.needs_detail(required) and l.profile_url]
    pending = candidates[: args.max_detail]
    if not pending:
        return 0
    capped = len(candidates) - len(pending)
    note = f", {capped} skipped at --max-detail {args.max_detail}" if capped else ""
    print(f"[run] visiting {len(pending)} detail page(s) for missing fields{note}")
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
# the run
# --------------------------------------------------------------------------

class RunResult:
    def __init__(self):
        self.listings: List[parse.Listing] = []
        self.pages = 0
        self.used_browser = False
        self.approach = ""            # what actually served the run
        self.per_location = []          # (location, count) in pull order
        self.budget_spent = False


class Clients:
    """The HTTP session and browser, shared across every category and metro.

    One endpoint resolution and one browser launch per run, not per category:
    re-resolving would re-probe, and relaunching would drop the cookies that
    keep a long run looking like one person browsing.
    """

    def __init__(self, args):
        self.args = args
        self.session = None
        self.browser = None
        self.used_browser = False
        self.api_failed = False

    def api(self, location: str):
        """The resolved ApiSession, or None when Approach A isn't usable."""
        if self.args.browser or self.api_failed:
            return None
        if self.session is None:
            session = ApiSession(self.args)
            try:
                session.resolve(location)
                self.session = session
            except (api_client.EndpointUnavailable, api_client.BlockedError) as exc:
                print(f"[run] Approach A unavailable: {exc}", file=sys.stderr)
                session.close()
                self.api_failed = True
                return None
        return self.session

    def drop_api(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        self.api_failed = True

    def browser_client(self):
        if self.browser is None:
            self.browser = open_browser(self.args)
            self.used_browser = True
        return self.browser

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.browser is not None:
            self.browser.close()
            self.browser = None


def collect_all(args, category, locations, progress, required_fields, clients) -> RunResult:
    """Pull every location for one category, sharing one budget."""
    result = RunResult()
    paths = _default_paths(args, category, locations)
    per_metro_cap = args.max_per_metro or args.max_results

    if not args.browser and clients.api(locations[0]) is None and args.no_fallback:
        return result

    try:
        for location in locations:
            remaining = args.max_results - len(result.listings)
            if remaining <= 0:
                result.budget_spent = True
                print(f"[run] budget of {args.max_results} reached -- "
                      f"stopping before {location}")
                break
            if progress.is_done(location):
                print(f"[run] {location}: already collected, skipping")
                continue

            checkpoint = Checkpoint(category=category, location=location,
                                    path=paths[location])
            if not args.no_resume:
                checkpoint = Checkpoint.load(paths[location], category, location)
                if checkpoint.last_page:
                    print(f"[run] {location}: resuming after page {checkpoint.last_page}")

            collector = Collector(min(per_metro_cap, remaining), checkpoint, verbose=args.verbose)
            if len(locations) > 1:
                print(f"[run] {location} ({len(result.listings)}/{args.max_results} collected)")

            ok = False
            session = clients.api(location)
            if session is not None:
                result.approach = ("A (rendered html)" if session.mode == "html"
                                   else "A (json api)")
                ok = collect_via_api(session, category, location, checkpoint, collector,
                                     required_fields)
                if not ok and not args.no_fallback:
                    clients.drop_api()
                    print("[run] falling back to Approach B (Playwright)")

            if not ok and (args.browser or not args.no_fallback):
                try:
                    collect_via_browser(clients.browser_client(), args, category, location,
                                        checkpoint, collector, required_fields)
                    ok = True
                except Exception as exc:
                    print(f"[run] Approach B failed for {location}: {exc}", file=sys.stderr)

            result.listings.extend(collector.listings)
            result.pages += collector.pages_fetched
            result.per_location.append((location, len(collector.listings)))

            if not collector.full:
                # Location exhausted rather than capped -- nothing left to resume.
                checkpoint.clear()
            if ok:
                progress.mark_done(location, len(collector.listings))
                progress.save()
    finally:
        result.used_browser = clients.used_browser

    return result


def resolve_categories(args) -> List[str]:
    """One or more category slugs, in the order they'll be pulled."""
    if args.categories_file:
        slugs = []
        with open(args.categories_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    slugs.append(line)
        return slugs
    return [c.strip() for c in (args.category or "").split(",") if c.strip()]


def output_for(args, category: str, categories: List[str]) -> str:
    """Where a category's rows go.

    An explicit --output collects every category into one file (the `category`
    column tells them apart); otherwise each gets its own.
    """
    if args.output:
        return args.output
    return default_output(category, args.location)


def inspect_har(path: str) -> int:
    """Describe a capture without scraping: what's in it, and what we'd use."""
    import replay

    try:
        print(replay.describe(path))
    except (OSError, ValueError) as exc:
        print(f"could not read HAR: {exc}", file=sys.stderr)
        return 2

    payload_count = sum(1 for _ in replay.iter_search_payloads(path))

    rows = replay.inventory(path)
    if not rows:
        print("no bbb.org responses in this capture at all -- was DevTools open "
              "before the page loaded?")
    else:
        print(f"\nbbb.org responses ({len(rows)}):")
        empty = 0
        for row in sorted(rows, key=lambda r: -(r["records"] * 1000 + r["profile_links"]))[:15]:
            if not row["bytes"]:
                empty += 1
                continue
            notes = []
            if row["records"]:
                notes.append(f"{row['records']} records")
            if row["profile_links"]:
                notes.append(f"{row['profile_links']} profile links")
            notes.extend(row["markers"])
            trailer = f"  [{', '.join(notes)}]" if notes else ""
            print(f"  {row['status']} {row['mime'] or '?':<24} {row['bytes']:>8,}B  "
                  f"{row['url'][:88]}{trailer}")
        if empty:
            print(f"  ({empty} response(s) had no saved body -- exported without content)")

    search_pages = sum(1 for _ in replay.iter_search_pages(path))
    if not payload_count:
        print("\nno JSON response carried business records.")
        if search_pages:
            print(f"{search_pages} rendered search page(s) found -- BBB serves results in the "
                  f"HTML, so that is what gets parsed.")
        else:
            print("re-capture with 'Save all as HAR with content', which keeps response bodies.")

    specs = api_client.endpoints_from_har(path)
    if specs:
        print(f"candidate endpoints ({len(specs)}, best first):")
        for spec in specs:
            params = ", ".join(sorted(spec.params)) or "none"
            print(f"  {spec.method} {spec.url}")
            print(f"    page={spec.page_param}  category={spec.category_param}  "
                  f"location={spec.location_param}")
            print(f"    params: {params}")
    elif not search_pages:
        print("no candidate search endpoints found -- was the XHR captured "
              "with response bodies ('Save all as HAR with content')?")

    listings, sources = replay.collect(path)
    print(f"\nparsed {len(listings)} listing(s) from {sources} source(s)")
    if listings:
        lines = parse.format_coverage(parse.field_coverage(listings))
        print("field coverage:")
        for line in lines:
            print(f"  {line}")
        sample = listings[0]
        print("first listing:")
        for key, value in sample.as_row().items():
            print(f"  {key:<18}: {value}")
    return 0 if listings else 1


def _excerpt(html: str, needle: str, before: int = 900, after: int = 1600) -> str:
    index = html.find(needle)
    if index < 0:
        return ""
    return html[max(0, index - before): index + after]


def dump_sample(path: str) -> int:
    """Show what the real markup actually contains.

    JSON-LD gives identity and location but not the commercial fields, so this
    prints one raw business node plus the surrounding card markup -- enough to
    write extraction against without guessing.
    """
    import json as json_mod
    import replay

    pages = list(replay.iter_search_pages(path))
    if not pages:
        print("no rendered search pages in this capture", file=sys.stderr)
        return 1

    url, html = max(pages, key=lambda pair: len(pair[1]))
    print(f"=== search page: {url[:110]}")
    print(f"=== {len(html):,} bytes\n")

    node = None
    for block in parse.find_jsonld_blocks(html):
        node = next(parse.iter_jsonld_businesses(block), None)
        if node:
            break
    print("--- first JSON-LD business node (raw) ---")
    print(json_mod.dumps(node, indent=1)[:1800] if node else "(none found)")

    print("\n--- markup around the first result card ---")
    card = _excerpt(html, "/profile/")
    print(re.sub(r"\s+", " ", card)[:2200] if card else "(no profile link found)")

    details = replay.detail_pages(path)
    if details:
        detail_url, detail_html = next(iter(details.items()))
        print(f"\n=== profile page: {detail_url[:110]}")
        for marker in ("Years in Business", "Number of Employees", "Business Started",
                       "BBB Rating", "Accredited", "Website"):
            excerpt = _excerpt(detail_html, marker, before=200, after=500)
            if excerpt:
                print(f"\n--- profile: '{marker}' ---")
                print(re.sub(r"\s+", " ", excerpt)[:700])

    for name, content in (("sample-search.html", html),
                          ("sample-profile.html", next(iter(details.values()), ""))):
        if content:
            with open(name, "w", encoding="utf-8") as fh:
                fh.write(content)
            print(f"\nwrote {name} ({len(content):,} bytes)")
    return 0


def collect_from_har(args, category: str) -> RunResult:
    """Build a RunResult from a capture instead of the network."""
    import replay

    result = RunResult()
    listings, payloads = replay.collect(args.replay, category=category,
                                        max_results=args.max_results)
    result.listings = listings
    result.pages = payloads
    result.per_location.append((args.location, len(listings)))

    if not args.no_detail:
        required = detail_fields_for(args, build_filters(args))
        enrich_details(replay.replay_fetcher(args.replay), result.listings, args,
                       required=required)
    return result


def run(args) -> int:
    if args.dump_sample:
        return dump_sample(args.dump_sample)
    if args.apollo_probe:
        return probe_apollo(args)

    if args.inspect_har:
        return inspect_har(args.inspect_har)

    try:
        categories = resolve_categories(args)
    except OSError as exc:
        print(f"could not read categories: {exc}", file=sys.stderr)
        return 2
    if not categories:
        print("no categories to pull", file=sys.stderr)
        return 2

    try:
        locations, source = metros.resolve_locations(
            args.location, args.metros, args.metros_file, args.max_metros
        )
    except (OSError, metros.UnknownState) as exc:
        print(f"could not resolve metros: {exc}", file=sys.stderr)
        return 2

    if args.list_metros:
        for location in locations:
            print(location)
        return 0

    if args.replay:
        locations = [args.location]      # a capture is whatever it captured
        print(f"[run] replaying {args.replay} -- no network")
    elif len(locations) > 1:
        print(f"[run] {args.location} -> {len(locations)} metros (source: {source})")
        if source.startswith("bundled"):
            print("[run] bundled metro list is a starting point -- "
                  "override with --metros / --metros-file if it misses your markets")

    if len(categories) > 1:
        print(f"[run] {len(categories)} categories: {', '.join(categories)}")
        print(f"[run] --max-results {args.max_results} applies per category")

    filters = build_filters(args)
    required_fields = detail_fields_for(args, filters)
    warn_blind_filters(args, filters)

    clients = Clients(args)
    written_to = set()
    totals = {"pulled": 0, "written": 0}
    worst = 0

    try:
        for index, category in enumerate(categories):
            if len(categories) > 1:
                print(f"\n[run] === {category} ({index + 1}/{len(categories)}) ===")

            output = output_for(args, category, categories)
            progress_path = args.progress or f".progress-{category}-{args.location}.json"
            progress = checkpoint_mod.RunProgress(category=category, scope=args.location,
                                                  path=progress_path)
            fingerprint = checkpoint_mod.settings_fingerprint(filter_settings(args))
            if len(locations) > 1 and not args.no_resume:
                progress = checkpoint_mod.RunProgress.load(progress_path, category, args.location)
                if progress.completed:
                    print(f"[run] resuming state pull -- "
                          f"{len(progress.completed)} metro(s) already done")
                    if progress.settings and progress.settings != fingerprint:
                        # Filters run at write time, so metros already collected
                        # are not re-filtered -- the earlier rows keep the old
                        # thresholds and the file ends up mixing two rulesets.
                        print("[run] warning: filter settings changed since the earlier run. "
                              "Already-collected metros are skipped, so their rows keep the "
                              "old thresholds. Use --no-resume --overwrite for a clean pull "
                              "under the new ones.", file=sys.stderr)
            progress.settings = fingerprint

            # A resumed state pull continues into the same CSV -- overwriting it
            # would silently discard metros an earlier invocation already paid
            # for. Several categories sharing one --output likewise append.
            resuming = bool(progress.completed)
            append = (
                args.append
                or output in written_to
                or (resuming and not args.overwrite)
            )
            if append and output not in written_to and os.path.exists(output) and not args.append:
                print(f"[run] appending to existing {output} (--overwrite to start fresh)")

            if args.replay:
                result = collect_from_har(args, category)
            else:
                result = collect_all(args, category, locations, progress, required_fields,
                                     clients)
            code = finish(args, result, output, filters, locations, append=append,
                          label=category if len(categories) > 1 else None)
            written_to.add(output)
            totals["pulled"] += len(result.listings)
            worst = max(worst, code)

            if not result.budget_spent and len(locations) > 1:
                progress.clear()   # whole state covered; a rerun starts clean

            for f in filters:      # per-category counts, not cumulative
                f.dropped = 0
                f.unknown_dropped = 0
    finally:
        clients.close()

    if len(categories) > 1:
        print(f"\n[run] {len(categories)} categories done, {totals['pulled']} listings pulled")
    return worst


def filter_settings(args) -> dict:
    """The settings a resumed run cannot retroactively apply to earlier metros."""
    return {
        "min_years": args.min_years,
        "min_employees": args.min_employees,
        "require_website": args.require_website,
        "min_bbb_reviews": args.min_bbb_reviews,
        "max_bbb_complaints": args.max_bbb_complaints,
        "min_google_reviews": args.min_google_reviews,
        "min_google_rating": args.min_google_rating,
        "allow_low_match": args.allow_low_match,
        "drop_unknown": args.drop_unknown,
        "exclude_name": args.exclude_name or "",
        "exclude_domain": args.exclude_domain or "",
        "exclude_file": args.exclude_file or "",
    }


def warn_blind_filters(args, filters) -> None:
    if args.require_website and args.no_detail:
        print("[run] warning: --require-website with --no-detail drops every row -- BBB "
              "publishes the website only on profile pages, which the detail pass fetches",
              file=sys.stderr)
    if not args.no_detail:
        return
    blind = {f.detail_field for f in filters if f.detail_field} & parse.DETAIL_FIELDS
    if blind:
        print(f"[run] warning: --no-detail with filters on {', '.join(sorted(blind))} -- "
              f"those values stay unknown for any listing whose search card omits them",
              file=sys.stderr)


def uses_apollo(args) -> bool:
    return bool(args.apollo or args.apollo_key)


def probe_apollo(args) -> int:
    """Report which Apollo lookup URLs answer. Free -- matches nothing."""
    import enrich_apollo

    key = enrich_apollo.resolve_api_key(args.apollo_key)
    if not key:
        print("[apollo] no API key -- pass --apollo-key or set APOLLO_API_KEY",
              file=sys.stderr)
        return 2

    print("probing Apollo lookup endpoints (query matches nothing, so nothing is billed)")
    print("-------------------------------------------------------------------------")
    live = []
    for row in enrich_apollo.probe_endpoints(key):
        if row["error"]:
            print(f"  error  {row['url']}  ({row['error']})")
        elif row["ok"]:
            live.append(row["url"])
            print(f"  OK {row['status']}  {row['url']}")
        else:
            print(f"  {row['status']:<6} {row['url']}")

    if not live:
        print("\nNo endpoint answered. Check the key with:")
        print("  curl -H \"x-api-key: $APOLLO_API_KEY\" https://api.apollo.io/v1/auth/health")
        return 1
    print(f"\nUse: --apollo --apollo-endpoint {live[0]}")
    if live[0] != enrich_apollo.DEFAULT_ENDPOINT:
        print(f"(the built-in default {enrich_apollo.DEFAULT_ENDPOINT} did not answer)")
    return 0


def run_apollo_enrichment(args, listings):
    """Fill missing websites from Apollo. Free, cached, capped.

    Runs before the quality filters, not after: --require-website reads the
    field this pass fills, so the order is the whole point.
    """
    import enrich_apollo

    key = enrich_apollo.resolve_api_key(args.apollo_key)
    if not key:
        print("[run] Apollo lookup requested but no API key -- skipping "
              "(pass --apollo-key or set APOLLO_API_KEY)", file=sys.stderr)
        return None

    cache = enrich_apollo.OrgCache(args.apollo_cache, ttl_days=args.apollo_cache_ttl)
    try:
        client = enrich_apollo.ApolloClient(
            key, cache=cache, min_delay=args.apollo_delay, verbose=args.verbose,
            endpoint=args.apollo_endpoint or enrich_apollo.DEFAULT_ENDPOINT,
        )
    except enrich_apollo.ApolloUnavailable as exc:
        print(f"[run] Apollo lookup unavailable: {exc}", file=sys.stderr)
        return None

    print(f"[run] looking up {len(listings)} company/companies in Apollo "
          f"(free; cap {args.max_apollo_lookups})")
    try:
        return enrich_apollo.enrich_listings(
            listings, client, max_lookups=args.max_apollo_lookups)
    finally:
        client.close()


def report_google_preflight(args, listings) -> None:
    """Print what enrichment would cost, without spending anything."""
    import enrich_google

    cache = enrich_google.PlacesCache(args.google_cache, ttl_days=args.google_cache_ttl)
    stats = enrich_google.preflight(listings, cache, max_lookups=args.max_google_lookups)
    print("")
    print("google preflight (dry run -- nothing was billed)")
    print("-----------------------------------------------")
    print(stats.report(args.google_cost_per_lookup))
    print("  google filters were not applied; rerun without --google-dry-run to enrich")


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


def finish(args, result: RunResult, output: str, filters=None, locations=None,
           append: bool = False, label: Optional[str] = None) -> int:
    pulled = len(result.listings)
    unique, dupes = parse.dedupe(result.listings)

    filters = filters or []

    names, domains = load_exclusions(args)
    excluded_rows = []
    if names or domains:
        keep = []
        for listing in unique:
            (excluded_rows if excluded(listing, names, domains) else keep).append(listing)
        unique = keep

    # Apollo first: it fills `website`, and --require-website is a local
    # filter that reads it. Reversed, every row would still look "unknown".
    apollo_stats = None
    if uses_apollo(args) and unique:
        apollo_stats = run_apollo_enrichment(args, unique)

    local = [f for f in filters if not f.needs_google]
    google = [f for f in filters if f.needs_google]

    # BBB-side filters run first so Google is only asked about survivors --
    # every uncached lookup is billed, and the cheap filters cut the set hard.
    kept, rejected = apply_filters(unique, local, args.drop_unknown)
    rejected.extend(excluded_rows)

    google_stats = None
    if uses_google(args) and kept and args.google_dry_run:
        report_google_preflight(args, kept)
    elif uses_google(args) and kept:
        google_stats = run_google_enrichment(args, kept)
        kept, rejected_google = apply_filters(kept, google, args.drop_unknown)
        rejected.extend(rejected_google)

    trimmed = 0
    if args.target_rows and len(kept) > args.target_rows:
        trimmed = len(kept) - args.target_rows
        kept = kept[:args.target_rows]

    column_map = None
    if args.column_map:
        try:
            column_map = parse.load_column_map(args.column_map)
        except (OSError, ValueError) as exc:
            print(f"[run] column map ignored: {exc}", file=sys.stderr)

    already_written = 0
    if append:
        seen = (parse.existing_keys(output, column_map)
                | parse.existing_keys(lowconfidence_path(output), column_map))
        if seen:
            before = len(kept)
            kept = [l for l in kept if l.dedupe_key() not in seen]
            already_written = before - len(kept)

    main_rows = [l for l in kept if not l.is_low_confidence()]
    low_rows = [l for l in kept if l.is_low_confidence()]

    parse.write_csv(output, main_rows, append=append, column_map=column_map)
    low_path = ""
    if low_rows:
        low_path = lowconfidence_path(output)
        parse.write_csv(low_path, low_rows, append=append, column_map=column_map)
    if args.rejects and rejected:
        # Rejects keep the full schema -- they exist to be inspected, not ingested.
        parse.write_csv(args.rejects, rejected, append=append)

    coverage = parse.field_coverage(main_rows)
    with_website = sum(1 for l in main_rows if l.website)
    pct = (with_website / len(main_rows) * 100) if main_rows else 0.0

    heading = f"run summary -- {label}" if label else "run summary"
    print("")
    print(heading)
    print("-" * len(heading))
    approach = "B (playwright)" if result.used_browser else (result.approach or "A")
    print(f"  approach        : {approach}")
    if locations and len(locations) > 1:
        pulled_from = [f"{loc} {n}" for loc, n in result.per_location if n]
        print(f"  metros pulled   : {len(result.per_location)}/{len(locations)}")
        if pulled_from:
            print(f"                    {', '.join(pulled_from)}")
        if result.budget_spent:
            print(f"  budget          : {args.max_results} reached -- rerun to continue")
    print(f"  pages fetched   : {result.pages}")
    print(f"  pulled          : {pulled}")
    print(f"  deduped         : {len(dupes)}")
    if already_written:
        print(f"  already in file : {already_written}")
    if excluded_rows:
        print(f"  excluded        : {len(excluded_rows)} (name/domain exclusions)")
    if trimmed:
        print(f"  trimmed         : {trimmed} row(s) over --target-rows "
              f"{args.target_rows} (they passed; there was just no room)")

    if apollo_stats:
        print(f"  apollo lookups  : {apollo_stats.looked_up} free, "
              f"{apollo_stats.cached} cached, {apollo_stats.matched} matched "
              f"({apollo_stats.low_match} low-confidence)")
        print(f"                    {apollo_stats.websites_filled} website(s) "
              f"recovered that BBB withheld")
        if apollo_stats.no_result or apollo_stats.errors or apollo_stats.capped:
            print(f"                    {apollo_stats.no_result} not in Apollo, "
                  f"{apollo_stats.errors} errors, {apollo_stats.capped} skipped at cap")

    if google_stats:
        print(f"  google lookups  : {google_stats.looked_up} billed, "
              f"{google_stats.cached} cached, {google_stats.matched} matched "
              f"({google_stats.low_match} low-confidence)")
        if google_stats.no_result or google_stats.errors or google_stats.capped:
            print(f"                    {google_stats.no_result} no result, "
                  f"{google_stats.errors} errors, {google_stats.capped} skipped at cap")
    dry_run = uses_google(args) and args.google_dry_run
    for f in filters:
        if f.needs_google and dry_run:
            print(f"  filtered {f.name:<18}: not applied (dry run)")
            continue
        detail = f" ({f.unknown_dropped} unknown)" if f.unknown_dropped else ""
        print(f"  filtered {f.name:<18}: {f.dropped}{detail}")
    if filters:
        print(f"  filtered total  : {len(rejected)}"
              + (f" -> {args.rejects}" if args.rejects and rejected else ""))
    print(f"  low-confidence  : {len(low_rows)}")
    verb = "appended" if append else "written"
    print(f"  {verb:<16}: {len(main_rows)} -> {output}")
    if low_path:
        print(f"                    {len(low_rows)} -> {low_path}")
    social_only = sum(1 for l in main_rows if not l.website and l.social_url)
    print(f"  website coverage: {with_website}/{len(main_rows)} ({pct:.0f}%)")
    if social_only:
        print(f"  social-only     : {social_only} (facebook/directory page, no domain "
              f"-- kept in social_url, not enrichable by domain)")

    lines = parse.format_coverage(coverage)
    if lines:
        print("")
        print("  field coverage (share of rows with a value)")
        for line in lines:
            print(f"    {line}")
        empty = [n for n, filled in coverage["filled"].items() if filled == 0]
        if empty:
            print(f"    (!) never populated: {', '.join(empty)} -- if BBB shows these, the "
                  f"key names moved; capture a HAR and rerun with --replay")

    if args.report:
        write_report(args, result, output, filters, locations, label,
                     pulled=pulled, dupes=len(dupes), excluded=len(excluded_rows),
                     rejected=len(rejected), main_rows=main_rows, low_rows=low_rows,
                     coverage=coverage, google_stats=google_stats)

    if pulled and not main_rows:
        if already_written:
            print("  (nothing new -- every listing was already in the output file)")
            return 0
        if rejected:
            print("  (every listing was filtered out -- loosen the thresholds)")
            return 0
        return 1
    if not pulled:
        print("  (nothing collected -- see errors above)")
        return 1
    return 0


def write_report(args, result, output, filters, locations, label, **counts) -> None:
    """A JSON sidecar, so the next pipeline step doesn't have to scrape stdout."""
    import json

    coverage = counts["coverage"]
    google = counts["google_stats"]
    report = {
        "category": label or args.category,
        "location": args.location,
        "locations_planned": list(locations or []),
        "locations_pulled": [{"location": loc, "count": n} for loc, n in result.per_location],
        "approach": "browser" if result.used_browser else "api",
        "budget_spent": result.budget_spent,
        "pages_fetched": result.pages,
        "counts": {
            "pulled": counts["pulled"],
            "deduped": counts["dupes"],
            "excluded": counts["excluded"],
            "filtered": counts["rejected"],
            "low_confidence": len(counts["low_rows"]),
            "written": len(counts["main_rows"]),
        },
        "filters": {f.name: {"dropped": f.dropped, "unknown_dropped": f.unknown_dropped}
                    for f in filters or []},
        "field_coverage": {
            name: {"filled": filled, "total": coverage["total"]}
            for name, filled in coverage["filled"].items()
        },
        "output": output,
        "replayed_from": args.replay,
    }
    if google:
        report["google"] = {
            "billed": google.looked_up, "cached": google.cached, "matched": google.matched,
            "low_confidence": google.low_match, "no_result": google.no_result,
            "errors": google.errors, "skipped_at_cap": google.capped,
        }

    path = args.report
    if label:                      # one file per category on a batch run
        base, ext = os.path.splitext(path)
        path = f"{base}-{label}{ext or '.json'}"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"  report          : {path}")
    except OSError as exc:
        print(f"[run] could not write report: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    diagnostic = args.inspect_har or args.dump_sample
    if not diagnostic and not (args.category or args.categories_file):
        print("--category (or --categories-file) is required", file=sys.stderr)
        return 2
    if not diagnostic and not args.location:
        print("--location is required", file=sys.stderr)
        return 2
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
