"""Owner/email enrichment, the headcount gate, and the credit governor."""

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import apollo_people
from parse import Listing
from people_server import start_people


def listing(name, org_id, city="Wilmington", state="NC", website="x.com"):
    return Listing(company_name=name, apollo_org_id=org_id, city=city,
                   state=state, website=website)


class GovernorTest(unittest.TestCase):
    def test_spend_is_measured_from_the_balance_not_estimated(self):
        g = apollo_people.CreditGovernor(cap=10)
        g.observe(100)
        g.observe(94)
        self.assertEqual(g.spent, 6)
        self.assertEqual(g.remaining, 4)

    def test_stops_once_the_cap_is_reached(self):
        g = apollo_people.CreditGovernor(cap=5)
        g.observe(100)
        g.observe(95)
        with self.assertRaises(apollo_people.CreditCapReached):
            g.check()

    def test_under_the_cap_does_not_stop(self):
        g = apollo_people.CreditGovernor(cap=5)
        g.observe(100)
        g.observe(97)
        g.check()   # must not raise

    def test_an_unreadable_balance_does_not_block_the_run(self):
        """A reporting hiccup must not stop every list; the caller flags it."""
        g = apollo_people.CreditGovernor(cap=5)
        g.observe(None)
        g.check()
        self.assertEqual(g.spent, 0)

    def test_a_balance_that_goes_up_is_not_negative_spend(self):
        g = apollo_people.CreditGovernor(cap=5)
        g.observe(100)
        g.observe(120)   # a top-up mid-run
        self.assertEqual(g.spent, 0)


class GuardTest(unittest.TestCase):
    def test_masked_last_name_is_not_a_name(self):
        self.assertTrue(apollo_people.is_masked("M****"))
        self.assertFalse(apollo_people.is_masked("Reyes"))

    def test_matching_city_is_the_same_place(self):
        l = listing("Ace", "a" * 24)
        person = {"organization": {"city": "Wilmington", "state": "NC"}}
        self.assertIs(apollo_people.same_place(l, person), True)

    def test_a_different_state_is_not_the_same_place(self):
        l = listing("Ace", "a" * 24)
        person = {"organization": {"city": "Detroit", "state": "MI"}}
        self.assertIs(apollo_people.same_place(l, person), False)

    def test_no_location_is_unknown_not_agreement(self):
        self.assertIsNone(apollo_people.same_place(listing("Ace", "a" * 24),
                                                   {"organization": {}}))

    def test_state_matches_as_a_token_not_a_substring(self):
        """'ks' must not match 'Kansas City, MO' by living inside a word."""
        l = listing("Ace", "a" * 24, city="Wichita", state="KS")
        person = {"organization": {"city": "Kansas City", "state": "MO"}}
        self.assertIs(apollo_people.same_place(l, person), False)

    def test_headcount_reads_through_missing_values(self):
        self.assertEqual(apollo_people.headcount(
            {"organization": {"estimated_num_employees": 12}}), 12)
        self.assertIsNone(apollo_people.headcount({"organization": {}}))
        self.assertIsNone(apollo_people.headcount(
            {"organization": {"estimated_num_employees": "n/a"}}))


