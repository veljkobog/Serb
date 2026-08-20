"""Offline HAR replay and field coverage reporting."""

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

import parse
import replay
import scraper
from fixture_server import build_page

PROFILE_HTML = """
<html><body>
  <h1>Replay Plumbing</h1>
  <p>BBB Rating: A+</p>
  <p>Business Started: 3/1/1999</p>
  <p>Number of Employees: 32</p>
  <p>Average of 61 Customer Reviews</p>
  <p>2 complaints closed in last 3 years</p>
</body></html>
"""


def har_entry(url, body, mime="application/json", method="GET"):
    return {
        "request": {"url": url, "method": method,
                    "headers": [{"name": "User-Agent", "value": "Mozilla/5.0 Test"}],
                    "cookies": []},
        "response": {"content": {"mimeType": mime, "text": body}},
    }


def write_har(path, entries):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"log": {"entries": entries}}, fh)


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.har = os.path.join(self.tmp.name, "capture.har")
        self.out = os.path.join(self.tmp.name, "out.csv")
        # Page 1 is hand-written to mirror a sparse BBB search card: no years,
        # no accreditation, no headcount -- exactly the record that needs a
        # detail-page visit. Page 2 reuses the fixture's fuller records.
        sparse = {"data": {"searchResults": [{
            "businessName": "Replay Plumbing",
            "websiteUrl": "https://www.replayplumbing.com",
            "phone": "(910) 555-7000",
            "address": {"street": "1 Dock St", "city": "Wilmington",
                        "state": "NC", "postalCode": "28401"},
            "customerReviewCount": 30,
            "reportUrl": "/us/nc/wilmington/profile/plumber/replay-1",
        }]}}
        write_har(self.har, [
            har_entry("https://www.bbb.org/static/app.css", "body{}", mime="text/css"),
            har_entry("https://www.bbb.org/api/businesssearch?page=1&find_text=plumber",
                      json.dumps(sparse)),
            har_entry("https://www.bbb.org/api/businesssearch?page=2&find_text=plumber",
                      json.dumps(build_page(2, location="wilmington-nc"))),
            har_entry("https://www.bbb.org/us/nc/wilmington/profile/plumber/replay-1",
                      PROFILE_HTML, mime="text/html"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = ["--category", "plumber", "--location", "wilmington-nc",
                "--replay", self.har, "--output", self.out,
                "--checkpoint", os.path.join(self.tmp.name, "ck.json"),
                "--no-resume", *extra]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    # ------------------------------------------------------------------
    def test_payloads_are_found_in_order(self):
        payloads = list(replay.iter_search_payloads(self.har))
        self.assertEqual(len(payloads), 2)
        self.assertIn("page=1", payloads[0][0])
        self.assertIn("page=2", payloads[1][0])

    def test_profile_pages_are_picked_up(self):
        pages = replay.detail_pages(self.har)
        self.assertEqual(len(pages), 1)
        self.assertIn("Number of Employees", next(iter(pages.values())))

    def test_full_pipeline_runs_with_no_network(self):
        code, output = self.run_cli("--no-detail")
        self.assertEqual(code, 0, output)
        self.assertIn("no network", output)
        rows = read_csv(self.out)
        self.assertTrue(rows)
        domains = [r["website"] for r in rows if r["website"]]
        self.assertEqual(len(domains), len(set(domains)))

    def test_detail_pages_replay_too(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0, output)
        rows = read_csv(self.out)
        target = [r for r in rows if r["profile_url"].endswith("/profile/plumber/replay-1")]
        self.assertTrue(target, "the profiled listing is missing from the output")
        # these exist only on the profile page, not on the search card
        self.assertEqual(target[0]["employees"], "32")
        self.assertEqual(target[0]["bbb_rating"], "A+")
        self.assertTrue(target[0]["years_in_business"])
        # the card's own review count is not clobbered by the profile page
        self.assertEqual(target[0]["bbb_reviews"], "30")

    def test_only_records_missing_fields_get_a_detail_visit(self):
        """Complete records are not re-fetched -- 2 visits out of 11 listings."""
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("visiting 2 detail page(s)", output)
        self.assertIn("pulled          : 11", output)

    def test_filters_apply_to_replayed_data(self):
        code, output = self.run_cli("--no-detail", "--min-bbb-reviews", "24")
        self.assertEqual(code, 0, output)
        for row in read_csv(self.out):
            if row["bbb_reviews"]:
                self.assertGreaterEqual(int(row["bbb_reviews"]), 24)

    def test_replay_needs_no_endpoint_config(self):
        # no --endpoints, no --from-har, no network: still works
        code, output = self.run_cli("--no-detail")
        self.assertEqual(code, 0)
        self.assertNotIn("Approach A unavailable", output)


class InspectHarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.har = os.path.join(self.tmp.name, "capture.har")

    def tearDown(self):
        self.tmp.cleanup()

    def inspect(self, path):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(["--inspect-har", path])
        return code, buf.getvalue()

    def test_reports_endpoints_and_coverage(self):
        write_har(self.har, [
            har_entry("https://www.bbb.org/api/businesssearch?page=1&find_text=plumber&find_loc=nc",
                      json.dumps(build_page(1, location="wilmington-nc"))),
        ])
        code, output = self.inspect(self.har)
        self.assertEqual(code, 0)
        self.assertIn("candidate endpoints", output)
        self.assertIn("businesssearch", output)
        self.assertIn("field coverage", output)
        self.assertIn("first listing", output)

    def test_capture_without_bodies_says_so(self):
        write_har(self.har, [
            {"request": {"url": "https://www.bbb.org/api/businesssearch", "method": "GET",
                         "headers": [], "cookies": []},
             "response": {"content": {"mimeType": "application/json"}}},   # no text
        ])
        code, output = self.inspect(self.har)
        self.assertEqual(code, 1)
        self.assertIn("Save all as HAR with content", output)

    def test_inspect_needs_no_category_or_location(self):
        write_har(self.har, [
            har_entry("https://www.bbb.org/api/businesssearch?page=1",
                      json.dumps(build_page(1, location="x"))),
        ])
        code, _ = self.inspect(self.har)
        self.assertEqual(code, 0)


class CoverageReportTest(unittest.TestCase):
    def test_zero_coverage_is_flagged(self):
        listings = [parse.Listing(company_name="A", website="a.com", phone="+19105550134")
                    for _ in range(4)]
        coverage = parse.field_coverage(listings)
        self.assertEqual(coverage["total"], 4)
        self.assertEqual(coverage["filled"]["website"], 4)
        self.assertEqual(coverage["filled"]["employees"], 0)
        rendered = "\n".join(parse.format_coverage(coverage))
        self.assertIn("website 100%", rendered)
        self.assertIn("employees 0% (!)", rendered)

    def test_unknown_key_names_surface_as_zero_coverage(self):
        """The real failure mode: BBB renames a key, the column goes quiet."""
        renamed = {
            "businessName": "Acme Plumbing",
            "websiteUrl": "https://acmeplumbing.com",
            # these are the shapes we do NOT know about
            "totalCustomerFeedback": 42,
            "staffHeadcount": "11-50",
        }
        listing = parse.listing_from_record(renamed)
        self.assertEqual(listing.website, "acmeplumbing.com")
        self.assertIsNone(listing.bbb_reviews)
        self.assertIsNone(listing.employees)

        rendered = "\n".join(parse.format_coverage(parse.field_coverage([listing])))
        self.assertIn("bbb_reviews 0% (!)", rendered)
        self.assertIn("employees 0% (!)", rendered)

    def test_empty_input_renders_nothing(self):
        self.assertEqual(parse.format_coverage(parse.field_coverage([])), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
