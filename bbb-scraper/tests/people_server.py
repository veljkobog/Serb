"""A stand-in for Apollo's people search / bulk match / profile endpoints.

Models the behaviours the client has to survive: a same-named company in
another state, a company with no headcount on file, a masked last name, and a
balance that actually decreases so the credit governor has something real to
measure.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PEOPLE = {
    "a" * 24: [
        {"id": "p" * 24, "first_name": "Dana", "last_name": "Reyes",
         "title": "Owner", "organization_name": "Ace Plumbing Co."},
        {"id": "q" * 24, "first_name": "Sam", "last_name": "Cole",
         "title": "Operations Manager", "organization_name": "Ace Plumbing Co."},
    ],
    "b" * 24: [
        {"id": "r" * 24, "first_name": "Pat", "last_name": "Nguyen",
         "title": "President", "organization_name": "Budget Plumbing"},
    ],
    "c" * 24: [
        {"id": "s" * 24, "first_name": "Lee", "last_name": "Barr",
         "title": "Owner", "organization_name": "Tiny Shop"},
    ],
    "d" * 24: [
        {"id": "t" * 24, "first_name": "Jo", "last_name": "Marsh",
         "title": "Owner", "organization_name": "Faraway Plumbing"},
    ],
    "e" * 24: [
        {"id": "u" * 24, "first_name": "Kim", "last_name": "M****",
         "title": "Owner", "organization_name": "Masked Co"},
    ],
}

MATCHES = {
    "p" * 24: {"first_name": "Dana", "last_name": "Reyes", "title": "Owner",
               "email": "dana@aceplumbing.com", "email_status": "verified",
               "organization": {"city": "Wilmington", "state": "NC",
                                "estimated_num_employees": 27}},
    "q" * 24: {"first_name": "Sam", "last_name": "Cole", "title": "Operations Manager",
               "email": "sam@aceplumbing.com", "email_status": "verified",
               "organization": {"city": "Wilmington", "state": "NC",
                                "estimated_num_employees": 27}},
    "r" * 24: {"first_name": "Pat", "last_name": "Nguyen", "title": "President",
               "email": "pat@budget.com", "email_status": "verified",
               "organization": {"city": "Wilmington", "state": "NC"}},  # no headcount
    "s" * 24: {"first_name": "Lee", "last_name": "Barr", "title": "Owner",
               "email": "lee@tiny.com", "email_status": "verified",
               "organization": {"city": "Wilmington", "state": "NC",
                                "estimated_num_employees": 2}},
    "t" * 24: {"first_name": "Jo", "last_name": "Marsh", "title": "Owner",
               "email": "jo@faraway.com", "email_status": "verified",
               "organization": {"city": "Detroit", "state": "MI",
                                "estimated_num_employees": 40}},
    "u" * 24: {"first_name": "Kim", "last_name": "M****", "title": "Owner",
               "email": "kim@masked.com", "email_status": "verified",
               "organization": {"city": "Wilmington", "state": "NC",
                                "estimated_num_employees": 12}},
}


class Handler(BaseHTTPRequestHandler):
    balance = 1000
    cost_per_match = 1
    searches = 0
    matches = 0
    profile_broken = False

    def log_message(self, *a):
        pass

    def _send(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/users/api_profile"):
            if self.__class__.profile_broken:
                self._send({"error": "nope"}, 500)
                return
            self._send({"num_credits_remaining": self.__class__.balance})
            return
        self._send({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/v1/mixed_people/search":
            self.__class__.searches += 1
            org_ids = body.get("organization_ids") or []
            people = []
            for org_id in org_ids:
                people.extend(PEOPLE.get(org_id, []))
            self._send({"people": people})
            return

        if self.path == "/v1/people/bulk_match":
            details = body.get("details") or []
            self.__class__.matches += 1
            out = []
            for entry in details:
                match = MATCHES.get(entry.get("id"))
                out.append(match)
                if match:
                    self.__class__.balance -= self.__class__.cost_per_match
            self._send({"matches": out})
            return

        self._send({}, 404)


def start_people():
    Handler.balance = 1000
    Handler.cost_per_match = 1
    Handler.searches = 0
    Handler.matches = 0
    Handler.profile_broken = False
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}/v1", Handler
