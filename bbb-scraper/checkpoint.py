"""Resume logic.

A run writes a small JSON file after every page so an aborted run (block,
Ctrl-C, crash) can pick up where it stopped instead of re-walking pages we
already paid for.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional, Set


@dataclass
class Checkpoint:
    category: str = ""
    location: str = ""
    last_page: int = 0                       # last page fully collected
    collected_ids: Set[str] = field(default_factory=set)
    count: int = 0                           # rows collected so far
    path: Optional[str] = None

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str, category: str, location: str) -> "Checkpoint":
        """Load a checkpoint, or return a fresh one.

        A checkpoint from a different category/location is ignored rather than
        silently resumed -- resuming the wrong run is worse than starting over.
        """
        if not path or not os.path.exists(path):
            return cls(category=category, location=location, path=path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls(category=category, location=location, path=path)

        if data.get("category") != category or data.get("location") != location:
            return cls(category=category, location=location, path=path)

        return cls(
            category=category,
            location=location,
            last_page=int(data.get("last_page") or 0),
            collected_ids=set(data.get("collected_ids") or []),
            count=int(data.get("count") or 0),
            path=path,
        )

    # ------------------------------------------------------------------
    def next_page(self, start_page: int = 1) -> int:
        return max(start_page, self.last_page + 1)

    def seen(self, listing_id: Optional[str]) -> bool:
        return bool(listing_id) and listing_id in self.collected_ids

    def record(self, page: int, listing_ids, count_delta: int = 0) -> None:
        self.last_page = max(self.last_page, page)
        for listing_id in listing_ids:
            if listing_id:
                self.collected_ids.add(listing_id)
        self.count += count_delta

    def save(self) -> None:
        """Atomic write -- a half-written checkpoint is worse than none."""
        if not self.path:
            return
        payload = {
            "category": self.category,
            "location": self.location,
            "last_page": self.last_page,
            "collected_ids": sorted(self.collected_ids),
            "count": self.count,
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".checkpoint-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def clear(self) -> None:
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)


@dataclass
class RunProgress:
    """Which locations of a multi-metro run are already finished.

    Per-location page checkpoints handle resuming *within* a metro; this
    handles resuming *across* them, so an interrupted state pull doesn't
    re-walk metros it already collected.
    """

    category: str = ""
    scope: str = ""                          # the state or list this run covers
    completed: Set[str] = field(default_factory=set)
    collected: int = 0
    path: Optional[str] = None

    @classmethod
    def load(cls, path: str, category: str, scope: str) -> "RunProgress":
        if not path or not os.path.exists(path):
            return cls(category=category, scope=scope, path=path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls(category=category, scope=scope, path=path)
        if data.get("category") != category or data.get("scope") != scope:
            return cls(category=category, scope=scope, path=path)
        return cls(
            category=category,
            scope=scope,
            completed=set(data.get("completed") or []),
            collected=int(data.get("collected") or 0),
            path=path,
        )

    def is_done(self, location: str) -> bool:
        return location in self.completed

    def mark_done(self, location: str, count: int = 0) -> None:
        self.completed.add(location)
        self.collected += count

    def save(self) -> None:
        if not self.path:
            return
        payload = {
            "category": self.category,
            "scope": self.scope,
            "completed": sorted(self.completed),
            "collected": self.collected,
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".progress-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def clear(self) -> None:
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)


def listing_id(listing) -> Optional[str]:
    """Stable per-listing id for checkpointing: profile URL, else dedupe key."""
    if getattr(listing, "profile_url", ""):
        return listing.profile_url
    return listing.dedupe_key()
