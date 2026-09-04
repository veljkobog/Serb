"""The 9am orchestrator: rotation, reporting, and failing loudly."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import daily

CONFIG = {
    "target_rows": 15,
    "max_results": 60,
    "daily_credit_cap": 40,
    "min_employees": 5,
    "metros": ["wichita-ks", "tulsa-ok", "omaha-ne", "topeka-ks"],
    "schedule": {
        "monday": ["roofing-contractors", "plumber"],
        "tuesday": ["heating-and-air-conditioning"],
        "saturday": [],
        "sunday": [],
    },
}

MONDAY = dt.date(2026, 9, 7)
TUESDAY = dt.date(2026, 9, 8)
SATURDAY = dt.date(2026, 9, 5)


class RotationTest(unittest.TestCase):
    def test_two_lists_on_the_same_day_use_two_different_metros(self):
        """Otherwise a morning's two lists are both the same city."""
        plan = daily.todays_lists(CONFIG, {"metro_index": 0}, MONDAY)
        self.assertEqual(len(plan), 2)
        self.assertNotEqual(plan[0]["metro"], plan[1]["metro"])
        self.assertEqual([p["category"] for p in plan],
                         ["roofing-contractors", "plumber"])

    def test_the_cursor_advances_so_tomorrow_is_a_new_city(self):
        monday = daily.todays_lists(CONFIG, {"metro_index": 0}, MONDAY)
        tuesday = daily.todays_lists(CONFIG, {"metro_index": 2}, TUESDAY)
        self.assertNotIn(tuesday[0]["metro"], [p["metro"] for p in monday])

    def test_the_cursor_wraps_without_running_off_the_end(self):
        plan = daily.todays_lists(CONFIG, {"metro_index": 3}, MONDAY)
        self.assertEqual([p["metro"] for p in plan], ["topeka-ks", "wichita-ks"])

    def test_weekends_are_empty(self):
        self.assertEqual(daily.todays_lists(CONFIG, {"metro_index": 0}, SATURDAY), [])

    def test_an_unlisted_weekday_is_empty_not_an_error(self):
        sparse = dict(CONFIG, schedule={"monday": ["plumber"]})
        self.assertEqual(daily.todays_lists(sparse, {"metro_index": 0}, TUESDAY), [])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_missing_state_file_starts_at_the_beginning(self):
        self.assertEqual(daily.load_state(self.path)["metro_index"], 0)

    def test_a_corrupt_state_file_costs_a_repeat_not_a_crash(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(daily.load_state(self.path)["metro_index"], 0)

    def test_state_round_trips(self):
        daily.save_state(self.path, {"metro_index": 7, "history": []})
        self.assertEqual(daily.load_state(self.path)["metro_index"], 7)


class ConfigTest(unittest.TestCase):
    def test_a_config_with_no_metros_is_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"schedule": {"monday": ["plumber"]}}, fh)
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                daily.load_config(path)
        finally:
            os.unlink(path)

    def test_the_shipped_example_config_is_valid(self):
        path = os.path.join(os.path.dirname(HERE), "rotation.example.json")
        config = daily.load_config(path)
        self.assertTrue(config["metros"])
        self.assertEqual(sum(len(v) for v in config["schedule"].values()), 10)

    def test_the_example_schedule_matches_partner_demand(self):
        """Roofing has the most EL'd partners, so it should appear most."""
        path = os.path.join(os.path.dirname(HERE), "rotation.example.json")
        config = daily.load_config(path)
        counts = {}
        for cats in config["schedule"].values():
            for cat in cats:
                counts[cat] = counts.get(cat, 0) + 1
        self.assertEqual(max(counts, key=counts.get), "roofing-contractors")


class ReportingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_clean_run_writes_status_and_no_banner(self):
        daily.write_status(self.dir, {"date": "2026-09-07", "sheets": [{"f": 1}],
                                      "problems": []})
        self.assertTrue(os.path.exists(os.path.join(self.dir, "_daily-status.json")))
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "ATTENTION-2026-09-07.txt")))

    def test_a_failure_drops_a_banner_in_the_folder_you_open_anyway(self):
        daily.write_status(self.dir, {"date": "2026-09-07", "sheets": [],
                                      "problems": ["scrape failed"]})
        banner = os.path.join(self.dir, "ATTENTION-2026-09-07.txt")
        self.assertTrue(os.path.exists(banner))
        self.assertIn("scrape failed", open(banner, encoding="utf-8").read())

    def test_a_clean_run_clears_yesterdays_banner(self):
        """A stale banner would read as today's failure."""
        daily.write_status(self.dir, {"date": "2026-09-07", "sheets": [],
                                      "problems": ["boom"]})
        daily.write_status(self.dir, {"date": "2026-09-07", "sheets": [{"f": 1}],
                                      "problems": []})
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "ATTENTION-2026-09-07.txt")))


class ExitCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "rotation.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(CONFIG, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def run_daily(self, *extra):
        return daily.main(["--config", self.config,
                           "--export-dir", self.tmp.name,
                           "--state", os.path.join(self.tmp.name, "s.json"),
                           *extra])

    def test_a_missing_export_folder_refuses_rather_than_creating_one(self):
        code = daily.main(["--config", self.config,
                           "--export-dir", os.path.join(self.tmp.name, "nope"),
                           "--date", "2026-09-07"])
        self.assertEqual(code, 2)

    def test_a_missing_config_reports_where_to_get_one(self):
        code = daily.main(["--config", os.path.join(self.tmp.name, "gone.json"),
                           "--export-dir", self.tmp.name])
        self.assertEqual(code, 2)

    def test_a_dry_run_fetches_nothing(self):
        self.assertEqual(self.run_daily("--date", "2026-09-07", "--dry-run"), 0)
        self.assertEqual(
            [f for f in os.listdir(self.tmp.name) if f.endswith(".csv")], [])

    def test_a_quiet_weekend_is_success_not_failure(self):
        self.assertEqual(self.run_daily("--date", "2026-09-05"), 0)


class ConfigSlugTest(unittest.TestCase):
    def test_every_metro_in_the_shipped_config_is_a_real_slug(self):
        """A typo here means a silent empty list every time it comes up."""
        import json

        import metros
        path = os.path.join(os.path.dirname(HERE), "rotation.example.json")
        config = daily.load_config(path)
        known = set()
        for code in json.load(
                open(os.path.join(os.path.dirname(HERE), "data", "metros.json"),
                     encoding="utf-8"))["metros"]:
            known.update(metros.metros_for_state(code))
        bad = [m for m in config["metros"] if m not in known]
        self.assertEqual(bad, [], f"unknown metro slugs: {bad}")

    def test_the_screen_targets_acquirable_companies(self):
        """>= $500K EBITDA is roughly 20+ employees at home-services margins.
        A bar of 5 would fill the sheet with companies that cannot qualify."""
        path = os.path.join(os.path.dirname(HERE), "rotation.example.json")
        config = daily.load_config(path)
        self.assertGreaterEqual(config["min_employees"], 20)

    def test_partner_exclusions_are_wired_in(self):
        path = os.path.join(os.path.dirname(HERE), "rotation.example.json")
        config = daily.load_config(path)
        self.assertTrue(config.get("exclude_file"))
        self.assertTrue(os.path.exists(
            os.path.join(os.path.dirname(HERE), config["exclude_file"])))


class HeadcountGuardTest(unittest.TestCase):
    """A blocked profile page turns the size screen off without failing."""

    def test_all_blank_headcount_is_a_full_gap(self):
        rows = [{"employees": ""}, {"employees": ""}]
        self.assertEqual(daily.unknown_headcount(rows), 1.0)

    def test_all_populated_is_no_gap(self):
        rows = [{"employees": "27"}, {"employees": "40"}]
        self.assertEqual(daily.unknown_headcount(rows), 0.0)

    def test_an_empty_sheet_is_unknown_not_zero(self):
        self.assertIsNone(daily.unknown_headcount([]))

    def test_whitespace_counts_as_missing(self):
        self.assertEqual(daily.unknown_headcount([{"employees": "  "}]), 1.0)


