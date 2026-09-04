"""Approach B (fallback) -- Playwright.

Used when the JSON API rejects non-browser sessions. Parses the *rendered* DOM,
never a raw HTML fetch, and persists the browser profile between runs so the
Cloudflare clearance cookie survives and challenges stay rare.

Playwright is imported lazily: Approach A must keep working on a machine that
never installed browsers.
"""

from __future__ import annotations

import os
import random
import re
import time
import urllib.parse

import parse
from typing import Iterator, List, Optional

from parse import (
    Listing,
    classify_website,
    listing_from_profile_html,
    parse_count,
    parse_employees,
    normalize_phone,
    normalize_rating,
    normalize_state,
    normalize_zip,
    parse_years_in_business,
)

BBB_BASE = "https://www.bbb.org"
SEARCH_URL = BBB_BASE + "/search"

# One card per business in the search results. BBB renames these classes
# periodically, so we try several selectors before giving up.
CARD_SELECTORS = [
    "div.card.result-card",
    "div[class*='result-card']",
    "div.search-results-cards div.card",
    "[data-testid='search-result-card']",
    "li[class*='result']",
]

NEXT_SELECTORS = [
    "a[aria-label='Next page']",
    "a[rel='next']",
    "button[aria-label='Next page']",
    "a.next",
]


class BrowserUnavailable(RuntimeError):
    """Playwright isn't installed, or no browser could be launched."""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "playwright not installed -- pip install -r requirements.txt && playwright install chromium"
        ) from exc
    return sync_playwright


class BrowserClient:
    def __init__(
        self,
        user_data_dir: str = ".bbb-browser-profile",
        headless: bool = True,
        min_delay: float = 2.0,
        max_delay: float = 4.0,
        user_agent: Optional[str] = None,
        stealth: bool = True,
        verbose: bool = False,
        timeout_ms: int = 45000,
        executable_path: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
        base_url: str = BBB_BASE,
    ):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.user_agent = user_agent
        self.stealth = stealth
        self.verbose = verbose
        self.timeout_ms = timeout_ms
        # Playwright ships expecting one exact Chromium build. Machines that
        # already have a browser (CI images, containers, this repo's own dev
        # boxes) otherwise fail with "playwright install" even though a perfectly
        # good Chromium is sitting there.
        self.executable_path = executable_path or os.environ.get("BBB_BROWSER_EXECUTABLE")
        self.extra_args = list(extra_args or [])
        self.base_url = base_url.rstrip("/")
        self.challenged = 0
        self._pw = None
        self._context = None
        self._page = None

    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[browser] {message}", flush=True)

    def start(self) -> None:
        sync_playwright = _require_playwright()
        self._pw = sync_playwright().start()
        launch_kwargs = {
            "user_data_dir": self.user_data_dir,   # persisted profile == persisted cookies
            "headless": self.headless,
            "args": ["--disable-blink-features=AutomationControlled"] + self.extra_args,
        }
        if self.user_agent:
            launch_kwargs["user_agent"] = self.user_agent
        if self.executable_path:
            launch_kwargs["executable_path"] = self.executable_path
            self._log(f"using browser at {self.executable_path}")
        try:
            self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as exc:
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(
                f"could not launch Chromium: {exc}\n"
                f"Run 'playwright install chromium', or point --browser-executable "
                f"(or $BBB_BROWSER_EXECUTABLE) at an existing browser binary."
            ) from exc
        self._context.set_default_timeout(self.timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        if self.stealth:
            self._apply_stealth()

    def _apply_stealth(self) -> None:
        try:
            from playwright_stealth import stealth_sync
            stealth_sync(self._page)
            self._log("playwright-stealth applied")
            return
        except Exception:
            pass
        # Minimal fallback if playwright-stealth isn't available.
        self._page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
        )
        self._log("using built-in stealth shim")

    def close(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._context = self._pw = self._page = None

    def __enter__(self) -> "BrowserClient":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pace(self) -> None:
        time.sleep(random.uniform(self.min_delay, self.max_delay))

    # ------------------------------------------------------------------
    def search_url(self, category: str, location: str, page: int) -> str:
        params = {
            "find_country": "USA",
            "find_text": category.replace("-", " "),
            "find_loc": location.replace("-", " "),
            "page": str(page),
        }
        return f"{self.base_url}/search?" + urllib.parse.urlencode(params)

    def _absolute(self, url: str) -> str:
        """Resolve a profile link against the host this client is pointed at.

        Cards are parsed by a shared, client-agnostic function that absolutizes
        against bbb.org, so the rewrite has to come before the already-absolute
        passthrough or it never runs.
        """
        if not url:
            return url
        if self.base_url != BBB_BASE and url.startswith(BBB_BASE):
            return self.base_url + url[len(BBB_BASE):]
        if url.startswith("http"):
            return url
        if url.startswith("/"):
            return self.base_url + url
        return url

    def iter_listings(
        self,
        category: str,
        location: str,
        start_page: int = 1,
        max_pages: int = 50,
    ) -> Iterator:
        """Yield (page_number, [Listing, ...]) from rendered result pages."""
        if self._page is None:
            raise BrowserUnavailable("call start() first")

        previous_signature = None
        for offset in range(max_pages):
            page_no = start_page + offset
            url = self.search_url(category, location, page_no)
            self._pace()
            self._log(f"GET {url}")
            self._page.goto(url, wait_until="domcontentloaded")
            self._wait_for_cards()

            html_cards = self._card_html()
            if not html_cards:
                self._log(f"page {page_no} has no cards -- end of results")
                return

            listings = [listing_from_card_html(html, default_category=category) for html in html_cards]
            listings = [l for l in listings if l.company_name]
            if not listings:
                return

            signature = "::".join(f"{l.company_name}|{l.profile_url}" for l in listings[:10])
            if signature == previous_signature:
                self._log(f"page {page_no} repeats the previous page -- end of results")
                return
            previous_signature = signature

            yield page_no, listings

    def _wait_for_cards(self) -> None:
        for selector in CARD_SELECTORS:
            try:
                self._page.wait_for_selector(selector, timeout=8000)
                return
            except Exception:
                continue
        self._log("no known card selector matched -- page may be challenged")

    def _card_html(self) -> List[str]:
        for selector in CARD_SELECTORS:
            handles = self._page.query_selector_all(selector)
            if handles:
                self._log(f"{len(handles)} cards via '{selector}'")
                return [h.inner_html() for h in handles]
        return []

    # ------------------------------------------------------------------
    def fetch_detail(self, profile_url: str) -> Listing:
        """Render a business profile page and pull the fields cards omit.

        A Cloudflare interstitial is HTTP 200 with a full page of markup, so
        handing it straight to the parser yields a Listing with every field
        empty -- which reads as "the markup changed" and sent a whole
        investigation the wrong way. The search path has always checked for
        this; the detail path did not.
        """
        if self._page is None:
            raise BrowserUnavailable("call start() first")
        self._pace()
        profile_url = self._absolute(profile_url)
        self._log(f"detail {profile_url}")
        self._page.goto(profile_url, wait_until="domcontentloaded")

        html = self._page.content()
        if parse.looks_challenged(html):
            self.challenged += 1
            self._log(f"CHALLENGED on {profile_url} -- Cloudflare interstitial, "
                      f"not a profile page")
            raise parse.BlockedError(
                "Cloudflare served a challenge instead of the profile page. "
                "Try --headed and solve it once: the clearance cookie is kept "
                "in the browser profile and later runs reuse it.")
        return listing_from_detail_html(html)


# --------------------------------------------------------------------------
# DOM parsing (kept module-level so it is unit-testable without a browser)
# --------------------------------------------------------------------------

def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


_PHONE_TEXT_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_ADDRESS_RE = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})")
_YEARS_RE = re.compile(r"(\d{1,3})\s*\+?\s*(?:years?|yrs?)\s*in\s*business", re.I)

