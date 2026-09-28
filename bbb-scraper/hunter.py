"""Optional Hunter.io pass: verify the emails we have, find the ones we lack.

Apollo is the only contact source in this pipeline, and `email_status:
verified` on a row is Apollo asserting deliverability, not us confirming it.
This module adds a second, independent opinion, in that order of priority:

  1. VERIFY every email already on the sheet. A dead address costs sender
     reputation, which is expensive to get back, so this runs first and gets
     the budget first.
  2. FILL an email for rows that have a website but no contact -- roughly half
     of every sheet, because Apollo's coverage of trade contractors is thin.

Three rules it exists to enforce:

  * No invented addresses. Hunter returns addresses it has actually seen. A
    pattern-guessed firstname@domain never enters the sheet, from here or
    anywhere else.
  * Every email says where it came from. `email_source` is apollo or hunter,
    never blank when an email is present, so the two are never confused when
    you decide what to send.
  * An undeliverable address is marked, not deleted. The row still carries the
    company; it just sorts below the rows you can actually mail, and the
    reason is written down.

Quota is measured, not assumed: the account endpoint reports what the plan has
left, and the run stops on the smaller of that and the configured cap. On the
free plan (25 searches/month) the cap matters -- two sheets of 15 rows can ask
for 60 lookups in a morning.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - exercised by the unavailable path
    httpx = None

from apollo_people import UNRANKED, is_masked, title_rank
from diskcache import MISS, JsonCache

DEFAULT_BASE = "https://api.hunter.io/v2"
VERIFY_PATH = "/email-verifier"
SEARCH_PATH = "/domain-search"
ACCOUNT_PATH = "/account"

#: Hunter's verdicts, best to worst. "unknown" is not a failure: it usually
#: means the mail server refuses to confirm addresses, which is common on the
#: small hosts these companies use.
DELIVERABLE = "deliverable"
RISKY = "risky"
UNDELIVERABLE = "undeliverable"
UNKNOWN = "unknown"
NOT_CHECKED = "not-checked"

#: An address that verified as undeliverable should not be mailed. Anything
#: else is worth a send, with risky flagged for a human to weigh.
SENDABLE = {DELIVERABLE, RISKY, UNKNOWN, NOT_CHECKED, ""}

#: Verification goes stale as people leave, so it expires. Hunter's terms
#: allow storing results; they do not make them true forever.
DEFAULT_TTL_DAYS = 30

#: Below this, a discovered address is a guess dressed as a finding.
MIN_CONFIDENCE = 70


class HunterUnavailable(RuntimeError):
    """No API key, or httpx missing."""


class QuotaReached(RuntimeError):
    """The configured cap, or the plan's own remaining quota, is spent."""


class HunterCache(JsonCache):
    prefix = ".hunter-"


def resolve_api_key(explicit: Optional[str]) -> Optional[str]:
    return explicit or os.environ.get("HUNTER_API_KEY")


# --------------------------------------------------------------------------
# choosing a person
# --------------------------------------------------------------------------

def pick_contact(emails: List[dict]) -> Optional[dict]:
    """The best owner-ish personal address Hunter holds for a domain.

    Ranked by the same title table the Apollo match uses, so "Owner" beats
    "General Manager" beats "VP of Sales" here too. A generic mailbox
    (info@, office@) is only ever used when nothing personal exists, and it
    comes back without a person's name attached -- writing a name next to a
    shared mailbox is how a sequence greets the wrong human.
    """
    personal, generic = [], []
    for entry in emails or []:
        if not (entry.get("value") or "").strip():
            continue
        confidence = entry.get("confidence")
        if isinstance(confidence, (int, float)) and confidence < MIN_CONFIDENCE:
            continue
        if (entry.get("type") or "").lower() == "generic":
            generic.append(entry)
        else:
            personal.append(entry)

    if personal:
        def sort_key(entry):
            return (title_rank(entry.get("position") or ""),
                    -(entry.get("confidence") or 0))
        best = sorted(personal, key=sort_key)[0]
        last = best.get("last_name") or ""
        return {
            "email": best["value"].strip(),
            "first_name": best.get("first_name") or "",
            "last_name": "" if is_masked(last) else last,
            "title": best.get("position") or "",
            "confidence": best.get("confidence"),
            "generic": False,
            # An unranked title is not an owner. The email is still real, so it
            # is kept, but the sheet should not imply we found a decision maker.
            "unranked_title": title_rank(best.get("position") or "") == UNRANKED,
        }
    if generic:
        best = sorted(generic, key=lambda e: -(e.get("confidence") or 0))[0]
        return {
            "email": best["value"].strip(),
            "first_name": "",
            "last_name": "",
            "title": "",
            "confidence": best.get("confidence"),
            "generic": True,
            "unranked_title": True,
        }
    return None


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------

