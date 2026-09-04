"""A stand-in Apollo lookup endpoint.

Mirrors the two things the real one does that the client depends on: results
split across `organizations` (net-new, id is the org id) and `accounts`
(already saved, org id lives in organization_id), and rows that legitimately
carry no website at all.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ORGS = {
    "ace plumbing": [
        {"id": "a" * 24, "name": "Ace Plumbing Co.",
         "domain": "aceplumbing.com", "website_url": "http://www.aceplumbing.com"},
    ],
    "budget plumbing": [   # in Apollo, but Apollo has no website for it
        {"id": "b" * 24, "name": "Budget Plumbing"},
    ],
    "saved roofing": [],   # returned via the accounts bucket instead
    "totally different": [
        {"id": "c" * 24, "name": "Zebra Industrial Holdings",
         "domain": "zebra-ind.com", "website_url": "http://zebra-ind.com"},
    ],
}

ACCOUNTS = {
    "saved roofing": [
        {"id": "acct-1", "organization_id": "d" * 24, "name": "Saved Roofing",
         "domain": "savedroofing.com"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    calls = 0
    bad_status = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        if not self.path.endswith("/organizations/search"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if self.__class__.bad_status:
            self.send_response(self.__class__.bad_status)
            self.end_headers()
            self.wfile.write(b'{"error":"nope"}')
            return
        if self.headers.get("x-api-key") != "test-key":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.calls += 1
        name = (body.get("q_organization_name")
                or body.get("q_organization_fuzzy_name") or "").lower()
        payload = {"organizations": ORGS.get(name, []),
                   "accounts": ACCOUNTS.get(name, [])}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_apollo():
    Handler.calls = 0
    Handler.bad_status = None
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1/organizations/search", Handler
