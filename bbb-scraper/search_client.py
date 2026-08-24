"""Approach A -- fetch BBB's server-rendered search pages.

The spec assumed an internal JSON search API. A capture of the live site says
otherwise: search results are rendered into the page, and every result carries
schema.org JSON-LD alongside it. So Approach A is a plain HTTP GET of

    https://www.bbb.org/search?find_country=USA&find_text=Plumber
                              &find_loc=Wichita%2C+KS&page=2

parsed by parse.listings_from_html. No browser needed unless Cloudflare says so.

Observed from the capture: page size is 15 (the first item on the second page
carried "position": 16), and a search page is roughly 240KB.
"""

from __future__ import annotations

import urllib.parse
from typing import Iterator, Optional, Tuple

import metros
import parse
from api_client import BLOCK_STATUSES, BlockedError, RateLimiter, ApiClient

BBB_BASE = "https://www.bbb.org"
SEARCH_PATH = "/search"
PAGE_SIZE = 15

# A Cloudflare interstitial is a 200 with no results, which is otherwise
# indistinguishable from "this city has no plumbers".
_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "attention required!",
)


def build_search_url(category: str, location: str, page: int = 1,
                     base_url: str = BBB_BASE, entity: Optional[str] = None) -> str:
    """The search URL for a category + location, in the site's own parameter shape."""
    params = {
        "find_country": "USA",
        "find_text": metros.category_label(category),
        "find_loc": metros.location_label(location),
    }
    if entity:
        # BBB's own category id (tob_id), e.g. 10113-000 for Plumber. Optional:
        # find_text alone returns the same listings.
        params["find_entity"] = entity
    if page and page > 1:
        params["page"] = str(page)
    return f"{base_url.rstrip('/')}{SEARCH_PATH}?" + urllib.parse.urlencode(params)


def looks_challenged(html: str) -> bool:
    head = (html or "")[:4000].lower()
    return any(marker in head for marker in _CHALLENGE_MARKERS)


class SearchClient:
    """Paginating client over the rendered search pages."""

    def __init__(
        self,
        limiter: Optional[RateLimiter] = None,
        timeout: float = 30.0,
        max_consecutive_blocks: int = 3,
        base_url: str = BBB_BASE,
        entity: Optional[str] = None,
        verbose: bool = False,
    ):
        self.base_url = base_url
        self.entity = entity
        self.verbose = verbose
        # Reuse the API client's session, pacing, UA rotation and backoff.
        self._api = ApiClient(
            limiter=limiter,
            timeout=timeout,
            max_consecutive_blocks=max_consecutive_blocks,
            verbose=verbose,
        )
        self._api.client.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )

    # ------------------------------------------------------------------
    @property
    def client(self):
        return self._api.client

    def close(self) -> None:
        self._api.close()

    def __enter__(self) -> "SearchClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[search] {message}", flush=True)

    # ------------------------------------------------------------------
    def fetch_page(self, category: str, location: str, page: int) -> str:
        """One search page's HTML. Raises BlockedError when Cloudflare intervenes."""
        url = build_search_url(category, location, page, self.base_url, self.entity)
        self._log(f"GET {url}")
        self._api.limiter.wait()

        response = self._api.client.get(url, headers={"Referer": self.base_url + "/"})
        if response.status_code in BLOCK_STATUSES:
            self._api.consecutive_blocks += 1
            raise BlockedError(
                f"search page {page} returned {response.status_code} "
                f"(consecutive={self._api.consecutive_blocks})"
            )
        if response.status_code >= 400:
            self._log(f"page {page} returned {response.status_code}")
            return ""

        html = response.text
        if looks_challenged(html):
            self._api.consecutive_blocks += 1
            raise BlockedError("Cloudflare served a challenge page instead of results")

        self._api.consecutive_blocks = 0
        return html

    def probe(self, category: str, location: str) -> int:
        """Listings on page one. Zero means this path is not usable."""
        try:
            html = self.fetch_page(category, location, 1)
        except BlockedError as exc:
            self._log(f"probe blocked: {exc}")
            return 0
        listings, _skipped = parse.listings_from_html(html, default_category=category)
        self._log(f"probe found {len(listings)} listing(s)")
        return len(listings)

    def iter_pages(
        self,
        category: str,
        location: str,
        start_page: int = 1,
        max_pages: int = 100,
    ) -> Iterator[Tuple[int, list]]:
        """Yield (page_number, listings) until the results run out.

        Stops on an empty page, and on a page repeating the previous one --
        paginating past the end tends to clamp to the last page rather than 404.
        """
        previous = None
        for offset in range(max_pages):
            page = start_page + offset
            html = self.fetch_page(category, location, page)
            if not html:
                return

            listings, skipped = parse.listings_from_html(html, default_category=category)
            if skipped:
                self._log(f"page {page}: {skipped} record(s) had no readable name")
            if not listings:
                self._log(f"page {page} has no listings -- end of results")
                return

            signature = "::".join(f"{l.company_name}|{l.profile_url}" for l in listings[:10])
            if signature == previous:
                self._log(f"page {page} repeats page {page - 1} -- end of results")
                return
            previous = signature

            yield page, listings

    # ------------------------------------------------------------------
    def absolute(self, url: str) -> str:
        """Resolve a profile link against the host this client is pointed at.

        Listings are parsed by a client-agnostic function that absolutizes
        against bbb.org, so a client aimed elsewhere (a proxy, a test double)
        has to rewrite the host before fetching.
        """
        if not url:
            return url
        if self.base_url.rstrip("/") != BBB_BASE and url.startswith(BBB_BASE):
            return self.base_url.rstrip("/") + url[len(BBB_BASE):]
        if url.startswith("/"):
            return self.base_url.rstrip("/") + url
        return url

    def fetch_detail(self, profile_url: str):
        """Profile page as a Listing, using the same paced session."""
        html = self._api.fetch_detail_html(self.absolute(profile_url))
        return parse.listing_from_profile_html(html) if html else None
