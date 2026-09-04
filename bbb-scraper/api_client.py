"""Approach A -- BBB's internal search JSON API.

The endpoint is deliberately NOT hardcoded. BBB's search API is undocumented,
unversioned, and gated by Cloudflare, so a constant baked in today is a silent
breakage tomorrow. Instead the endpoint comes from one of three places, in
order of reliability:

  1. --from-har FILE  : a HAR exported from your browser's Network tab while
                        doing the search by hand. Highest fidelity -- it carries
                        the exact URL, query params and full header set
                        (User-Agent, Referer, cookies) of a real session.
  2. --endpoints FILE : a small JSON config (see endpoints.example.json), which
                        `--from-har` can generate for you with --save-endpoints.
  3. --discover       : fetch the human search page and mine it for API routes
                        (Next.js data blobs, fetch() calls in the JS bundles).
                        Best-effort; use it to bootstrap, then save the result.

If none of these produce a working endpoint, or Cloudflare rejects the
non-browser session, scraper.py falls back to Approach B (Playwright).
"""

from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - dependency is in requirements.txt
    httpx = None

BBB_BASE = "https://www.bbb.org"
SEARCH_PAGE = BBB_BASE + "/search"

# Rotated once per session, not per request -- a UA that changes mid-session is
# a stronger bot signal than a stale one.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
]

BLOCK_STATUSES = {403, 429, 503}


def document_headers(referer: Optional[str] = None) -> Dict[str, str]:
    """What a browser sends when a person clicks through to a page.

    Kept in one place so the search and detail paths cannot drift apart: it
    was the drift -- a JSON Accept on an HTML request -- that got profile
    pages blocked.
    """
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Referer": referer or (BBB_BASE + "/"),
    }


class BlockedError(RuntimeError):
    """Raised after N consecutive blocks -- caller saves partial results."""


class EndpointUnavailable(RuntimeError):
    """No usable API endpoint. Caller should fall back to Approach B."""


# --------------------------------------------------------------------------
# rate limiting
# --------------------------------------------------------------------------

@dataclass
class RateLimiter:
    """1 request per 2-4s with jitter. No concurrency in v1, by design."""

    min_delay: float = 2.0
    max_delay: float = 4.0
    _last: float = field(default=0.0, repr=False)

    def wait(self) -> None:
        target = random.uniform(self.min_delay, self.max_delay)
        elapsed = time.monotonic() - self._last
        if self._last and elapsed < target:
            time.sleep(target - elapsed)
        self._last = time.monotonic()


# --------------------------------------------------------------------------
# endpoint spec
# --------------------------------------------------------------------------

@dataclass
class EndpointSpec:
    """One search API call, with everything needed to replay + paginate it."""

    url: str
    method: str = "GET"
    params: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    page_param: str = "page"
    page_size_param: Optional[str] = None
    page_size: Optional[int] = None
    first_page: int = 1
    category_param: Optional[str] = None
    location_param: Optional[str] = None
    source: str = "config"

    @classmethod
    def from_dict(cls, data: dict) -> "EndpointSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "method": self.method,
            "params": self.params,
            "headers": self.headers,
            "cookies": self.cookies,
            "page_param": self.page_param,
            "page_size_param": self.page_size_param,
            "page_size": self.page_size,
            "first_page": self.first_page,
            "category_param": self.category_param,
            "location_param": self.location_param,
        }

    def build_params(self, page: int, category: str, location: str) -> Dict[str, str]:
        params = dict(self.params)
        params[self.page_param] = str(page)
        if self.page_size_param and self.page_size:
            params[self.page_size_param] = str(self.page_size)
        if self.category_param and category:
            params[self.category_param] = category
        if self.location_param and location:
            params[self.location_param] = location
        return params


def load_endpoints(path: str) -> List[EndpointSpec]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("endpoints", [data])
    specs = [EndpointSpec.from_dict(d) for d in data]
    for spec in specs:
        spec.source = f"config:{os.path.basename(path)}"
    return specs


def save_endpoints(path: str, specs: List[EndpointSpec], include_cookies: bool = False) -> None:
    payload = []
    for spec in specs:
        d = spec.to_dict()
        if not include_cookies:
            # Cookies are session credentials; don't persist them unless asked.
            d["cookies"] = {}
            d["headers"] = {k: v for k, v in d["headers"].items() if k.lower() != "cookie"}
        payload.append(d)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"endpoints": payload}, fh, indent=2)