@dataclass
class HunterStats:
    verified: int = 0
    deliverable: int = 0
    risky: int = 0
    undeliverable: int = 0
    unknown: int = 0
    searched: int = 0
    found: int = 0
    found_generic: int = 0
    no_result: int = 0
    cached: int = 0
    errors: int = 0
    capped: int = 0
    quota_unverified: bool = False
    cap_hit: bool = False
    notes: List[str] = field(default_factory=list)


class HunterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE,
        cache: Optional[HunterCache] = None,
        cap: int = 60,
        timeout: float = 20.0,
        min_delay: float = 0.0,
        verbose: bool = False,
    ):
        if httpx is None:
            raise HunterUnavailable("httpx is required for Hunter enrichment")
        if not api_key:
            raise HunterUnavailable(
                "no Hunter API key -- set HUNTER_API_KEY or rotation.json hunter_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.cache = cache or HunterCache(None)
        self.cap = cap
        self.spent = 0
        self.min_delay = min_delay
        self.verbose = verbose
        self.stats = HunterStats()
        self.client = httpx.Client(timeout=timeout)
        self.remaining = self._read_quota()

    # ------------------------------------------------------------------
    def close(self) -> None:
        self.client.close()
        self.cache.save()

    def __enter__(self) -> "HunterClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[hunter] {message}", flush=True)

    # ------------------------------------------------------------------
    def _get(self, path: str, params: dict) -> dict:
        if self.min_delay:
            time.sleep(self.min_delay)
        response = self.client.get(f"{self.base_url}{path}",
                                   params=dict(params, api_key=self.api_key))
        body = {}
        try:
            body = response.json() or {}
        except ValueError:
            pass
        if response.status_code >= 400:
            errors = body.get("errors") or []
            detail = (errors[0].get("details") if errors else "") or response.text[:200]
            raise RuntimeError(f"Hunter {response.status_code}: {detail}")
        return body.get("data") or {}

    def _read_quota(self) -> Optional[int]:
        """What the plan has left, or None when it cannot be read.

        None means unknown, and unknown fails open -- refusing to run because a
        reporting endpoint hiccuped would cost a whole morning. The caller is
        told, and the run summary says the quota was not verified.
        """
        try:
            data = self._get(ACCOUNT_PATH, {})
        except Exception as exc:
            self._log(f"could not read the account quota ({exc}) -- "
                      f"the plan's own limit will NOT be enforced")
            self.stats.quota_unverified = True
            return None
        requests = data.get("requests") or {}
        left = []
        for name in ("searches", "verifications"):
            bucket = requests.get(name) or {}
            available, used = bucket.get("available"), bucket.get("used")
            if isinstance(available, (int, float)) and isinstance(used, (int, float)):
                left.append(int(available) - int(used))
        if not left:
            self.stats.quota_unverified = True
            return None
        return max(min(left), 0)

    def _spend(self) -> None:
        """Called before each billable call. Fails closed on a known quota."""
        if self.spent >= self.cap:
            self.stats.cap_hit = True
            raise QuotaReached(f"hunter cap of {self.cap} lookups reached")
        if self.remaining is not None and self.spent >= self.remaining:
            self.stats.cap_hit = True
            raise QuotaReached(
                f"the Hunter plan has {self.remaining} lookup(s) left this "
                f"period and they are spent")
        self.spent += 1

    # ------------------------------------------------------------------
    def verify(self, email: str) -> dict:
        """Hunter's verdict on one address: {status, score}."""
        email = (email or "").strip()
        if not email:
            return {"status": NOT_CHECKED, "score": None}

        key = f"verify|{email.lower()}"
        cached = self.cache.get(key)
        if cached is not MISS:
            self.stats.cached += 1
            return cached or {"status": UNKNOWN, "score": None}

        self._spend()
        try:
            data = self._get(VERIFY_PATH, {"email": email})
        except QuotaReached:
            raise
        except Exception as exc:
            self.stats.errors += 1
            self.stats.notes.append(f"verify {email}: {exc}")
            return {"status": NOT_CHECKED, "score": None}

        status = (data.get("status") or data.get("result") or UNKNOWN).lower()
        result = {"status": status, "score": data.get("score")}
        self.cache.put(key, result)
        self.stats.verified += 1
        counter = {DELIVERABLE: "deliverable", RISKY: "risky",
                   UNDELIVERABLE: "undeliverable"}.get(status, "unknown")
        setattr(self.stats, counter, getattr(self.stats, counter) + 1)
        return result

    def find_contact(self, domain: str) -> Optional[dict]:
        """The best owner-ish address Hunter has for a domain, or None."""
        domain = (domain or "").strip().lower()
        if not domain:
            return None

        key = f"domain|{domain}"
        cached = self.cache.get(key)
        if cached is not MISS:
            self.stats.cached += 1
            return cached

        self._spend()
        try:
            data = self._get(SEARCH_PATH, {"domain": domain, "limit": 10})
        except QuotaReached:
            raise
        except Exception as exc:
            self.stats.errors += 1
            self.stats.notes.append(f"domain-search {domain}: {exc}")
            return None

        contact = pick_contact(data.get("emails") or [])
        self.cache.put(key, contact)
        self.stats.searched += 1
        if contact:
            self.stats.found += 1
            if contact.get("generic"):
                self.stats.found_generic += 1
        else:
            self.stats.no_result += 1
        return contact


