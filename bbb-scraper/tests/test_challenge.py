"""A Cloudflare interstitial must never be mistaken for content.

It arrives as HTTP 200 with a full page of markup, so a parser handed one
returns a Listing with every field empty -- which reads as "the site changed
its field names". That misreading cost a whole investigation: the browser
detail path had no challenge check, reported 0% coverage, and pointed at the
parser instead of at Cloudflare.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import api_client
import browser_client
import parse
import search_client

#: Trimmed from the page a real run actually received.
CHALLENGE = """<!DOCTYPE html><html><head><title>Just a moment... |
Better Business Bureau</title><meta http-equiv="refresh" content="390"></head>
<body><div class="main-wrapper"><h1>www.bbb.org</h1>
<p>Verifying you are human. This may take a few seconds.</p>
<div id="cf-please-wait"></div><script>window._cf_chl_opt={cvId:'3'};</script>
<noscript>Enable JavaScript and cookies to continue</noscript></div></body></html>"""

REAL_PROFILE = """<!DOCTYPE html><html><head><title>Whitman's Contracting |
Better Business Bureau Profile</title></head><body>
<h1>Whitman's Contracting &amp; Roofing</h1>
<div>Business Started: 3/1/2009</div><div>Number of Employees: 24</div>
</body></html>"""


class MarkerTest(unittest.TestCase):
    def test_the_real_challenge_page_is_recognised(self):
        self.assertTrue(parse.looks_challenged(CHALLENGE))

    def test_a_real_profile_is_not_flagged(self):
        self.assertFalse(parse.looks_challenged(REAL_PROFILE))

    def test_empty_html_is_not_a_challenge(self):
        self.assertFalse(parse.looks_challenged(""))
        self.assertFalse(parse.looks_challenged(None))

    def test_only_the_head_of_the_page_is_scanned(self):
        """A business legitimately named in a way that trips a marker, far down
        a long page, must not read as a challenge."""
        page = "<html><title>Acme</title>" + ("x" * 8000) + "just a moment</html>"
        self.assertFalse(parse.looks_challenged(page))


class SharedImplementationTest(unittest.TestCase):
    """One definition, so the two fetch paths cannot drift apart -- the drift
    is what let the detail path ship with no check at all."""

    def test_search_client_uses_the_shared_check(self):
        self.assertIs(search_client.looks_challenged, parse.looks_challenged)

    def test_blocked_error_is_shared(self):
        self.assertIs(api_client.BlockedError, parse.BlockedError)

    def test_the_browser_path_does_not_need_the_http_client(self):
        """browser_client must not pull in httpx to know it was challenged."""
        path = os.path.join(os.path.dirname(HERE), "browser_client.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("import api_client", source)
        self.assertNotIn("from api_client", source)


class BrowserDetailTest(unittest.TestCase):
    """fetch_detail refuses a challenge rather than parsing it."""

    class FakePage:
        def __init__(self, html):
            self.html = html
            self.visited = []

        def goto(self, url, **kw):
            self.visited.append(url)

        def content(self):
            return self.html

    def client_with(self, html):
        client = browser_client.BrowserClient.__new__(browser_client.BrowserClient)
        client._page = self.FakePage(html)
        client.base_url = "https://www.bbb.org"
        client.verbose = False
        client.challenged = 0
        client.min_delay = 0
        client.max_delay = 0
        return client

    def test_a_challenge_raises_instead_of_returning_an_empty_listing(self):
        client = self.client_with(CHALLENGE)
        with self.assertRaises(parse.BlockedError) as caught:
            client.fetch_detail("https://www.bbb.org/us/tx/houston/profile/x/y")
        self.assertIn("challenge", str(caught.exception).lower())
        self.assertEqual(client.challenged, 1)

    def test_the_error_says_how_to_get_past_it(self):
        client = self.client_with(CHALLENGE)
        with self.assertRaises(parse.BlockedError) as caught:
            client.fetch_detail("https://www.bbb.org/us/tx/houston/profile/x/y")
        self.assertIn("--headed", str(caught.exception))

    def test_a_real_page_is_parsed_normally(self):
        client = self.client_with(REAL_PROFILE)
        listing = client.fetch_detail("https://www.bbb.org/us/tx/houston/profile/x/y")
        self.assertIsNotNone(listing)
        self.assertEqual(client.challenged, 0)


class ChannelTest(unittest.TestCase):
    """Playwright's bundled Chrome for Testing is fingerprinted by Cloudflare
    and hangs on a challenge that never resolves; an installed Chrome is the
    way round it."""

    def build(self, **kw):
        return browser_client.BrowserClient(**kw)

    def test_channel_defaults_to_unset(self):
        self.assertIsNone(self.build().channel)

    def test_channel_can_be_passed(self):
        self.assertEqual(self.build(channel="chrome").channel, "chrome")

    def test_channel_comes_from_the_environment_too(self):
        os.environ["BBB_BROWSER_CHANNEL"] = "msedge"
        try:
            self.assertEqual(self.build().channel, "msedge")
        finally:
            del os.environ["BBB_BROWSER_CHANNEL"]

    def test_an_explicit_channel_beats_the_environment(self):
        os.environ["BBB_BROWSER_CHANNEL"] = "msedge"
        try:
            self.assertEqual(self.build(channel="chrome").channel, "chrome")
        finally:
            del os.environ["BBB_BROWSER_CHANNEL"]

    def test_channel_and_executable_path_are_not_both_sent(self):
        """Playwright rejects a launch carrying both."""
        path = os.path.join(os.path.dirname(HERE), "browser_client.py")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("def start(self)")
        body = source[start:source.index("def _apply_stealth")]
        self.assertIn('elif self.executable_path:', body,
                      "executable_path must be an elif after channel, not a "
                      "second independent branch")


if __name__ == "__main__":
    unittest.main()
