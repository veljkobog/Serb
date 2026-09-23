import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from odte.runs import (
    CONFIRMED, FADED, FLIPPED, HOLDING, NEW, Entry, Run, RunLog, compare,
)

T0 = datetime.fromisoformat("2026-09-22T10:30:00")


def entry(symbol="SPY", side="BULL", score=7.0, bias=55.0, spot=590.0,
          flow=0.3, source="side", em=1.2):
    return Entry(symbol=symbol, side=side, score=score, bias=bias, confidence=75.0,
                 spot=spot, vwap=586.0, em=em, flow_tilt=flow, flow_source=source,
                 verdict="LONG (calls)", band="STRONG", setups=["gap_and_go"])


def run_at(minutes, press=1, **kw):
    return Run(ts=(T0 + timedelta(minutes=minutes)).isoformat(), press=press,
               provider="test", entries={"SPY": entry(**kw)})


class TestRunLog(unittest.TestCase):
    def test_presses_increment_and_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = RunLog.for_date(Path(tmp), T0)
            self.assertEqual(log.next_press, 1)
            log.append(run_at(0, press=1))
            log.append(run_at(35, press=2))
            self.assertEqual(log.next_press, 3)

            reloaded = RunLog.for_date(Path(tmp), T0)
            self.assertEqual(len(reloaded.runs), 2)
            self.assertEqual(reloaded.first.press, 1)
            self.assertEqual(reloaded.last.press, 2)
            self.assertEqual(reloaded.first.entries["SPY"].side, "BULL")
            payload = json.loads(reloaded.path.read_text())
            self.assertEqual(payload["date"], "2026-09-22")

    def test_file_is_named_for_the_session_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(
                RunLog.for_date(Path(tmp), T0).path.name.endswith("runs-2026-09-22.json")
            )


class TestCompare(unittest.TestCase):
    def test_no_baseline_means_nothing_to_grade(self):
        self.assertEqual(compare(run_at(0), None), {})
        first = run_at(0)
        self.assertEqual(compare(first, first), {})

    def test_price_progress_and_a_held_score_confirms(self):
        later = run_at(35, press=2, spot=591.2, score=7.5)
        change = compare(later, run_at(0))["SPY"]
        self.assertEqual(change.status, CONFIRMED)
        self.assertEqual(change.minutes, 35.0)
        self.assertGreater(change.progress_em, 0.5)
        self.assertEqual(change.d_score, 0.5)

    def test_progress_is_signed_for_the_side(self):
        # A BEAR read that fell is progress, even though the price went down.
        first = Run(ts=T0.isoformat(), press=1, provider="t",
                    entries={"IWM": entry(symbol="IWM", side="BEAR", spot=220.0)})
        later = Run(ts=(T0 + timedelta(minutes=20)).isoformat(), press=2, provider="t",
                    entries={"IWM": entry(symbol="IWM", side="BEAR", spot=218.8)})
        change = compare(later, first)["IWM"]
        self.assertGreater(change.progress_em, 0)
        self.assertEqual(change.status, CONFIRMED)

    def test_bleeding_score_fades(self):
        change = compare(run_at(35, press=2, score=4.5), run_at(0))["SPY"]
        self.assertEqual(change.status, FADED)
        self.assertIn("score", change.note)

    def test_price_going_the_other_way_fades(self):
        change = compare(run_at(35, press=2, spot=589.0), run_at(0))["SPY"]
        self.assertEqual(change.status, FADED)
        self.assertLess(change.progress_em, 0)

    def test_side_change_flips(self):
        change = compare(run_at(35, press=2, side="BEAR", score=6.0), run_at(0))["SPY"]
        self.assertEqual(change.status, FLIPPED)
        self.assertIn("was BULL", change.note)
        self.assertEqual(change.baseline_side, "BULL")

    def test_standing_still_is_holding(self):
        change = compare(run_at(20, press=2, spot=590.05), run_at(0))["SPY"]
        self.assertEqual(change.status, HOLDING)

    def test_flow_flipping_under_a_standing_read_is_called_out(self):
        change = compare(run_at(20, press=2, spot=590.05, flow=-0.3), run_at(0))["SPY"]
        self.assertEqual(change.status, FADED)
        self.assertIn("flow flipped", change.note)

    def test_symbol_absent_from_the_first_read_is_new(self):
        later = Run(ts=(T0 + timedelta(minutes=30)).isoformat(), press=2, provider="t",
                    entries={"TSLA": entry(symbol="TSLA")})
        change = compare(later, run_at(0))["TSLA"]
        self.assertEqual(change.status, NEW)

    def test_missing_expected_move_leaves_progress_unknown(self):
        change = compare(run_at(30, press=2, em=None, spot=592.0), run_at(0, em=None))["SPY"]
        self.assertIsNone(change.progress_em)


if __name__ == "__main__":
    unittest.main()