# A trailing "+" is not a word character, so \b after the grade never matches --
# use a negative lookahead instead, or "A+" silently parses as no rating.
_RATING_PATTERNS = (
    re.compile(r"BBB\s*Rating:?\s*([A-DF][+-]?|NR)(?![A-Za-z0-9])", re.I),
    re.compile(r"\b([A-DF][+-]?|NR)(?![A-Za-z0-9])\s*(?:BBB\s*)?rating", re.I),
)


# Size signals as they appear in profile copy.
_REVIEWS_PATTERNS = (
    re.compile(r"average\s+of\s+([\d,]+)\s+customer\s+reviews?", re.I),
    re.compile(r"([\d,]+)\s+customer\s+reviews?", re.I),
    re.compile(r"customer\s+reviews?\s*\(?\s*([\d,]+)\s*\)?", re.I),
)
_COMPLAINTS_PATTERNS = (
    re.compile(r"([\d,]+)\s+complaints?\s+closed", re.I),
    re.compile(r"([\d,]+)\s+(?:customer\s+)?complaints?\b", re.I),
    re.compile(r"customer\s+complaints?\s*\(?\s*([\d,]+)\s*\)?", re.I),
)
_EMPLOYEES_PATTERNS = (
    re.compile(r"number\s+of\s+employees:?\s*([\d,]+(?:\s*(?:-|to|–)\s*[\d,]+)?)", re.I),
    re.compile(r"([\d,]+(?:\s*(?:-|to|–)\s*[\d,]+)?)\s+employees\b", re.I),
)