# --------------------------------------------------------------------------
# discovery: HAR
# --------------------------------------------------------------------------

def is_bbb_url(url: str) -> bool:
    """True only when the *host* is bbb.org.

    A substring check is not enough: ad and analytics pixels carry the page
    address in a query parameter (url=https%3A%2F%2Fwww.bbb.org%2F...), so
    every tracker on the page looked like a BBB endpoint.
    """
    if not url:
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "bbb.org" or host.endswith(".bbb.org")


_JSON_HINT = re.compile(r"json", re.I)
_SEARCH_HINT = re.compile(r"(search|seek|find|business|accredited|typeahead)", re.I)
_PAGE_PARAM_NAMES = ("page", "pageNumber", "page_number", "pg", "offset", "start", "from", "skip")
_CATEGORY_PARAM_NAMES = ("find_text", "findText", "category", "categorySlug", "term", "query", "q",
                         "keyword", "search", "filter_category")
_LOCATION_PARAM_NAMES = ("find_loc", "findLoc", "location", "locationSlug", "city", "geo", "place",
                         "where", "postalCode")


def endpoints_from_har(path: str, max_endpoints: int = 5) -> List[EndpointSpec]:
    """Mine a browser-exported HAR for the search XHR calls.

    This is the spec's "watch the Network tab" step, done mechanically: it picks
    JSON responses whose bodies actually contain business-shaped records, and
    lifts the full header set from the real session.
    """
    from parse import find_records  # local import keeps parse/api decoupled

    with open(path, encoding="utf-8") as fh:
        har = json.load(fh)

    entries = har.get("log", {}).get("entries", [])
    scored: List = []

    for entry in entries:
        request = entry.get("request", {})
        response = entry.get("response", {})
        url = request.get("url", "")
        if not is_bbb_url(url):
            continue

        mime = (response.get("content", {}) or {}).get("mimeType", "")
        body = (response.get("content", {}) or {}).get("text", "") or ""
        if not _JSON_HINT.search(mime) and not body.strip().startswith(("{", "[")):
            continue

        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None

        record_count = len(find_records(payload)) if payload is not None else 0
        score = record_count * 10
        if _SEARCH_HINT.search(url):
            score += 5
        if "/api/" in url:
            score += 3
        if score <= 0:
            continue

        spec = _spec_from_har_request(request)
        spec.source = f"har:{os.path.basename(path)}"
        scored.append((score, record_count, spec))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [spec for _, _, spec in scored[:max_endpoints]]


def _spec_from_har_request(request: dict) -> EndpointSpec:
    url = request.get("url", "")
    split = urllib.parse.urlsplit(url)
    params = {k: v[0] for k, v in urllib.parse.parse_qs(split.query).items()}

    headers = {}
    for header in request.get("headers", []):
        name = header.get("name", "")
        if not name or name.startswith(":"):       # HTTP/2 pseudo-headers
            continue
        if name.lower() in {"content-length", "host", "connection"}:
            continue
        headers[name] = header.get("value", "")

    cookies = {c.get("name"): c.get("value", "") for c in request.get("cookies", []) if c.get("name")}

    return EndpointSpec(
        url=urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", "")),
        method=request.get("method", "GET").upper(),
        params=params,
        headers=headers,
        cookies=cookies,
        page_param=_pick_param(params, _PAGE_PARAM_NAMES, default="page"),
        category_param=_pick_param(params, _CATEGORY_PARAM_NAMES),
        location_param=_pick_param(params, _LOCATION_PARAM_NAMES),
        first_page=_first_page_from(params),
    )


def _pick_param(params: Dict[str, str], candidates, default: Optional[str] = None) -> Optional[str]:
    lowered = {k.lower(): k for k in params}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return default


def _first_page_from(params: Dict[str, str]) -> int:
    name = _pick_param(params, _PAGE_PARAM_NAMES)
    if name and str(params.get(name, "")).isdigit():
        return int(params[name])
    return 1


# --------------------------------------------------------------------------
# discovery: probe the live search page
# --------------------------------------------------------------------------