class EnrichTest(unittest.TestCase):
    def setUp(self):
        self.server, self.base, self.handler = start_people()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def client(self, cap=1000, min_employees=5, cache=None):
        return apollo_people.PeopleClient(
            "test-key", base_url=self.base, min_employees=min_employees,
            governor=apollo_people.CreditGovernor(cap=cap),
            cache=cache or apollo_people.PeopleCache(None))

    def test_finds_the_owner_and_the_email(self):
        with self.client() as c:
            rows = apollo_people.enrich_listings([listing("Ace Plumbing", "a" * 24)], c)
        row = list(rows.values())[0]
        self.assertEqual(row["owner_first_name"], "Dana")
        self.assertEqual(row["owner_last_name"], "Reyes")
        self.assertEqual(row["email"], "dana@aceplumbing.com")
        self.assertEqual(row["apollo_employees"], 27)

    def test_owner_outranks_operations_manager(self):
        """Both work at Ace; the more senior title should win."""
        with self.client() as c:
            person = c.find_person(listing("Ace Plumbing", "a" * 24))
        self.assertEqual(person["title"], "Owner")

    def test_a_company_in_another_state_never_yields_its_email(self):
        """The failure that showed up on the real Wichita list."""
        with self.client() as c:
            rows = apollo_people.enrich_listings(
                [listing("Faraway Plumbing", "d" * 24)], c)
        row = list(rows.values())[0]
        self.assertNotIn("email", row)
        self.assertIn("REVIEW", row["notes"])
        self.assertEqual(c.stats.wrong_place, 1)

    def test_a_company_under_the_headcount_bar_is_dropped(self):
        with self.client(min_employees=5) as c:
            rows = apollo_people.enrich_listings([listing("Tiny Shop", "c" * 24)], c)
        row = list(rows.values())[0]
        self.assertNotIn("email", row)
        self.assertIn("2 employees", row["notes"])
        self.assertEqual(c.stats.too_small, 1)

    def test_unknown_headcount_is_kept_and_flagged_not_dropped(self):
        """Unknown is not absent -- dropping these loses real leads silently."""
        with self.client(min_employees=5) as c:
            rows = apollo_people.enrich_listings([listing("Budget", "b" * 24)], c)
        row = list(rows.values())[0]
        self.assertEqual(row["email"], "pat@budget.com")
        self.assertIn("headcount unknown", row["notes"])
        self.assertEqual(c.stats.size_unknown, 1)
        self.assertEqual(c.stats.too_small, 0)

    def test_a_masked_last_name_is_left_blank_and_flagged(self):
        with self.client() as c:
            rows = apollo_people.enrich_listings([listing("Masked Co", "e" * 24)], c)
        row = list(rows.values())[0]
        self.assertEqual(row["owner_last_name"], "")
        self.assertIn("masked", row["notes"])

    def test_a_company_with_no_people_is_counted_not_crashed(self):
        with self.client() as c:
            rows = apollo_people.enrich_listings(
                [listing("Nobody", "f" * 24)], c)
        self.assertEqual(rows, {})
        self.assertEqual(c.stats.no_person, 1)

    def test_a_listing_without_an_org_id_is_skipped(self):
        with self.client() as c:
            rows = apollo_people.enrich_listings([listing("No Org", "")], c)
        self.assertEqual(rows, {})
        self.assertEqual(self.handler.searches, 0, "should not have searched")

    def test_the_credit_cap_stops_the_run(self):
        self.handler.cost_per_match = 50
        many = [listing(f"C{i}", "a" * 24, website=f"c{i}.com") for i in range(30)]
        with self.client(cap=60) as c:
            apollo_people.enrich_listings(many, c)
        self.assertTrue(c.stats.cap_hit)
        self.assertLess(self.handler.matches, 3, "should have stopped early")

    def test_an_unreadable_balance_is_reported_not_hidden(self):
        """Silently not enforcing a budget is worse than not having one."""
        self.handler.profile_broken = True
        with self.client(cap=1) as c:
            apollo_people.enrich_listings([listing("Ace", "a" * 24)], c)
        self.assertTrue(c.stats.cap_unverified)
        self.assertTrue(any("cap was not enforced" in n for n in c.stats.notes))

    def test_search_results_are_cached_between_runs(self):
        path = os.path.join(self.tmp.name, "people.json")
        with self.client(cache=apollo_people.PeopleCache(path)) as c:
            apollo_people.enrich_listings([listing("Ace", "a" * 24)], c)
        first = self.handler.searches
        with self.client(cache=apollo_people.PeopleCache(path)) as c:
            apollo_people.enrich_listings([listing("Ace", "a" * 24)], c)
        self.assertEqual(self.handler.searches, first, "search should be cached")

    def test_batches_respect_apollos_ten_per_request_ceiling(self):
        many = [listing(f"C{i}", "a" * 24, website=f"c{i}.com") for i in range(25)]
        with self.client() as c:
            apollo_people.enrich_listings(many, c)
        self.assertEqual(self.handler.matches, 3, "25 people -> 3 batches of <=10")

    def test_no_email_is_ever_invented(self):
        """Every emitted address must have come from the API."""
        with self.client() as c:
            rows = apollo_people.enrich_listings(
                [listing("Ace Plumbing", "a" * 24),
                 listing("Faraway Plumbing", "d" * 24),
                 listing("Tiny Shop", "c" * 24)], c)
        emails = {r.get("email") for r in rows.values() if r.get("email")}
        self.assertEqual(emails, {"dana@aceplumbing.com"})

    def test_missing_key_refuses_to_build_a_client(self):
        with self.assertRaises(apollo_people.ApolloPeopleUnavailable):
            apollo_people.PeopleClient("")


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.server, self.base, self.handler = start_people()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_probe_reports_each_path(self):
        rows = apollo_people.probe_paths("test-key", [self.base])
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["ok"] for r in rows), rows)

    def test_probe_marks_a_dead_prefix(self):
        rows = apollo_people.probe_paths("test-key", [self.base + "/nope"])
        self.assertTrue(all(not r["ok"] for r in rows))

    def test_probe_spends_nothing(self):
        """bulk_match bills per successful match, so the probe must miss."""
        before = self.handler.balance
        apollo_people.probe_paths("test-key", [self.base])
        self.assertEqual(self.handler.balance, before)

    def test_probe_person_matches_nobody_in_the_fixture(self):
        from people_server import MATCHES
        self.assertNotIn(apollo_people.PROBE_ORG_ID, MATCHES)
        names = {(m["first_name"], m["last_name"]) for m in MATCHES.values()}
        self.assertNotIn(
            (apollo_people.PROBE_PERSON["first_name"],
             apollo_people.PROBE_PERSON["last_name"]), names)



