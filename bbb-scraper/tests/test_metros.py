"""Full-state pulls: metro expansion, shared budget, cross-metro dedupe, resume."""

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

import metros
import scraper
from checkpoint import RunProgress
from fixture_server import start_server


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class MetroResolutionTest(unittest.TestCase):
    def test_city_slug_stays_a_single_run(self):
        self.assertEqual(metros.resolve_locations("charlotte-nc"), (["charlotte-nc"], "single"))
        self.assertIsNone(metros.state_code("charlotte-nc"))

    def test_state_code_and_name_both_expand(self):
        by_code, src = metros.resolve_locations("nc")
        by_name, _ = metros.resolve_locations("North Carolina")
        self.assertEqual(by_code, by_name)
        self.assertTrue(src.startswith("bundled"))
        self.assertIn("charlotte-nc", by_code)
        self.assertTrue(all(slug.endswith("-nc") for slug in by_code))

    def test_every_state_has_metros(self):
        data = metros.load_metro_data()
        self.assertGreaterEqual(len(data), 50)
        for code, cities in data.items():
            self.assertTrue(cities, f"{code} has no metros")
            for city in cities:
                self.assertRegex(city, r"^[a-z0-9-]+$", f"{code}: bad slug {city!r}")
        # every full state name resolves to a code we have data for
        for name, code in metros.STATE_CODES.items():
            self.assertIn(code, data, f"{name} -> {code} missing from metros.json")

    def test_explicit_list_overrides_bundled(self):
        locations, source = metros.resolve_locations("nc", metros_arg="raleigh-nc, durham-nc")
        self.assertEqual(locations, ["raleigh-nc", "durham-nc"])
        self.assertEqual(source, "--metros")

    def test_metros_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# my markets\nraleigh-nc\n\ndurham-nc  # inline comment\n")
            locations, source = metros.resolve_locations("nc", metros_file=path)
        self.assertEqual(locations, ["raleigh-nc", "durham-nc"])
        self.assertTrue(source.startswith("file:"))

    def test_max_metros_limits(self):
        locations, _ = metros.resolve_locations("tx", limit=3)
        self.assertEqual(len(locations), 3)

    def test_unknown_state_is_an_error_not_a_silent_empty(self):
        with self.assertRaises(metros.UnknownState):
            metros.metros_for_state("ZZ")


class RunProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "progress.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        progress = RunProgress(category="plumber", scope="nc", path=self.path)
        progress.mark_done("charlotte-nc", 40)
        progress.mark_done("raleigh-nc", 35)
        progress.save()

        loaded = RunProgress.load(self.path, "plumber", "nc")
        self.assertTrue(loaded.is_done("charlotte-nc"))
        self.assertFalse(loaded.is_done("durham-nc"))
        self.assertEqual(loaded.collected, 75)

    def test_different_scope_is_not_resumed(self):
        progress = RunProgress(category="plumber", scope="nc", path=self.path)
        progress.mark_done("charlotte-nc", 10)
        progress.save()
        other = RunProgress.load(self.path, "plumber", "sc")
        self.assertEqual(other.completed, set())


