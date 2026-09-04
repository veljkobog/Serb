"""Field extraction + normalization.

Everything that turns a raw record (JSON dict from the search API, or a DOM
card from Playwright) into a clean `Listing` lives here, so the two clients
stay thin.

BBB's JSON schema is not documented and changes without notice, so extraction
is key-path tolerant: we try a list of candidate keys per field rather than
assuming one shape.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date
from typing import Any, Iterable, Iterator, Optional

# CSV column order, per spec.
# A Cloudflare interstitial is a 200 with a full page of markup, which is
# otherwise indistinguishable from "this city has no plumbers" or "the field
# names moved". Both the HTTP and the browser path must check for it, so the
# check lives here rather than in either client.
CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript and cookies",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "attention required!",
)


class BlockedError(RuntimeError):
    """The site served a block or a challenge instead of content.

    Lives here rather than in the HTTP client so the browser path can raise it
    without importing httpx.
    """


def looks_challenged(html: str) -> bool:
    head = (html or "")[:4000].lower()
    return any(marker in head for marker in CHALLENGE_MARKERS)


FIELD_ORDER = [
    "company_name",
    "website",
    "phone",
    "street",
    "city",
    "state",
    "zip",
    "category",
    "years_in_business",
    "accredited",
    "bbb_rating",
    "profile_url",
    "social_url",
    # size / traction signals, appended after the spec's columns
    "bbb_reviews",
    "bbb_complaints",
    "employees",
    "google_rating",
    "google_reviews",
    "google_place_id",
    "google_match",
    # Apollo lookup: recovers the website BBB's 403'd profile page withholds,
    # and carries the org id forward so the people-match can scope to it.
    "apollo_org_id",
    "apollo_match",
]

# Fields that a search card rarely carries -- filtering on any of these means
# the run has to visit detail pages to answer honestly.
DETAIL_FIELDS = {"years_in_business", "accredited", "bbb_reviews", "bbb_complaints", "employees"}

VALID_RATINGS = {
    "A+", "A", "A-",
    "B+", "B", "B-",
    "C+", "C", "C-",
    "D+", "D", "D-",
    "F",
}


@dataclass
class Listing:
    company_name: str = ""
    website: str = ""
    phone: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    category: str = ""
    years_in_business: Optional[int] = None
    accredited: Optional[bool] = None
    bbb_rating: str = ""
    profile_url: str = ""
    social_url: str = ""
    bbb_reviews: Optional[int] = None
    bbb_complaints: Optional[int] = None
    employees: Optional[int] = None
    google_rating: Optional[float] = None
    google_reviews: Optional[int] = None
    google_place_id: str = ""
    google_match: str = ""
    apollo_org_id: str = ""
    apollo_match: str = ""

    def dedupe_key(self) -> Optional[str]:
        """Normalized website, falling back to phone. None if neither is known.

        A toll-free number is never used as identity: franchisees, answering
        services and marketing agencies share them, so two unrelated shops can
        carry the same 800 number. Collapsing them would drop a real lead
        permanently, while letting a genuine duplicate through only costs one
        row that the downstream HubSpot dedupe catches anyway.
        """
        if self.website:
            return f"web:{self.website}"
        if self.phone and not is_toll_free(self.phone):
            return f"tel:{self.phone}"
        return None

    def is_low_confidence(self) -> bool:
        """Missing website AND phone -> goes to the _lowconfidence file."""
        return not self.website and not self.phone

    def needs_detail(self, required=None) -> bool:
        """True when only a detail-page visit can fill the fields we need.

        `required` defaults to the two fields the spec calls out; a run that
        filters on review or headcount counts passes those in too.
        """
        required = required or {"years_in_business", "accredited"}
        return any(getattr(self, name) is None for name in required)

    def merge(self, other: "Listing") -> None:
        """Fill blanks from `other` (a detail-page parse). Never overwrite."""
        for f in dataclass_fields(self):
            cur = getattr(self, f.name)
            new = getattr(other, f.name)
            if new in (None, "") or cur not in (None, ""):
                continue
            setattr(self, f.name, new)

    def as_row(self) -> dict:
        return {
            "company_name": self.company_name,
            "website": self.website,
            "phone": self.phone,
            "street": self.street,
            "city": self.city,
            "state": self.state,
            "zip": self.zip,
            "category": self.category,
            # blank, not 0, when unknown -- never guess.
            "years_in_business": "" if self.years_in_business is None else self.years_in_business,
            "accredited": "" if self.accredited is None else str(self.accredited).lower(),
            "bbb_rating": self.bbb_rating,
            "profile_url": self.profile_url,
            "social_url": self.social_url,
            "bbb_reviews": _blank_if_none(self.bbb_reviews),
            "bbb_complaints": _blank_if_none(self.bbb_complaints),
            "employees": _blank_if_none(self.employees),
            "google_rating": _blank_if_none(self.google_rating),
            "google_reviews": _blank_if_none(self.google_reviews),
            "google_place_id": self.google_place_id,
            "google_match": self.google_match,
            # Added to FIELD_ORDER without being added here, which wrote
            # them as blanks for every row and crashed the column-map
            # path outright. as_row and FIELD_ORDER must agree.
            "apollo_org_id": self.apollo_org_id,
            "apollo_match": self.apollo_match,
        }


# --------------------------------------------------------------------------
# normalizers
# --------------------------------------------------------------------------

_TRACKING_HOSTS = {"bbb.org", "www.bbb.org"}

# Hosts where the *path* identifies the business, not the domain. A shop whose
# only web presence is facebook.com/acme-plumbing must not be recorded as
# owning "facebook.com": every such listing would share one dedupe key and all
# but the first would be dropped as duplicates. They are also useless to
# domain-based enrichment downstream, so they are kept separately instead.
NON_COMPANY_HOSTS = {
    "facebook.com", "m.facebook.com", "business.facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "tiktok.com", "pinterest.com", "nextdoor.com",
    "yelp.com", "angi.com", "angieslist.com", "homeadvisor.com", "thumbtack.com",
    "porch.com", "houzz.com", "manta.com", "alignable.com", "bark.com",
    "yellowpages.com", "superpages.com", "mapquest.com", "foursquare.com",
    "sites.google.com", "google.com", "maps.google.com", "goo.gl",
    "linktr.ee", "bit.ly", "wa.me",
}


def is_company_domain(domain: str) -> bool:
    """False for social profiles, directories and link shorteners."""
    if not domain:
        return False
    return domain not in NON_COMPANY_HOSTS


def classify_website(value: Any) -> tuple:
    """Split a listed URL into (company_domain, social_or_directory_url).

    Exactly one of the two is populated. A social/directory link is still worth
    keeping -- it's often the only web presence a founder-led shop has -- it
    just isn't a domain.
    """
    domain = normalize_domain(value)
    if not domain:
        return "", ""
    if is_company_domain(domain):
        return domain, ""
    return "", _clean_url(value)


def _clean_url(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    url = value.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")
    return url


def normalize_domain(value: Any) -> str:
    """Domain only: strip scheme, www., path, query, port. Lowercased.

    Returns "" for anything that isn't a usable third-party domain (BBB's own
    links, mailto:, bare paths, obvious junk).
    """
    if not value or not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw or raw.lower() in {"n/a", "none", "null", "-"}:
        return ""
    if raw.lower().startswith(("mailto:", "tel:")):
        return ""
    raw = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)
    raw = raw.split("/")[0].split("?")[0].split("#")[0]
    raw = raw.split("@")[-1]          # strip any user:pass@
    raw = raw.split(":")[0]           # strip port
    raw = raw.strip().strip(".").lower()
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or "." not in raw or " " in raw:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", raw):
        return ""
    if raw in _TRACKING_HOSTS or raw.endswith(".bbb.org"):
        return ""
    return raw


def normalize_phone(value: Any) -> str:
    """US numbers to E.164 (+1XXXXXXXXXX). Anything else -> ""."""
    if value in (None, ""):
        return ""
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    if digits[0] in "01" or digits[3] in "01":  # invalid NANP area/exchange
        return ""
    return "+1" + digits


# Shared across franchisees and answering services, so not an identity.
TOLL_FREE_AREA_CODES = {"800", "833", "844", "855", "866", "877", "888"}


def is_toll_free(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return len(digits) == 10 and digits[:3] in TOLL_FREE_AREA_CODES


def normalize_state(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    v = value.strip()
    return v.upper() if len(v) == 2 else v


def normalize_zip(value: Any) -> str:
    if value in (None, ""):
        return ""
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", str(value))
    return m.group(1) if m else ""


def normalize_rating(value: Any) -> str:
    """A+ .. F. Blank for NR / unrated / anything unrecognized."""
    if not value or not isinstance(value, str):
        return ""
    v = value.strip().upper().replace(" ", "")
    if v in {"NR", "N/R", "NOTRATED", "NONE"}:
        return ""
    return v if v in VALID_RATINGS else ""


def _blank_if_none(value):
    return "" if value is None else value


def parse_count(value: Any) -> Optional[int]:
    """A non-negative count, or None. Never coerces "unknown" into 0.

    Handles the shapes these arrive in: 12, "12", "12 reviews", "1,204",
    "1.2K customer reviews".
    """
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 else None

    text = str(value).strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", text)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    elif "." in match.group(1):
        return None          # a bare decimal is a rating, not a count
    return int(number)


def parse_rating_value(value: Any, maximum: float = 5.0) -> Optional[float]:
    """A star rating in 0..maximum, or None."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().split()[0])
    except (ValueError, IndexError):
        return None
    if 0 <= number <= maximum:
        return round(number, 2)
    return None


