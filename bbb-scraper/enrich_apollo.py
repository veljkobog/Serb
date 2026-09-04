"""Apollo organization lookup -- recovers the website BBB won't hand over.

Why this exists: BBB profile pages return 403 to a plain HTTP client, and the
website / headcount / rating fields live on the profile, not the search card.
A live pull therefore arrives with `website` blank, which quietly defeats
--require-website: every row looks like "we can't tell" and nothing is
filtered. This pass fills the gap from a second source.

It uses Apollo's *lookup* endpoint, which is free, rather than `enrich` or
`mixed_companies/search`, which bill a credit each. Lookup returns shallow
records -- id, name, domain, website_url -- and that is exactly enough to:

  * fill `website`, so --require-website means something again
  * carry `apollo_org_id` forward, so the later people-match can scope itself
    to the right company instead of matching on a name string

What this pass deliberately does NOT do is filter on headcount. Apollo honours
only its own buckets (1-10, 11-50, 51-200, ...) and *silently ignores* any
other range: a probe with "5,1000000" came back byte-identical to no filter at
all. There is no boundary at 5, so a >=5 rule cannot be enforced here without
either moving the bar to 11 or paying per company. The headcount instead rides
along with the paid people-match downstream, where it costs nothing extra.

Match confidence, same idea as the Google pass -- a wrong domain on a lead is
worse than a blank one, because blank is visibly missing and wrong is not:

  high    the BBB record already had a domain and Apollo agrees with it
  medium  strong name overlap, and Apollo is the only source for the domain
  low     weak overlap -- recorded, but never used to fill `website`
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from diskcache import MISS, JsonCache
from parse import Listing, normalize_domain

# The REST path for the free lookup is discovered, not remembered: Apollo has
# moved between /v1 and /api/v1, and guessing it produced a 404 once already.
# `probe_endpoints` resolves it, and --apollo-endpoint pins it.
# Discovered by --apollo-probe, not remembered: /organizations/lookup was a
# guess and 404s. /organizations/search is what this account actually serves.
DEFAULT_ENDPOINT = "https://api.apollo.io/v1/organizations/search"

CANDIDATE_ENDPOINTS = [
    "https://api.apollo.io/v1/organizations/search",
    "https://api.apollo.io/api/v1/organizations/search",
    "https://api.apollo.io/v1/organizations/lookup",
    "https://api.apollo.io/api/v1/organizations/lookup",
]

# A query no real company matches. Apollo bills search-style endpoints only
# when they return results, so probing with this costs nothing whichever
# endpoint answers.
PROBE_NAME = "zzzqx-nonexistent-company-999"

DEFAULT_TTL_DAYS = 30

#: Apollo looked and has no such company. Distinct from "" (never looked).
#: A BBB listing with real headcount and no Apollo record is a company the
#: Apollo-first tools cannot see at all -- the reason to scrape BBB in the
#: first place -- so it is labelled rather than dropped.
NOT_IN_APOLLO = "not-in-apollo"

_STOPWORDS = {
    "the", "and", "inc", "llc", "co", "company", "corp", "corporation", "ltd",
    "services", "service", "of", "&", "plumbing", "roofing", "heating",
    "cooling", "electric", "electrical", "hvac", "air", "landscaping",
}


class ApolloUnavailable(RuntimeError):
    """No API key, or httpx missing."""


class OrgCache(JsonCache):
    """Lookup results on disk. Free calls, but still rate-limited and slow."""
    prefix = ".apollo-"


def cache_key(listing: Listing) -> str:
    if listing.profile_url:
        return listing.profile_url
    return f"{listing.company_name}|{listing.city}|{listing.state}".lower()


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def _tokens(name: str) -> set:
    """Name words that actually distinguish one business from another.

    Trade words are stopwords here: in a single-vertical pull every candidate
    is a plumbing company, so "plumbing" overlapping proves nothing and would
    inflate every score toward a match.
    """
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower())
            if t and t not in _STOPWORDS}


def match_confidence(listing: Listing, org: dict) -> str:
    org_domain = normalize_domain(org.get("website_url") or org.get("domain"))
    if listing.website and org_domain and listing.website == org_domain:
        return "high"

    ours = _tokens(listing.company_name)
    theirs = _tokens(org.get("name", ""))
    if not ours or not theirs:
        return "low"

    overlap = len(ours & theirs) / len(ours)
    if overlap >= 0.8:
        return "medium"
    if overlap >= 0.6 and len(ours) > 1:
        return "medium"
    return "low"


def best_match(listing: Listing, orgs: List[dict]) -> tuple:
    """(org, confidence) for the strongest candidate, or (None, "")."""
    ranked = []
    for org in orgs or []:
        confidence = match_confidence(listing, org)
        ranked.append(({"high": 2, "medium": 1, "low": 0}[confidence], org, confidence))
    if not ranked:
        return None, ""
    ranked.sort(key=lambda row: row[0], reverse=True)
    _score, org, confidence = ranked[0]
    return org, confidence


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

PROFILE_PATH = "/users/api_profile"


@dataclass
class ApolloStats:
    looked_up: int = 0
    cached: int = 0
    matched: int = 0
    low_match: int = 0
    websites_filled: int = 0
    no_result: int = 0
    errors: int = 0
    capped: int = 0
    #: Balance before and after, so the run reports what it actually cost
    #: rather than what the docs imply. 150 lookups per list is not a spend
    #: to take on trust.
    balance_before: Optional[int] = None
    balance_after: Optional[int] = None

    @property
    def credits_spent(self) -> Optional[int]:
        if self.balance_before is None or self.balance_after is None:
            return None
        return max(0, self.balance_before - self.balance_after)


class ApolloClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        cache: Optional[OrgCache] = None,
        timeout: float = 20.0,
        min_delay: float = 0.0,
        verbose: bool = False,
    ):
        if httpx is None:
            raise ApolloUnavailable("httpx is required for Apollo enrichment")
        if not api_key:
            raise ApolloUnavailable(
                "no Apollo API key -- pass --apollo-key or set APOLLO_API_KEY")
        self.api_key = api_key
        self.endpoint = endpoint
        self.cache = cache or OrgCache(None)
        self.min_delay = min_delay
        self.verbose = verbose
        self.stats = ApolloStats()
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()
        self.cache.save()

    def read_balance(self) -> Optional[int]:
        """Live credit balance, or None when it cannot be read."""
        base = self.endpoint.split("/organizations/")[0]
        try:
            response = self.client.get(
                base + PROFILE_PATH,
                headers={"accept": "application/json", "x-api-key": self.api_key},
                params={"include_credit_usage": "true"})
            if response.status_code >= 400:
                return None
            value = (response.json() or {}).get("num_credits_remaining")
            return int(value) if value is not None else None
        except Exception:
            return None

    def __enter__(self) -> "ApolloClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[apollo] {message}", flush=True)

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "accept": "application/json",
                "x-api-key": self.api_key}

    # ------------------------------------------------------------------
    def payload(self, listing: Listing) -> dict:
        """Fuzzy name, narrowed by city/state so a national chain's HQ in
        another state doesn't outrank the local shop we actually scraped."""
        # /organizations/search takes q_organization_name; the fuzzy_name +
        # display_mode pair belonged to the lookup path, which 404s on this
        # account. Sending the wrong one back is a 422, not a match.
        body = {
            "q_organization_name": listing.company_name,
            "per_page": 5,
        }
        where = ", ".join(p for p in (listing.city, listing.state) if p)
        if where:
            body["organization_locations"] = [where]
        return body

    def search(self, listing: Listing) -> List[dict]:
        response = self.client.post(self.endpoint, headers=self._headers(),
                                    json=self.payload(listing))
        if response.status_code >= 400:
            raise RuntimeError(f"Apollo {response.status_code}: {response.text[:200]}")
        body = response.json() or {}
        # Net-new companies come back under "organizations"; ones the team has
        # already saved come back under "accounts" with a different id field.
        return list(body.get("organizations") or []) + list(body.get("accounts") or [])

    # ------------------------------------------------------------------
    def enrich(self, listing: Listing) -> None:
        """Fill website / apollo_* on one listing, in place."""
        key = cache_key(listing)
        cached = self.cache.get(key)
        if cached is not MISS:
            orgs = cached or []
            self.stats.cached += 1
        else:
            if self.min_delay:
                time.sleep(self.min_delay)
            try:
                orgs = self.search(listing)
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"lookup failed for {listing.company_name}: {exc}")
                return
            self.stats.looked_up += 1
            self.cache.put(key, orgs)

        org, confidence = best_match(listing, orgs)
        if not org:
            # Recorded, not left blank. "" means the lookup never ran;
            # NOT_IN_APOLLO means it ran and Apollo has no such company --
            # which for a sizeable BBB listing is the interesting case, not a
            # reason to discard the row.
            listing.apollo_match = NOT_IN_APOLLO
            self.stats.no_result += 1
            return

        listing.apollo_match = confidence
        # An account row carries the org id under a different key than a
        # net-new organization row; using the wrong one matches nothing later.
        listing.apollo_org_id = org.get("organization_id") or org.get("id") or ""
        self.stats.matched += 1

        if confidence == "low":
            self.stats.low_match += 1
            return   # never let a weak match invent a website

        domain = normalize_domain(org.get("website_url") or org.get("domain"))
        if domain and not listing.website:
            listing.website = domain
            self.stats.websites_filled += 1


