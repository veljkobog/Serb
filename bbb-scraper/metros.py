"""Expanding a state into metros, for full-state pulls.

BBB search is city-scoped in practice, so `--location nc` is run as a sequence
of metro pulls rather than one query. The bundled list in data/metros.json is
the largest cities per state -- a starting point, not an authoritative metro
list. Override it per run with --metros or --metros-file.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "metros.json")

STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

_cache: Optional[dict] = None


class UnknownState(ValueError):
    pass


def load_metro_data(path: str = DATA_PATH) -> dict:
    global _cache
    if _cache is None:
        with open(path, encoding="utf-8") as fh:
            _cache = json.load(fh).get("metros", {})
    return _cache


def state_code(location: str) -> Optional[str]:
    """The state code if `location` names a whole state, else None.

    "nc", "NC", "north-carolina" and "North Carolina" are states.
    "charlotte-nc" is not -- it already names a city.
    """
    if not location:
        return None
    text = location.strip().lower()
    if re.fullmatch(r"[a-z]{2}", text):
        return text.upper() if text.upper() in load_metro_data() else None
    normalized = re.sub(r"[-_]+", " ", text).strip()
    return STATE_CODES.get(normalized)


def metros_for_state(code: str, path: str = DATA_PATH) -> List[str]:
    """Metro slugs ("charlotte-nc") for a state code."""
    data = load_metro_data(path)
    cities = data.get(code.upper())
    if not cities:
        raise UnknownState(f"no metros listed for '{code}' in {os.path.basename(path)}")
    suffix = code.lower()
    return [f"{city}-{suffix}" for city in cities]


def location_label(slug: str) -> str:
    """A location slug as BBB writes it: "wichita-ks" -> "Wichita, KS".

    Captured search URLs use find_loc=Cheney%2C+KS, i.e. "City, ST" -- so the
    trailing two-letter state is split off and the rest title-cased.
    """
    if not slug:
        return ""
    text = slug.strip()
    if "," in text:                       # already "City, ST"
        return text
    parts = [p for p in re.split(r"[-_\s]+", text) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0].upper() if len(parts[0]) == 2 else parts[0].title()
    if len(parts[-1]) == 2 and parts[-1].isalpha():
        return f"{' '.join(p.title() for p in parts[:-1])}, {parts[-1].upper()}"
    return " ".join(p.title() for p in parts)


_SMALL_WORDS = {"and", "of", "the", "in", "for", "or"}


def category_label(slug: str) -> str:
    """A category slug as search text: "heating-and-air-conditioning" ->
    "Heating and Air Conditioning"."""
    if not slug:
        return ""
    parts = [p for p in re.split(r"[-_\s]+", slug.strip()) if p]
    return " ".join(
        part.lower() if index and part.lower() in _SMALL_WORDS else part.title()
        for index, part in enumerate(parts)
    )


def parse_metros_arg(value: str) -> List[str]:
    """Comma- or whitespace-separated slugs from --metros."""
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,\s]+", value) if part.strip()]


def load_metros_file(path: str) -> List[str]:
    """One slug per line; blank lines and # comments ignored."""
    slugs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                slugs.append(line)
    return slugs


def resolve_locations(location: str, metros_arg: Optional[str] = None,
                      metros_file: Optional[str] = None, limit: Optional[int] = None):
    """Work out which locations to pull.

    Returns (locations, source). An explicit list always wins over the bundled
    data; a location that already names a city stays a single-location run.
    """
    if metros_file:
        return _limit(load_metros_file(metros_file), limit), f"file:{os.path.basename(metros_file)}"
    if metros_arg:
        return _limit(parse_metros_arg(metros_arg), limit), "--metros"

    code = state_code(location)
    if code:
        return _limit(metros_for_state(code), limit), f"bundled:{code}"
    return [location], "single"


def _limit(items: List[str], limit: Optional[int]) -> List[str]:
    return items[:limit] if limit and limit > 0 else items
