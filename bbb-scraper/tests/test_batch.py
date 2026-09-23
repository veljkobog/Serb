"""Batch category runs and the Google cost preflight."""

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


class BatchCategoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)      # default output names land in the temp dir
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "page_param": "page", "category_param": "find_text", "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def run_cli(self, *extra, category="plumber,roofing-contractors", location="wilmington-nc"):
        argv = [
            "--category", category, "--location", location, "--max-results", "20",
            "--endpoints", self.endpoints, "--no-fallback",
            "--min-delay", "0", "--max-delay", "0", "--no-detail", "--no-resume",
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_each_category_gets_its_own_file(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0, output)
        for category in ("plumber", "roofing-contractors"):
            path = f"{category}-wilmington-nc.csv"
            self.assertTrue(os.path.exists(path), f"missing {path}")
            rows = read_csv(path)
            self.assertTrue(rows)
            self.assertTrue(all(r["category"] for r in rows))
        self.assertIn("=== plumber (1/2) ===", output)
        self.assertIn("=== roofing-contractors (2/2) ===", output)
        self.assertIn("run summary -- plumber", output)

    def test_shared_output_collects_every_category(self):
        out = os.path.join(self.tmp.name, "all.csv")
        code, output = self.run_cli("--output", out)
        self.assertEqual(code, 0, output)
        rows = read_csv(out)
        self.assertTrue(rows)
        # one header only, and both categories present
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("company_name,website"), 1)
        domains = [r["website"] for r in rows if r["website"]]
        self.assertEqual(len(domains), len(set(domains)), "duplicate domains in the merged file")

    def test_endpoint_is_resolved_once_for_the_whole_batch(self):
        code, output = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(output.count("using endpoint"), 1,
                         "endpoint was re-resolved per category")

    def test_categories_file(self):
        path = os.path.join(self.tmp.name, "cats.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# trades\nplumber\n\nelectrician  # inline\n")
        code, output = self.run_cli("--categories-file", path)
        self.assertEqual(code, 0, output)
        self.assertTrue(os.path.exists("electrician-wilmington-nc.csv"))
        self.assertIn("2 categories: plumber, electrician", output)

    def test_filter_counts_are_per_category(self):
        code, output = self.run_cli("--min-bbb-reviews", "24")
        self.assertEqual(code, 0, output)
        blocks = output.split("run summary -- ")
        self.assertEqual(len(blocks), 3)   # preamble + two summaries
        counts = []
        for block in blocks[1:]:
            for line in block.splitlines():
                if "filtered min-bbb-reviews" in line:
                    counts.append(int(line.split(":")[1].strip()))
        self.assertEqual(len(counts), 2)
        # the second summary must not carry the first one's drops
        self.assertLess(counts[1], sum(counts), "filter counts accumulated across categories")

    def test_single_category_output_is_unchanged(self):
        code, output = self.run_cli(category="plumber")
        self.assertNotIn("categories:", output)
        self.assertIn("run summary", output)
        self.assertNotIn("run summary -- ", output)
        self.assertEqual(code, 0)


class PreflightTest(unittest.TestCase):
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
        self.cache = os.path.join(self.tmp.name, "gcache.json")
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        self.places_handler.requests = 0
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "page_param": "page", "category_param": "find_text", "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = [
            "--category", "plumber", "--location", "wilmington-nc", "--max-results", "20",
            "--endpoints", self.endpoints, "--output", self.out,
            "--checkpoint", os.path.join(self.tmp.name, "ck.json"),
            "--no-fallback", "--min-delay", "0", "--max-delay", "0", "--no-detail",
            "--no-resume", "--google-cache", self.cache,
            "--google-endpoint", self.places_endpoint,
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_dry_run_bills_nothing(self):
        code, output = self.run_cli("--google-key", "k", "--min-google-reviews", "100",
                                    "--google-dry-run")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.places_handler.requests, 0, "dry run called the API")
        self.assertIn("google preflight", output)
        self.assertIn("billable lookups", output)
        # the CSV is still written -- the BBB data is real
        rows = read_csv(self.out)
        self.assertTrue(rows)
        self.assertTrue(all(not r["google_match"] for r in rows))

    def test_dry_run_count_matches_what_the_real_pass_bills(self):
        _, dry = self.run_cli("--google-key", "k", "--min-google-reviews", "1",
                              "--google-dry-run")
        predicted = int([l for l in dry.splitlines() if "billable lookups" in l][0].split(":")[1])

        self.places_handler.requests = 0
        _, live = self.run_cli("--google-key", "k", "--min-google-reviews", "1")
        self.assertEqual(self.places_handler.requests, predicted,
                         f"preflight said {predicted}, run billed {self.places_handler.requests}")

    def test_dry_run_accounts_for_the_cache(self):
        self.run_cli("--google-key", "k", "--min-google-reviews", "1")   # warms the cache
        _, output = self.run_cli("--google-key", "k", "--min-google-reviews", "1",
                                 "--google-dry-run")
        cached = int([l for l in output.splitlines() if "already cached" in l][0].split(":")[1])
        billable = int([l for l in output.splitlines() if "billable lookups" in l][0].split(":")[1])
        self.assertGreater(cached, 0)
        self.assertEqual(billable, 0, "cached lookups were counted as billable")

    def test_cost_estimate_only_when_a_price_is_given(self):
        _, without = self.run_cli("--google-key", "k", "--min-google-reviews", "1",
                                  "--google-dry-run")
        self.assertNotIn("estimated cost", without)
        _, with_price = self.run_cli("--google-key", "k", "--min-google-reviews", "1",
                                     "--google-dry-run", "--google-cost-per-lookup", "0.032")
        self.assertIn("estimated cost", with_price)

    def test_preflight_unit_respects_cap(self):
        listings = [Listing(company_name=f"Co {i}", profile_url=f"/p/{i}") for i in range(10)]
        stats = enrich_google.preflight(listings, enrich_google.PlacesCache(None), max_lookups=4)
        self.assertEqual(stats.total, 10)
        self.assertEqual(stats.billable, 4)
        self.assertEqual(stats.capped, 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
