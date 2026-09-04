"""Approach B driven by a real Chromium against a stand-in BBB site.

Skipped when Playwright or a browser binary isn't available, so CI (which
installs neither) stays green while these run wherever a browser exists.
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from bbb_site import start_site


def find_browser():
    """An existing Chromium, if the machine has one."""
    explicit = os.environ.get("BBB_BROWSER_EXECUTABLE")
    if explicit and os.path.exists(explicit):
        return explicit
    for candidate in ("/opt/pw-browsers/chromium",
                      "/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("chromium") or shutil.which("google-chrome")


import importlib.util

HAVE_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None

BROWSER = find_browser()
REASON = "playwright or a chromium binary is not available here"


def build_client(base_url, **over):
    import browser_client
    kwargs = dict(
        headless=True, min_delay=0, max_delay=0,
        executable_path=BROWSER, extra_args=["--no-sandbox"],   # --no-sandbox: containers
        base_url=base_url,
    )
    kwargs.update(over)
    return browser_client.BrowserClient(**kwargs)


@unittest.skipUnless(HAVE_PLAYWRIGHT and BROWSER, REASON)
class BrowserApproachTest(unittest.TestCase):
    # Launching Chromium costs tens of seconds, so the read-only tests share
    # one browser; the few that need their own profile or a broken binary
    # build their own client.
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_site()
        cls.class_tmp = tempfile.TemporaryDirectory()
        cls.shared = build_client(cls.base_url, user_data_dir=os.path.join(cls.class_tmp.name, "p"))
        cls.shared.start()
        cls.pages = list(cls.shared.iter_listings("plumber", "wilmington-nc", max_pages=10))
        cls.listings = [l for _page, batch in cls.pages for l in batch]

    @classmethod
    def tearDownClass(cls):
        cls.shared.close()
        cls.class_tmp.cleanup()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    @contextlib.contextmanager
    def isolated_client(self, **over):
        """A second client, with the shared one paused.

        Playwright's sync API allows one instance per thread, so a test that
        needs its own browser has to stand the shared one down first.
        """
        kwargs = {"user_data_dir": os.path.join(self.tmp.name, "profile")}
        kwargs.update(over)
        self.shared.close()
        try:
            yield build_client(self.base_url, **kwargs)
        finally:
            self.shared.start()

    # ------------------------------------------------------------------
    def test_pages_are_walked_and_parsed_from_the_rendered_dom(self):
        self.assertEqual(len(self.pages), 3, "should walk exactly the three real pages")
        self.assertEqual(len(self.listings), 15)

        first = self.listings[0]
        self.assertEqual(first.company_name, "Test Plumbing 000")
        self.assertEqual(first.website, "testplumbing000.com")
        self.assertEqual(first.phone, "+19105551000")
        self.assertEqual(first.city, "Wilmington")
        self.assertEqual(first.state, "NC")
        self.assertEqual(first.zip, "28401")
        self.assertEqual(first.bbb_rating, "A+")
        self.assertTrue(first.accredited)
        self.assertTrue(first.profile_url.endswith("/profile/plumber/test-000"))

    def test_stops_cleanly_at_the_end_of_pagination(self):
        """Asked for 10 pages, three exist: the empty fourth must not raise."""
        self.assertEqual([p for p, _ in self.pages], [1, 2, 3])

    def test_facebook_only_listings_keep_their_social_link(self):
        social_only = [l for l in self.listings if not l.website]
        self.assertTrue(social_only, "the fixture has facebook-only listings")
        for listing in social_only:
            self.assertIn("facebook.com", listing.social_url)
            self.assertTrue(listing.phone, "these are still reachable by phone")

    def test_a_real_domain_beats_the_facebook_link_on_the_same_card(self):
        with_site = [l for l in self.listings if l.website]
        self.assertTrue(with_site)
        for listing in with_site:
            self.assertNotIn("facebook", listing.website)
            self.assertIn("facebook.com", listing.social_url)

    def test_detail_page_fills_what_the_card_omits(self):
        import copy
        listing = copy.deepcopy(self.listings[0])
        self.assertIsNone(listing.employees)              # not on the card
        listing.merge(self.shared.fetch_detail(listing.profile_url))

        self.assertEqual(listing.employees, 27)
        self.assertEqual(listing.bbb_reviews, 44)
        self.assertEqual(listing.bbb_complaints, 1)
        self.assertIsNotNone(listing.years_in_business)

    def test_browser_profile_persists_between_sessions(self):
        """A reused profile dir is what keeps a Cloudflare clearance cookie."""
        profile = os.path.join(self.tmp.name, "shared-profile")
        with self.isolated_client(user_data_dir=profile) as browser:
            with browser:
                browser._page.goto(f"{self.base_url}/search?page=1")
            self.assertTrue(os.path.isdir(profile))
            self.assertTrue(os.listdir(profile), "no profile data was written")

            with browser:                       # reopens the same profile, no crash
                listings = next(browser.iter_listings("plumber", "wilmington-nc"))[1]
            self.assertTrue(listings)

    def test_stealth_shim_hides_the_automation_flag(self):
        self.shared._page.goto(f"{self.base_url}/search?page=1")
        self.assertIsNone(self.shared._page.evaluate("navigator.webdriver"))

    def test_missing_browser_binary_reports_the_fix(self):
        import browser_client
        with self.isolated_client(executable_path="/nonexistent/chromium") as client:
            with self.assertRaises(browser_client.BrowserUnavailable) as ctx:
                client.start()
        self.assertIn("--browser-executable", str(ctx.exception))


@unittest.skipUnless(HAVE_PLAYWRIGHT and BROWSER, REASON)
class BrowserFallbackRunTest(unittest.TestCase):
    """The whole CLI, forced down the Playwright path."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_site()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_browser_run_writes_a_csv(self):
        import csv
        import io
        from contextlib import redirect_stdout

        import browser_client
        import scraper

        original = browser_client.BrowserClient
        base_url = self.base_url

        def patched(**kwargs):
            kwargs.update(executable_path=BROWSER, extra_args=["--no-sandbox"],
                          base_url=base_url)
            return original(**kwargs)

        browser_client.BrowserClient = patched
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "out.csv")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = scraper.main([
                        "--category", "plumber", "--location", "wilmington-nc",
                        "--browser", "--profile-dir", os.path.join(tmp, "profile"),
                        "--output", out, "--checkpoint", os.path.join(tmp, "ck.json"),
                        "--min-delay", "0", "--max-delay", "0", "--max-results", "10",
                        "--no-resume",
                    ])
                output = buf.getvalue()
                with open(out, encoding="utf-8") as fh:
                    rows = list(csv.DictReader(fh))
        finally:
            browser_client.BrowserClient = original

        self.assertEqual(code, 0, output)
        self.assertIn("B (playwright)", output)
        self.assertTrue(rows)
        self.assertLessEqual(len(rows), 10)
        self.assertTrue(all(r["company_name"] for r in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