_API_PATH_RE = re.compile(r"[\"'](/api/[A-Za-z0-9_\-/\.]{3,120})[\"']")
_ABS_API_RE = re.compile(r"[\"'](https://[a-z0-9.\-]*bbb\.org/api/[A-Za-z0-9_\-/\.]{3,120})[\"']")
_SCRIPT_SRC_RE = re.compile(r"<script[^>]+src=[\"']([^\"']+)[\"']", re.I)


def discover_endpoints(
    client: "httpx.Client",
    category: str,
    location: str,
    limiter: Optional[RateLimiter] = None,
    max_bundles: int = 6,
) -> List[EndpointSpec]:
    """Best-effort discovery from the rendered search page and its JS bundles.

    Returns candidate specs, most promising first. Verify with `probe()` before
    trusting one -- this finds API *routes*, and not every route is the search.
    """
    limiter = limiter or RateLimiter()
    search_url = f"{SEARCH_PAGE}?find_country=USA&find_text={urllib.parse.quote(category)}" \
                 f"&find_loc={urllib.parse.quote(location)}"

    limiter.wait()
    response = client.get(search_url, headers={"Referer": BBB_BASE + "/"})
    if response.status_code in BLOCK_STATUSES:
        raise BlockedError(f"search page returned {response.status_code} (Cloudflare?)")
    html = response.text

    paths: List[str] = []
    for match in _ABS_API_RE.finditer(html):
        paths.append(match.group(1))
    for match in _API_PATH_RE.finditer(html):
        paths.append(BBB_BASE + match.group(1))

    # The routes usually live in the JS bundles, not the HTML shell.
    bundles = [_join(search_url, src) for src in _SCRIPT_SRC_RE.findall(html)]
    bundles = [b for b in bundles if b and "bbb.org" in b][:max_bundles]
    for bundle in bundles:
        limiter.wait()
        try:
            js = client.get(bundle, headers={"Referer": search_url}).text
        except Exception:
            continue
        for match in _ABS_API_RE.finditer(js):
            paths.append(match.group(1))
        for match in _API_PATH_RE.finditer(js):
            paths.append(BBB_BASE + match.group(1))

    seen = set()
    specs: List[EndpointSpec] = []
    for url in paths:
        if url in seen:
            continue
        seen.add(url)
        if not _SEARCH_HINT.search(url):
            continue
        specs.append(
            EndpointSpec(
                url=url,
                params={"find_country": "USA", "find_text": category, "find_loc": location},
                headers={"Referer": search_url, "Accept": "application/json"},
                page_param="page",
                category_param="find_text",
                location_param="find_loc",
                source="discovered",
            )
        )
    specs.sort(key=lambda s: (0 if "search" in s.url.lower() else 1, len(s.url)))
    return specs


