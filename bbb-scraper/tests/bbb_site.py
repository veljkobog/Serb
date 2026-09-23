"""A stand-in BBB website, so Approach B can be driven by a real browser.

Serves search result pages with cards, working pagination, an empty page past
the end, and profile pages carrying the fields the cards omit. The markup
mirrors the shapes browser_client's selectors target -- card classes, a profile
link, a website link alongside a Facebook link, a phone, an address block.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE_SIZE = 5
TOTAL_PAGES = 3

CARD = """
<div class="card result-card">
  <h3><a href="/us/nc/wilmington/profile/plumber/test-{i:03d}">Test Plumbing {i:03d}</a></h3>
  {accredited}
  <div class="address">{i} Market St, Wilmington, NC 28401</div>
  <a href="tel:+1910555{phone:04d}">(910) 555-{phone:04d}</a>
  <a href="https://www.facebook.com/testplumbing{i:03d}">Facebook</a>
  {website}
  <span class="rating">A+ rating</span>
</div>
"""

PROFILE = """
<html><body>
  <h1>Test Plumbing {i:03d}</h1>
  <p>BBB Rating: A+</p>
  <p>Business Started: 3/1/{year}</p>
  <p>Number of Employees: 27</p>
  <p>Average of 44 Customer Reviews</p>
  <p>1 complaint closed in last 3 years</p>
  <a href="https://www.testplumbing{i:03d}.com">Website</a>
</body></html>
"""


def _jsonld(page: int) -> str:
    """The schema.org block BBB renders alongside the cards.

    Same shape as the live site: a SearchResultsPage wrapping an ItemList of
    LocalBusinesses, carrying name, address, telephone and the profile URL and
    nothing else.
    """
    start = (page - 1) * PAGE_SIZE
    items = []
    for offset in range(PAGE_SIZE):
        i = start + offset
        items.append({
            "@type": "ListItem",
            "position": i + 1,
            "item": {
                "@type": "LocalBusiness",
                "name": f"Test Plumbing {i:03d}",
                "address": {
                    "@type": "PostalAddress",
                    "addressLocality": "Wilmington",
                    "addressRegion": "NC",
                    "postalCode": "28401-1234",
                    "addressCountry": "US",
                    # service-area businesses have no street, like the real data
                    "streetAddress": "" if i % 4 == 3 else f"{i} Market St",
                },
                "telephone": f"(910) 555-{1000 + i:04d}",
                "url": f"https://www.bbb.org/us/nc/wilmington/profile/plumber/test-{i:03d}",
            },
        })
    doc = {"@context": "https://schema.org", "@type": "SearchResultsPage",
           "mainEntity": {"@type": "ItemList", "itemListElement": items}}
    return ('<script type="application/ld+json">' + json.dumps(doc) + "</script>")


def search_page(page: int) -> str:
    if page < 1 or page > TOTAL_PAGES:
        return "<html><body><div class='search-results-cards'></div></body></html>"
    start = (page - 1) * PAGE_SIZE
    cards = []
    for offset in range(PAGE_SIZE):
        i = start + offset
        cards.append(CARD.format(
            i=i,
            phone=1000 + i,
            # every third listing has only a Facebook page
            website="" if i % 3 == 1 else f'<a href="https://www.testplumbing{i:03d}.com">Website</a>',
            accredited='<p class="bds-body">BBB Accredited Business</p>' if i % 2 == 0 else "",
        ))
    return (f"<html><head>{_jsonld(page)}</head><body>"
            f"<div class='search-results-cards'>{''.join(cards)}</div></body></html>")


CHALLENGE = ("<html><head><title>Just a moment...</title></head><body>"
             "<div class='cf-browser-verification'>Enable JavaScript and cookies to continue"
             "</div></body></html>")


class Handler(BaseHTTPRequestHandler):
    requests = 0
    detail_requests = 0
    challenge = False        # flip to serve a Cloudflare interstitial instead

    def do_GET(self):  # noqa: N802
        Handler.requests += 1
        split = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)

        if getattr(Handler, "challenge", False):
            body = CHALLENGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if "/profile/" in split.path:
            index = int(split.path.rsplit("-", 1)[-1])
            body = PROFILE.format(i=index, year=1990 + (index % 20))
        elif split.path.startswith("/search"):
            body = search_page(int(query.get("page", ["1"])[0]))
        else:
            self.send_response(404)
            self.end_headers()
            return

        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


def start_site():
    Handler.requests = 0
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", Handler