class DeprecationTest(unittest.TestCase):
    """Apollo answers a deprecated route with 422 and a message naming its
    replacement. A 422 reads as "your payload is wrong", so without reading
    the body the route looks alive and the caller looks at fault -- which is
    how /mixed_people/search went a full run before anyone noticed."""

    def setUp(self):
        self.server, self.base, self.handler = start_people()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_the_live_path_is_the_replacement_apollo_named(self):
        self.assertEqual(apollo_people.PEOPLE_SEARCH, "/mixed_people/api_search")

    def test_a_deprecation_is_reported_as_one(self):
        import json as _json

        class Response:
            status_code = 422
            text = _json.dumps({
                "error": "This endpoint is deprecated for API callers. Please "
                         "use the new mixed_people/api_search endpoint."})

            def json(self):
                return {}

        client = apollo_people.PeopleClient.__new__(apollo_people.PeopleClient)
        client.base_url = "http://x"
        client.api_key = "k"
        client.min_delay = 0

        class Fake:
            def post(self, *a, **kw):
                return Response()

        client.client = Fake()
        with self.assertRaises(RuntimeError) as caught:
            client._post("/mixed_people/search", {})
        message = str(caught.exception)
        self.assertIn("deprecated", message.lower())
        self.assertIn("api_search", message,
                      "the replacement Apollo named must survive into the error")

    def test_a_normal_error_is_not_mislabelled_a_deprecation(self):
        class Response:
            status_code = 500
            text = "internal error"

            def json(self):
                return {}

        client = apollo_people.PeopleClient.__new__(apollo_people.PeopleClient)
        client.base_url = "http://x"
        client.api_key = "k"
        client.min_delay = 0

        class Fake:
            def post(self, *a, **kw):
                return Response()

        client.client = Fake()
        with self.assertRaises(RuntimeError) as caught:
            client._post("/x", {})
        self.assertNotIn("deprecated", str(caught.exception).lower())

    def test_a_real_search_still_works_end_to_end(self):
        client = apollo_people.PeopleClient(
            "test-key", base_url=self.base, min_employees=5,
            governor=apollo_people.CreditGovernor(cap=1000),
            cache=apollo_people.PeopleCache(None))
        rows = apollo_people.enrich_listings(
            [listing("Ace Plumbing", "a" * 24)], client)
        client.close()
        self.assertEqual(list(rows.values())[0]["email"], "dana@aceplumbing.com")


if __name__ == "__main__":
    unittest.main()
