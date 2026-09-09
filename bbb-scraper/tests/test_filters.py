"""Size / quality filters and Google enrichment -- all offline."""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import enrich_google
import scraper
from parse import Listing
from fixture_server import start_server
from places_server import start_places_server


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class FilterUnitTest(unittest.TestCase):
    def args(self, **over):
        """Real parser defaults, overridden per test.

        Hand-listing the fields meant every new filter flag broke six
        unrelated tests with an AttributeError, which says nothing about the
        thing under test. Taking the defaults from the parser itself keeps the
        helper honest and makes a genuinely missing flag fail loudly.
        """
        args = scraper.build_parser().parse_args([])
        for name, value in over.items():
            if not hasattr(args, name):
                raise AttributeError(f"no such scraper flag: {name}")
            setattr(args, name, value)
        return args

    def test_unknown_passes_by_default(self):
        filters = scraper.build_filters(self.args(min_bbb_reviews=50))
        kept, rejected = scraper.apply_filters(
            [Listing(company_name="unknown"), Listing(company_name="small", bbb_reviews=3),
             Listing(company_name="big", bbb_reviews=200)],
            filters, drop_unknown=False,
        )
        self.assertEqual([l.company_name for l in kept], ["unknown", "big"])
        self.assertEqual([l.company_name for l in rejected], ["small"])
        self.assertEqual(filters[0].dropped, 1)
        self.assertEqual(filters[0].unknown_dropped, 0)

    def test_drop_unknown_is_strict(self):
        filters = scraper.build_filters(self.args(min_bbb_reviews=50))
        kept, rejected = scraper.apply_filters(
            [Listing(company_name="unknown"), Listing(company_name="big", bbb_reviews=200)],
            filters, drop_unknown=True,
        )
        self.assertEqual([l.company_name for l in kept], ["big"])
        self.assertEqual(filters[0].unknown_dropped, 1)
        self.assertEqual(len(rejected), 1)

    def test_max_complaints(self):
        filters = scraper.build_filters(self.args(max_bbb_complaints=2))
        kept, _ = scraper.apply_filters(
            [Listing(company_name="clean", bbb_complaints=0),
             Listing(company_name="ok", bbb_complaints=2),
             Listing(company_name="messy", bbb_complaints=9)],
            filters, drop_unknown=False,
        )
        self.assertEqual([l.company_name for l in kept], ["clean", "ok"])

    def test_employee_range_uses_low_end(self):
        filters = scraper.build_filters(self.args(min_employees=11))
        listing = Listing(company_name="A")
        listing.employees = 11   # "11-50" parses to 11
        kept, _ = scraper.apply_filters([listing], filters, False)
        self.assertEqual(len(kept), 1)

        filters = scraper.build_filters(self.args(min_employees=12))
        kept, _ = scraper.apply_filters([listing], filters, False)
        self.assertEqual(kept, [])

    def test_low_confidence_google_match_counts_as_unknown(self):
        weak = Listing(company_name="A", google_reviews=5000, google_match="low")
        strong = Listing(company_name="B", google_reviews=300, google_match="high")

        filters = scraper.build_filters(self.args(min_google_reviews=100))
        kept, _ = scraper.apply_filters([weak, strong], filters, drop_unknown=False)
        self.assertEqual([l.company_name for l in kept], ["A", "B"])   # weak passes as unknown

        filters = scraper.build_filters(self.args(min_google_reviews=100))
        kept, _ = scraper.apply_filters([weak, strong], filters, drop_unknown=True)
        self.assertEqual([l.company_name for l in kept], ["B"])        # strict drops the weak one

        filters = scraper.build_filters(self.args(min_google_reviews=100, allow_low_match=True))
        kept, _ = scraper.apply_filters([weak, strong], filters, drop_unknown=True)
        self.assertEqual([l.company_name for l in kept], ["A", "B"])   # opted in, so it counts

    def test_detail_fields_follow_active_filters(self):
        self.assertEqual(scraper.detail_fields_for(self.args(), []),
                         {"years_in_business", "accredited"})
        filters = scraper.build_filters(self.args(min_employees=10, min_bbb_reviews=5))
        self.assertEqual(
            scraper.detail_fields_for(self.args(min_employees=10, min_bbb_reviews=5), filters),
            {"years_in_business", "accredited", "employees", "bbb_reviews"},
        )
        # Google fields never trigger a BBB detail visit -- they aren't on BBB
        filters = scraper.build_filters(self.args(min_google_reviews=100))
        self.assertEqual(
            scraper.detail_fields_for(self.args(min_google_reviews=100), filters),
            {"years_in_business", "accredited"},
        )

    def test_needs_detail_respects_required_set(self):
        listing = Listing(company_name="A", years_in_business=5, accredited=True)
        self.assertFalse(listing.needs_detail())
        self.assertTrue(listing.needs_detail({"years_in_business", "employees"}))


