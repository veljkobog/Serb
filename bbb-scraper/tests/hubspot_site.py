"""A stand-in HubSpot CRM search API, so crm_check is testable offline."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Seeded records. Keyed by the property the search filters on.
CONTACTS = {
    "unsubscribed@acme.com": {
        "id": "101",
        "properties": {"email": "unsubscribed@acme.com", "firstname": "Opted", "lastname": "Out",
                       "hs_email_optout": "true", "notes_last_contacted": "2026-01-05T10:00:00Z"},
    },
    "bouncer@acme.com": {
        "id": "102",
        "properties": {"email": "bouncer@acme.com", "hs_email_bounce": "3"},
    },
    "known@acme.com": {
        "id": "103",
        "properties": {"email": "known@acme.com", "lifecyclestage": "customer",
                       "num_contacted_notes": "7", "hubspot_owner_id": "555",
                       "notes_last_contacted": "2026-08-01T09:00:00Z"},
    },
}
CONTACTS_BY_PHONE = {
    "+13165550111": {
        "id": "104",
        "properties": {"email": "viaphone@acme.com", "phone": "+13165550111",
                       "hs_lead_status": "IN_PROGRESS", "hubspot_owner_id": "777"},
    },
}
COMPANIES_BY_DOMAIN = {
    "existingco.com": {
        "id": "201",
        "properties": {"name": "Existing Co", "domain": "existingco.com",
                       "num_associated_deals": "2", "hubspot_owner_id": "888"},
    },
}
COMPANIES_BY_NAME = {
    "Common Name Plumbing": {
        "id": "202",
        "properties": {"name": "Common Name Plumbing", "num_associated_deals": "0"},
    },
}


class Handler(BaseHTTPRequestHandler):
    empty_portal = False
    unauthorized = False
    requests = 0

    def do_POST(self):  # noqa: N802
        Handler.requests += 1
        if Handler.unauthorized:
            return self._send(401, {"message": "unauthorized"})

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        groups = body.get("filterGroups") or []

        # No filters = the portal-size probe.
        if not groups or not (groups[0].get("filters") or []):
            total = 0 if Handler.empty_portal else 77914
            results = [] if Handler.empty_portal else [{"id": "1", "properties": {}}]
            return self._send(200, {"total": total, "results": results})

        if Handler.empty_portal:
            return self._send(200, {"total": 0, "results": []})

        flt = groups[0]["filters"][0]
        prop, value = flt.get("propertyName"), flt.get("value")
        is_company = "/companies/" in self.path

        record = None
        if is_company and prop == "domain":
            record = COMPANIES_BY_DOMAIN.get(value)
        elif is_company and prop == "name":
            record = COMPANIES_BY_NAME.get(value)
        elif prop == "email":
            record = CONTACTS.get(value)
        elif prop in ("phone", "mobilephone"):
            record = CONTACTS_BY_PHONE.get(value) if prop == "phone" else None

        results = [record] if record else []
        return self._send(200, {"total": len(results), "results": results})

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_hubspot():
    Handler.empty_portal = False
    Handler.unauthorized = False
    Handler.requests = 0
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", Handler
