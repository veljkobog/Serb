"""Owner name + work email for a scraped company, and the >=5 headcount gate.

Three stages, in this order for a reason:

  1. people search   -- who at this company has an owner-ish title. Returns a
                        person id and a name; never an email.
  2. bulk match      -- turn those ids into work emails, 10 per request. This
                        is the step that costs credits.
  3. headcount gate  -- the match response carries the employer's headcount,
                        so the >=5 rule is applied from data already paid for.
                        Apollo cannot filter headcount at 5 for free: it
                        honours only its own buckets and silently ignores any
                        other range, so filtering here is the honest option.

The credit governor deliberately does NOT price each endpoint. Apollo's costs
vary by plan, waterfall spend is explicitly variable, and a hardcoded price
list would drift silently and overspend. Instead it reads the live balance
before each billable step and stops when the measured spend reaches the cap.
An unattended job must fail closed.

Two mistakes this module exists to avoid, both seen on real data:

  * matching a company in the wrong state. A name search for a local shop can
    surface a same-named company a thousand miles away, and the email that
    comes back is a stranger's. Every match is cross-checked against the BBB
    city/state and rejected on a mismatch.
  * treating "Apollo returned no headcount" as "under 5 employees". Unknown is
    not absent. Those rows are kept and flagged, never silently dropped.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from diskcache import MISS, JsonCache
from parse import Listing

# Paths are discovered, never remembered -- Apollo has moved between /v1 and
# /api/v1 and a guess already cost a 404. `scraper.py --apollo-probe` resolves
# these; --apollo-base pins the prefix once resolved.
DEFAULT_BASE = "https://api.apollo.io/v1"

PEOPLE_SEARCH = "/mixed_people/search"
BULK_MATCH = "/people/bulk_match"
PROFILE = "/users/api_profile"

#: Whoever signs off on a service contract at a 5-200 person trade business.
OWNER_TITLES = [
    "owner", "president", "founder", "co-founder", "ceo",
    "general manager", "vice president", "operations manager",
]

MAX_MATCH_BATCH = 10   # Apollo's documented ceiling for bulk_match

#: Apollo masks last names on some plans. A masked value is not a name.
_MASK_MARKERS = ("*", "•", "xxxxx")


class ApolloPeopleUnavailable(RuntimeError):
    pass


class CreditCapReached(RuntimeError):
    """Raised to stop a run rather than overspend an unattended budget."""


class PeopleCache(JsonCache):
    prefix = ".apollo-people-"


# --------------------------------------------------------------------------
# credit governor
# --------------------------------------------------------------------------

@dataclass
class CreditGovernor:
    """Stops the run at `cap` credits of *measured* spend.

    Measured, not estimated: it reads the balance from Apollo rather than
    pricing each call, because waterfall cost is variable by plan and a
    hardcoded price list would drift without anyone noticing.
    """
    cap: int
    starting_balance: Optional[int] = None
    latest_balance: Optional[int] = None

    @property
    def spent(self) -> int:
        if self.starting_balance is None or self.latest_balance is None:
            return 0
        return max(0, self.starting_balance - self.latest_balance)

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.spent)

    def observe(self, balance: Optional[int]) -> None:
        if balance is None:
            return
        if self.starting_balance is None:
            self.starting_balance = balance
        self.latest_balance = balance

    def check(self) -> None:
        """Called before each billable step. Fails closed."""
        if self.starting_balance is None:
            # Never managed to read a balance. Refusing here would block every
            # run on a reporting hiccup, so allow it -- but the caller records
            # that the cap was unverified, and says so in the summary.
            return
        if self.spent >= self.cap:
            raise CreditCapReached(
                f"daily cap of {self.cap} credits reached ({self.spent} spent); "
                f"stopping before spending more")


# --------------------------------------------------------------------------
# matching guards
# --------------------------------------------------------------------------

def is_masked(value: str) -> bool:
    """Apollo obfuscates last names on some plans -- that is not a name."""
    low = (value or "").lower()
    return any(marker in low for marker in _MASK_MARKERS)


def same_place(listing: Listing, person: dict) -> Optional[bool]:
    """Does the matched person's employer sit where the BBB record does?

    None when Apollo gave no location to compare -- unknown, not agreement.
    A national franchise's HQ in another state is exactly how a name search
    hands back a stranger's email.
    """
    org = person.get("organization") or {}
    haystack = " ".join(str(v) for v in (
        org.get("city"), org.get("state"), org.get("raw_address"),
        person.get("city"), person.get("state"),
    ) if v).lower()
    if not haystack:
        return None

    city = (listing.city or "").lower().strip()
    state = (listing.state or "").lower().strip()
    if city and city in haystack:
        return True
    if state and len(state) == 2:
        # Match the state as a standalone token, so "ks" doesn't hit "Kansas
        # City, MO" via a substring or "ks" inside another word.
        if state in haystack.replace(",", " ").split():
            return True
    return False


def headcount(person: dict) -> Optional[int]:
    org = person.get("organization") or {}
    value = org.get("estimated_num_employees")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class PeopleStats:
    searched: int = 0
    candidates: int = 0
    matched: int = 0
    emails: int = 0
    cached: int = 0
    wrong_place: int = 0
    too_small: int = 0
    size_unknown: int = 0
    no_person: int = 0
    errors: int = 0
    cap_hit: bool = False
    cap_unverified: bool = False
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class PeopleClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE,
        governor: Optional[CreditGovernor] = None,
        cache: Optional[PeopleCache] = None,
        min_employees: int = 0,
        timeout: float = 30.0,
        min_delay: float = 0.0,
        verbose: bool = False,
    ):
        if httpx is None:
            raise ApolloPeopleUnavailable("httpx is required for Apollo people enrichment")
        if not api_key:
            raise ApolloPeopleUnavailable(
                "no Apollo API key -- pass --apollo-key or set APOLLO_API_KEY")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.governor = governor or CreditGovernor(cap=10 ** 9)
        self.cache = cache or PeopleCache(None)
        self.min_employees = min_employees
        self.min_delay = min_delay
        self.verbose = verbose
        self.stats = PeopleStats()
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()
        self.cache.save()

    def __enter__(self) -> "PeopleClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[apollo-people] {message}", flush=True)

    def _post(self, path: str, body: dict) -> dict:
        if self.min_delay:
            time.sleep(self.min_delay)
        response = self.client.post(
            self.base_url + path,
            headers={"Content-Type": "application/json", "accept": "application/json",
                     "x-api-key": self.api_key},
            json=body,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Apollo {response.status_code} on {path}: "
                               f"{response.text[:200]}")
        return response.json() or {}

    # ------------------------------------------------------------------
    def read_balance(self) -> Optional[int]:
        """Live credit balance, or None if it can't be read."""
        try:
            response = self.client.get(
                self.base_url + PROFILE,
                headers={"accept": "application/json", "x-api-key": self.api_key},
                params={"include_credit_usage": "true"},
            )
            if response.status_code >= 400:
                return None
            body = response.json() or {}
        except Exception:
            return None
        value = body.get("num_credits_remaining")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def refresh_governor(self) -> None:
        self.governor.observe(self.read_balance())

    # ------------------------------------------------------------------
    def find_person(self, listing: Listing) -> Optional[dict]:
        """The most owner-ish person at this company, or None."""
        if not listing.apollo_org_id:
            return None
        body = {
            "organization_ids": [listing.apollo_org_id],
            "person_titles": OWNER_TITLES,
            "per_page": 5,
            "page": 1,
        }
        found = self._post(PEOPLE_SEARCH, body)
        self.stats.searched += 1
        people = found.get("people") or found.get("contacts") or []
        if not people:
            return None
        self.stats.candidates += len(people)
        # OWNER_TITLES is in descending authority, so prefer the earliest
        # title that appears rather than whatever Apollo happened to rank first.
        def rank(person):
            title = (person.get("title") or "").lower()
            for index, wanted in enumerate(OWNER_TITLES):
                if wanted in title:
                    return index
            return len(OWNER_TITLES)
        return sorted(people, key=rank)[0]

    def match_people(self, people: List[dict]) -> List[dict]:
        """Emails for up to MAX_MATCH_BATCH people. This is the billable step."""
        if not people:
            return []
        self.governor.check()
        details = []
        for person in people:
            entry = {}
            if person.get("id"):
                entry["id"] = person["id"]
            else:
                for key in ("first_name", "last_name", "organization_name"):
                    if person.get(key):
                        entry[key] = person[key]
            if entry:
                details.append(entry)
        if not details:
            return []
        body = self._post(BULK_MATCH, {"details": details[:MAX_MATCH_BATCH]})
        self.refresh_governor()
        return body.get("matches") or body.get("people") or []