class GoogleMatchTest(unittest.TestCase):
    def test_domain_match_is_high(self):
        listing = Listing(company_name="Acme Plumbing LLC", website="acmeplumbing.com")
        place = {"displayName": {"text": "Totally Different Name"},
                 "websiteUri": "https://www.acmeplumbing.com/"}
        self.assertEqual(enrich_google.match_confidence(listing, place), "high")

    def test_phone_match_is_high(self):
        listing = Listing(company_name="Acme", phone="+19105550134")
        place = {"displayName": {"text": "Acme"}, "nationalPhoneNumber": "(910) 555-0134"}
        self.assertEqual(enrich_google.match_confidence(listing, place), "high")

    def test_name_and_city_is_medium(self):
        listing = Listing(company_name="Acme Plumbing LLC", city="Wilmington")
        place = {"displayName": {"text": "Acme Plumbing"},
                 "formattedAddress": "12 Market St, Wilmington, NC"}
        self.assertEqual(enrich_google.match_confidence(listing, place), "medium")

    def test_unrelated_is_low(self):
        listing = Listing(company_name="Acme Plumbing", city="Wilmington")
        place = {"displayName": {"text": "Bobs Roofing"},
                 "formattedAddress": "9 Other Rd, Raleigh, NC"}
        self.assertEqual(enrich_google.match_confidence(listing, place), "low")

    def test_legal_suffixes_ignored(self):
        listing = Listing(company_name="Acme Plumbing, Inc.", city="Wilmington")
        place = {"displayName": {"text": "Acme Plumbing LLC"},
                 "formattedAddress": "12 Market St, Wilmington, NC"}
        self.assertEqual(enrich_google.match_confidence(listing, place), "medium")


class PlacesClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.endpoint, cls.handler = start_places_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.handler.requests = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self.tmp.name, "cache.json")

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, cache=None):
        return enrich_google.PlacesClient("test-key", endpoint=self.endpoint,
                                          cache=cache or enrich_google.PlacesCache(None))

    def test_enrich_fills_fields_with_high_confidence(self):
        listing = Listing(company_name="Test Plumbing 007", website="testplumbing007.com",
                          city="Wilmington", state="NC")
        with self.client() as client:
            client.enrich(listing)
        self.assertEqual(listing.google_place_id, "place-007")
        self.assertEqual(listing.google_match, "high")
        self.assertIsNotNone(listing.google_rating)
        self.assertIsNotNone(listing.google_reviews)

    def test_wrong_neighbour_is_flagged_low(self):
        listing = Listing(company_name="No Contact Co 005", city="Wilmington", state="NC")
        with self.client() as client:
            client.enrich(listing)
        self.assertEqual(listing.google_match, "low")
        self.assertEqual(listing.google_reviews, 5000)   # captured, but not trusted

    def test_cache_prevents_a_second_bill(self):
        listings = [Listing(company_name="Test Plumbing 007", website="testplumbing007.com",
                            profile_url="https://www.bbb.org/p/7", city="Wilmington")]
        cache = enrich_google.PlacesCache(self.cache_path)
        with self.client(cache) as client:
            enrich_google.enrich_listings(listings, client)
        self.assertEqual(self.handler.requests, 1)

        fresh = [Listing(company_name="Test Plumbing 007", website="testplumbing007.com",
                         profile_url="https://www.bbb.org/p/7", city="Wilmington")]
        cache2 = enrich_google.PlacesCache(self.cache_path)
        with self.client(cache2) as client:
            stats = enrich_google.enrich_listings(fresh, client)
        self.assertEqual(self.handler.requests, 1, "cached lookup was re-billed")
        self.assertEqual(stats.cached, 1)
        self.assertEqual(fresh[0].google_place_id, "place-007")

    def test_expired_cache_refetches(self):
        listing = Listing(company_name="Test Plumbing 007", profile_url="https://www.bbb.org/p/7")
        cache = enrich_google.PlacesCache(self.cache_path, ttl_days=30)
        with self.client(cache) as client:
            client.enrich(listing)
        self.assertEqual(self.handler.requests, 1)

        import time
        stale = enrich_google.PlacesCache(self.cache_path, ttl_days=30,
                                          now=time.time() + 31 * 86400)
        with self.client(stale) as client:
            client.enrich(listing)
        self.assertEqual(self.handler.requests, 2)

    def test_lookup_cap(self):
        listings = [Listing(company_name=f"Test Plumbing {i:03d}", profile_url=f"/p/{i}")
                    for i in range(10)]
        with self.client() as client:
            stats = enrich_google.enrich_listings(listings, client, max_lookups=3)
        self.assertEqual(self.handler.requests, 3)
        self.assertEqual(stats.looked_up, 3)
        self.assertEqual(stats.capped, 7)

    def test_api_error_is_survivable(self):
        client = enrich_google.PlacesClient("k", endpoint="http://127.0.0.1:1/v1/places:searchText")
        listing = Listing(company_name="Test Plumbing 001")
        try:
            client.enrich(listing)
        finally:
            client.close()
        self.assertEqual(client.stats.errors, 1)
        self.assertEqual(listing.google_match, "")

    def test_missing_key_is_refused_clearly(self):
        with self.assertRaises(enrich_google.GoogleUnavailable):
            enrich_google.PlacesClient("")


class EndToEndFilterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = start_server()
        cls.places, cls.places_endpoint, cls.places_handler = start_places_server()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.places):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.csv")
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "page_param": "page", "category_param": "find_text", "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = [
            "--category", "plumber", "--location", "wilmington-nc", "--max-results", "100",
            "--endpoints", self.endpoints, "--output", self.out,
            "--checkpoint", os.path.join(self.tmp.name, "ck.json"),
            "--no-fallback", "--min-delay", "0", "--max-delay", "0", "--no-detail",
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def run_cli_with_detail(self, *extra):
        argv = [
            "--category", "plumber", "--location", "wilmington-nc", "--max-results", "20",
            "--endpoints", self.endpoints, "--output", self.out,
            "--checkpoint", os.path.join(self.tmp.name, "ckd.json"),
            "--no-fallback", "--min-delay", "0", "--max-delay", "0",
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_bbb_review_filter_cuts_the_tail(self):
        baseline, _ = self.run_cli()
        self.assertEqual(baseline, 0)
        before = len(read_csv(self.out))

        code, output = self.run_cli("--min-bbb-reviews", "24")
        self.assertEqual(code, 0)
        rows = read_csv(self.out)
        self.assertLess(len(rows), before, "filter kept everything")
        for row in rows:
            if row["bbb_reviews"]:
                self.assertGreaterEqual(int(row["bbb_reviews"]), 24)
        self.assertIn("filtered min-bbb-reviews", output)

    def test_rejects_file_captures_the_drops(self):
        rejects = os.path.join(self.tmp.name, "rejects.csv")
        code, _ = self.run_cli("--min-bbb-reviews", "24", "--rejects", rejects)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(rejects))
        kept = {r["profile_url"] for r in read_csv(self.out)}
        dropped = {r["profile_url"] for r in read_csv(rejects)}
        self.assertTrue(dropped)
        self.assertFalse(kept & dropped)

    def test_drop_unknown_removes_blank_employees(self):
        code, output = self.run_cli("--min-employees", "11", "--drop-unknown")
        self.assertEqual(code, 0)
        for row in read_csv(self.out):
            self.assertTrue(row["employees"])
            self.assertGreaterEqual(int(row["employees"]), 11)
        self.assertIn("unknown)", output)

    def test_api_path_fetches_detail_pages_for_filtered_fields(self):
        """Approach A must fetch the fields it filters on, not assume them."""
        from fixture_server import Handler as SearchHandler
        SearchHandler.detail_requests = 0

        # profile_url is relative to bbb.org in the fixture, so point detail
        # fetches back at the local server
        import parse as parse_mod
        original_base = parse_mod.BBB_BASE
        parse_mod.BBB_BASE = self.base_url
        try:
            code, output = self.run_cli_with_detail("--min-employees", "20", "--drop-unknown")
        finally:
            parse_mod.BBB_BASE = original_base

        self.assertEqual(code, 0, output)
        self.assertGreater(SearchHandler.detail_requests, 0, "no detail pages were fetched")
        rows = read_csv(self.out)
        self.assertTrue(rows)
        for row in rows:
            self.assertGreaterEqual(int(row["employees"]), 20)   # 25, from the profile page

    def test_no_detail_warns_when_a_filter_needs_a_detail_field(self):
        buf = io.StringIO()
        err = io.StringIO()
        import contextlib
        with redirect_stdout(buf), contextlib.redirect_stderr(err):
            scraper.main([
                "--category", "plumber", "--location", "wilmington-nc", "--max-results", "10",
                "--endpoints", self.endpoints, "--output", self.out,
                "--checkpoint", os.path.join(self.tmp.name, "ck2.json"), "--no-fallback",
                "--min-delay", "0", "--max-delay", "0", "--no-detail",
                "--min-employees", "20",
            ])
        self.assertIn("--no-detail", err.getvalue())
        self.assertIn("employees", err.getvalue())

    def test_google_columns_populated_and_filtered(self):
        code, output = self.run_cli(
            "--google-key", "test-key", "--min-google-reviews", "100",
            "--google-endpoint", self.places_endpoint,
            "--google-cache", os.path.join(self.tmp.name, "gcache.json"),
        )
        self.assertEqual(code, 0, output)
        rows = read_csv(self.out)
        self.assertTrue(rows)
        enriched = [r for r in rows if r["google_match"]]
        self.assertTrue(enriched, "no google matches at all")
        for row in enriched:
            self.assertIn(row["google_match"], {"high", "medium", "low"})
            if row["google_match"] != "low" and row["google_reviews"]:
                self.assertGreaterEqual(int(row["google_reviews"]), 100)
        self.assertIn("google lookups", output)

    def test_google_filter_without_key_warns_and_keeps_going(self):
        env_backup = {k: os.environ.pop(k, None)
                      for k in ("GOOGLE_MAPS_API_KEY", "GOOGLE_PLACES_API_KEY")}
        try:
            code, output = self.run_cli("--min-google-reviews", "100")
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v
        self.assertEqual(code, 0)
        # no enrichment ran, so every google value is unknown and nothing is cut
        self.assertTrue(read_csv(self.out))

    def test_google_endpoint_is_overridable_for_tests(self):
        # the real client points at Google; tests point it somewhere safe
        self.assertTrue(enrich_google.PLACES_URL.startswith("https://places.googleapis.com/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