def parse_employees(value: Any) -> Optional[int]:
    """Headcount as an int. Ranges ("11-50") take the low end -- the
    conservative read, so a size filter never passes a shop on optimism."""
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value).strip().replace(",", "")
    match = re.search(r"(\d+)\s*(?:-|to|–)\s*(\d+)", text)
    if match:
        return int(match.group(1))
    return parse_count(text)


def parse_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    v = str(value).strip().lower()
    if v in {"true", "yes", "y", "1", "accredited"}:
        return True
    if v in {"false", "no", "n", "0", "not accredited", "non-accredited"}:
        return False
    return None


def parse_years_in_business(value: Any, today: Optional[date] = None) -> Optional[int]:
    """Years in business as an int, or None.

    Accepts an explicit count ("25", "In business: 25 years") or an explicit
    start date/year ("3/1/1998", "1998-03-01", "Business Started: 1998"), which
    is arithmetic on a stated fact -- not a guess. Anything ambiguous is None.
    """
    if value in (None, ""):
        return None
    today = today or date.today()

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        return n if 0 <= n <= 200 else None

    text = str(value).strip()
    if not text:
        return None

    # explicit "N years"
    m = re.search(r"(\d{1,3})\s*\+?\s*(?:years?|yrs?)\b", text, re.I)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= 200 else None

    # ISO-ish or US-style start date
    m = re.search(r"\b(19|20)\d{2}-\d{1,2}-\d{1,2}\b", text)
    if m:
        year = int(m.group(0)[:4])
        return _years_since(year, today)
    m = re.search(r"\b\d{1,2}/\d{1,2}/((?:19|20)\d{2})\b", text)
    if m:
        return _years_since(int(m.group(1)), today)

    # bare count, or bare year
    if re.fullmatch(r"\d{1,3}", text):
        n = int(text)
        return n if 0 <= n <= 200 else None
    if re.fullmatch(r"(19|20)\d{2}", text):
        return _years_since(int(text), today)

    return None


