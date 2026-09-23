"""A small JSON-on-disk cache shared by the enrichment passes.

Both enrichment sources are billed or rate-limited per call, so a re-run or a
resumed run must not pay twice for an answer we already have. The semantics
that matter are in `get`:

  * a hit returns the stored result, which may legitimately be None -- "the
    provider looked and found nothing" is a real answer worth remembering
  * a miss returns the MISS sentinel, never None

Conflating those two is how a negative cache defeats its own TTL: `if key in
entries` treats an expired-but-present entry as a hit forever.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Dict, Optional

MISS = object()   # distinct from a cached "the provider found nothing"


class JsonCache:
    """Disk-backed {key: {fetched_at, result}}, written atomically."""

    #: temp-file prefix, so a half-written cache is identifiable on disk
    prefix = ".cache-"

    def __init__(self, path: Optional[str], ttl_days: int = 30, now: Optional[float] = None):
        self.path = path
        self.ttl = ttl_days * 86400
        self._now = now if now is not None else time.time()
        self.entries: Dict[str, dict] = {}
        self.hits = 0
        self.dirty = False
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.entries = json.load(fh).get("entries", {})
            except (OSError, ValueError):
                # A corrupt cache is a performance problem, not a correctness
                # one -- start empty rather than failing the run.
                self.entries = {}

    def get(self, key: str):
        """Cached result (possibly None), or MISS."""
        entry = self.entries.get(key)
        if not entry:
            return MISS
        if self.ttl and self._now - entry.get("fetched_at", 0) > self.ttl:
            del self.entries[key]
            self.dirty = True
            return MISS
        self.hits += 1
        return entry.get("result")

    def put(self, key: str, result: Optional[dict]) -> None:
        self.entries[key] = {"fetched_at": self._now, "result": result}
        self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=self.prefix, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"entries": self.entries}, fh)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
