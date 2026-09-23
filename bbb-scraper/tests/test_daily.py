"""The 9am orchestrator: rotation, reporting, and failing loudly."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_the_review_bar_is_on_the_plan_too(self):
        """It is the signal that carries these sheets, and a stale config
        could switch it off without the plan ever showing it."""
        _code, out = self.dry_run()
        self.assertIn("min google revs", out)
        self.assertIn("30", out)

    def test_a_missing_google_key_is_called_out(self):
        """It warned about the other two keys and not this one. A live config
        lost its key and the plan looked perfectly healthy."""
        with mock.patch.dict(os.environ, {}, clear=True):
            _code, out = self.dry_run()
        self.assertIn("no Google key", out)

    def test_a_present_google_key_draws_no_warning(self):
        with mock.patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "AIza-test"}):
            _code, out = self.dry_run()
        self.assertNotIn("no Google key", out)

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


class MissingGoogleKeyTest(unittest.TestCase):
    """Screening on review counts with no key is not a screen: every row comes
    back unsized and the sheet still looks full."""

    def config(self, **over):
        config = dict(CONFIG, min_google_reviews=30)
        config.update(over)
        return config

    def run_status(self, config, env):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, env, clear=True):
                return daily.run(config, tmp, MONDAY,
                                 os.path.join(tmp, "s.json"), dry_run=True)

    def test_the_run_reports_it_rather_than_sizing_on_apollo_in_silence(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(daily, "scrape", return_value=1):
                    status = daily.run(config, tmp, MONDAY,
                                       os.path.join(tmp, "s.json"))
        self.assertTrue(any("no Google key" in p for p in status["problems"]),
                        status["problems"])

    def test_a_config_that_does_not_screen_on_reviews_is_left_alone(self):
        """A bar of 0 means do not screen on this signal, so no key is needed."""
        config = self.config(min_google_reviews=0)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(daily, "scrape", return_value=1):
                    status = daily.run(config, tmp, MONDAY,
                                       os.path.join(tmp, "s.json"))
        self.assertFalse([p for p in status["problems"] if "Google key" in p])

    def test_a_dry_run_says_nothing_it_cannot_know(self):
        """The plan warns; the problem list belongs to a real run."""
        status = self.run_status(self.config(), {})
        self.assertEqual(status["problems"], [])



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

    def test_a_zero_bar_means_do_not_screen_on_that_signal(self):
        """Not "everything passes" -- otherwise switching a criterion off
        would qualify every row that happens to carry the field."""
        self.assertEqual(daily.screen_verdict({"apollo_employees": "2"}, 0),
                         "REVIEW-UNSIZED")
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



class CategoryAllowTest(unittest.TestCase):
    """BBB's search returns adjacent trades.

    A Charlotte roofing pull came back with a carport installer, a
    stamped-concrete company and a general contractor -- 3 of 12 rows nobody
    is buying. Nothing downstream can tell they are off-target, so the sheet
    just looks 25% weaker for no visible reason.
    """

    def config(self):
        return daily.load_config(
            os.path.join(os.path.dirname(HERE), "rotation.example.json"))

    def test_every_scheduled_vertical_has_an_allow_list(self):
        config = self.config()
        allow = config.get("category_allow") or {}
        scheduled = {c for day in config["schedule"].values() for c in day}
        self.assertEqual(scheduled - set(allow), set(),
                         "a scheduled vertical with no allow-list keeps everything")

    def keeps(self, category, vertical="roofing-contractors"):
        """Assert on behaviour, not on the fragments that produce it.

        The literal fragment changed once already -- "roof" was tightened to
        "roofing" so it would stop matching roof-inspection -- and a test
        pinned to the string failed while the behaviour was correct.
        """
        config = self.config()
        allow = config["category_allow"][vertical]
        deny = config.get("category_deny") or []
        low = category.lower()
        return any(a in low for a in allow) and not any(d in low for d in deny)

    def test_roofing_keeps_the_exteriors_trades_partners_buy(self):
        for category in ("roofing-contractors", "commercial-roofing",
                         "residential-roofing", "siding-contractors",
                         "replacement-windows", "gutter-services"):
            self.assertTrue(self.keeps(category), category)

    def test_roofing_rejects_the_trades_that_diluted_the_real_sheet(self):
        for category in ("carport", "stamped-concrete", "general-contractor",
                         "construction-services", "commercial-renovation",
                         "building-restoration", "home-improvement"):
            self.assertFalse(self.keeps(category), category)

    def test_roofing_rejects_service_providers_to_the_trade(self):
        for category in ("roof-inspection", "public-adjuster"):
            self.assertFalse(self.keeps(category), category)

    def test_the_flag_reaches_the_scraper(self):
        import inspect
        source = inspect.getsource(daily.scrape)
        self.assertIn("--category-allow", source)



class NationalChainTest(unittest.TestCase):
    """National platforms and franchises are competitors to the EL'd partners,
    not founder-owned tuck-in targets. DaBella and Bumble Roofing both landed
    on real sheets."""

    def config(self):
        return daily.load_config(
            os.path.join(os.path.dirname(HERE), "rotation.example.json"))

    def excluded(self, name):
        import argparse

        import scraper
        ns = argparse.Namespace(
            exclude_name=",".join(self.config()["exclude_names"]),
            exclude_domain="", exclude_file=None)
        names, domains = scraper.load_exclusions(ns)

        class L:
            company_name = name
            website = ""
        return scraper.excluded(L(), names, domains)

    def test_the_chains_seen_on_real_sheets_are_excluded(self):
        self.assertTrue(self.excluded("DaBella"))
        self.assertTrue(self.excluded("Bumble Roofing of Charlotte"))

    def test_a_franchise_is_caught_under_any_of_its_local_names(self):
        """'Benjamin Franklin of Orlando' reached a live sheet as QUALIFIED
        with 1,882 Google reviews: the list said 'Benjamin Franklin Plumbing',
        which is not a substring of how that city spells it."""
        for name in ("Benjamin Franklin Plumbing of Tampa",
                     "Benjamin Franklin of Orlando",
                     "One Hour Heating & Air Conditioning",
                     "One Hour Air Conditioning and Heating of KC",
                     "Mister Sparky Electric"):
            self.assertTrue(self.excluded(name), name)

    def test_punctuation_does_not_hide_a_chain(self):
        """BBB spells the same brand both ways from one city to the next."""
        for name in ("Roto-Rooter Plumbing & Water Cleanup",
                     "Roto Rooter Services Company",
                     "Mr. Rooter Plumbing of Orlando",
                     "Mr Rooter Plumbing"):
            self.assertTrue(self.excluded(name), name)

    def test_independent_operators_are_untouched(self):
        for name in ("Horizon Roofing", "Merritt Roofing, LLC",
                     "Tribe Built Roofing, LLC", "Four Peaks Roofing",
                     "Rob's Roofing LLC",
                     # Shorter stems must not start swallowing real targets:
                     # a first name, a word inside a longer word, and a
                     # champion who does not sell windows.
                     "Franklin Plumbing & Drain", "Benjamin Heating Co",
                     "Champion Roofing & Siding", "Sparky's Electric LLC",
                     "Window World of Central Florida",
                     "Rooter Ranger Plumbing"):
            self.assertFalse(self.excluded(name), name)

    def test_the_exclusions_reach_the_scraper(self):
        import inspect
        self.assertIn("--exclude-name", inspect.getsource(daily.scrape))



class EnrichmentReportTest(unittest.TestCase):
    """"12 rows" says nothing about whether those rows are contactable, which
    is the only question worth asking of a lead sheet. It was previously only
    answerable by opening the file, or by spotting the gap between rows
    written and rows the CRM check saw."""

    def render(self, status):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily.print_enrichment(status)
        return buf.getvalue()

    def status(self, **over):
        detail = {"sheet": "s.csv", "emails": 5, "credits_spent": 6}
        detail.update(over)
        return {"sheets": [{"file": "s.csv", "rows": 12}], "enrichment": [detail]}

    def test_it_reports_the_email_count_and_share(self):
        out = self.render(self.status())
        self.assertIn("12 rows, 5 with an email (42%)", out)

    def test_zero_emails_is_stated_plainly(self):
        out = self.render(self.status(emails=0))
        self.assertIn("0 with an email (0%)", out)

    def test_an_empty_sheet_does_not_divide_by_zero(self):
        status = {"sheets": [{"file": "s.csv", "rows": 0}],
                  "enrichment": [{"sheet": "s.csv", "emails": 0}]}
        self.assertIn("n/a", self.render(status))

    def test_the_size_gate_is_visible(self):
        """And says the rows were kept -- they used to be deleted -- without
        reading as a verdict: Apollo's headcount is one signal of two."""
        out = self.render(self.status(apollo_small_headcount=3))
        self.assertIn("3 under the headcount bar on Apollo alone", out)
        self.assertIn("kept", out)
        self.assertIn("the screen column decides", out)

    def test_unsized_rows_are_named_as_such(self):
        out = self.render(self.status(size_unknown=9))
        self.assertIn("9 unsized (not in Apollo)", out)

    def test_a_withheld_contact_is_reported(self):
        """A match in the wrong city keeps the company but not the email."""
        out = self.render(self.status(wrong_place=1))
        self.assertIn("wrong city", out)

    def test_a_skipped_crm_check_is_never_silent(self):
        out = self.render(self.status(crm={"skipped": "no HUBSPOT_TOKEN"}))
        self.assertIn("no HUBSPOT_TOKEN", out)

    def test_enrichment_that_never_ran_says_why(self):
        out = self.render(self.status(skipped="no APOLLO_API_KEY"))
        self.assertIn("not enriched: no APOLLO_API_KEY", out)

    def test_no_sheets_prints_nothing(self):
        self.assertEqual(self.render({"sheets": [], "enrichment": []}), "")

    def test_a_sheet_with_no_enrichment_record_still_reports_rows(self):
        status = {"sheets": [{"file": "s.csv", "rows": 7}], "enrichment": []}
        self.assertIn("7 rows", self.render(status))