def _years_since(year: int, today: date) -> Optional[int]:
    if year < 1800 or year > today.year:
        return None
    return today.year - year


# --------------------------------------------------------------------------
# key-path tolerant lookup
# --------------------------------------------------------------------------

def _get(record: Any, *paths: str) -> Any:
    """First non-empty value among dotted paths. `a.b` and `a.0.b` both work.

    Key matching is case/underscore-insensitive so `businessName`,
    `business_name` and `BusinessName` all hit the same path.
    """
    for path in paths:
        val = _dig(record, path.split("."))
        if val not in (None, "", [], {}):
            return val
    return None


def _dig(node: Any, parts: list) -> Any:
    for part in parts:
        if node is None:
            return None
        if isinstance(node, list):
            if part.isdigit():
                idx = int(part)
                node = node[idx] if idx < len(node) else None
                continue
            node = node[0] if node else None
            if node is None:
                return None
        if not isinstance(node, dict):
            return None
        node = _lookup_key(node, part)
    return node


def _lookup_key(d: dict, key: str) -> Any:
    if key in d:
        return d[key]
    want = key.replace("_", "").lower()
    for k, v in d.items():
        if isinstance(k, str) and k.replace("_", "").lower() == want:
            return v
    return None


NAME_KEYS = ("businessName", "companyName", "name", "displayName", "title", "legalName",
             "organizationName", "orgName", "tradeName", "dbaName", "businessTitle")
WEBSITE_KEYS = ("websiteUrl", "website", "webAddress", "primaryWebsite", "homepage", "businessUrl", "url")
PHONE_KEYS = ("phone", "phoneNumber", "primaryPhone", "telephone", "phones.0.number", "phones.0", "phone.0")
STREET_KEYS = ("address.street", "address.address1", "address.addressLine1", "street", "address1",
               "addressLine1", "streetAddress", "address.streetAddress")
