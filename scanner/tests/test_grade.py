import argparse
import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from odte.grade import (
    Bucket, Result, bucket_by, collect, grade_run_log, render, run_grade,
)
from odte.runs import Entry, Run, RunLog

T0 = datetime.fromisoformat("2026-09-24T10:05:00")


def entry(symbol="SPY", side="BULL", spot=590.0, em=1.0, score=7.0,
          band="STRONG", setups=("gap_and_go",)):
    return Entry(symbol=symbol, side=side, score=score, bias=55.0, confidence=75.0,
                 spot=spot, vwap=586.0, em=em, flow_tilt=0.3, flow_source="side",
                 verdict="LONG (calls)", band=band, setups=list(setups))


def log_with(entries_per_press, tmp: Path, day="2026-09-24") -> RunLog:
    log = RunLog(tmp / "runs" / f"runs-{day}.json")
    for i, entries in enumerate(entries_per_press, start=1):
        log.append(
            Run(ts=(T0 + timedelta(minutes=45 * (i - 1))).isoformat(), press=i,
                provider="test", entries={e.symbol: e for e in entries})
        )
    return log


class TestGrading(unittest.TestCase):
    def test_a_single_press_has_nothing_to_settle_against(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with([[entry()]], Path(tmp))
            self.assertEqual(grade_run_log(log), [])

    def test_bull_progress_is_positive_when_price_rises(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with([[entry(spot=590.0, em=2.0)], [entry(spot=593.0)]], Path(tmp))
            r = grade_run_log(log)[0]
            self.assertAlmostEqual(r.progress_em, 1.5)
            self.assertTrue(r.hit)
            self.assertTrue(r.full_em)
            self.assertEqual(r.minutes_held, 45.0)

    def test_bear_progress_is_positive_when_price_falls(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with(
                [[entry(side="BEAR", spot=590.0, em=2.0)], [entry(side="BEAR", spot=588.0)]],
                Path(tmp),
            )
            r = grade_run_log(log)[0]
            self.assertAlmostEqual(r.progress_em, 1.0)
            self.assertTrue(r.hit)

    def test_a_read_that_went_the_wrong_way_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with([[entry(spot=590.0, em=1.0)], [entry(spot=589.0)]], Path(tmp))
            r = grade_run_log(log)[0]
            self.assertAlmostEqual(r.progress_em, -1.0)
            self.assertFalse(r.hit)
            self.assertFalse(r.full_em)

    def test_every_press_but_the_last_is_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with(
                [[entry(spot=590.0)], [entry(spot=591.0)], [entry(spot=592.0)]], Path(tmp)
            )
            graded = grade_run_log(log)
            self.assertEqual([r.press for r in graded], [1, 2])
            self.assertTrue(all(r.final_spot == 592.0 for r in graded))

    def test_missing_expected_move_is_left_ungraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with([[entry(em=None)], [entry(spot=595.0)]], Path(tmp))
            r = grade_run_log(log)[0]
            self.assertIsNone(r.progress_em)
            self.assertIsNone(r.hit)

    def test_a_symbol_absent_from_the_last_press_is_left_ungraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = log_with([[entry(symbol="TSLA")], [entry(symbol="SPY")]], Path(tmp))
            self.assertIsNone(grade_run_log(log)[0].progress_em)


class TestBuckets(unittest.TestCase):
    def results(self):
        return [
            Result("d", "SPY", 1, "BULL", 8.0, "STRONG", ["gap_and_go"], 1, 1, 1.5, 45),
            Result("d", "QQQ", 1, "BULL", 7.0, "STRONG", ["gap_and_go", "magnet_up"], 1, 1, -0.5, 45),
            Result("d", "IWM", 1, "BEAR", 4.0, "LEAN", ["vwap_rejection"], 1, 1, 0.2, 45),
            Result("d", "DIA", 1, "BULL", 4.0, "LEAN", [], 1, 1, None, 45),
        ]

    def test_bucket_stats(self):
        b = Bucket("x")
        for r in self.results():
            b.add(r)
        self.assertEqual(b.reads, 3)          # the ungraded one is skipped
        self.assertEqual(b.hits, 2)
        self.assertEqual(b.full, 1)
        self.assertAlmostEqual(b.hit_rate, 2 / 3)
        self.assertAlmostEqual(b.mean_em, (1.5 - 0.5 + 0.2) / 3)
        self.assertAlmostEqual(b.worst_em, -0.5)
        self.assertAlmostEqual(b.best_em, 1.5)

    def test_empty_bucket_reports_nothing_rather_than_zero(self):
        b = Bucket("x")
        for stat in (b.hit_rate, b.mean_em, b.median_em, b.worst_em, b.best_em):
            self.assertIsNone(stat)

    def test_bucketing_by_band_setup_and_symbol(self):
        rs = self.results()
        bands = bucket_by(rs, lambda r: [r.band])
        self.assertEqual(bands["STRONG"].reads, 2)
        setups = bucket_by(rs, lambda r: r.setups or ["(none)"])
        self.assertEqual(setups["gap_and_go"].reads, 2)
        self.assertEqual(setups["magnet_up"].reads, 1)
        self.assertEqual(bucket_by(rs, lambda r: [r.symbol])["SPY"].hits, 1)


class TestRenderAndCli(unittest.TestCase):
    def test_render_lays_out_every_cut(self):
        rs = [
            Result("2026-09-24", "SPY", 1, "BULL", 8.0, "STRONG", ["gap_and_go"], 590, 592, 1.2, 45),
            Result("2026-09-24", "IWM", 1, "BEAR", 4.0, "LEAN", ["vwap_rejection"], 220, 221, -0.6, 45),
        ]
        text = render(rs, ["2026-09-24"])
        for heading in ("BY BAND", "OVERALL", "BY SETUP", "BY SYMBOL", "STRONG", "gap_and_go"):
            self.assertIn(heading, text)

    def test_render_says_so_when_nothing_settled(self):
        self.assertIn("Nothing to grade", render([], []))

    def test_collect_spans_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            log_with([[entry(spot=590.0)], [entry(spot=592.0)]], out, day="2026-09-23")
            log_with([[entry(spot=500.0)], [entry(spot=499.0)]], out, day="2026-09-24")
            results, dates = collect(out, None)
            self.assertEqual(dates, ["2026-09-23", "2026-09-24"])
            self.assertEqual(len(results), 2)
            only, dates = collect(out, "2026-09-24")
            self.assertEqual(dates, ["2026-09-24"])
            self.assertEqual(len(only), 1)

    def test_cli_grades_and_writes_the_detail_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            log_with([[entry(spot=590.0, em=2.0)], [entry(spot=593.0)]], out)
            csv_path = out / "grades.csv"
            args = argparse.Namespace(out_dir=str(out), date=None, csv=str(csv_path))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(run_grade(args), 0)
            self.assertIn("BY BAND", buf.getvalue())
            with csv_path.open() as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbol"], "SPY")
            self.assertEqual(rows[0]["hit"], "True")

    def test_cli_with_no_sessions_explains_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(out_dir=tmp, date=None, csv=None)
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(run_grade(args), 1)
            self.assertIn("two presses", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