class SmallCompaniesStayTest(unittest.TestCase):
    """A sized, small company is information; a deleted one is nothing.

    Real Austin and Fort Worth sheets came back with zero emails while the run
    reported matches, because the size gate removed each row that had a
    contact before it reached the sheet. Apollo's coverage of trade
    contractors skews small, so the rows it can size are the rows it can
    contact.
    """

    def test_too_small_is_a_sort_position_not_a_deletion(self):
        import inspect
        source = inspect.getsource(daily.enrich_contacts)
        self.assertNotIn('startswith("dropped:")', source,
                         "rows under the bar are being deleted again")
        self.assertIn("TOO-SMALL", inspect.getsource(daily.enrich_contacts))

    def test_the_sheet_orders_qualified_first_and_small_last(self):
        order = {"QUALIFIED": 0, "REVIEW-UNSIZED": 1, "REVIEW": 2, "TOO-SMALL": 3}
        rows = [{"screen": s} for s in
                ("TOO-SMALL", "REVIEW-UNSIZED", "QUALIFIED", "REVIEW")]
        rows.sort(key=lambda r: order.get(r["screen"], 4))
        self.assertEqual([r["screen"] for r in rows],
                         ["QUALIFIED", "REVIEW-UNSIZED", "REVIEW", "TOO-SMALL"])

    def test_contactable_rows_sort_above_uncontactable_ones(self):
        rows = [{"screen": "REVIEW-UNSIZED", "email": ""},
                {"screen": "REVIEW-UNSIZED", "email": "a@b.com"}]
        rows.sort(key=lambda r: (0, 0 if r.get("email") else 1))
        self.assertEqual(rows[0]["email"], "a@b.com")

    def test_the_report_says_they_were_kept(self):
        import io
        from contextlib import redirect_stdout
        status = {"sheets": [{"file": "s.csv", "rows": 12}],
                  "enrichment": [{"sheet": "s.csv", "emails": 4,
                                  "apollo_small_headcount": 3}]}
        buf = io.StringIO()
        with redirect_stdout(buf):
            daily.print_enrichment(status)
        self.assertIn("kept", buf.getvalue())



