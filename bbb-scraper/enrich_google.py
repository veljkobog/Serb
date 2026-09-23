"""Optional Google Places enrichment -- rating + review count per company.

BBB does not carry Google reviews, so this is a second source, not a filter on
the BBB pull. It is opt-in (`--google-key`), billed per request by Google, and
deliberately conservative:

  * one request per company (Places API v1 `places:searchText`, field-masked so
    we're not paying for fields we don't use)
  * results cached on disk, so a re-run or a resumed run costs nothing twice
  * a hard `--max-google-lookups` cap per run
  * every match carries a confidence, because a wrong match silently inflates
    the review count of a business that never earned it

Match confidence:
  high    domain or phone matches the BBB record
  medium  name matches strongly and the city agrees
  low     anything weaker -- kept but flagged, and dropped by
          --min-google-reviews unless you pass --allow-low-match
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from diskcache import MISS, JsonCache
from parse import Listing, normalize_domain, normalize_phone, parse_count, parse_rating_value

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.rating",
    "places.userRatingCount",
    "places.websiteUri",
    "places.nationalPhoneNumber",
])

# Google's terms allow caching place IDs indefinitely but not other place
# content long-term, so ratings expire.
DEFAULT_TTL_DAYS = 30

_STOPWORDS = {
    "the", "and", "inc", "llc", "co", "company", "corp", "corporation", "ltd", "services",
    "service", "of", "&",
}


class GoogleUnavailable(RuntimeError):
    """No API key, or httpx missing."""


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

class PlacesCache(JsonCache):
    """Places results on disk, so a re-run or a resumed run doesn't re-bill.

    Google's terms allow caching place IDs indefinitely but not other place
    content long-term, so ratings expire on the TTL.
    """
    prefix = ".places-"


def cache_key(listing: Listing) -> str:
    if listing.profile_url:
        return listing.profile_url
    return f"{listing.company_name}|{listing.city}|{listing.state}".lower()


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def _tokens(name: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t and t not in _STOPWORDS}


def match_confidence(listing: Listing, place: dict) -> str:
    """How much to trust that `place` is the same business as `listing`."""
    place_domain = normalize_domain(place.get("websiteUri"))
    place_phone = normalize_phone(place.get("nationalPhoneNumber"))

    if listing.website and place_domain and listing.website == place_domain:
        return "high"
    if listing.phone and place_phone and listing.phone == place_phone:
        return "high"

    ours = _tokens(listing.company_name)
    theirs = _tokens((place.get("displayName") or {}).get("text", ""))
    if not ours or not theirs:
        return "low"
    overlap = len(ours & theirs) / len(ours)

    address = (place.get("formattedAddress") or "").lower()
    city_ok = bool(listing.city) and listing.city.lower() in address

    if overlap >= 0.6 and city_ok:
        return "medium"
    if overlap >= 0.8:
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

@dataclass
class GoogleStats:
    looked_up: int = 0
    cached: int = 0
    matched: int = 0
    low_match: int = 0
    no_result: int = 0
    errors: int = 0
    capped: int = 0


class PlacesClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str = PLACES_URL,
        cache: Optional[PlacesCache] = None,
        timeout: float = 20.0,
        min_delay: float = 0.0,
        verbose: bool = False,
    ):
        if httpx is None:
            raise GoogleUnavailable("httpx is required for Google enrichment")
        if not api_key:
            raise GoogleUnavailable("no Google API key -- pass --google-key or set GOOGLE_MAPS_API_KEY")
        self.api_key = api_key
        self.endpoint = endpoint
        self.cache = cache or PlacesCache(None)
        self.min_delay = min_delay
        self.verbose = verbose
        self.stats = GoogleStats()
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()
        self.cache.save()

    def __enter__(self) -> "PlacesClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[google] {message}", flush=True)

    # ------------------------------------------------------------------
    def query_text(self, listing: Listing) -> str:
        parts = [listing.company_name, listing.street, listing.city, listing.state]
        return " ".join(p for p in parts if p)

    def search(self, listing: Listing) -> Optional[dict]:
        """One Places search. Returns the top candidate, or None."""
        response = self.client.post(
            self.endpoint,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": self.api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json={"textQuery": self.query_text(listing), "maxResultCount": 1},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Places API {response.status_code}: {response.text[:200]}")
        places = (response.json() or {}).get("places") or []
        return places[0] if places else None

    # ------------------------------------------------------------------
    def enrich(self, listing: Listing) -> None:
        """Fill the google_* fields on one listing, in place."""
        key = cache_key(listing)
        cached = self.cache.get(key)
        if cached is not MISS:
            place = cached
            self.stats.cached += 1
        else:
            if self.min_delay:
                time.sleep(self.min_delay)
            try:
                place = self.search(listing)
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"lookup failed for {listing.company_name}: {exc}")
                return
            self.stats.looked_up += 1
            self.cache.put(key, place)

        if not place:
            self.stats.no_result += 1
            return

        confidence = match_confidence(listing, place)
        listing.google_place_id = place.get("id", "") or ""
        listing.google_rating = parse_rating_value(place.get("rating"))
        listing.google_reviews = parse_count(place.get("userRatingCount"))
        listing.google_match = confidence
        self.stats.matched += 1
        if confidence == "low":
            self.stats.low_match += 1


@dataclass
class Preflight:
    """What an enrichment run would cost, before any of it is billed."""
    total: int = 0
    cached: int = 0
    billable: int = 0
    capped: int = 0

    def report(self, cost_per_lookup: Optional[float] = None) -> str:
        lines = [
            f"  candidates      : {self.total}",
            f"  already cached  : {self.cached}",
            f"  billable lookups: {self.billable}",
        ]
        if self.capped:
            lines.append(f"  over the cap    : {self.capped} (raise --max-google-lookups)")
        if cost_per_lookup:
            lines.append(f"  estimated cost  : {self.billable * cost_per_lookup:,.2f} "
                         f"at {cost_per_lookup} per lookup")
        return "\n".join(lines)


def preflight(listings: Iterable[Listing], cache: PlacesCache,
              max_lookups: Optional[int] = None) -> Preflight:
    """Count what enrichment would cost without calling Google.

    Uses the same cache keys and the same cap as the real pass, so the number
    it reports is the number you would actually be billed for.
    """
    stats = Preflight()
    for listing in listings:
        stats.total += 1
        if cache.get(cache_key(listing)) is not MISS:
            stats.cached += 1
            continue
        if max_lookups is not None and stats.billable >= max_lookups:
            stats.capped += 1
            continue
        stats.billable += 1
    return stats


def enrich_listings(
    listings: Iterable[Listing],
    client: PlacesClient,
    max_lookups: Optional[int] = None,
) -> GoogleStats:
    """Enrich up to `max_lookups` listings, cache hits not counted against it."""
    for listing in listings:
        if max_lookups is not None and client.stats.looked_up >= max_lookups:
            client.stats.capped += 1
            continue
        client.enrich(listing)
    return client.stats


def resolve_api_key(explicit: Optional[str]) -> Optional[str]:
    return explicit or os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_PLACES_API_KEY")
