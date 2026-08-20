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
    """Every listing in the capture. Returns (listings, payloads_seen)."""
    listings: List[parse.Listing] = []
    payloads = 0
    for _url, payload in iter_search_payloads(path):
        payloads += 1
        for listing in parse.iter_listings_from_payload(payload, default_category=category):
            listings.append(listing)
            if max_results is not None and len(listings) >= max_results:
                return listings, payloads
    return listings, payloads


def describe(path: str) -> str:
    """A one-line summary of what a capture contains."""
    payload_count = sum(1 for _ in iter_search_payloads(path))
    details = len(detail_pages(path))
    return (f"{os.path.basename(path)}: {payload_count} search payload(s) with records, "
            f"{details} profile page(s)")
