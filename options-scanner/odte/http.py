"""Tiny stdlib HTTP client: retries, per-host rate limiting, on-disk response cache."""
from __future__ import annotations

import gzip
import hashlib
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_COOKIES = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_COOKIES))
_LOCK = threading.Lock()
_LAST_CALL: Dict[str, float] = {}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class Http:
    def __init__(self, cache_dir: Optional[str] = None, cache_ttl: int = 300,
                 min_interval: float = 0.2, timeout: int = 20, retries: int = 3,
                 force_fresh: bool = False):
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        # When set, live quotes and chains skip the cache. Files marked ``immutable``
        # (a published FINRA session file never changes) are still served from disk,
        # so pressing Scan does not re-download 25 days of history every time.
        self.force_fresh = force_fresh
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- cache -------------------------------------------------------------
    def _cache_path(self, key: str) -> Optional[str]:
        if not self.cache_dir:
            return None
        return os.path.join(self.cache_dir, hashlib.sha256(key.encode()).hexdigest()[:32] + ".cache")

    def _cache_get(self, key: str, ttl: Optional[int] = None) -> Optional[str]:
        path = self._cache_path(key)
        ttl = self.cache_ttl if ttl is None else ttl
        if not path or ttl <= 0 or not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > ttl:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def _cache_put(self, key: str, body: str) -> None:
        path = self._cache_path(key)
        if not path:
            return
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, path)
        except OSError:
            pass

    # -- transport ---------------------------------------------------------
    def _throttle(self, host: str) -> None:
        if self.min_interval <= 0:
            return
        with _LOCK:
            last = _LAST_CALL.get(host, 0.0)
            wait = self.min_interval - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            _LAST_CALL[host] = time.time()

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, cache_ttl: Optional[int] = None,
                 immutable: bool = False) -> str:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if self.force_fresh and not immutable:
            cached = None
        else:
            cached = self._cache_get(url, cache_ttl)
        if cached is not None:
            return cached

        hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "gzip"}
        hdrs.update(headers or {})
        host = urllib.parse.urlparse(url).netloc
        last_err: Optional[Exception] = None

        for attempt in range(self.retries):
            self._throttle(host)
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with _OPENER.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    body = raw.decode("utf-8", errors="replace")
                self._cache_put(url, body)
                return body
            except urllib.error.HTTPError as exc:  # noqa: PERF203
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:  # pragma: no cover - best effort
                    pass
                last_err = HttpError(exc.code, url, body)
                # 404 means "not there"; don't burn retries on it.
                if exc.code in (400, 401, 403, 404):
                    raise last_err
                time.sleep(0.6 * (2 ** attempt))
            except Exception as exc:  # network / timeout
                last_err = exc
                time.sleep(0.6 * (2 ** attempt))
        raise last_err if last_err else RuntimeError("request failed")

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, cache_ttl: Optional[int] = None,
                 immutable: bool = False) -> Any:
        return json.loads(self.get_text(url, params=params, headers=headers,
                                        cache_ttl=cache_ttl, immutable=immutable))