# --------------------------------------------------------------------------
# run helpers
# --------------------------------------------------------------------------

def enrich_listings(listings: Iterable[Listing], client: ApolloClient,
                    max_lookups: Optional[int] = None) -> ApolloStats:
    client.stats.balance_before = client.read_balance()
    for listing in listings:
        if max_lookups is not None and client.stats.looked_up >= max_lookups:
            client.stats.capped += 1
            continue
        client.enrich(listing)
    client.stats.balance_after = client.read_balance()
    return client.stats


def probe_endpoints(api_key: str, candidates: Optional[List[str]] = None,
                    timeout: float = 15.0) -> List[dict]:
    """Which candidate paths answer, using a query that matches nothing.

    Apollo bills its search-style endpoints only when they return results, so
    this discovery pass is free whichever path turns out to be live.
    """
    if httpx is None:
        raise ApolloUnavailable("httpx is required to probe Apollo")
    if not api_key:
        raise ApolloUnavailable("no Apollo API key")

    results = []
    body = {"q_organization_fuzzy_name": PROBE_NAME,
            "display_mode": "fuzzy_select_mode", "per_page": 1}
    headers = {"Content-Type": "application/json", "accept": "application/json",
               "x-api-key": api_key}
    with httpx.Client(timeout=timeout) as client:
        for url in candidates or CANDIDATE_ENDPOINTS:
            row = {"url": url, "status": None, "ok": False, "error": ""}
            try:
                response = client.post(url, headers=headers, json=body)
                row["status"] = response.status_code
                row["ok"] = response.status_code < 400
            except Exception as exc:
                row["error"] = str(exc)[:120]
            results.append(row)
    return results


def resolve_api_key(explicit: Optional[str]) -> Optional[str]:
    return explicit or os.environ.get("APOLLO_API_KEY")