class ExcludeListTest(unittest.TestCase):
    def setUp(self):
        import argparse

        import scraper
        path = os.path.join(os.path.dirname(HERE), "partners.exclude.txt")
        ns = argparse.Namespace(exclude_name="", exclude_domain="", exclude_file=path)
        self.names, self.domains = scraper.load_exclusions(ns)
        self.scraper = scraper

    def excluded(self, name):
        class L:
            company_name = name
            website = ""
        return self.scraper.excluded(L(), self.names, self.domains)

    def test_an_existing_partner_is_never_sourced_as_a_target(self):
        """AVI Roofing came back from a live Apollo query while testing."""
        self.assertTrue(self.excluded("AVI Roofing, Inc."))
        self.assertTrue(self.excluded("Ridgeline Roofing"))

    def test_generic_partner_names_do_not_drop_unrelated_companies(self):
        """'Apex' and 'Orion' as fragments would delete real targets."""
        for name in ("Apex Plumbing & Heating", "Orion Electric LLC",
                     "Cornett Roofing Systems", "Rogers Roofing",
                     "Zeus Mechanical"):
            self.assertFalse(self.excluded(name), name)


class DryRunOutputTest(unittest.TestCase):
    """A dry run exists to show the plan. One that prints only 'nothing was
    fetched' confirms nothing and is worse than useless -- it looks like a
    successful check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = os.path.join(self.tmp.name, "rotation.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(CONFIG, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def dry_run(self, date="2026-09-07"):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = daily.main(["--config", self.config,
                               "--export-dir", self.tmp.name,
                               "--state", os.path.join(self.tmp.name, "s.json"),
                               "--date", date, "--dry-run"])
        return code, buf.getvalue()

    def test_it_names_every_list_it_would_pull(self):
        _code, out = self.dry_run()
        self.assertIn("roofing-contractors", out)
        self.assertIn("plumber", out)
        self.assertIn("wichita-ks", out)

    def test_it_shows_the_screen_being_applied(self):
        _code, out = self.dry_run()
        self.assertIn("min employees", out)
        self.assertIn("20", out)

    def test_it_names_the_destination(self):
        _code, out = self.dry_run()
        self.assertIn(self.tmp.name, out)

    def test_a_quiet_day_says_so_rather_than_printing_nothing(self):
        _code, out = self.dry_run(date="2026-09-05")
        self.assertIn("nothing scheduled", out)

    def test_a_dry_run_never_overwrites_a_real_runs_status_file(self):
        """Otherwise checking the plan erases the morning's report."""
        status_path = os.path.join(self.tmp.name, "_daily-status.json")
        daily.write_status(self.tmp.name, {"date": "2026-09-07",
                                           "sheets": [{"file": "real.csv", "rows": 12}],
                                           "problems": []})
        self.dry_run()
        with open(status_path, encoding="utf-8") as fh:
            kept = json.load(fh)
        self.assertEqual(kept["sheets"][0]["file"], "real.csv")

    def test_missing_credentials_are_called_out_before_the_real_run(self):
        saved = os.environ.pop("HUBSPOT_TOKEN", None)
        try:
            _code, out = self.dry_run()
        finally:
            if saved is not None:
                os.environ["HUBSPOT_TOKEN"] = saved
        self.assertIn("HUBSPOT_TOKEN", out)