CITY_KEYS = ("address.city", "city", "addressLocality", "address.addressLocality")
STATE_KEYS = ("address.state", "state", "stateProvince", "addressRegion", "address.stateProvince",
              "address.addressRegion")
ZIP_KEYS = ("address.postalCode", "postalCode", "zip", "zipCode", "address.zip", "address.zipCode")
CATEGORY_KEYS = ("primaryCategory", "category", "categories.0.name", "categories.0", "businessCategory",
                 "primaryCategory.name", "businessType")
YEARS_KEYS = ("yearsInBusiness", "years_in_business", "businessStartedDate", "dateStarted", "startDate",
              "yearEstablished", "businessStarted", "dateBusinessStarted")
ACCREDITED_KEYS = ("isAccredited", "accredited", "isBBBAccredited", "accreditedBusiness", "isAccreditedBusiness")
RATING_KEYS = ("bbbRating", "rating", "letterGrade", "ratingLetter", "grade", "rating.letter")
REVIEW_COUNT_KEYS = ("customerReviewCount", "reviewCount", "numberOfReviews", "totalReviews",
                     "reviews.count", "customerReviews.count", "reviewSummary.count",
                     "customerReviewStats.totalCount")
COMPLAINT_KEYS = ("complaintCount", "numberOfComplaints", "totalComplaints", "complaints.count",
                  "complaintSummary.count", "complaintStats.totalCount")
EMPLOYEE_KEYS = ("numberOfEmployees", "employeeCount", "employees", "numEmployees", "staffSize",
                 "businessDetails.numberOfEmployees")
PROFILE_KEYS = ("reportUrl", "profileUrl", "businessProfileUrl", "bbbProfileUrl", "reportURL", "url", "path", "slug")

BBB_BASE = "https://www.bbb.org"


