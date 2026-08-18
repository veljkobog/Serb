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
import re
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date
from typing import Any, Iterable, Iterator, Optional

# CSV column order, per spec.
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
    # size / traction signals, appended after the spec's columns
    "bbb_reviews",
    "bbb_complaints",
    "employees",
    "google_rating",
    "google_reviews",
    "google_place_id",
    "google_match",
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
    bbb_reviews: Optional[int] = None
    bbb_complaints: Optional[int] = None
    employees: Optional[int] = None
    google_rating: Optional[float] = None
    google_reviews: Optional[int] = None
    google_place_id: str = ""
    google_match: str = ""

    def dedupe_key(self) -> Optional[str]:
        """Normalized website, falling back to phone. None if neither is known."""
        if self.website:
            return f"web:{self.website}"
        if self.phone:
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
            "bbb_reviews": _blank_if_none(self.bbb_reviews),
            "bbb_complaints": _blank_if_none(self.bbb_complaints),
            "employees": _blank_if_none(self.employees),
            "google_rating": _blank_if_none(self.google_rating),
            "google_reviews": _blank_if_none(self.google_reviews),
            "google_place_id": self.google_place_id,
            "google_match": self.google_match,
        }


# --------------------------------------------------------------------------
# normalizers
# --------------------------------------------------------------------------

_TRACKING_HOSTS = {"bbb.org", "www.bbb.org"}


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


NAME_KEYS = ("businessName", "companyName", "name", "displayName", "title", "legalName")
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

    return Listing(
        company_name=_clean_text(_get(record, *NAME_KEYS)),
        website=normalize_domain(website_raw),
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

def write_csv(path, listings: Iterable[Listing]) -> int:
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELD_ORDER)
        writer.writeheader()
        for listing in listings:
            writer.writerow(listing.as_row())
            rows += 1
    return rows


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


def iter_listings_from_payload(payload: Any, default_category: str = "") -> Iterator[Listing]:
    for record in find_records(payload):
        listing = listing_from_record(record, default_category=default_category)
        if listing.company_name:
            yield listing
