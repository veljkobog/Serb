"""Approach A against a stand-in BBB: rendered search pages, not an XHR API."""

import csv
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import api_client
import scraper
import search_client
from bbb_site import start_site


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class SearchUrlTest(unittest.TestCase):
    def test_matches_the_shape_of_a_captured_url(self):
        url = search_client.build_search_url("plumber", "wichita-ks")
        self.assertIn("https://www.bbb.org/search?", url)
        self.assertIn("find_country=USA", url)
        self.assertIn("find_text=Plumber", url)
        self.assertIn("find_loc=Wichita%2C+KS", url)   # "Wichita, KS"
        self.assertNotIn("page=", url)                 # page 1 carries no page param

    def test_multiword_category_and_city(self):
        url = search_client.build_search_url("heating-and-air-conditioning",
                                             "winston-salem-nc", page=3)
        self.assertIn("find_text=Heating+and+Air+Conditioning", url)
        self.assertIn("find_loc=Winston+Salem%2C+NC", url)
        self.assertIn("page=3", url)

    def test_find_entity_is_optional(self):
        plain = search_client.build_search_url("plumber", "wichita-ks")
        pinned = search_client.build_search_url("plumber", "wichita-ks", entity="10113-000")
        self.assertNotIn("find_entity", plain)
        self.assertIn("find_entity=10113-000", pinned)

    def test_challenge_detection(self):
        self.assertTrue(search_client.looks_challenged(
            "<html><title>Just a moment...</title>"))
        self.assertTrue(search_client.looks_challenged(
            "<html><body>Enable JavaScript and cookies to continue</body>"))
        self.assertFalse(search_client.looks_challenged(
            '<html><script type="application/ld+json">{"@type":"ItemList"}</script>'))


class SearchClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_site()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.handler.challenge = False

    def client(self):
        return search_client.SearchClient(
            limiter=api_client.RateLimiter(0, 0), base_url=self.base_url)

    def test_probe_finds_listings(self):
        with self.client() as client:
            self.assertEqual(client.probe("plumber", "wilmington-nc"), 5)

    def test_pagination_walks_then_stops(self):
        with self.client() as client:
            pages = list(client.iter_pages("plumber", "wilmington-nc", max_pages=10))
        self.assertEqual([page for page, _ in pages], [1, 2, 3])
        listings = [l for _page, batch in pages for l in batch]
        self.assertEqual(len(listings), 15)

    def test_fields_come_off_the_json_ld(self):
        with self.client() as client:
            _page, listings = next(client.iter_pages("plumber", "wilmington-nc"))
        first = listings[0]
        self.assertEqual(first.company_name, "Test Plumbing 000")
        self.assertEqual(first.phone, "+19105551000")
        self.assertEqual(first.city, "Wilmington")
        self.assertEqual(first.state, "NC")
        self.assertEqual(first.zip, "28401")
        self.assertEqual(first.category, "plumber")

    def test_service_area_listings_have_no_street(self):
        with self.client() as client:
            _page, listings = next(client.iter_pages("plumber", "wilmington-nc"))
        self.assertTrue(any(not l.street for l in listings))
        self.assertTrue(all(l.city for l in listings))

    def test_cloudflare_challenge_raises_rather_than_returning_nothing(self):
        """A challenge is a 200 with no results -- silence would look like 'no plumbers here'."""
        self.handler.challenge = True
        with self.client() as client:
            with self.assertRaises(api_client.BlockedError) as ctx:
                client.fetch_page("plumber", "wilmington-nc", 1)
        self.assertIn("challenge", str(ctx.exception).lower())

    def test_probe_reports_zero_when_challenged(self):
        self.handler.challenge = True
        with self.client() as client:
            self.assertEqual(client.probe("plumber", "wilmington-nc"), 0)

    def test_detail_fetch_uses_the_same_session(self):
        with self.client() as client:
            detail = client.fetch_detail(
                f"{self.base_url}/us/nc/wilmington/profile/plumber/test-000")
        self.assertIsNotNone(detail)
        self.assertEqual(detail.employees, 27)      # only on the profile page
        self.assertEqual(detail.bbb_reviews, 44)


class LiveRunTest(unittest.TestCase):
    """The CLI end to end over rendered pages, with no endpoint config at all."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_site()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.handler.challenge = False
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = ["--category", "plumber", "--location", "wilmington-nc",
                "--base-url", self.base_url, "--output", self.out,
                "--checkpoint", os.path.join(self.tmp.name, "ck.json"),
                "--min-delay", "0", "--max-delay", "0", "--no-resume",
                "--no-fallback", *extra]
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = scraper.main(argv)
        return code, buf.getvalue() + err.getvalue()

    def test_run_without_any_endpoint_config(self):
        code, output = self.run_cli("--max-results", "20", "--no-detail")
        self.assertEqual(code, 0, output)
        self.assertIn("reading rendered search pages", output)
        self.assertIn("A (rendered html)", output)
        rows = read_csv(self.out)
        self.assertEqual(len(rows), 15)
        self.assertTrue(all(r["company_name"] and r["phone"] for r in rows))
        self.assertTrue(all(r["category"] == "plumber" for r in rows))

    def test_detail_pass_fills_the_commercial_fields(self):
        code, output = self.run_cli("--max-results", "5")
        self.assertEqual(code, 0, output)
        self.assertIn("detail page(s)", output)
        rows = read_csv(self.out)
        self.assertTrue(rows)
        filled = [r for r in rows if r["employees"]]
        self.assertTrue(filled, "detail pass populated nothing")
        self.assertEqual(filled[0]["employees"], "27")
        self.assertEqual(filled[0]["bbb_rating"], "A+")

    def test_challenge_is_reported_not_swallowed(self):
        self.handler.challenge = True
        code, output = self.run_cli("--max-results", "10")
        self.assertEqual(code, 1)
        self.assertIn("Approach A unavailable", output)

    def test_min_years_filter_over_live_shaped_data(self):
        code, output = self.run_cli("--max-results", "10", "--min-years", "20")
        self.assertEqual(code, 0, output)
        for row in read_csv(self.out):
            if row["years_in_business"]:
                self.assertGreaterEqual(int(row["years_in_business"]), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