def _absolutize(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""
    if v.startswith("http://") or v.startswith("https://"):
        return v
    if v.startswith("//"):
        return "https:" + v
    if v.startswith("/"):
        return BBB_BASE + v
    return ""


def _is_bbb_url(value: Any) -> bool:
    if not value or not isinstance(value, str):
        return False
    v = value.strip().lower()
    return v.startswith("/") or "bbb.org" in v


def listing_from_record(record: dict, default_category: str = "") -> Listing:
    """Build a Listing from one search-API JSON record."""
    if not isinstance(record, dict):
        return Listing()

    # `url` is ambiguous -- BBB profile link on some payloads, the company's own
    # site on others. Route it by host rather than trusting the key name.
    website_raw = _get(record, *WEBSITE_KEYS)
    profile_raw = _get(record, *PROFILE_KEYS)
    if _is_bbb_url(website_raw):
        profile_raw = profile_raw or website_raw
        website_raw = _get(record, *[k for k in WEBSITE_KEYS if k != "url"])
    if profile_raw and not _is_bbb_url(profile_raw):
        # Not a BBB link, so it isn't a profile URL. Use it as the website if we
        # don't have one, otherwise drop it rather than mislabel it.
        if not website_raw:
            website_raw = profile_raw
        profile_raw = None

    category = _get(record, *CATEGORY_KEYS)
    if isinstance(category, dict):
        category = _get(category, "name", "title", "displayName")
    if isinstance(category, list) and category:
        first = category[0]
        category = first.get("name") if isinstance(first, dict) else first

    phone = _get(record, *PHONE_KEYS)
    if isinstance(phone, dict):
        phone = _get(phone, "number", "phone", "value")
    if isinstance(phone, list) and phone:
        first = phone[0]
        phone = first.get("number") if isinstance(first, dict) else first

    rating = _get(record, *RATING_KEYS)
    if isinstance(rating, dict):
        rating = _get(rating, "letter", "grade", "value")

    website, social_url = classify_website(website_raw)

    return Listing(
        company_name=_clean_text(_get(record, *NAME_KEYS)),
        website=website,
        phone=normalize_phone(phone),
        street=_clean_text(_get(record, *STREET_KEYS)),
        city=_clean_text(_get(record, *CITY_KEYS)),
        state=normalize_state(_get(record, *STATE_KEYS)),
        zip=normalize_zip(_get(record, *ZIP_KEYS)),
        category=_clean_text(category) or default_category,
        years_in_business=parse_years_in_business(_get(record, *YEARS_KEYS)),
        accredited=parse_bool(_get(record, *ACCREDITED_KEYS)),
        bbb_rating=normalize_rating(rating if isinstance(rating, str) else None),
        profile_url=_absolutize(profile_raw),
        social_url=social_url,
        bbb_reviews=parse_count(_get(record, *REVIEW_COUNT_KEYS)),
        bbb_complaints=parse_count(_get(record, *COMPLAINT_KEYS)),
        employees=parse_employees(_get(record, *EMPLOYEE_KEYS)),
    )


def _clean_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        value = str(value)
    return re.sub(r"\s+", " ", value).strip()


# --------------------------------------------------------------------------
# record discovery inside an unknown JSON envelope
# --------------------------------------------------------------------------

_RECORD_SIGNALS = (
    NAME_KEYS + PHONE_KEYS + PROFILE_KEYS + RATING_KEYS + ACCREDITED_KEYS + CITY_KEYS
)


def _record_score(d: Any) -> int:
    if not isinstance(d, dict):
        return 0
    keys = {k.replace("_", "").lower() for k in d.keys() if isinstance(k, str)}
    score = 0
    for signal in _RECORD_SIGNALS:
        top = signal.split(".")[0].replace("_", "").lower()
        if top in keys:
            score += 1
    return score


def find_records(payload: Any, min_score: int = 2) -> list:
    """Locate the list of business records inside an arbitrary JSON envelope.

    Walks the whole tree and returns the highest-scoring list of dicts, so we
    don't have to know whether results live under `results`, `searchResults`,
    `data.businesses`, or something new next quarter.
    """
    best: list = []
    best_score = 0

    def walk(node: Any) -> None:
        nonlocal best, best_score
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if dicts:
                avg = sum(_record_score(d) for d in dicts) / len(dicts)
                if avg >= min_score and (avg, len(dicts)) > (best_score, len(best)):
                    best, best_score = dicts, avg
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    walk(payload)
    return best


def find_total_count(payload: Any) -> Optional[int]:
    """Best-effort total-result count, used only for logging/progress."""
    for key in ("totalResults", "total", "totalCount", "resultCount", "count", "numResults"):
        val = _get(payload, key, f"data.{key}", f"meta.{key}", f"pagination.{key}")
        if isinstance(val, int) and val >= 0:
            return val
        if isinstance(val, str) and val.isdigit():
            return int(val)
    return None


# --------------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------------

def load_column_map(path) -> tuple:
    """Read a column map: {"columns": {"website": "Company Domain", ...}}.

    Returns (fieldnames, mapping). Only the listed columns are written, in the
    order given, under the names given -- so a CSV can drop straight into
    whatever Apollo or Smartlead expects without hand-editing headers.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    columns = data.get("columns", data)
    if not isinstance(columns, dict) or not columns:
        raise ValueError(f"{path}: expected a non-empty 'columns' object")
    unknown = [k for k in columns if k not in FIELD_ORDER]
    if unknown:
        raise ValueError(f"{path}: unknown column(s): {', '.join(sorted(unknown))}. "
                         f"Available: {', '.join(FIELD_ORDER)}")
    return [columns[k] for k in columns], columns


# Fields carrying free text straight off BBB. The rest are built by our own
# normalizers (E.164, bare domains, https URLs, ints) and cannot start with a
# formula character, so escaping them would only corrupt them.
FREE_TEXT_FIELDS = {"company_name", "street", "city", "state", "category"}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def sanitize_cell(value):
    """Neutralize a value a spreadsheet would execute as a formula.

    The CSV is opened in Excel and Sheets and fed to other tools, and a company
    name is attacker-controlled text on a page anyone can get listed on.
    """
    if not isinstance(value, str) or not value.startswith(_FORMULA_START):
        return value
    return "'" + value


def write_csv(path, listings: Iterable[Listing], append: bool = False,
              column_map: Optional[tuple] = None) -> int:
    """Write rows to `path`. Appending skips the header if one is already there."""
    existing = append and os.path.exists(path) and os.path.getsize(path) > 0
    fieldnames, mapping = (column_map or (FIELD_ORDER, None))
    rows = 0
    with open(path, "a" if existing else "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if not existing:
            writer.writeheader()
        for listing in listings:
            row = listing.as_row()
            for field in FREE_TEXT_FIELDS:
                row[field] = sanitize_cell(row[field])
            if mapping:
                row = {out: row[src] for src, out in mapping.items()}
            writer.writerow(row)
            rows += 1
    return rows


# Fields worth reporting coverage on. company_name/profile_url are effectively
# always present, so they'd only add noise.
COVERAGE_FIELDS = [
    "website", "social_url", "phone", "street", "city", "state", "zip",
    "years_in_business", "accredited", "bbb_rating",
    "bbb_reviews", "bbb_complaints", "employees",
]


def field_coverage(listings) -> dict:
    """How many listings have a value for each field.

    A field sitting at 0% is the signal that a key name changed (or was
    guessed wrong) -- without this it just looks like BBB doesn't publish it.
    """
    listings = list(listings)
    counts = {}
    for name in COVERAGE_FIELDS:
        filled = 0
        for listing in listings:
            value = getattr(listing, name, None)
            if value not in (None, ""):
                filled += 1
        counts[name] = filled
    return {"total": len(listings), "filled": counts}


def format_coverage(coverage: dict, width: int = 3) -> list:
    """Render coverage as aligned lines, flagging anything at zero."""
    total = coverage["total"]
    if not total:
        return []
    lines, row = [], []
    for name, filled in coverage["filled"].items():
        pct = filled * 100 // total
        cell = f"{name} {pct}%"
        if filled == 0:
            cell += " (!)"
        row.append(f"{cell:<26}")
        if len(row) == width:
            lines.append("".join(row).rstrip())
            row = []
    if row:
        lines.append("".join(row).rstrip())
    return lines


def existing_keys(path, column_map: Optional[tuple] = None) -> set:
    """Dedupe keys already present in a CSV we're about to append to.

    A state pull spread over several invocations appends into one file, and a
    business listed in two metros would otherwise land in it twice.

    With a column map in play the headers are whatever the destination wanted,
    so the map itself says which columns hold the website and phone -- guessing
    from header text silently missed "Company Domain" and re-added every row.
    """
    keys = set()
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return keys

    website_col, phone_col = "website", "phone"
    if column_map:
        _fieldnames, mapping = column_map
        website_col = mapping.get("website")
        phone_col = mapping.get("phone")

    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            names = reader.fieldnames or []
            if website_col not in names and phone_col not in names:
                # Neither key is in the file; appending can't be deduped.
                return set()
            for row in reader:
                website = normalize_domain(row.get(website_col)) if website_col else ""
                phone = normalize_phone(row.get(phone_col)) if phone_col else ""
                if website:
                    keys.add(f"web:{website}")
                elif phone:
                    keys.add(f"tel:{phone}")
    except (OSError, csv.Error):
        return set()
    return keys


def dedupe(listings: Iterable[Listing]) -> tuple:
    """Split into (unique, duplicates) on normalized website, phone as fallback.

    Records with neither key are never treated as duplicates of each other --
    they can't be compared, so they pass through and get split off downstream.
    """
    seen: dict = {}
    unique: list = []
    dupes: list = []
    for listing in listings:
        key = listing.dedupe_key()
        if key is None:
            unique.append(listing)
            continue
        if key in seen:
            seen[key].merge(listing)   # keep the richer of the two
            dupes.append(listing)
            continue
        seen[key] = listing
        unique.append(listing)
    return unique, dupes


# --------------------------------------------------------------------------
# JSON-LD (schema.org) embedded in server-rendered pages
# --------------------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)

# schema.org types that describe a business we want.
_BUSINESS_TYPES = {
    "localbusiness", "organization", "corporation", "homeandconstructionbusiness",
    "plumber", "electrician", "roofingcontractor", "hvacbusiness", "generalcontractor",
    "movingcompany", "professionalservice", "store", "locksmith", "painter",
}


def find_jsonld_blocks(html: str) -> list:
    """Parsed <script type="application/ld+json"> payloads. Stdlib only.

    Deliberately regex-based rather than DOM-based: this runs in --inspect-har
    on machines that have nothing installed but Python.
    """
    blocks = []
    for match in _JSONLD_RE.finditer(html or ""):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            blocks.append(json.loads(raw))
        except ValueError:
            # Some sites emit HTML-escaped or trailing-comma JSON; skip quietly.
            continue
    return blocks


def _type_names(node: Any) -> set:
    value = node.get("@type") if isinstance(node, dict) else None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return set()
    return {str(v).replace(" ", "").lower() for v in value}


def _looks_like_business(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    if _type_names(node) & _BUSINESS_TYPES:
        return True
    # Untyped or unknown-typed entries still count when they carry the shape.
    has_name = bool(_get(node, "name", "legalName"))
    has_contact = bool(_get(node, "telephone", "address", "url"))
    return has_name and has_contact


def iter_jsonld_businesses(node: Any) -> Iterator[dict]:
    """Walk a JSON-LD document yielding business nodes, unwrapping ItemLists."""
    if isinstance(node, list):
        for item in node:
            yield from iter_jsonld_businesses(item)
        return
    if not isinstance(node, dict):
        return

    if "itemListElement" in node:
        for element in node.get("itemListElement") or []:
            target = element.get("item") if isinstance(element, dict) else None
            yield from iter_jsonld_businesses(target if target is not None else element)
        return

    if _looks_like_business(node):
        yield node

    for key, value in node.items():
        if key in ("@context", "@type", "address", "aggregateRating"):
            continue
        if isinstance(value, (dict, list)):
            yield from iter_jsonld_businesses(value)


def listing_from_jsonld(node: dict, default_category: str = "") -> Listing:
    """Build a Listing from one schema.org business node."""
    address = _get(node, "address") or {}
    if isinstance(address, list):
        address = address[0] if address else {}
    if not isinstance(address, dict):
        address = {}

    rating = _get(node, "aggregateRating") or {}
    if not isinstance(rating, dict):
        rating = {}

    url_value = _get(node, "url", "@id", "mainEntityOfPage")
    if isinstance(url_value, dict):
        url_value = _get(url_value, "@id", "url")

    website, social = "", ""
    profile = ""
    if _is_bbb_url(url_value):
        profile = _absolutize(url_value if isinstance(url_value, str) else "")
    else:
        website, social = classify_website(url_value)

    # sameAs is where schema.org puts social profiles, and sometimes the site.
    same_as = _get(node, "sameAs")
    if isinstance(same_as, str):
        same_as = [same_as]
    for link in same_as or []:
        candidate_domain, candidate_social = classify_website(link)
        if candidate_domain and not website:
            website = candidate_domain
        elif candidate_social and not social:
            social = candidate_social

    category = _get(node, "additionalType", "knowsAbout")
    if isinstance(category, list):
        category = category[0] if category else None
    # BBB's search JSON-LD carries no category, but its profile URLs embed one.
    category = category or category_from_profile_url(profile) or default_category

    return Listing(
        company_name=_clean_text(_get(node, "name", "legalName", "alternateName")),
        website=website,
        phone=normalize_phone(_get(node, "telephone", "contactPoint.telephone")),
        street=_clean_text(_get(address, "streetAddress")),
        city=_clean_text(_get(address, "addressLocality")),
        state=normalize_state(_get(address, "addressRegion")),
        zip=normalize_zip(_get(address, "postalCode")),
        category=_clean_text(category) or default_category,
        years_in_business=parse_years_in_business(_get(node, "foundingDate", "yearsInBusiness")),
        accredited=parse_bool(_get(node, "isAccredited")),
        bbb_rating=normalize_rating(_get(node, "bbbRating", "award")),
        profile_url=profile,
        social_url=social,
        bbb_reviews=parse_count(_get(rating, "reviewCount", "ratingCount")),
    )


def listings_from_html(html: str, default_category: str = "") -> tuple:
    """(listings, skipped) from a server-rendered page's JSON-LD.

    BBB has no JSON search API -- results are rendered into the page, with
    schema.org metadata alongside them. That metadata is cleaner than the
    markup, so it is tried first; DOM parsing stays as the fallback.
    """
    listings, skipped = [], 0
    for block in find_jsonld_blocks(html):
        for node in iter_jsonld_businesses(block):
            listing = listing_from_jsonld(node, default_category=default_category)
            if listing.company_name:
                listings.append(listing)
            else:
                skipped += 1
    return listings, skipped


# --------------------------------------------------------------------------
# Business profile pages
# --------------------------------------------------------------------------
#
# Written against real BBB markup. Two sources sit on every profile: the
# rendered HTML, and a large embedded JSON blob the app ships for analytics.
# The blob is the more stable of the two, so it wins where both exist.
#
# React server-rendering splits text nodes with an empty HTML comment
# (`<strong>Years in Business:</strong> <!-- -->78`), which every pattern here
# has to tolerate.

_GAP = r"\s*(?:<!--\s*-->)?\s*"


def _labelled(label: str) -> "re.Pattern":
    """Match `Label:` followed by its value across whatever markup separates them.

    BBB uses <dt>/<dd> in one place and <strong> in another, with React's empty
    comment between text nodes; a pattern tied to one of those shapes breaks on
    the next redesign for no good reason.
    """
    return re.compile(
        label + r"\s*:?\s*(?:</?[^>]{1,200}>|<!--\s*-->|\s)*([^<\n]{1,40})",
        re.I,
    )


_PROFILE_PATTERNS = {
    "years": (
        _labelled(r"Years in Business"),
    ),
    "started": (
        _labelled(r"Business Started"),
    ),
    "rating": (
        re.compile(r'"business_rating"\s*:\s*"([A-DF][+-]?)"'),
        re.compile(r'class="bpr-letter-grade"[^>]*>\s*([A-DF][+-]?)\s*<', re.I),
        _labelled(r"BBB Rating"),
    ),
    "website": (
        re.compile(r'"additionalWebsiteAddresses"\s*:\s*\[\s*"([^"]+)"'),
        re.compile(r'"websiteAddress(?:es)?"\s*:\s*"([^"]+)"'),
        re.compile(r'"website(?:Url|Uri)"\s*:\s*"([^"]+)"', re.I),
    ),
    "accredited_status": (
        re.compile(r'"accredited_status"\s*:\s*"([A-Za-z]*)"'),
    ),
    "accredited_since": (
        _labelled(r"BBB Accredited Since"),
    ),
    "reviews": (
        re.compile(r'"(?:customerReviewCount|reviewCount)"\s*:\s*"?(\d+)"?'),
        re.compile(r"([\d,]+)\s*Customer Reviews?", re.I),
    ),
    "complaints": (
        re.compile(r'"complaintCount"\s*:\s*"?(\d+)"?'),
        re.compile(r"([\d,]+)\s*(?:Customer )?Complaints?\b", re.I),
    ),
    "employees": (
        _labelled(r"Number of Employees"),
    ),
}


def _first(html: str, key: str) -> Optional[str]:
    for pattern in _PROFILE_PATTERNS[key]:
        match = pattern.search(html or "")
        if match:
            return match.group(1)
    return None


def category_from_profile_url(url: Any) -> str:
    """BBB profile URLs embed the category: /us/ks/wichita/profile/plumber/name-0714-15740."""
    if not url or not isinstance(url, str):
        return ""
    match = re.search(r"/profile/([^/]+)/", url)
    return match.group(1) if match else ""


_CANONICAL_PATTERNS = (
    re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.I),
    re.compile(r'"canonical"\s*:\s*"([^"]+)"'),
    re.compile(r'(/us/[a-z]{2}/[^"\s]*?/profile/[^"\s]+)'),
)


def _profile_url_in(html: str) -> str:
    """The profile's own URL, for reading the category slug out of the path."""
    for pattern in _CANONICAL_PATTERNS:
        match = pattern.search(html or "")
        if match:
            return match.group(1)
    return ""


def listing_from_profile_html(html: str) -> Listing:
    """Everything a profile page adds to a search result. Stdlib only.

    Deliberately not DOM-based: this has to run wherever --replay does, which
    includes machines with nothing installed but Python.
    """
    years = parse_count(_first(html, "years"))
    if years is None:
        years = parse_years_in_business(_first(html, "started"))

    status = _first(html, "accredited_status")
    accredited = None
    if status:
        accredited = status.upper() == "AB"
    elif _first(html, "accredited_since"):
        accredited = True

    website, social = classify_website(_first(html, "website"))

    return Listing(
        website=website,
        social_url=social,
        category=category_from_profile_url(_profile_url_in(html)),
        years_in_business=years if years is not None and years <= 200 else None,
        accredited=accredited,
        bbb_rating=normalize_rating(_first(html, "rating")),
        bbb_reviews=parse_count(_first(html, "reviews")),
        bbb_complaints=parse_count(_first(html, "complaints")),
        employees=parse_employees(_first(html, "employees")),
    )


def listings_from_payload(payload: Any, default_category: str = "") -> tuple:
    """(listings, skipped). Skipped records looked like businesses but had no name.

    A non-zero skip count is the loudest schema-drift signal there is: the
    records are right there in the payload and we can't read the one field
    every row needs.
    """
    listings, skipped = [], 0
    for record in find_records(payload):
        listing = listing_from_record(record, default_category=default_category)
        if listing.company_name:
            listings.append(listing)
        else:
            skipped += 1
    return listings, skipped


def iter_listings_from_payload(payload: Any, default_category: str = "") -> Iterator[Listing]:
    listings, _skipped = listings_from_payload(payload, default_category=default_category)
    return iter(listings)
