"""A stand-in for Google Places `places:searchText`, so enrichment is testable
offline. Counts requests, so tests can prove the cache and the cap work.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    requests = 0
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        query = (body.get("textQuery") or "").lower()

        with Handler.lock:
            Handler.requests += 1

        place = self._place_for(query)
        payload = json.dumps({"places": [place]} if place else {}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _place_for(self, query: str):
        # "Test Plumbing 007 ..." -> the matching place, with a domain that
        # lines up so the match scores as high confidence.
        import re
        match = re.search(r"test plumbing (\d+)", query)
        if match:
            n = int(match.group(1))
            return {
                "id": f"place-{n:03d}",
                "displayName": {"text": f"Test Plumbing {n:03d}"},
                "formattedAddress": f"{n} Market St, Wilmington, NC 28401, USA",
                "rating": 3.0 + (n % 21) / 10.0,
                "userRatingCount": (n % 11) * 25,
                "websiteUri": f"https://www.testplumbing{n:03d}.com/",
                "nationalPhoneNumber": f"(9{n % 10}0) 555-{1000 + (n % 8999):04d}",
            }
        if "no contact" in query:
            # a plausible-but-wrong neighbour: different name, same city
            return {
                "id": "place-wrong",
                "displayName": {"text": "Completely Different Plumbing"},
                "formattedAddress": "1 Elsewhere Rd, Wilmington, NC 28401, USA",
                "rating": 4.9,
                "userRatingCount": 5000,
                "websiteUri": "https://different.example.com",
                "nationalPhoneNumber": "(910) 555-9999",
            }
        return None

    def log_message(self, *args):
        pass


def start_places_server():
    Handler.requests = 0
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/v1/places:searchText", Handler