class StatePullTest(unittest.TestCase):
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
        self.endpoints = os.path.join(self.tmp.name, "endpoints.json")
        with open(self.endpoints, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{
                "url": f"{self.base_url}/api/businesssearch",
                "page_param": "page", "category_param": "find_text", "location_param": "find_loc",
            }]}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *extra, location="nc"):
        argv = [
            "--category", "plumber", "--location", location,
            "--endpoints", self.endpoints, "--output", self.out,
            "--checkpoint", os.path.join(self.tmp.name, "ck.json"),
            "--progress", os.path.join(self.tmp.name, "progress.json"),
            "--no-fallback", "--min-delay", "0", "--max-delay", "0", "--no-detail",
            *extra,
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = scraper.main(argv)
        return code, buf.getvalue()

    def test_list_metros_exits_without_scraping(self):
        code, output = self.run_cli("--list-metros", "--max-metros", "4")
        self.assertEqual(code, 0)
        lines = [l for l in output.splitlines() if l.endswith("-nc")]
        self.assertEqual(len(lines), 4)
        self.assertFalse(os.path.exists(self.out), "a listing run should not write a CSV")

    def test_state_pull_covers_several_metros(self):
        code, output = self.run_cli("--max-metros", "3", "--max-results", "60",
                                    "--max-per-metro", "20")
        self.assertEqual(code, 0, output)
        self.assertIn("metros pulled   : 3/3", output)
        rows = read_csv(self.out)
        self.assertGreater(len(rows), 20, "looks like only one metro was pulled")

    def test_cross_metro_duplicates_collapse(self):
        code, output = self.run_cli("--max-metros", "3", "--max-results", "60",
                                    "--max-per-metro", "20")
        self.assertEqual(code, 0)
        rows = read_csv(self.out)
        domains = [r["website"] for r in rows if r["website"]]
        self.assertEqual(len(domains), len(set(domains)), "duplicate domains across metros")
        # the regional operator is listed in every metro but appears once
        regional = [r for r in rows if r["company_name"].startswith("Test Plumbing 999")]
        self.assertEqual(len(regional), 1, regional)
        self.assertIn("deduped", output)

    def test_budget_is_shared_across_metros(self):
        code, output = self.run_cli("--max-metros", "5", "--max-results", "25",
                                    "--max-per-metro", "10")
        self.assertEqual(code, 0, output)
        rows = read_csv(self.out)
        low = scraper.lowconfidence_path(self.out)
        total = len(rows) + (len(read_csv(low)) if os.path.exists(low) else 0)
        self.assertLessEqual(total, 25)
        self.assertIn("budget", output)

    def test_resume_skips_completed_metros(self):
        code, first = self.run_cli("--max-metros", "4", "--max-results", "20",
                                   "--max-per-metro", "10")
        self.assertEqual(code, 0)
        first_rows = {r["profile_url"] for r in read_csv(self.out)}

        progress_path = os.path.join(self.tmp.name, "progress.json")
        self.assertTrue(os.path.exists(progress_path), "budget stop should leave progress behind")
        saved = RunProgress.load(progress_path, "plumber", "nc")
        self.assertGreaterEqual(len(saved.completed), 2)

        code, second = self.run_cli("--max-metros", "4", "--max-results", "20",
                                    "--max-per-metro", "10")
        self.assertEqual(code, 0)
        self.assertIn("already collected, skipping", second)
        # A resumed state pull appends into the same CSV rather than clobbering
        # it, so the file now holds both batches.
        after = read_csv(self.out)
        after_rows = {r["profile_url"] for r in after}
        self.assertTrue(first_rows <= after_rows, "resume overwrote the earlier metros")
        self.assertTrue(after_rows - first_rows, "resume added nothing")

        domains = [r["website"] for r in after if r["website"]]
        self.assertEqual(len(domains), len(set(domains)),
                         "appended file has duplicate domains across invocations")
        self.assertIn("already collected, skipping", second)

    def test_overwrite_forces_a_fresh_file(self):
        self.run_cli("--max-metros", "4", "--max-results", "20", "--max-per-metro", "10")
        first = len(read_csv(self.out))
        code, _ = self.run_cli("--max-metros", "4", "--max-results", "20",
                               "--max-per-metro", "10", "--overwrite")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(read_csv(self.out)), first + 1)

    def test_progress_cleared_once_the_state_is_covered(self):
        code, _ = self.run_cli("--max-metros", "2", "--max-results", "5000",
                               "--max-per-metro", "15")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "progress.json")),
                         "a fully covered state should not leave progress behind")

    def test_single_city_run_is_unchanged(self):
        code, output = self.run_cli("--max-results", "20", location="charlotte-nc")
        self.assertEqual(code, 0)
        self.assertNotIn("metros pulled", output)
        self.assertTrue(read_csv(self.out))

    def test_explicit_metros_flag(self):
        code, output = self.run_cli("--metros", "raleigh-nc,durham-nc", "--max-results", "20",
                                    "--max-per-metro", "10")
        self.assertEqual(code, 0, output)
        self.assertIn("2 metros (source: --metros)", output)

    def test_per_metro_checkpoints_do_not_collide(self):
        self.run_cli("--max-metros", "3", "--max-results", "12", "--max-per-metro", "4")
        written = [f for f in os.listdir(self.tmp.name) if f.startswith("ck-")]
        self.assertGreaterEqual(len(written), 1, os.listdir(self.tmp.name))


if __name__ == "__main__":
    unittest.main(verbosity=2)
