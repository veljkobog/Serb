"""A stand-in for BBB's search API, so the pipeline is testable offline.

Serves paginated JSON that mimics the shapes we expect: an unknown envelope,
mixed key casing, duplicate domains across pages, records missing website and
phone, and an empty final page.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE_SIZE = 10


def _business(i: int, dupe_of: int = None) -> dict:
    """One business. With `dupe_of`, the same company under a second BBB
    profile -- same domain and phone, different profile URL, which is how real
    duplicates show up and the only kind `parse.dedupe` has to catch."""
    n = dupe_of if dupe_of is not None else i
    return {
        "businessName": f"Test Plumbing {n:03d}" + (" (dup)" if dupe_of is not None else ""),
        "websiteUrl": f"https://www.testplumbing{n:03d}.com/",
        "phone": f"(910) 555-{1000 + n:04d}",
        "address": {
            "street": f"{n} Market St",
            "city": "Wilmington",
            "state": "NC",
            "postalCode": "28401",
        },
        "primaryCategory": "Plumber",
        "yearsInBusiness": 5 + (n % 30),
        "isAccredited": n % 2 == 0,
        "bbbRating": "A+" if n % 3 else "NR",
        # size signals: a long tail of tiny shops, a handful of real operators
        "customerReviewCount": 0 if n % 4 == 0 else (n % 7) * 12,
        "complaintCount": n % 5,
        "numberOfEmployees": "11-50" if n % 6 == 0 else ("2-10" if n % 3 else None),
        "reportUrl": f"/us/nc/wilmington/profile/plumber/test-{i:03d}",
    }


def _no_contact(i: int) -> dict:
    """Missing website AND phone -> belongs in _lowconfidence.csv."""
    return {
        "businessName": f"No Contact Co {i:03d}",
        "address": {"city": "Wilmington", "state": "NC", "postalCode": "28401"},
        "primaryCategory": "Plumber",
        "reportUrl": f"/us/nc/wilmington/profile/plumber/nocontact-{i:03d}",
    }


def build_page(page: int, total_pages: int = 6) -> dict:
    """Page N of results. Past the end -> empty list, like a real API."""
    if page < 1 or page > total_pages:
        return {"meta": {"totalResults": total_pages * PAGE_SIZE}, "data": {"searchResults": []}}

    start = (page - 1) * PAGE_SIZE
    records = []
    for offset in range(PAGE_SIZE):
        i = start + offset
        if i % 17 == 5:
            records.append(_no_contact(i))
        elif i % 13 == 7 and i > PAGE_SIZE:
            records.append(_business(i, dupe_of=i - PAGE_SIZE))  # same domain, earlier page
        else:
            records.append(_business(i))

    return {
        "meta": {"totalResults": total_pages * PAGE_SIZE, "page": page},
        "filters": [{"id": 1, "label": "Accredited"}, {"id": 2, "label": "Distance"}],
        "data": {"searchResults": records},
    }


# What a profile page carries that the search card doesn't.
PROFILE_HTML = """
<html><body>
  <p>BBB Rating: A+</p>
  <p>Number of Employees: 25</p>
  <p>Average of 40 Customer Reviews</p>
  <p>1 complaint closed in last 3 years</p>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    total_pages = 6

    detail_requests = 0

    def do_GET(self):  # noqa: N802
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)

        if "/profile/" in split.path:
            Handler.detail_requests += 1
            body = PROFILE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if not split.path.startswith("/api/businesssearch"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"{}")
            return

        page = int(query.get("page", ["1"])[0])
        body = json.dumps(build_page(page, self.total_pages)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def start_server() -> tuple:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"