class SizeEvidenceTest(unittest.TestCase):
    """Headcount alone is a poor measure for a trade contractor.

    Crews are not on LinkedIn, so Apollo reports a twenty-truck roofing
    company as eight people. Google review volume tracks jobs completed, which
    is closer to revenue, so either signal clearing its bar qualifies a row.
    """

    BAR_EMPLOYEES = 20
    BAR_REVIEWS = 150

    def verdict(self, **row):
        return daily.size_evidence(row, self.BAR_EMPLOYEES, self.BAR_REVIEWS)

    def test_reviews_qualify_a_company_apollo_undercounts(self):
        """The case this exists for."""
        verdict, why = self.verdict(apollo_employees="8", google_reviews="600")
        self.assertEqual(verdict, "QUALIFIED")
        self.assertIn("600 Google reviews", why)
        self.assertIn("Apollo says 8", why, "the disagreement should be visible")

    def test_headcount_alone_still_qualifies(self):
        self.assertEqual(self.verdict(apollo_employees="40")[0], "QUALIFIED")

    def test_reviews_alone_still_qualify(self):
        self.assertEqual(self.verdict(google_reviews="600")[0], "QUALIFIED")

    def test_small_on_both_signals_is_too_small(self):
        verdict, why = self.verdict(apollo_employees="8", google_reviews="30")
        self.assertEqual(verdict, "TOO-SMALL")
        self.assertIn("8 employees", why)
        self.assertIn("30 Google reviews", why)

    def test_no_signal_at_all_is_unsized(self):
        self.assertEqual(self.verdict()[0], "REVIEW-UNSIZED")

    def test_a_weak_google_match_is_not_used_as_evidence(self):
        """A low-confidence match is somebody else's review count."""
        self.assertEqual(
            self.verdict(google_reviews="600", google_match="low")[0],
            "REVIEW-UNSIZED")
        self.assertEqual(
            self.verdict(google_reviews="600", google_match="high")[0],
            "QUALIFIED")

    def test_unparseable_values_are_unknown_not_zero(self):
        """Zero would read as evidence of smallness."""
        self.assertEqual(self.verdict(apollo_employees="n/a")[0], "REVIEW-UNSIZED")
        self.assertEqual(self.verdict(google_reviews="")[0], "REVIEW-UNSIZED")

    def test_the_evidence_is_always_stated(self):
        for row in ({"apollo_employees": "40"}, {"google_reviews": "600"},
                    {"apollo_employees": "8", "google_reviews": "30"}, {}):
            _verdict, why = daily.size_evidence(row, 20, 150)
            self.assertTrue(why, f"no evidence recorded for {row}")

    def test_with_no_review_bar_it_falls_back_to_headcount(self):
        self.assertEqual(
            daily.size_evidence({"apollo_employees": "8", "google_reviews": "600"},
                                20, 0)[0], "TOO-SMALL")

    def test_the_shipped_config_sets_a_review_bar(self):
        config = daily.load_config(
            os.path.join(os.path.dirname(HERE), "rotation.example.json"))
        self.assertGreater(config.get("min_google_reviews", 0), 0)
        self.assertIn("max_google_lookups", config)

    def test_google_enrichment_does_not_filter_rows_out(self):
        """--min-google-reviews would delete rows during the scrape; the
        screen has to weigh them instead, or a company with no Google listing
        disappears rather than being marked unsized."""
        import inspect
        code = [line.split("#", 1)[0]
                for line in inspect.getsource(daily.scrape).splitlines()]
        code = "\n".join(code)
        self.assertIn("--google-key", code)
        self.assertNotIn("--min-google-reviews", code,
                         "the scrape must not filter on reviews")