def enrich_listings(
    listings: Iterable[Listing],
    client: PeopleClient,
) -> Dict[str, dict]:
    """Owner/email per listing, keyed by the listing's dedupe identity.

    Returns a plain dict rather than writing onto Listing, because owner and
    email are contact columns the scrape does not own -- they belong to the
    enrichment half of the sheet.
    """
    rows: Dict[str, dict] = {}
    listings = list(listings)

    client.refresh_governor()
    if client.governor.starting_balance is None:
        client.stats.cap_unverified = True
        client.stats.notes.append(
            "could not read the credit balance -- the daily cap was not enforced")

    # Stage 1: find a person per company. Free, so do it for everything first.
    candidates = []
    for listing in listings:
        key = listing.dedupe_key() or listing.company_name
        cached = client.cache.get(f"person:{key}")
        if cached is not MISS:
            client.stats.cached += 1
            person = cached
        else:
            try:
                person = client.find_person(listing)
            except Exception as exc:
                client.stats.errors += 1
                client._log(f"search failed for {listing.company_name}: {exc}")
                continue
            client.cache.put(f"person:{key}", person)
        if not person:
            client.stats.no_person += 1
            continue
        candidates.append((key, listing, person))

    # Stage 2: emails, in batches. Billable, so the cap is checked per batch.
    for start in range(0, len(candidates), MAX_MATCH_BATCH):
        batch = candidates[start:start + MAX_MATCH_BATCH]
        try:
            matches = client.match_people([person for _k, _l, person in batch])
        except CreditCapReached as exc:
            client.stats.cap_hit = True
            client.stats.notes.append(str(exc))
            break
        except Exception as exc:
            client.stats.errors += 1
            client._log(f"match failed: {exc}")
            continue

        for (key, listing, _person), match in zip(batch, matches):
            if not match:
                continue
            client.stats.matched += 1

            placed = same_place(listing, match)
            if placed is False:
                # A same-named company in another state. Its email belongs to
                # a stranger, so the row keeps the company but not the contact.
                client.stats.wrong_place += 1
                rows[key] = {"notes": "REVIEW: Apollo match is in a different "
                                      "city/state than the BBB listing"}
                continue

            size = headcount(match)
            if size is None:
                client.stats.size_unknown += 1
            elif client.min_employees and size < client.min_employees:
                client.stats.too_small += 1
                rows[key] = {"apollo_employees": size,
                             "notes": f"dropped: {size} employees"}
                continue

            last = match.get("last_name") or ""
            row = {
                "owner_first_name": match.get("first_name") or "",
                "owner_last_name": "" if is_masked(last) else last,
                "title": match.get("title") or "",
                "email": match.get("email") or "",
                "email_status": match.get("email_status") or "",
                "linkedin_url": match.get("linkedin_url") or "",
                "apollo_employees": size if size is not None else "",
                "notes": "",
            }
            if is_masked(last):
                row["notes"] = "last name masked by Apollo plan"
            if size is None:
                row["notes"] = ("; ".join(n for n in [row["notes"],
                                "headcount unknown -- size filter not applied"] if n))
            if row["email"]:
                client.stats.emails += 1
            rows[key] = row

    return rows


