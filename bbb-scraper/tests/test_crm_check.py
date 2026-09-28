"""The pre-send CRM check: suppression, prior contact, open deals, fallback matching."""

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

import crm_check
from hubspot_site import start_hubspot


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class VerdictLogicTest(unittest.TestCase):
    def test_suppression_reasons(self):
        self.assertIn("unsubscribed from all email",
                      crm_check.suppression_reasons({"hs_email_optout": "true"}))
        self.assertIn("email address quarantined",
                      crm_check.suppression_reasons({"hs_email_quarantined": "true"}))
        self.assertIn("3 previous bounce(s)",
                      crm_check.suppression_reasons({"hs_email_bounce": "3"}))
        self.assertEqual(crm_check.suppression_reasons({"hs_email_optout": "false"}), [])

    def test_zero_bounces_is_not_a_reason(self):
        self.assertEqual(crm_check.suppression_reasons({"hs_email_bounce": "0"}), [])

    def test_engagement_reasons(self):
        reasons = crm_check.engagement_reasons(
            {"num_contacted_notes": "7", "lifecyclestage": "customer",
             "notes_last_contacted": "2026-08-01T09:00:00Z"})
        self.assertIn("7 logged touch(es)", reasons)
        self.assertIn("lifecycle stage: customer", reasons)
        self.assertTrue(any("2026-08-01" in r for r in reasons))


class MatchingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_hubspot()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.handler.empty_portal = False
        self.handler.unauthorized = False
        self.client = crm_check.HubSpotClient("test-token", base_url=self.base_url)

    def tearDown(self):
        self.client.close()

    def check(self, **lead):
        return crm_check.check_lead(self.client, lead)

    # ------------------------------------------------------------------
    def test_unsubscribed_contact_is_suppressed(self):
        verdict = self.check(company_name="Acme", email="unsubscribed@acme.com")
        self.assertEqual(verdict.status, crm_check.SKIP_SUPPRESSED)
        self.assertIn("unsubscribed from all email", verdict.reasons)

    def test_bouncing_address_is_suppressed(self):
        verdict = self.check(company_name="Acme", email="bouncer@acme.com")
        self.assertEqual(verdict.status, crm_check.SKIP_SUPPRESSED)
        self.assertIn("3 previous bounce(s)", verdict.reasons)

    def test_suppression_outranks_being_a_known_contact(self):
        """A suppressed record must never be reported as merely 'already in CRM'."""
        verdict = self.check(company_name="Acme", email="unsubscribed@acme.com")
        self.assertNotEqual(verdict.status, crm_check.SKIP_EXISTING)

    def test_existing_customer_is_skipped_with_context(self):
        verdict = self.check(company_name="Acme", email="known@acme.com")
        self.assertEqual(verdict.status, crm_check.SKIP_EXISTING)
        self.assertEqual(verdict.owner_id, "555")
        self.assertTrue(any("customer" in r for r in verdict.reasons))

    def test_phone_finds_a_record_email_would_miss(self):
        """The gap this closes: CRM record with no domain and a different email."""
        verdict = self.check(company_name="Phone Co", phone="(316) 555-0111")
        self.assertEqual(verdict.status, crm_check.SKIP_EXISTING)
        self.assertEqual(verdict.matched_on, "contact phone")
        self.assertEqual(verdict.owner_id, "777")

    def test_toll_free_is_never_used_for_matching(self):
        """Franchisees share 800 numbers; matching on one merges strangers."""
        verdict = self.check(company_name="Somebody", phone="(800) 555-0111")
        self.assertEqual(verdict.status, crm_check.SEND)

    def test_company_domain_match_reports_open_deals(self):
        verdict = self.check(company_name="Existing Co", website="https://existingco.com")
        self.assertEqual(verdict.status, crm_check.SKIP_EXISTING)
        self.assertEqual(verdict.open_deals, "2")
        self.assertTrue(any("deal" in r for r in verdict.reasons))

    def test_name_only_match_is_review_not_skip(self):
        """'Brown's Plumbing' is not a unique string."""
        verdict = self.check(company_name="Common Name Plumbing")
        self.assertEqual(verdict.status, crm_check.REVIEW)
        self.assertIn("name-only match, verify before skipping", verdict.reasons[0])

    def test_genuinely_new_lead_passes(self):
        verdict = self.check(company_name="Brand New Co", email="nobody@brandnew.com",
                             phone="(316) 555-9999", website="brandnew.com")
        self.assertEqual(verdict.status, crm_check.SEND)
        self.assertIn("no CRM record found", verdict.reasons[0])


class PortalGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url, cls.handler = start_hubspot()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.handler.empty_portal = False
        self.handler.unauthorized = False
        self.tmp = tempfile.TemporaryDirectory()
        self.input = os.path.join(self.tmp.name, "leads.csv")
        with open(self.input, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["company_name", "email", "phone", "website"])
            writer.writeheader()
            writer.writerows([
                {"company_name": "Acme", "email": "unsubscribed@acme.com", "phone": "", "website": ""},
                {"company_name": "Known Co", "email": "known@acme.com", "phone": "", "website": ""},
                {"company_name": "Existing Co", "email": "", "phone": "", "website": "existingco.com"},
                {"company_name": "Brand New Co", "email": "new@brandnew.com",
                 "phone": "(316) 555-9999", "website": "brandnew.com"},
            ])

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        out = os.path.join(self.tmp.name, "checked.csv")
        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = crm_check.main([self.input, "--output", out, "--hubspot-token", "t",
                                   "--base-url", self.base_url, *extra])
        return code, buf.getvalue() + err.getvalue(), out

    def test_end_to_end_verdicts(self):
        code, output, out_path = self.run_cli()
        self.assertEqual(code, 0, output)
        rows = read_csv(out_path)
        self.assertEqual(len(rows), 4)
        verdicts = {r["company_name"]: r["crm_verdict"] for r in rows}
        self.assertEqual(verdicts["Acme"], crm_check.SKIP_SUPPRESSED)
        self.assertEqual(verdicts["Known Co"], crm_check.SKIP_EXISTING)
        self.assertEqual(verdicts["Existing Co"], crm_check.SKIP_EXISTING)
        self.assertEqual(verdicts["Brand New Co"], crm_check.SEND)
        self.assertIn("do not mail these", output)

    def test_original_columns_are_preserved(self):
        _code, _output, out_path = self.run_cli()
        row = read_csv(out_path)[0]
        self.assertIn("company_name", row)
        self.assertIn("crm_verdict", row)

    def test_empty_portal_refuses_to_give_an_all_clear(self):
        """Zero matches from an empty portal look exactly like a clean list."""
        self.handler.empty_portal = True
        code, output, _out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Refusing to report", output)

    def test_bad_token_fails_loudly(self):
        self.handler.unauthorized = True
        code, output, _out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("401", output)

    def test_missing_token_is_a_clear_error(self):
        buf = io.StringIO()
        env = os.environ.pop("HUBSPOT_TOKEN", None)
        try:
            with redirect_stderr(buf):
                code = crm_check.main([self.input, "--base-url", self.base_url])
        finally:
            if env is not None:
                os.environ["HUBSPOT_TOKEN"] = env
        self.assertEqual(code, 2)
        self.assertIn("HUBSPOT_TOKEN", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RateLimitTest(unittest.TestCase):
    """HubSpot enforces a per-second cap, and four lookups per lead across a
    full sheet arrives as a burst that trips it immediately. A 429 is "come
    back shortly", not a verdict -- treating it as a failure abandons the CRM
    check partway and leaves rows unchecked while the sheet looks complete."""

    def test_requests_are_paced(self):
        self.assertLessEqual(1 / crm_check.MIN_INTERVAL, 10,
                             "pacing must stay inside HubSpot's per-second cap")

    def test_a_429_is_retried_rather_than_raised(self):
        import itertools

        class Response:
            def __init__(self, status, body=None):
                self.status_code = status
                self._body = body or {"total": 1, "results": []}
                self.headers = {}
                self.text = ""

            def json(self):
                return self._body

        client = crm_check.HubSpotClient.__new__(crm_check.HubSpotClient)
        client.base_url = "http://x"
        client.min_interval = 0
        client.max_retries = 4
        client.rate_limited = 0
        client._last_call = 0.0
        replies = itertools.chain([Response(429), Response(429)],
                                  itertools.repeat(Response(200)))

        class Fake:
            def post(self, *a, **kw):
                return next(replies)

        client.client = Fake()
        body = client._post("/x", {}, "test")
        self.assertEqual(body["total"], 1)
        self.assertEqual(client.rate_limited, 2)

    def test_a_persistent_429_is_reported_not_swallowed(self):
        class Response:
            status_code = 429
            headers = {}
            text = ""

            def json(self):
                return {}

        client = crm_check.HubSpotClient.__new__(crm_check.HubSpotClient)
        client.base_url = "http://x"
        client.min_interval = 0
        client.max_retries = 2
        client.rate_limited = 0
        client._last_call = 0.0

        class Fake:
            def post(self, *a, **kw):
                return Response()

        client.client = Fake()
        with self.assertRaises(crm_check.HubSpotUnavailable) as caught:
            client._post("/x", {}, "contacts search")
        self.assertIn("NOT all checked", str(caught.exception))