class CmdletNameTest(unittest.TestCase):
    """A misspelled cmdlet fails only when the script is run.

    `New-ScheduledTaskSettings` shipped in place of
    `New-ScheduledTaskSettingsSet` and got as far as the user: PowerShell
    cannot run where these are written, so nothing catches a name that does
    not exist until someone types the command.
    """

    def checker(self):
        sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
        import check_powershell
        return check_powershell

    def test_the_real_scripts_use_real_cmdlets(self):
        import subprocess
        root = os.path.dirname(HERE)
        result = subprocess.run(
            [sys.executable, os.path.join(root, "tools", "check_powershell.py")],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_exact_typo_that_shipped_is_caught(self):
        check = self.checker()
        self.assertEqual(
            check.unknown_cmdlets(["$s = New-ScheduledTaskSettings -WakeToRun"]),
            ["New-ScheduledTaskSettings"])

    def test_the_correct_name_passes(self):
        check = self.checker()
        self.assertEqual(
            check.unknown_cmdlets(["$s = New-ScheduledTaskSettingsSet -WakeToRun"]),
            [])

    def test_cmdlets_outside_the_checked_modules_are_left_alone(self):
        """Listing every built-in would make this a burden that gets switched
        off, so only the scheduler cmdlets are covered."""
        check = self.checker()
        self.assertEqual(
            check.unknown_cmdlets(["Write-Host x", "Join-Path a b",
                                   "Invoke-RestMethod -Uri y"]), [])

    def test_a_comment_mentioning_a_typo_is_not_a_finding(self):
        """Comments are stripped before scanning -- the checker was fooled by
        its own explanatory comment once already."""
        import tempfile as tf
        check = self.checker()
        with tf.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.ps1")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# not New-ScheduledTaskSettings, which is wrong\n"
                         "New-ScheduledTaskSettingsSet -WakeToRun\n")
            self.assertEqual(check.check(path), [])



