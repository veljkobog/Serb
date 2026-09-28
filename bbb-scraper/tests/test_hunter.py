"""Hunter.io: the second opinion on contacts, and what it must never do.

Every test runs against a fake transport. A test suite that reaches Hunter
proves nothing repeatable and spends the plan's quota to do it.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import hunter


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeTransport:
    """Answers the three Hunter paths from canned data, and counts calls."""

    def __init__(self, verify=None, domains=None, account=None, fail=()):
        self.verify = verify or {}
        self.domains = domains or {}
        self.account = account
        self.fail = set(fail)
        self.calls = []

    def get(self, url, params=None):
        params = params or {}
        self.calls.append((url, params))
        if url.endswith(hunter.ACCOUNT_PATH):
            if "account" in self.fail:
                return FakeResponse({"errors": [{"details": "nope"}]}, 401)
            return FakeResponse({"data": self.account or {}})
        if url.endswith(hunter.VERIFY_PATH):
            email = params.get("email", "")
            if email in self.fail:
                return FakeResponse({"errors": [{"details": "boom"}]}, 500)
            return FakeResponse({"data": self.verify.get(email, {"status": "unknown"})})
        if url.endswith(hunter.SEARCH_PATH):
            domain = params.get("domain", "")
            if domain in self.fail:
                return FakeResponse({"errors": [{"details": "boom"}]}, 500)
            return FakeResponse({"data": {"emails": self.domains.get(domain, [])}})
        raise AssertionError(f"unexpected path: {url}")

    def close(self):
        pass


def client(transport, cap=60):
    c = hunter.HunterClient.__new__(hunter.HunterClient)
    c.api_key = "k"
    c.base_url = hunter.DEFAULT_BASE
    c.cache = hunter.HunterCache(None)
    c.cap = cap
    c.spent = 0
    c.min_delay = 0.0
    c.verbose = False
    c.stats = hunter.HunterStats()
    c.client = transport
    c.remaining = c._read_quota()
    return c


PLENTY = {"requests": {"searches": {"used": 0, "available": 500},
                       "verifications": {"used": 0, "available": 500}}}


class PickContactTest(unittest.TestCase):
    def test_an_owner_outranks_a_sales_vp(self):
        picked = hunter.pick_contact([
            {"value": "vp@x.com", "type": "personal", "confidence": 99,
             "first_name": "Dana", "last_name": "Cole",
             "position": "VP of Sales"},
            {"value": "owner@x.com", "type": "personal", "confidence": 80,
             "first_name": "Sam", "last_name": "Reed", "position": "Owner"},
        ])
        self.assertEqual(picked["email"], "owner@x.com")
        self.assertFalse(picked["unranked_title"])

    def test_a_shared_mailbox_never_carries_a_persons_name(self):
        """Greeting a human by name at info@ is how a sequence embarrasses you."""
        picked = hunter.pick_contact([
            {"value": "info@x.com", "type": "generic", "confidence": 95,
             "first_name": "Front", "last_name": "Desk", "position": "Office"},
        ])
        self.assertEqual(picked["email"], "info@x.com")
        self.assertTrue(picked["generic"])
        self.assertEqual(picked["first_name"], "")
        self.assertEqual(picked["last_name"], "")

    def test_a_personal_address_beats_a_shared_one(self):
        picked = hunter.pick_contact([
            {"value": "info@x.com", "type": "generic", "confidence": 99},
            {"value": "sam@x.com", "type": "personal", "confidence": 75,
             "first_name": "Sam", "position": "President"},
        ])
        self.assertEqual(picked["email"], "sam@x.com")

    def test_a_low_confidence_address_is_a_guess_and_is_dropped(self):
        self.assertIsNone(hunter.pick_contact([
            {"value": "maybe@x.com", "type": "personal", "confidence": 20,
             "position": "Owner"},
        ]))

    def test_nothing_found_is_none_not_an_empty_person(self):
        self.assertIsNone(hunter.pick_contact([]))

    def test_an_unranked_title_is_flagged_rather_than_passed_off_as_an_owner(self):
        picked = hunter.pick_contact([
            {"value": "tech@x.com", "type": "personal", "confidence": 90,
             "first_name": "Lee", "position": "Field Technician"},
        ])
        self.assertTrue(picked["unranked_title"])


class VerifyTest(unittest.TestCase):
    def test_a_verdict_and_score_come_back(self):
        t = FakeTransport(account=PLENTY,
                          verify={"a@b.com": {"status": "deliverable", "score": 97}})
        c = client(t)
        self.assertEqual(c.verify("a@b.com"),
                         {"status": "deliverable", "score": 97})
        self.assertEqual(c.stats.deliverable, 1)

    def test_the_same_address_is_never_paid_for_twice(self):
        t = FakeTransport(account=PLENTY,
                          verify={"a@b.com": {"status": "deliverable", "score": 97}})
        c = client(t)
        c.verify("a@b.com")
        c.verify("A@B.com")
        self.assertEqual(c.spent, 1)
        self.assertEqual(c.stats.cached, 1)

    def test_a_failed_lookup_is_not_a_verdict(self):
        """'not-checked' must never read as 'checked and fine'."""
        t = FakeTransport(account=PLENTY, fail={"a@b.com"})
        c = client(t)
        self.assertEqual(c.verify("a@b.com")["status"], hunter.NOT_CHECKED)
        self.assertEqual(c.stats.errors, 1)
        self.assertEqual(c.stats.verified, 0)

    def test_an_empty_address_costs_nothing(self):
        c = client(FakeTransport(account=PLENTY))
        self.assertEqual(c.verify("")["status"], hunter.NOT_CHECKED)
        self.assertEqual(c.spent, 0)


class QuotaTest(unittest.TestCase):
    def test_the_local_cap_stops_the_run(self):
        t = FakeTransport(account=PLENTY, verify={})
        c = client(t, cap=2)
        c.verify("a@b.com")
        c.verify("c@d.com")
        with self.assertRaises(hunter.QuotaReached):
            c.verify("e@f.com")
        self.assertTrue(c.stats.cap_hit)

    def test_the_plans_own_remaining_quota_stops_it_too(self):
        """A cap of 60 is no help on a plan with 1 lookup left."""
        t = FakeTransport(account={"requests": {
            "searches": {"used": 24, "available": 25},
            "verifications": {"used": 0, "available": 50}}})
        c = client(t, cap=60)
        self.assertEqual(c.remaining, 1)
        c.verify("a@b.com")
        with self.assertRaises(hunter.QuotaReached):
            c.verify("c@d.com")

    def test_an_unreadable_quota_fails_open_but_says_so(self):
        """Refusing to run because a reporting endpoint hiccuped costs a whole
        morning. The local cap still applies."""
        c = client(FakeTransport(account=PLENTY, fail={"account"}), cap=2)
        self.assertIsNone(c.remaining)
        self.assertTrue(c.stats.quota_unverified)
        c.verify("a@b.com")
        self.assertEqual(c.spent, 1)


class EnrichRowsTest(unittest.TestCase):
    def rows(self):
        return [
            {"company_name": "Has Email", "website": "hasemail.com",
             "email": "sam@hasemail.com", "email_status": "verified",
             "owner_first_name": "Sam", "notes": ""},
            {"company_name": "Dead Email", "website": "dead.com",
             "email": "gone@dead.com", "email_status": "verified", "notes": ""},
            {"company_name": "No Email", "website": "noemail.com",
             "email": "", "notes": ""},
            {"company_name": "No Website", "website": "", "email": "",
             "notes": ""},
        ]

    def run_pass(self, cap=60):
        t = FakeTransport(
            account=PLENTY,
            verify={"sam@hasemail.com": {"status": "deliverable", "score": 98},
                    "gone@dead.com": {"status": "undeliverable", "score": 3}},
            domains={"noemail.com": [
                {"value": "pat@noemail.com", "type": "personal",
                 "confidence": 92, "first_name": "Pat", "last_name": "Vance",
                 "position": "Owner"}]},
        )
        rows = self.rows()
        c = client(t, cap=cap)
        hunter.enrich_rows(rows, c)
        return rows, c

    def test_an_existing_email_is_checked_and_labelled_apollo(self):
        rows, _c = self.run_pass()
        self.assertEqual(rows[0]["email_check"], "deliverable")
        self.assertEqual(rows[0]["email_score"], 98)
        self.assertEqual(rows[0]["email_source"], "apollo")

    def test_a_dead_address_is_marked_and_kept_not_deleted(self):
        rows, _c = self.run_pass()
        dead = rows[1]
        self.assertEqual(dead["email_check"], "undeliverable")
        self.assertEqual(dead["email"], "gone@dead.com")
        self.assertIn("do not send", dead["notes"])

    def test_a_missing_email_is_filled_and_labelled_hunter(self):
        rows, _c = self.run_pass()
        filled = rows[2]
        self.assertEqual(filled["email"], "pat@noemail.com")
        self.assertEqual(filled["email_source"], "hunter")
        self.assertEqual(filled["owner_first_name"], "Pat")
        self.assertEqual(filled["title"], "Owner")

    def test_a_row_with_no_website_is_never_guessed_at(self):
        rows, _c = self.run_pass()
        self.assertEqual(rows[3]["email"], "")
        self.assertEqual(rows[3]["email_source"], "")

    def test_an_apollo_email_is_never_relabelled_as_hunters(self):
        rows, _c = self.run_pass()
        self.assertEqual(rows[0]["email_source"], "apollo")
        self.assertEqual(rows[0]["email_status"], "verified")

    def test_verification_gets_the_budget_before_discovery(self):
        """On a small plan the money runs out. It must run out on finding new
        addresses, not on checking the ones about to be mailed."""
        rows, c = self.run_pass(cap=2)
        self.assertEqual(rows[0]["email_check"], "deliverable")
        self.assertEqual(rows[1]["email_check"], "undeliverable")
        self.assertEqual(rows[2]["email"], "")
        self.assertTrue(c.stats.cap_hit)

    def test_hitting_the_cap_is_written_on_the_row(self):
        rows, _c = self.run_pass(cap=2)
        self.assertIn("cap", rows[2]["notes"])


class SummaryTest(unittest.TestCase):
    def test_the_summary_carries_what_the_morning_report_prints(self):
        stats = hunter.HunterStats(verified=3, deliverable=2, undeliverable=1,
                                   found=1)
        out = hunter.summary(stats, spent=4)
        self.assertEqual(out["verified"], 3)
        self.assertEqual(out["undeliverable"], 1)
        self.assertEqual(out["lookups_spent"], 4)


class UnavailableTest(unittest.TestCase):
    def test_no_key_is_refused_clearly(self):
        with self.assertRaises(hunter.HunterUnavailable):
            hunter.HunterClient("")


if __name__ == "__main__":
    unittest.main()
