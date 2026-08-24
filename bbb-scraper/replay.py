"""Offline replay of a captured HAR.

The gap this closes: everything here is verified against fixtures, and the
first contact with real BBB payloads is also the first chance to find out the
field names are different. Capture a HAR once and this runs the entire
pipeline against those real responses -- parsing, normalization, dedupe,
filters, CSV -- with no network at all.

    python scraper.py --category plumber --location wilmington-nc --replay search.har

Pair it with the field coverage report: a column sitting at 0% is a key name
we guessed wrong, and the HAR has the answer.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Dict, Iterator, List, Optional, Tuple

import parse


def _entries(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)
    return har.get("log", {}).get("entries", []) or []


def _body(entry: dict) -> str:
    content = (entry.get("response", {}) or {}).get("content", {}) or {}
    return content.get("text", "") or ""


def iter_search_payloads(path: str) -> Iterator[Tuple[str, dict]]:
    """Yield (url, payload) for HAR responses that carry business records.

    Order is preserved, so a capture that paged through results replays as
    those pages in the same order.
    """
    for entry in _entries(path):
        url = (entry.get("request", {}) or {}).get("url", "")
        body = _body(entry)
        if not body.strip().startswith(("{", "[")):
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        if parse.find_records(payload):
            yield url, payload


def iter_search_pages(path: str) -> Iterator[Tuple[str, str]]:
    """(url, html) for server-rendered search pages in the capture."""
    for entry in _entries(path):
        url = (entry.get("request", {}) or {}).get("url", "")
        if not is_bbb_url(url) or "/search" not in urllib.parse.urlsplit(url).path:
            continue
        mime = ((entry.get("response", {}) or {}).get("content", {}) or {}).get("mimeType", "")
        if "html" not in mime.lower():
            continue
        body = _body(entry)
        if body:
            yield url, body


def detail_pages(path: str) -> Dict[str, str]:
    """HTML profile pages in the capture, keyed by URL.

    A capture that visited detail pages replays those too, so the
    years-in-business and headcount extraction gets tested against real markup.
    """
    pages = {}
    for entry in _entries(path):
        request = entry.get("request", {}) or {}
        url = request.get("url", "")
        mime = ((entry.get("response", {}) or {}).get("content", {}) or {}).get("mimeType", "")
        if "html" not in mime.lower():
            continue
        if "/profile/" not in url:
            continue
        body = _body(entry)
        if body:
            pages[url] = body
    return pages


def replay_fetcher(path: str):
    """Detail fetcher backed by the HAR instead of the network."""
    from browser_client import listing_from_detail_html

    pages = detail_pages(path)
    by_suffix = {url.split("bbb.org", 1)[-1]: html for url, html in pages.items()}

    def fetch(url: str):
        html = pages.get(url) or by_suffix.get(url.split("bbb.org", 1)[-1])
        return listing_from_detail_html(html) if html else None

    return fetch


def collect(path: str, category: str = "", max_results: Optional[int] = None) -> Tuple[list, int]:
    """Every listing in the capture, from JSON payloads and rendered pages alike.

    Returns (listings, sources_seen).
    """
    listings: List[parse.Listing] = []
    sources = 0

    for _url, payload in iter_search_payloads(path):
        sources += 1
        for listing in parse.iter_listings_from_payload(payload, default_category=category):
            listings.append(listing)
            if max_results is not None and len(listings) >= max_results:
                return listings, sources

    # BBB renders results into the page, so the HTML is the real source.
    for _url, html in iter_search_pages(path):
        sources += 1
        found, _skipped = parse.listings_from_html(html, default_category=category)
        for listing in found:
            listings.append(listing)
            if max_results is not None and len(listings) >= max_results:
                return listings, sources

    return listings, sources


# Server-rendered pages hide their data in the HTML rather than an XHR.
_EMBEDDED_MARKERS = (
    ("__NEXT_DATA__", "Next.js page data"),
    ("self.__next_f", "React Server Components stream"),
    ("application/ld+json", "JSON-LD structured data"),
    ("window.__INITIAL_STATE__", "inlined app state"),
)


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def is_bbb_url(url: str) -> bool:
    host = _host(url)
    return host == "bbb.org" or host.endswith(".bbb.org")


def inventory(path: str) -> list:
    """Every bbb.org response in the capture, with what it appears to contain.

    When the search API turns out not to exist, this is what says where the
    data actually lives.
    """
    rows = []
    for entry in _entries(path):
        url = (entry.get("request", {}) or {}).get("url", "")
        if not is_bbb_url(url):
            continue
        response = entry.get("response", {}) or {}
        content = response.get("content", {}) or {}
        body = content.get("text", "") or ""
        mime = (content.get("mimeType") or "").split(";")[0]

        markers = [label for token, label in _EMBEDDED_MARKERS if token in body]
        profile_links = body.count("/profile/")
        records = 0
        if body.strip().startswith(("{", "[")):
            try:
                records = len(parse.find_records(json.loads(body)))
            except ValueError:
                records = 0

        rows.append({
            "url": url,
            "status": response.get("status"),
            "mime": mime,
            "bytes": len(body),
            "records": records,
            "profile_links": profile_links,
            "markers": markers,
        })
    return rows


def describe(path: str) -> str:
    """A one-line summary of what a capture contains."""
    payload_count = sum(1 for _ in iter_search_payloads(path))
    search_pages = sum(1 for _ in iter_search_pages(path))
    details = len(detail_pages(path))
    return (f"{os.path.basename(path)}: {payload_count} search payload(s) with records, "
            f"{search_pages} rendered search page(s), {details} profile page(s)")