class StaleConfigTest(unittest.TestCase):
    """rotation.json is the user's own file and an update never overwrites it,
    so a key added later is simply absent -- and a size bar read as 0 means
    "do not screen on this signal". A real run therefore had the whole Google
    signal switched off while its sheets carried 341 unused reviews."""

    def config(self, **keys):
        import json
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            body = {"metros": ["x"], "schedule": {"monday": ["plumber"]}}
            body.update(keys)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
            return daily.load_config(path)

    def test_a_missing_review_bar_falls_back_to_the_default(self):
        self.assertEqual(self.config()["min_google_reviews"],
                         daily.CONFIG_DEFAULTS["min_google_reviews"])

    def test_an_explicit_value_is_never_overridden(self):
        self.assertEqual(self.config(min_google_reviews=300)["min_google_reviews"],
                         300)

    def test_an_explicit_zero_is_respected(self):
        """Setting a bar to 0 is a real choice: do not screen on it."""
        self.assertEqual(self.config(min_google_reviews=0)["min_google_reviews"], 0)

    def test_the_defaulted_keys_are_named(self):
        defaulted = self.config(min_employees=20)["_defaulted"]
        self.assertIn("min_google_reviews", defaulted)
        self.assertNotIn("min_employees", defaulted)

    def test_a_complete_config_reports_nothing_defaulted(self):
        self.assertEqual(self.config(**daily.CONFIG_DEFAULTS)["_defaulted"], [])


class WeakGoogleMatchTest(unittest.TestCase):
    """A low-confidence match may be somebody else's review count, so it never
    qualifies a row. Discarding it silently threw away real signal: a sheet
    carried a company with 341 reviews reported as having no size signal."""

    def verdict(self, **row):
        return daily.size_evidence(row, 20, 150)

    def test_a_weak_match_never_qualifies(self):
        verdict, _why = self.verdict(google_reviews="341", google_match="low")
        self.assertEqual(verdict, "REVIEW-UNSIZED")

    def test_but_the_count_is_reported_for_verification(self):
        _verdict, why = self.verdict(google_reviews="341", google_match="low")
        self.assertIn("341", why)
        self.assertIn("low-confidence", why)
        self.assertIn("verify", why)

    def test_a_weak_match_below_the_bar_is_still_named(self):
        _verdict, why = self.verdict(google_reviews="25", google_match="low")
        self.assertIn("25", why)
        self.assertIn("low-confidence", why)

    def test_a_strong_match_over_the_bar_qualifies_normally(self):
        verdict, why = self.verdict(google_reviews="341", google_match="high")
        self.assertEqual(verdict, "QUALIFIED")
        self.assertNotIn("low-confidence", why)

    def test_no_google_data_at_all_says_so(self):
        _verdict, why = self.verdict()
        self.assertEqual(why, "no size signal available")



class RotationShapeTest(unittest.TestCase):
    """Landscaping was dropped after four lists across four metros returned
    0-2 Apollo matches and at most one email between them -- and that one was
    a single-employee company."""

    def config(self):
        return daily.load_config(
            os.path.join(os.path.dirname(HERE), "rotation.example.json"))

    def scheduled(self):
        return [c for day in self.config()["schedule"].values() for c in day]

    def test_landscaping_is_not_scheduled(self):
        self.assertNotIn("landscape-contractors", self.scheduled())

    def test_the_slots_went_to_trades_that_enrich(self):
        """HVAC and roofing land 3-6 Apollo matches; landscaping landed 0-2."""
        from collections import Counter
        counts = Counter(self.scheduled())
        self.assertGreaterEqual(counts["roofing-contractors"], 3)
        self.assertGreaterEqual(counts["heating-and-air-conditioning"], 2)
        self.assertGreaterEqual(counts["plumber"], 2)

    def test_the_week_still_holds_ten_lists(self):
        self.assertEqual(len(self.scheduled()), 10)

    def test_landscaping_can_still_be_put_back(self):
        """The allow-list keeps it, so re-adding it is a one-line change."""
        self.assertIn("landscape-contractors", self.config()["category_allow"])

    def test_the_review_bar_matches_what_real_sheets_carry(self):
        """Memphis topped out at 341 with most under 40; 150 disqualified
        companies that were not small."""
        self.assertEqual(self.config()["min_google_reviews"], 30)
        self.assertEqual(daily.CONFIG_DEFAULTS["min_google_reviews"], 30)

    def test_a_thirty_review_company_now_qualifies(self):
        verdict, why = daily.size_evidence(
            {"google_reviews": "34", "google_match": "high"}, 20, 30)
        self.assertEqual(verdict, "QUALIFIED")
        self.assertIn("34 Google reviews", why)

    def test_a_genuinely_tiny_shop_still_does_not(self):
        verdict, _why = daily.size_evidence(
            {"google_reviews": "2", "google_match": "high"}, 20, 30)
        self.assertEqual(verdict, "TOO-SMALL")


if __name__ == "__main__":
    unittest.main()
