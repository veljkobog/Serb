"""Full-pipeline tests against a local fixture API.

These cover the acceptance test from the spec -- website coverage, zero
duplicate domains, no crash at pagination end -- plus HAR discovery, resume,
and the low-confidence split, without touching bbb.org.
"""

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

import api_client
import scraper
from checkpoint import Checkpoint
from fixture_server import start_server


def _summary_value(output: str, label: str) -> str:
    for line in output.splitlines():
        if line.strip().startswith(label):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no '{label}' line in run summary:\n{output}")


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base_url = start_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out.csv")
        self.ckpt = os.path.join(self.tmp.name, "ckpt.json")
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "params": {"find_country": "USA"},
                "page_param": "page",
                "category_param": "find_text",
                "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra):
        argv = [
            "--category", "plumber", "--location", "wilmington-nc",
            "--endpoints", self.endpoints, "--output", self.out,
            "--checkpoint", self.ckpt, "--no-fallback",
            "--min-delay", "0", "--max-delay", "0", "--no-detail",
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    # ------------------------------------------------------------------
    def test_acceptance_100_results(self):
        """--max-results 100 -> >=80% website coverage, no dup domains, clean end."""
        code, output = self.run_cli("--max-results", "100")
        self.assertEqual(code, 0, output)

        rows = read_csv(self.out)
        self.assertGreater(len(rows), 0)

        with_site = [r for r in rows if r["website"]]
        coverage = len(with_site) / len(rows)
        self.assertGreaterEqual(coverage, 0.80, f"website coverage {coverage:.0%}")

        domains = [r["website"] for r in rows if r["website"]]
        self.assertEqual(len(domains), len(set(domains)), "duplicate domains in output")

        # the fixture plants same-company-second-profile rows; they must have
        # been caught by domain dedupe, not merely counted
        deduped = int(_summary_value(output, "deduped"))
        self.assertGreater(deduped, 0, output)

        self.assertIn("run summary", output)
        self.assertIn("website coverage", output)

    def test_no_crash_past_pagination_end(self):
        """Asking for more than exists stops cleanly instead of erroring."""
        code, output = self.run_cli("--max-results", "5000")
        self.assertEqual(code, 0, output)
        rows = read_csv(self.out)
        # fixture serves 6 pages x 10, minus dupes and low-confidence rows
        self.assertGreater(len(rows), 30)
        self.assertLess(len(rows), 60)

    def test_max_results_is_respected(self):
        code, _ = self.run_cli("--max-results", "12")
        self.assertEqual(code, 0)
        main_rows = read_csv(self.out)
        low_path = scraper.lowconfidence_path(self.out)
        low_rows = read_csv(low_path) if os.path.exists(low_path) else []
        self.assertLessEqual(len(main_rows) + len(low_rows), 12)

    def test_low_confidence_split(self):
        code, output = self.run_cli("--max-results", "100")
        self.assertEqual(code, 0)
        low_path = scraper.lowconfidence_path(self.out)
        self.assertTrue(os.path.exists(low_path), "expected a _lowconfidence.csv")

        low_rows = read_csv(low_path)
        self.assertTrue(low_rows)
        for row in low_rows:
            self.assertEqual(row["website"], "")
            self.assertEqual(row["phone"], "")
            self.assertTrue(row["company_name"])

        # and none of them leaked into the main file
        for row in read_csv(self.out):
            self.assertTrue(row["website"] or row["phone"])

    def test_min_years_filter_keeps_unknowns(self):
        code, _ = self.run_cli("--max-results", "100", "--min-years", "20")
        self.assertEqual(code, 0)
        for row in read_csv(self.out):
            if row["years_in_business"]:
                self.assertGreaterEqual(int(row["years_in_business"]), 20)

    def test_blank_not_zero_for_unknown_fields(self):
        self.run_cli("--max-results", "20")
        low_path = scraper.lowconfidence_path(self.out)
        for row in read_csv(low_path):
            self.assertEqual(row["years_in_business"], "")
            self.assertEqual(row["accredited"], "")

    def test_nr_rating_written_blank(self):
        self.run_cli("--max-results", "100")
        ratings = {r["bbb_rating"] for r in read_csv(self.out)}
        self.assertTrue(ratings <= {"A+", ""}, ratings)

    def test_resume_from_checkpoint(self):
        # first run stops at the cap, leaving a checkpoint behind
        code, _ = self.run_cli("--max-results", "20")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(self.ckpt), "expected a checkpoint after a capped run")
        first = {r["profile_url"] for r in read_csv(self.out)}

        saved = Checkpoint.load(self.ckpt, "plumber", "wilmington-nc")
        self.assertGreaterEqual(saved.last_page, 2)

        # second run resumes past those pages and returns different companies
        code, _ = self.run_cli("--max-results", "20")
        self.assertEqual(code, 0)
        second = {r["profile_url"] for r in read_csv(self.out)}
        self.assertTrue(second)
        self.assertFalse(first & second, "resume re-collected pages already done")

    def test_skip_pages(self):
        self.run_cli("--max-results", "10", "--no-resume")
        page1 = {r["profile_url"] for r in read_csv(self.out)}
        self.run_cli("--max-results", "10", "--no-resume", "--skip", "3")
        skipped = {r["profile_url"] for r in read_csv(self.out)}
        self.assertFalse(page1 & skipped)

    def test_checkpoint_cleared_when_results_exhausted(self):
        self.run_cli("--max-results", "5000")
        self.assertFalse(os.path.exists(self.ckpt),
                         "checkpoint should be cleared once results are exhausted")

    def test_bad_endpoint_reports_failure(self):
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{"url": f"{self.base_url}/api/nope"}]}, fh)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main([
                "--category", "plumber", "--location", "wilmington-nc",
                "--endpoints", bad, "--output", self.out, "--checkpoint", self.ckpt,
                "--no-fallback", "--min-delay", "0", "--max-delay", "0",
            ])
        self.assertEqual(code, 1)