def _first_match(text: str, patterns, converter):
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = converter(match.group(1))
            if value is not None:
                return value
    return None


def _find_links(soup) -> tuple:
    """(company_domain, social_url) from a card or profile page.

    A real domain wins over a social link no matter which appears first -- BBB
    often lists the Facebook page above the website.
    """
    domain, social = "", ""
    for anchor in soup.find_all("a", href=True):
        candidate_domain, candidate_social = classify_website(anchor["href"])
        if candidate_domain and not domain:
            domain = candidate_domain
        elif candidate_social and not social:
            social = candidate_social
        if domain and social:
            break
    return domain, social


def _find_rating(soup, text: str) -> str:
    node = soup.find(attrs={"class": re.compile("rating", re.I)})
    haystacks = [node.get_text(" ", strip=True)] if node else []
    haystacks.append(text)
    for haystack in haystacks:
        for pattern in _RATING_PATTERNS:
            match = pattern.search(haystack)
            if match:
                rating = normalize_rating(match.group(1))
                if rating or match.group(1).upper() == "NR":
                    return rating
    return ""


def listing_from_card_html(html: str, default_category: str = "") -> Listing:
    """Extract one Listing from a rendered search-result card."""
    soup = _soup(html)
    text = soup.get_text(" ", strip=True)

    name, profile_url = "", ""
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/profile/" in href or re.search(r"/us/[a-z]{2}/", href):
            label = anchor.get_text(" ", strip=True)
            if label and len(label) > 1:
                name, profile_url = label, href
                break
    if not name:
        heading = soup.find(["h1", "h2", "h3", "h4"])
        name = heading.get_text(" ", strip=True) if heading else ""

    website, social_url = _find_links(soup)

    phone_match = _PHONE_TEXT_RE.search(text)

    street = city = state = zipcode = ""
    address_node = soup.find(["address"]) or soup.find(attrs={"class": re.compile("address", re.I)})
    address_text = address_node.get_text(" ", strip=True) if address_node else text
    address_match = _ADDRESS_RE.search(address_text) or _ADDRESS_RE.search(text)
    if address_match:
        street = address_match.group("street").strip()
        city = address_match.group("city").strip()
        state = address_match.group("state")
        zipcode = address_match.group("zip")

    rating = _find_rating(soup, text)

    accredited: Optional[bool] = True if re.search(r"BBB\s+Accredited\s+Business", text, re.I) else None

    years_match = _YEARS_RE.search(text)

    return Listing(
        company_name=name,
        website=website,
        phone=normalize_phone(phone_match.group(0)) if phone_match else "",
        street=street,
        city=city,
        state=normalize_state(state),
        zip=normalize_zip(zipcode),
        category=default_category,
        years_in_business=parse_years_in_business(years_match.group(1)) if years_match else None,
        accredited=accredited,
        bbb_rating=rating,
        profile_url=_absolutize(profile_url),
        social_url=social_url,
        bbb_reviews=_first_match(text, _REVIEWS_PATTERNS, parse_count),
    )


def listing_from_detail_html(html: str) -> Listing:
    """Pull years / accreditation / rating / website off a profile page.

    The stdlib extractor in parse.py handles the real BBB markup and its
    embedded JSON; the DOM pass below only fills anything it left blank, so
    this keeps working if the page shape changes underneath us.
    """
    listing = listing_from_profile_html(html)
    listing.merge(_listing_from_detail_dom(html))
    return listing


def _listing_from_detail_dom(html: str) -> Listing:
    soup = _soup(html)
    text = soup.get_text(" ", strip=True)

    years = None
    years_match = _YEARS_RE.search(text)
    if years_match:
        years = parse_years_in_business(years_match.group(1))
    if years is None:
        started = re.search(r"Business\s+Started:?\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{4}|[0-9]{4})", text, re.I)
        if started:
            years = parse_years_in_business(started.group(1))

    accredited: Optional[bool] = None
    if re.search(r"BBB\s+Accredited\s+Business", text, re.I):
        accredited = True
    elif re.search(r"not\s+BBB\s+Accredited|This business is not BBB Accredited", text, re.I):
        accredited = False

    rating = _find_rating(soup, text)

    website, social_url = _find_links(soup)

    phone_match = _PHONE_TEXT_RE.search(text)

    return Listing(
        website=website,
        social_url=social_url,
        phone=normalize_phone(phone_match.group(0)) if phone_match else "",
        years_in_business=years,
        accredited=accredited,
        bbb_rating=rating,
        bbb_reviews=_first_match(text, _REVIEWS_PATTERNS, parse_count),
        bbb_complaints=_first_match(text, _COMPLAINTS_PATTERNS, parse_count),
        employees=_first_match(text, _EMPLOYEES_PATTERNS, parse_employees),
    )


def _absolutize(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BBB_BASE + href
    return ""
