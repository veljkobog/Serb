"""Check a lead list against HubSpot before anyone emails it.

The dedupe question is not "is this email already in the CRM". It is "should we
contact this company at all", which is four questions wearing one coat:

  1. Is this person suppressed?  Unsubscribed, quarantined, or bouncing. Mailing
     them is a compliance problem and burns the sending domain for every other
     campaign.
  2. Have we already touched them?  A cold open to someone a rep emailed last
     week reads as chaos.
  3. Is there an open deal?  Prospecting into a live opportunity is worse than
     doing nothing.
  4. Do we already hold this company under a different key?  Matching on email
     alone misses a CRM record with a blank or different domain -- which for
     local trades businesses is most of them.

Matching runs strongest-key-first: email, then normalized phone, then company
domain, then company name. A name match is never treated as proof; it is
flagged for review, because "Brown's Plumbing" is not a unique string.

Usage:
    export HUBSPOT_TOKEN=pat-na1-...
    python crm_check.py leads.csv --output leads-checked.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

import parse

HUBSPOT_BASE = "https://api.hubapi.com"

# Everything that means "do not email this address".
SUPPRESSION_PROPERTIES = [
    "hs_email_optout",             # unsubscribed from all email
    "hs_email_quarantined",        # blocked for anti-abuse; sends fail
    "hs_email_bounce",             # bounce count on this address
    "hs_emailconfirmationstatus",  # marketing eligibility
]

# Everything that means "someone here already has a relationship".
ENGAGEMENT_PROPERTIES = [
    "notes_last_contacted",
    "num_contacted_notes",
    "hs_lead_status",
    "lifecyclestage",
    "hubspot_owner_id",
    "hs_is_unworked",
]

CONTACT_PROPERTIES = ["email", "firstname", "lastname", "company", "phone"] + \
    SUPPRESSION_PROPERTIES + ENGAGEMENT_PROPERTIES
COMPANY_PROPERTIES = ["name", "domain", "phone", "city", "state", "lifecyclestage",
                      "hubspot_owner_id", "num_associated_deals", "notes_last_contacted"]

SEND = "send"
SKIP_SUPPRESSED = "skip-suppressed"
SKIP_EXISTING = "skip-existing"
REVIEW = "review"


class HubSpotUnavailable(RuntimeError):
    pass


@dataclass
class Verdict:
    status: str = SEND
    reasons: List[str] = field(default_factory=list)
    matched_on: str = ""
    matched_id: str = ""
    owner_id: str = ""
    last_contacted: str = ""
    open_deals: str = ""

    def as_row(self) -> dict:
        return {
            "crm_verdict": self.status,
            "crm_reason": "; ".join(self.reasons),
            "crm_matched_on": self.matched_on,
            "crm_record_id": self.matched_id,
            "crm_owner_id": self.owner_id,
            "crm_last_contacted": self.last_contacted,
            "crm_open_deals": self.open_deals,
        }


#: HubSpot enforces a per-SECOND cap on the search API, separate from the
#: daily one. Four lookups per lead across fifteen leads arrives as a burst and
#: trips it immediately, so requests are paced rather than fired.
MIN_INTERVAL = 0.12          # ~8/sec, comfortably inside the documented cap
MAX_RETRIES = 5


class HubSpotClient:
    def __init__(self, token: str, base_url: str = HUBSPOT_BASE, timeout: float = 30.0,
                 min_interval: float = MIN_INTERVAL, max_retries: int = MAX_RETRIES):
        if httpx is None:
            raise HubSpotUnavailable("httpx is required (pip install httpx)")
        if not token:
            raise HubSpotUnavailable("no HubSpot token -- set HUBSPOT_TOKEN or pass --hubspot-token")
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.rate_limited = 0
        self._last_call = 0.0
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def _post(self, path: str, payload: dict, what: str) -> dict:
        """POST with pacing and backoff on 429.

        A rate limit is a "come back shortly", not a verdict. Treating it as a
        failure would abandon the CRM check partway and leave rows unchecked
        while the sheet still looked complete.
        """
        for attempt in range(self.max_retries):
            self._pace()
            response = self.client.post(f"{self.base_url}{path}", json=payload)
            if response.status_code == 401:
                raise HubSpotUnavailable("HubSpot rejected the token (401)")
            if response.status_code == 429:
                self.rate_limited += 1
                # Honour Retry-After when HubSpot sends one; otherwise back off.
                wait = response.headers.get("Retry-After")
                delay = float(wait) if wait and wait.replace(".", "").isdigit() \
                    else min(2 ** attempt, 10)
                if attempt == self.max_retries - 1:
                    raise HubSpotUnavailable(
                        f"HubSpot rate limit persisted through {self.max_retries} "
                        f"retries on {what} -- rows were NOT all checked")
                time.sleep(delay)
                continue
            if response.status_code >= 400:
                raise HubSpotUnavailable(
                    f"HubSpot {what} failed: {response.status_code} "
                    f"{response.text[:200]}")
            return response.json()
        raise HubSpotUnavailable(f"HubSpot {what}: retries exhausted")

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "HubSpotClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    def search(self, object_type: str, prop: str, operator: str, value,
               properties: List[str], limit: int = 10) -> dict:
        payload = {
            "filterGroups": [{"filters": [
                dict({"propertyName": prop, "operator": operator},
                     **({"values": value} if operator == "IN" else {"value": value}))
            ]}],
            "properties": properties,
            "limit": limit,
        }
        return self._post(f"/crm/v3/objects/{object_type}/search", payload,
                          f"{object_type} search")

    def portal_total(self, object_type: str = "contacts") -> int:
        """How many records exist at all.

        Guards the failure this whole module exists to prevent: an empty or
        wrong portal answers every lookup with zero, which is indistinguishable
        from a clean list and reads as an all-clear.
        """
        body = self._post(f"/crm/v3/objects/{object_type}/search",
                          {"filterGroups": [], "properties": ["hs_object_id"],
                           "limit": 1},
                          "portal check")
        return int(body.get("total") or 0)


# --------------------------------------------------------------------------
# verdict logic
# --------------------------------------------------------------------------

def _truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def suppression_reasons(props: dict) -> List[str]:
    reasons = []
    if _truthy(props.get("hs_email_optout")):
        reasons.append("unsubscribed from all email")
    if _truthy(props.get("hs_email_quarantined")):
        reasons.append("email address quarantined")
    try:
        bounces = int(props.get("hs_email_bounce") or 0)
    except (TypeError, ValueError):
        bounces = 0
    if bounces > 0:
        reasons.append(f"{bounces} previous bounce(s)")
    status = (props.get("hs_emailconfirmationstatus") or "").strip().lower()
    if status and status not in {"confirmed", "not_confirmed", ""}:
        reasons.append(f"marketing status: {status}")
    return reasons


def engagement_reasons(props: dict) -> List[str]:
    reasons = []
    if props.get("notes_last_contacted"):
        reasons.append(f"last contacted {str(props['notes_last_contacted'])[:10]}")
    try:
        touches = int(props.get("num_contacted_notes") or 0)
    except (TypeError, ValueError):
        touches = 0
    if touches:
        reasons.append(f"{touches} logged touch(es)")
    stage = (props.get("lifecyclestage") or "").strip().lower()
    if stage in {"customer", "opportunity", "salesqualifiedlead"}:
        reasons.append(f"lifecycle stage: {stage}")
    lead_status = (props.get("hs_lead_status") or "").strip()
    if lead_status:
        reasons.append(f"lead status: {lead_status}")
    return reasons


def check_lead(client: HubSpotClient, lead: dict) -> Verdict:
    """Decide whether this lead is safe to contact, strongest key first."""
    verdict = Verdict()

    email = (lead.get("email") or "").strip().lower()
    phone = parse.normalize_phone(lead.get("phone"))
    domain = parse.normalize_domain(lead.get("website"))
    company = (lead.get("company_name") or "").strip()

    # 1. Email -- the only exact person-level key.
    if email:
        hits = client.search("contacts", "email", "EQ", email, CONTACT_PROPERTIES).get("results", [])
        if hits:
            return _contact_verdict(hits[0], "email")

    # 2. Phone. Local businesses reuse one line, so this identifies the company
    #    reliably even when the CRM has no domain. Toll-free numbers are shared
    #    across unrelated franchisees and are never identity.
    if phone and not parse.is_toll_free(phone):
        for prop in ("phone", "mobilephone"):
            hits = client.search("contacts", prop, "EQ", phone,
                                 CONTACT_PROPERTIES).get("results", [])
            if hits:
                return _contact_verdict(hits[0], f"contact {prop}")

    # 3. Company domain.
    if domain:
        hits = client.search("companies", "domain", "EQ", domain,
                             COMPANY_PROPERTIES).get("results", [])
        if hits:
            return _company_verdict(hits[0], "company domain")

    # 4. Company name -- fuzzy, so never conclusive.
    if company:
        hits = client.search("companies", "name", "EQ", company,
                             COMPANY_PROPERTIES).get("results", [])
        if hits:
            found = _company_verdict(hits[0], "company name")
            found.status = REVIEW
            found.reasons.insert(0, "name-only match, verify before skipping")
            return found

    verdict.reasons.append("no CRM record found on email, phone, domain or name")
    return verdict


def _contact_verdict(record: dict, matched_on: str) -> Verdict:
    props = record.get("properties", {}) or {}
    verdict = Verdict(matched_on=matched_on, matched_id=str(record.get("id") or ""))
    verdict.owner_id = str(props.get("hubspot_owner_id") or "")
    verdict.last_contacted = str(props.get("notes_last_contacted") or "")[:10]

    suppressed = suppression_reasons(props)
    if suppressed:
        verdict.status = SKIP_SUPPRESSED
        verdict.reasons = suppressed
        return verdict

    verdict.status = SKIP_EXISTING
    verdict.reasons = ["already in CRM as a contact"] + engagement_reasons(props)
    return verdict


def _company_verdict(record: dict, matched_on: str) -> Verdict:
    props = record.get("properties", {}) or {}
    verdict = Verdict(status=SKIP_EXISTING, matched_on=matched_on,
                      matched_id=str(record.get("id") or ""))
    verdict.owner_id = str(props.get("hubspot_owner_id") or "")
    verdict.last_contacted = str(props.get("notes_last_contacted") or "")[:10]
    verdict.open_deals = str(props.get("num_associated_deals") or "")

    verdict.reasons = ["company already in CRM"] + engagement_reasons(props)
    try:
        if int(props.get("num_associated_deals") or 0) > 0:
            verdict.reasons.insert(0, f"{props['num_associated_deals']} associated deal(s)")
    except (TypeError, ValueError):
        pass
    return verdict


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def run(args) -> int:
    token = args.hubspot_token or os.environ.get("HUBSPOT_TOKEN")
    try:
        client = HubSpotClient(token, base_url=args.base_url)
    except HubSpotUnavailable as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    try:
        total = client.portal_total()
        if total == 0:
            print("HubSpot returned zero records for the whole portal. Refusing to report "
                  "'no duplicates' from an empty or wrong portal -- check the token.",
                  file=sys.stderr)
            return 1
        print(f"[crm] portal has {total:,} contacts")

        with open(args.input, newline="", encoding="utf-8") as fh:
            leads = list(csv.DictReader(fh))
        if not leads:
            print("no rows in the input file", file=sys.stderr)
            return 1

        counts: Dict[str, int] = {}
        rows = []
        for lead in leads:
            verdict = check_lead(client, lead)
            counts[verdict.status] = counts.get(verdict.status, 0) + 1
            rows.append(dict(lead, **verdict.as_row()))
            if args.verbose:
                print(f"[crm] {lead.get('company_name','?')[:34]:<36} {verdict.status}")

        fieldnames = list(rows[0].keys())
        with open(args.output, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        print("\ncrm check")
        print("---------")
        for status in (SEND, REVIEW, SKIP_EXISTING, SKIP_SUPPRESSED):
            print(f"  {status:<16}: {counts.get(status, 0)}")
        print(f"  written         : {len(rows)} -> {args.output}")
        if counts.get(SKIP_SUPPRESSED):
            print(f"  (!) {counts[SKIP_SUPPRESSED]} suppressed -- do not mail these")
        return 0
    except HubSpotUnavailable as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    finally:
        client.close()


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="crm_check.py",
        description="Check a lead CSV against HubSpot for suppression, prior contact and duplicates.")
    p.add_argument("input", help="lead CSV (needs company_name; uses email/phone/website when present)")
    p.add_argument("--output", default=None, help="output CSV (default: <input>-checked.csv)")
    p.add_argument("--hubspot-token", default=None, help="private-app token, or set $HUBSPOT_TOKEN")
    p.add_argument("--base-url", default=HUBSPOT_BASE, help="API root (for a proxy or a test double)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.output:
        base, ext = os.path.splitext(args.input)
        args.output = f"{base}-checked{ext or '.csv'}"
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