#: A person and a company that do not exist. bulk_match bills per successful
#: match and people search bills nothing for an empty result, so probing with
#: these costs nothing whichever base prefix answers.
PROBE_PERSON = {"first_name": "Zzzqx", "last_name": "Nonexistentperson",
                "organization_name": "Zzzqx Nonexistent Holdings"}
PROBE_ORG_ID = "0" * 24


def probe_paths(api_key: str, bases: Optional[List[str]] = None,
                timeout: float = 20.0) -> List[dict]:
    """Which base prefix serves the three people-side paths.

    Every probe is built to match nothing, so discovery never spends a credit
    regardless of which prefix turns out to be live.
    """
    if httpx is None:
        raise ApolloPeopleUnavailable("httpx is required to probe Apollo")
    if not api_key:
        raise ApolloPeopleUnavailable("no Apollo API key")

    headers = {"Content-Type": "application/json", "accept": "application/json",
               "x-api-key": api_key}
    probes = [
        (PROFILE, "GET", None),
        (PEOPLE_SEARCH, "POST", {"organization_ids": [PROBE_ORG_ID], "per_page": 1}),
        (BULK_MATCH, "POST", {"details": [PROBE_PERSON]}),
    ]

    results = []
    with httpx.Client(timeout=timeout) as client:
        for base in bases or ["https://api.apollo.io/v1", "https://api.apollo.io/api/v1"]:
            for path, method, body in probes:
                row = {"url": base.rstrip("/") + path, "status": None,
                       "ok": False, "error": ""}
                try:
                    if method == "GET":
                        response = client.get(row["url"], headers=headers)
                    else:
                        response = client.post(row["url"], headers=headers, json=body)
                    row["status"] = response.status_code
                    row["ok"] = response.status_code < 400
                except Exception as exc:
                    row["error"] = str(exc)[:120]
                results.append(row)
    return results


def resolve_api_key(explicit: Optional[str]) -> Optional[str]:
    return explicit or os.environ.get("APOLLO_API_KEY")
