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


if __name__ == "__main__":
    unittest.main()