class EndpointDiscoveryTest(unittest.TestCase):
    """--from-har picks the search XHR out of a browser capture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.har = os.path.join(self.tmp.name, "capture.har")
        sys.path.insert(0, HERE)
        from fixture_server import build_page

        entries = [
            {  # noise: an analytics beacon
                "request": {"url": "https://www.bbb.org/api/telemetry", "method": "POST", "headers": []},
                "response": {"content": {"mimeType": "application/json", "text": '{"ok":true}'}},
            },
            {  # noise: a stylesheet
                "request": {"url": "https://www.bbb.org/static/app.css", "method": "GET", "headers": []},
                "response": {"content": {"mimeType": "text/css", "text": "body{}"}},
            },
            {  # the real one
                "request": {
                    "url": "https://www.bbb.org/api/businesssearch?find_text=plumber&find_loc=wilmington-nc&page=1",
                    "method": "GET",
                    "headers": [
                        {"name": ":authority", "value": "www.bbb.org"},
                        {"name": "User-Agent", "value": "Mozilla/5.0 TestBrowser"},
                        {"name": "Referer", "value": "https://www.bbb.org/search?find_text=plumber"},
                        {"name": "Content-Length", "value": "0"},
                    ],
                    "cookies": [{"name": "cf_clearance", "value": "secret-token"}],
                },
                "response": {"content": {"mimeType": "application/json",
                                         "text": json.dumps(build_page(1))}},
            },
        ]
        with open(self.har, "w", encoding="utf-8") as fh:
            json.dump({"log": {"entries": entries}}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def test_har_mining_picks_the_search_call(self):
        specs = api_client.endpoints_from_har(self.har)
        self.assertTrue(specs)
        top = specs[0]
        self.assertEqual(top.url, "https://www.bbb.org/api/businesssearch")
        self.assertEqual(top.page_param, "page")
        self.assertEqual(top.category_param, "find_text")
        self.assertEqual(top.location_param, "find_loc")
        self.assertEqual(top.headers["User-Agent"], "Mozilla/5.0 TestBrowser")
        self.assertNotIn(":authority", top.headers)      # pseudo-headers dropped
        self.assertNotIn("Content-Length", top.headers)
        self.assertEqual(top.cookies["cf_clearance"], "secret-token")

    def test_saved_config_omits_cookies_by_default(self):
        specs = api_client.endpoints_from_har(self.har)
        out = os.path.join(self.tmp.name, "endpoints.json")

        api_client.save_endpoints(out, specs[:1])
        with open(out, encoding="utf-8") as fh:
            saved = json.load(fh)["endpoints"][0]
        self.assertEqual(saved["cookies"], {})

        api_client.save_endpoints(out, specs[:1], include_cookies=True)
        with open(out, encoding="utf-8") as fh:
            saved = json.load(fh)["endpoints"][0]
        self.assertEqual(saved["cookies"]["cf_clearance"], "secret-token")

    def test_roundtrip_through_config(self):
        specs = api_client.endpoints_from_har(self.har)
        out = os.path.join(self.tmp.name, "endpoints.json")
        api_client.save_endpoints(out, specs[:1])
        reloaded = api_client.load_endpoints(out)
        self.assertEqual(reloaded[0].url, specs[0].url)
        self.assertEqual(reloaded[0].category_param, "find_text")


class RateLimitAndBlockTest(unittest.TestCase):
    def test_rate_limiter_paces_requests(self):
        import time
        limiter = api_client.RateLimiter(min_delay=0.15, max_delay=0.2)
        start = time.monotonic()
        limiter.wait()   # first call is free
        limiter.wait()
        limiter.wait()
        self.assertGreaterEqual(time.monotonic() - start, 0.3)

    def test_aborts_after_three_consecutive_blocks(self):
        import http.server
        import threading

        class Blocker(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Blocker)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = api_client.ApiClient(
                limiter=api_client.RateLimiter(0, 0), max_consecutive_blocks=3
            )
            spec = api_client.EndpointSpec(url=f"http://127.0.0.1:{server.server_port}/api/search")
            # backoff sleeps are real; keep them tiny for the test
            import time as _time
            original_sleep = _time.sleep
            _time.sleep = lambda s: original_sleep(0)
            try:
                with self.assertRaises(api_client.BlockedError):
                    client.request_page(spec, 1, "plumber", "wilmington-nc")
            finally:
                _time.sleep = original_sleep
                client.close()
            self.assertEqual(client.consecutive_blocks, 3)
        finally:
            server.shutdown()
            server.server_close()

    def test_user_agent_is_stable_within_a_session(self):
        client = api_client.ApiClient(limiter=api_client.RateLimiter(0, 0))
        try:
            self.assertIn(client.user_agent, api_client.USER_AGENTS)
            self.assertEqual(client.client.headers["User-Agent"], client.user_agent)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