def _join(base: str, src: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("http"):
        return src
    return urllib.parse.urljoin(base, src)


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------

class ApiClient:
    """Paginating search-API client with polite pacing and block handling."""

    def __init__(
        self,
        spec: Optional[EndpointSpec] = None,
        limiter: Optional[RateLimiter] = None,
        timeout: float = 30.0,
        max_consecutive_blocks: int = 3,
        user_agent: Optional[str] = None,
        verbose: bool = False,
    ):
        if httpx is None:
            raise RuntimeError("httpx is required for Approach A (pip install -r requirements.txt)")
        self.spec = spec
        self.limiter = limiter or RateLimiter()
        self.max_consecutive_blocks = max_consecutive_blocks
        self.verbose = verbose
        self.user_agent = user_agent or random.choice(USER_AGENTS)
        self.consecutive_blocks = 0
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": BBB_BASE + "/",
            },
        )

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[api] {message}", flush=True)

    # ------------------------------------------------------------------
    def request_page(self, spec: EndpointSpec, page: int, category: str, location: str) -> Any:
        """One paginated request, with exponential backoff on blocks.

        Raises BlockedError once `max_consecutive_blocks` requests in a row are
        rejected, so the caller can stop and save partial results.
        """
        params = spec.build_params(page, category, location)
        headers = dict(spec.headers)
        headers.setdefault("User-Agent", self.user_agent)

        backoff = 5.0
        for attempt in range(self.max_consecutive_blocks):
            self.limiter.wait()
            try:
                response = self.client.request(
                    spec.method, spec.url, params=params, headers=headers,
                    cookies=spec.cookies or None,
                )
            except Exception as exc:  # network hiccup -- same backoff path
                self._log(f"page {page} request error: {exc}")
                time.sleep(backoff + random.uniform(0, 1))
                backoff *= 2
                continue

            if response.status_code in BLOCK_STATUSES:
                self.consecutive_blocks += 1
                self._log(
                    f"page {page} blocked with {response.status_code} "
                    f"(consecutive={self.consecutive_blocks})"
                )
                if self.consecutive_blocks >= self.max_consecutive_blocks:
                    raise BlockedError(
                        f"aborting after {self.consecutive_blocks} consecutive blocks "
                        f"(last status {response.status_code})"
                    )
                time.sleep(backoff + random.uniform(0, 1))
                backoff *= 2
                continue

            if response.status_code >= 400:
                raise EndpointUnavailable(f"{spec.url} returned {response.status_code}")

            self.consecutive_blocks = 0
            try:
                return response.json()
            except ValueError:
                raise EndpointUnavailable(f"{spec.url} returned non-JSON body")

        raise BlockedError(f"page {page} failed after {self.max_consecutive_blocks} attempts")

    # ------------------------------------------------------------------
    def fetch_detail_html(self, url: str, referer: Optional[str] = None) -> Optional[str]:
        """Fetch a business profile page as HTML, paced like any other request.

        Approach A needs this too: filters on headcount or review counts read
        fields the search card doesn't carry, and without it the run would
        filter on values it never fetched.

        Sent as a document navigation, not an API call. The client default
        Accept is "application/json" for the search endpoints, and asking an
        HTML page for JSON with no Sec-Fetch headers is a plain bot signature
        -- profile pages answered 403 to exactly that request shape. The
        Referer is the search page the link came from, because that is where a
        real click would have come from.
        """
        self.limiter.wait()
        try:
            response = self.client.get(url, headers=document_headers(referer))
        except Exception as exc:
            self._log(f"detail request failed for {url}: {exc}")
            return None
        if response.status_code in BLOCK_STATUSES:
            self.consecutive_blocks += 1
            self._log(f"detail blocked with {response.status_code} for {url}")
            if self.consecutive_blocks >= self.max_consecutive_blocks:
                raise BlockedError(f"aborting after {self.consecutive_blocks} consecutive blocks")
            return None
        if response.status_code >= 400:
            return None
        self.consecutive_blocks = 0
        return response.text

    # ------------------------------------------------------------------
    def probe(self, spec: EndpointSpec, category: str, location: str) -> int:
        """Try one page. Returns the number of business records found (0 = no good)."""
        from parse import find_records
        try:
            payload = self.request_page(spec, spec.first_page, category, location)
        except (EndpointUnavailable, BlockedError) as exc:
            self._log(f"probe failed for {spec.url}: {exc}")
            return 0
        count = len(find_records(payload))
        self._log(f"probe {spec.url} -> {count} records")
        return count

    def select_endpoint(self, specs: List[EndpointSpec], category: str, location: str) -> EndpointSpec:
        """First spec that actually returns business records wins."""
        for spec in specs:
            if self.probe(spec, category, location) > 0:
                self.spec = spec
                return spec
        raise EndpointUnavailable(
            "no candidate endpoint returned business records; "
            "capture a HAR from your browser (--from-har) or use --browser"
        )

    # ------------------------------------------------------------------
    def iter_pages(
        self,
        category: str,
        location: str,
        start_page: int = 1,
        max_pages: int = 100,
    ) -> Iterator:
        """Yield (page_number, payload) until results are exhausted.

        Stops cleanly on the last page -- an empty result list, or a page whose
        records repeat the previous page's (some APIs clamp instead of 404ing).
        """
        from parse import find_records

        if self.spec is None:
            raise EndpointUnavailable("no endpoint configured")

        previous_signature = None
        for offset in range(max_pages):
            page = start_page + offset
            payload = self.request_page(self.spec, page, category, location)
            records = find_records(payload)
            if not records:
                self._log(f"page {page} empty -- end of results")
                return
            signature = _signature(records)
            if signature == previous_signature:
                self._log(f"page {page} repeats page {page - 1} -- end of results")
                return
            previous_signature = signature
            yield page, payload


def _signature(records: List[dict]) -> str:
    from parse import listing_from_record
    parts = []
    for record in records[:10]:
        listing = listing_from_record(record)
        parts.append(f"{listing.company_name}|{listing.profile_url}|{listing.phone}")
    return "::".join(parts)