# --------------------------------------------------------------------------
# the pass over a sheet
# --------------------------------------------------------------------------

#: Columns this pass owns. They are appended to the sheet, so an older export
#: stays readable.
COLUMNS = ("email_source", "email_check", "email_score")


def domain_of(row: dict) -> str:
    """The row's website, as a bare domain."""
    from parse import normalize_domain
    return normalize_domain(row.get("website") or "")


def enrich_rows(rows: List[dict], client: HunterClient) -> None:
    """Verify the emails present, then fill the ones missing. In place.

    Verification goes first on purpose: protecting the addresses you are about
    to mail matters more than finding one more, and on a small plan the budget
    runs out before both are done.
    """
    for row in rows:
        row.setdefault("email_source", "apollo" if row.get("email") else "")
        row.setdefault("email_check", "")
        row.setdefault("email_score", "")

    with_email = [row for row in rows if (row.get("email") or "").strip()]
    for row in with_email:
        try:
            verdict = client.verify(row["email"])
        except QuotaReached as exc:
            client.stats.capped += 1
            _note(row, str(exc))
            continue
        row["email_check"] = verdict.get("status") or UNKNOWN
        score = verdict.get("score")
        row["email_score"] = "" if score is None else score
        if row["email_check"] == UNDELIVERABLE:
            # Kept, not deleted: the company is still a target, and a row that
            # vanishes teaches you nothing. It just must not be mailed.
            _note(row, "Hunter says this address is undeliverable -- do not send")
        elif row["email_check"] == RISKY:
            _note(row, "Hunter rates this address risky -- verify before sending")

    for row in rows:
        if (row.get("email") or "").strip():
            continue
        domain = domain_of(row)
        if not domain:
            continue
        try:
            contact = client.find_contact(domain)
        except QuotaReached as exc:
            client.stats.capped += 1
            _note(row, str(exc))
            continue
        if not contact:
            continue
        row["email"] = contact["email"]
        row["email_source"] = "hunter"
        row["email_status"] = "hunter-found"
        row["email_check"] = NOT_CHECKED
        row["email_score"] = contact.get("confidence") or ""
        if contact.get("generic"):
            _note(row, "shared mailbox found by Hunter, not a named owner")
        else:
            if not (row.get("owner_first_name") or "").strip():
                row["owner_first_name"] = contact["first_name"]
                row["owner_last_name"] = contact["last_name"]
                row["title"] = contact["title"]
            if contact.get("unranked_title"):
                _note(row, "Hunter's contact is not an owner-level title -- "
                           "check who this is before sending")


def _note(row: dict, message: str) -> None:
    existing = (row.get("notes") or "").strip()
    row["notes"] = f"{existing}; {message}" if existing else message


def summary(stats: HunterStats, spent: int) -> dict:
    return {
        "verified": stats.verified,
        "deliverable": stats.deliverable,
        "risky": stats.risky,
        "undeliverable": stats.undeliverable,
        "unknown": stats.unknown,
        "searched": stats.searched,
        "found": stats.found,
        "found_generic": stats.found_generic,
        "no_result": stats.no_result,
        "cached": stats.cached,
        "errors": stats.errors,
        "lookups_spent": spent,
        "cap_hit": stats.cap_hit,
        "quota_unverified": stats.quota_unverified,
        "notes": stats.notes,
    }
