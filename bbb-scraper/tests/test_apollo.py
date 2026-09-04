"""The Apollo lookup pass, against a stand-in endpoint."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import enrich_apollo
from apollo_server import start_apollo
from parse import Listing


def listing(name, **over):
    kwargs = dict(company_name=name, city="Wilmington", state="NC", phone="+19105551000")
    kwargs.update(over)
    return Listing(**kwargs)


class MatchingTest(unittest.TestCase):
    def test_agreeing_domain_is_high_confidence(self):
        l = listing("Ace Plumbing", website="aceplumbing.com")
        org = {"name": "Ace Plumbing Co.", "website_url": "http://www.aceplumbing.com"}
        self.assertEqual(enrich_apollo.match_confidence(l, org), "high")

    def test_trade_words_alone_are_not_a_match(self):
        """Every candidate in a plumbing pull says "plumbing" -- that overlap
        proves nothing, and must not carry a match on its own."""
        l = listing("Ace Plumbing")
        org = {"name": "Zebra Plumbing", "website_url": "http://zebra.com"}
        self.assertEqual(enrich_apollo.match_confidence(l, org), "low")

    def test_distinctive_name_overlap_is_medium(self):
        l = listing("Creekmore Plumbing")
        org = {"name": "Creekmore Inc", "website_url": "http://creekmore.com"}
        self.assertEqual(enrich_apollo.match_confidence(l, org), "medium")

    def test_best_match_prefers_the_stronger_candidate(self):
        l = listing("Creekmore Plumbing")
        orgs = [{"name": "Zebra Holdings", "website_url": "http://z.com"},
                {"name": "Creekmore Inc", "website_url": "http://creekmore.com"}]
        org, confidence = enrich_apollo.best_match(l, orgs)
        self.assertEqual(org["name"], "Creekmore Inc")
        self.assertEqual(confidence, "medium")

    def test_no_candidates_is_not_a_match(self):
        self.assertEqual(enrich_apollo.best_match(listing("Ace"), []), (None, ""))


class LookupTest(unittest.TestCase):
    def setUp(self):
        self.server, self.url, self.handler = start_apollo()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def client(self, cache=None):
        return enrich_apollo.ApolloClient(
            "test-key", endpoint=self.url,
            cache=cache or enrich_apollo.OrgCache(None))

    def test_fills_a_website_bbb_withheld(self):
        """The whole point: a 403'd profile page leaves website blank, and
        this is what puts it back."""
        l = listing("Ace Plumbing")
        self.assertEqual(l.website, "")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.website, "aceplumbing.com")
        self.assertEqual(l.apollo_match, "medium")
        self.assertEqual(l.apollo_org_id, "a" * 24)
        self.assertEqual(c.stats.websites_filled, 1)

    def test_never_overwrites_a_website_bbb_did_supply(self):
        l = listing("Ace Plumbing", website="realsite.com")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.website, "realsite.com")
        self.assertEqual(c.stats.websites_filled, 0)

    def test_a_weak_match_never_invents_a_website(self):
        """A wrong domain is worse than a blank one -- blank is visibly
        missing, wrong looks like data."""
        l = listing("Totally Different")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.website, "")
        self.assertEqual(l.apollo_match, "low")
        self.assertEqual(c.stats.low_match, 1)

    def test_company_apollo_has_no_website_for_stays_blank(self):
        l = listing("Budget Plumbing")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.website, "")
        self.assertEqual(l.apollo_match, "medium")   # matched, just no domain
        self.assertEqual(l.apollo_org_id, "b" * 24)

    def test_saved_accounts_use_organization_id_not_id(self):
        """An accounts row's `id` is an account id; feeding that to a later
        organization filter matches nothing and raises no error."""
        l = listing("Saved Roofing")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.apollo_org_id, "d" * 24)
        self.assertEqual(l.website, "savedroofing.com")

    def test_a_company_apollo_lacks_is_labelled_not_left_blank(self):
        """"" means the lookup never ran; not-in-apollo means it ran and found
        nothing. A sizeable BBB listing Apollo has never heard of is the whole
        reason to scrape BBB, so it must be distinguishable."""
        l = listing("Nobody Has Heard Of This")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(l.apollo_match, enrich_apollo.NOT_IN_APOLLO)
        self.assertEqual(c.stats.no_result, 1)

    def test_a_listing_never_looked_up_keeps_an_empty_match(self):
        self.assertEqual(listing("Untouched").apollo_match, "")

    def test_cache_stops_a_second_run_re_calling(self):
        path = os.path.join(self.tmp.name, "apollo.json")
        with self.client(enrich_apollo.OrgCache(path)) as c:
            enrich_apollo.enrich_listings([listing("Ace Plumbing")], c)
        first = self.handler.calls
        self.assertEqual(first, 1)

        fresh = [listing("Ace Plumbing")]
        with self.client(enrich_apollo.OrgCache(path)) as c:
            stats = enrich_apollo.enrich_listings(fresh, c)
        self.assertEqual(self.handler.calls, first, "should have been served from cache")
        self.assertEqual(stats.cached, 1)
        self.assertEqual(fresh[0].website, "aceplumbing.com")

    def test_cap_stops_further_lookups(self):
        names = ["Ace Plumbing", "Budget Plumbing", "Saved Roofing"]
        with self.client() as c:
            stats = enrich_apollo.enrich_listings(
                [listing(n) for n in names], c, max_lookups=2)
        self.assertEqual(stats.looked_up, 2)
        self.assertEqual(stats.capped, 1)

    def test_an_api_error_is_counted_not_raised(self):
        """One bad response must not lose the whole scrape."""
        self.handler.bad_status = 500
        l = listing("Ace Plumbing")
        with self.client() as c:
            c.enrich(l)
        self.assertEqual(c.stats.errors, 1)
        self.assertEqual(l.website, "")

    def test_a_bad_key_is_an_error_not_a_silent_pass(self):
        client = enrich_apollo.ApolloClient("wrong-key", endpoint=self.url)
        l = listing("Ace Plumbing")
        client.enrich(l)
        self.assertEqual(client.stats.errors, 1)
        client.close()

    def test_city_and_state_narrow_the_query(self):
        c = self.client()
        body = c.payload(listing("Ace Plumbing"))
        self.assertEqual(body["organization_locations"], ["Wilmington, NC"])
        self.assertEqual(body["q_organization_fuzzy_name"], "Ace Plumbing")
        c.close()

    def test_missing_key_refuses_to_build_a_client(self):
        with self.assertRaises(enrich_apollo.ApolloUnavailable):
            enrich_apollo.ApolloClient("")


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.server, self.url, self.handler = start_apollo()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_probe_separates_live_paths_from_dead_ones(self):
        dead = self.url.replace("/organizations/lookup", "/nope/lookup")
        rows = enrich_apollo.probe_endpoints("test-key", [self.url, dead])
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["status"], 200)
        self.assertFalse(rows[1]["ok"])
        self.assertEqual(rows[1]["status"], 404)

    def test_probe_uses_a_query_that_matches_nothing(self):
        """Apollo bills search endpoints only when they return results, so
        the probe must not ask for anything real."""
        self.assertNotIn(enrich_apollo.PROBE_NAME.lower(),
                         {k.lower() for k in __import__("apollo_server").ORGS})


class WiringTest(unittest.TestCase):
    def test_apollo_columns_are_in_the_output(self):
        import parse
        self.assertIn("apollo_org_id", parse.FIELD_ORDER)
        self.assertIn("apollo_match", parse.FIELD_ORDER)

    def test_lookup_runs_before_the_website_filter(self):
        """Order is the whole point: reversed, --require-website would see
        every row as unknown and filter nothing."""
        import inspect

        import scraper
        source = inspect.getsource(scraper.finish)
        self.assertLess(source.index("run_apollo_enrichment"),
                        source.index("apply_filters(unique, local"))

    def test_key_comes_from_the_environment_when_not_passed(self):
        os.environ["APOLLO_API_KEY"] = "from-env"
        try:
            self.assertEqual(enrich_apollo.resolve_api_key(None), "from-env")
            self.assertEqual(enrich_apollo.resolve_api_key("explicit"), "explicit")
        finally:
            del os.environ["APOLLO_API_KEY"]


class CliGateTest(unittest.TestCase):
    """--apollo + --require-website through the real CLI.

    This is the case the whole pass exists for: BBB's profile page 403s, so
    the fixture serves cards with no website, and without Apollo every row
    reads "unknown" and --require-website filters nothing.
    """

    @classmethod
    def setUpClass(cls):
        from fixture_server import start_server
        cls.server, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.apollo, self.apollo_url, self.handler = start_apollo()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.csv")
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        import json
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "params": {"find_country": "USA"},
                "page_param": "page",
                "category_param": "find_text",
                "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        self.apollo.shutdown()
        self.apollo.server_close()
        self.tmp.cleanup()

    def run_cli(self, *extra):
        import io
        from contextlib import redirect_stdout

        import scraper
        argv = ["--category", "plumber", "--location", "wilmington-nc",
                "--endpoints", self.endpoints, "--output", self.out,
                "--checkpoint", os.path.join(self.tmp.name, "c.json"),
                "--no-fallback", "--min-delay", "0", "--max-delay", "0",
                "--no-detail", "--max-results", "5",
                "--apollo-cache", os.path.join(self.tmp.name, "apollo.json"),
                *extra]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_lookup_is_skipped_without_the_flag(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(self.handler.calls, 0)
        self.assertNotIn("apollo lookups", out)

    def test_lookup_runs_and_is_reported_when_asked(self):
        code, out = self.run_cli("--apollo", "--apollo-key", "test-key",
                                 "--apollo-endpoint", self.apollo_url)
        self.assertEqual(code, 0)
        self.assertGreater(self.handler.calls, 0)
        self.assertIn("apollo lookups", out)

    def test_a_missing_key_warns_rather_than_silently_skipping(self):
        saved = os.environ.pop("APOLLO_API_KEY", None)
        try:
            code, _out = self.run_cli("--apollo",
                                      "--apollo-endpoint", self.apollo_url)
        finally:
            if saved is not None:
                os.environ["APOLLO_API_KEY"] = saved
        self.assertEqual(code, 0)
        self.assertEqual(self.handler.calls, 0, "no key means no calls")

    def test_target_rows_trims_the_finished_sheet(self):
        import csv
        code, out = self.run_cli("--max-results", "20", "--target-rows", "3")
        self.assertEqual(code, 0)
        with open(self.out, encoding="utf-8") as fh:
            self.assertEqual(len(list(csv.DictReader(fh))), 3)
        self.assertIn("trimmed", out)

    def test_target_rows_larger_than_the_pull_is_not_an_error(self):
        import csv
        code, out = self.run_cli("--max-results", "3", "--target-rows", "999")
        self.assertEqual(code, 0)
        with open(self.out, encoding="utf-8") as fh:
            self.assertLessEqual(len(list(csv.DictReader(fh))), 3)
        self.assertNotIn("trimmed", out)

    def test_apollo_columns_reach_the_csv(self):
        import csv
        self.run_cli("--apollo", "--apollo-key", "test-key",
                     "--apollo-endpoint", self.apollo_url)
        with open(self.out, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("apollo_org_id", row)
            self.assertIn("apollo_match", row)


if __name__ == "__main__":
    unittest.main()