class PowerShellStructureTest(unittest.TestCase):
    """PowerShell can't run here, so its known failure modes are asserted."""

    def test_no_script_has_a_structural_defect(self):
        import subprocess
        root = os.path.dirname(HERE)
        result = subprocess.run(
            [sys.executable, os.path.join(root, "tools", "check_powershell.py")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_checker_catches_a_terminating_stderr_pipe(self):
        """The check must fail on the bug it exists for, or it proves nothing."""
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import check_powershell

        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.ps1")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write('$ErrorActionPreference = "Stop"\n'
                         'python thing.py 2>&1 | Tee-Object -FilePath $log\n')
            self.assertTrue(any("terminating" in p
                                for p in check_powershell.check(bad)))

    def test_the_checker_accepts_a_guarded_pipe(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import check_powershell

        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.ps1")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write('$ErrorActionPreference = "Stop"\n'
                         '$ErrorActionPreference = "Continue"\n'
                         'python thing.py 2>&1 | Tee-Object -FilePath $log\n'
                         '$ErrorActionPreference = "Stop"\n')
            self.assertEqual(check_powershell.check(good), [])



class ScreenVerdictTest(unittest.TestCase):
    """Three outcomes, not two.

    BBB's headcount is behind a Cloudflare challenge, so a company Apollo has
    never indexed cannot be sized at all. Calling that "passed" would put an
    unscreened row at the top of the sheet looking exactly like a screened one
    -- and those rows are the sleepers, the whole reason to scrape BBB.
    """

    def test_a_sized_company_over_the_bar_qualifies(self):
        self.assertEqual(daily.screen_verdict({"apollo_employees": "40"}, 20),
                         "QUALIFIED")

    def test_a_sized_company_under_the_bar_is_marked_too_small(self):
        self.assertEqual(daily.screen_verdict({"apollo_employees": "3"}, 20),
                         "TOO-SMALL")

    def test_an_unsized_company_is_never_reported_as_passing(self):
        for row in ({"apollo_employees": ""}, {}, {"apollo_employees": "  "},
                    {"apollo_employees": "n/a"}):
            self.assertEqual(daily.screen_verdict(row, 20), "REVIEW-UNSIZED", row)

    def test_no_bar_still_distinguishes_sized_from_unsized(self):
        self.assertEqual(daily.screen_verdict({"apollo_employees": "2"}, 0),
                         "QUALIFIED")
        self.assertEqual(daily.screen_verdict({}, 0), "REVIEW-UNSIZED")

    def test_a_float_headcount_is_read_not_discarded(self):
        self.assertEqual(daily.screen_verdict({"apollo_employees": "40.0"}, 20),
                         "QUALIFIED")


class ShippedConfigTest(unittest.TestCase):
    def config(self):
        return daily.load_config(
            os.path.join(os.path.dirname(HERE), "rotation.example.json"))

    def test_the_detail_pass_is_off_because_it_is_blocked(self):
        """Attempting it costs seconds per listing to re-prove a known block."""
        self.assertFalse(self.config().get("detail"))

    def test_the_size_bar_still_targets_acquirable_companies(self):
        self.assertGreaterEqual(self.config()["min_employees"], 20)

    def test_the_comment_tells_the_reader_what_screen_means(self):
        comment = " ".join(self.config()["_comment"])
        self.assertIn("REVIEW-UNSIZED", comment)
        self.assertIn("NOT screened", comment)



class TotalFailureTest(unittest.TestCase):
    """Every lookup failing is a broken endpoint, not bad luck.

    A run reported "133 errors, 0 matched" inside its summary and still exited
    as a success, so the sheets looked finished while the entire enrichment had
    done nothing -- no websites recovered, no sleeper labels written.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write_report(self, **apollo):
        csv_path = os.path.join(self.tmp.name, "sheet.csv")
        with open(csv_path.replace(".csv", ".json"), "w", encoding="utf-8") as fh:
            json.dump({"apollo_stats": apollo}, fh)
        return csv_path

    def test_counters_are_read_back_from_the_report(self):
        path = self.write_report(errors=133, matched=0)
        self.assertEqual(daily.read_report(path),
                         {"apollo_errors": 133, "apollo_matched": 0})

    def test_a_missing_report_is_not_an_error(self):
        self.assertEqual(daily.read_report(
            os.path.join(self.tmp.name, "nope.csv")), {})

    def test_a_corrupt_report_is_not_an_error(self):
        csv_path = os.path.join(self.tmp.name, "bad.csv")
        with open(csv_path.replace(".csv", ".json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(daily.read_report(csv_path), {})

    def test_some_failures_alongside_matches_are_not_flagged(self):
        """Individual misses are normal; only a total wipeout is a defect."""
        path = self.write_report(errors=3, matched=40)
        counters = daily.read_report(path)
        self.assertTrue(counters["apollo_matched"])


if __name__ == "__main__":
    unittest.main()
