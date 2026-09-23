import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

from odte.config import Gates
from odte.configure import run_configure
from odte.live import parse_clock_time, press_times, run_live
from odte.session import to_et

MORNING = to_et(datetime.fromisoformat("2026-09-23T10:00:00"))


class FakeClock:
    """A clock the test drives: sleeping moves time forward instantly."""

    def __init__(self, start: datetime):
        self.now = start
        self.slept = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += timedelta(seconds=seconds)


def live_args(out_dir: str, **over) -> argparse.Namespace:
    args = argparse.Namespace(
        provider="synthetic", snapshot_path=None, interval="5m", as_of=None,
        force=True, sides=None, stream_seconds=0.0, stream_contracts=40,
        min_bias=Gates.min_bias_to_trade, min_confidence=Gates.min_confidence_to_trade,
        workers=4, save_snapshot=None, universe="index", symbols="SPY,QQQ",
        benchmark="SPY", tradier_token=None, tradier_env=None,
        first="10:05", every=45.0, until="15:30", presses=2, top=3,
        out_dir=out_dir, fresh=False, record=False,
    )
    for k, v in over.items():
        setattr(args, k, v)
    return args


class TestSchedule(unittest.TestCase):
    def test_presses_run_from_first_to_until(self):
        times = press_times(MORNING, "10:05", 45, "15:30", 8)
        self.assertEqual([f"{t:%H:%M}" for t in times][:3], ["10:05", "10:50", "11:35"])
        self.assertLessEqual(times[-1].hour, 15)

    def test_starting_late_presses_immediately(self):
        late = to_et(datetime.fromisoformat("2026-09-23T13:10:00"))
        first = press_times(late, "10:05", 45, "15:30", 8)[0]
        self.assertEqual(f"{first:%H:%M}", "13:10")

    def test_now_starts_at_once(self):
        self.assertEqual(parse_clock_time(MORNING, "now"), MORNING)

    def test_max_presses_is_respected(self):
        self.assertEqual(len(press_times(MORNING, "10:05", 5, "15:30", 3)), 3)

    def test_nothing_left_today_is_empty(self):
        evening = to_et(datetime.fromisoformat("2026-09-23T19:00:00"))
        self.assertEqual(press_times(evening, "10:05", 45, "15:30", 8), [])


class TestRunLive(unittest.TestCase):
    def test_a_session_presses_on_schedule_and_grades_itself(self):
        clock = FakeClock(MORNING)
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = run_live(
                    live_args(tmp, first="10:05", every=10, presses=2),
                    clock=clock, sleeper=clock.sleep,
                )
            text = out.getvalue()
            self.assertEqual(code, 0)
            self.assertIn("OPENING READ", text)
            self.assertIn("RE-CONFIRM #1", text)
            # It waited for 10:05, then ten more minutes.
            self.assertGreaterEqual(clock.slept, 15 * 60 - 5)
            log = json.loads(next(Path(tmp).glob("runs/runs-*.json")).read_text())
            self.assertEqual([r["press"] for r in log["runs"]], [1, 2])
            self.assertTrue((Path(tmp) / "latest.json").exists())

    def test_a_finished_day_says_so_instead_of_hanging(self):
        clock = FakeClock(to_et(datetime.fromisoformat("2026-09-23T19:00:00")))
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = run_live(live_args(tmp), clock=clock, sleeper=clock.sleep)
        self.assertEqual(code, 2)
        self.assertIn("already passed", err.getvalue())

    def test_a_configuration_failure_stops_the_session(self):
        clock = FakeClock(MORNING)
        with tempfile.TemporaryDirectory() as tmp:
            args = live_args(tmp, provider="snapshot", first="now", every=1, presses=3)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = run_live(args, clock=clock, sleeper=clock.sleep)
        self.assertEqual(code, 2)
        self.assertIn("snapshot", err.getvalue())
        self.assertLess(clock.slept, 120)  # bailed on the first press, didn't loop

    def test_recording_is_skipped_for_non_tradier_providers(self):
        clock = FakeClock(MORNING)
        with tempfile.TemporaryDirectory() as tmp:
            args = live_args(tmp, first="now", every=0, presses=1, record=True)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                run_live(args, clock=clock, sleeper=clock.sleep)
        self.assertIn("needs the tradier provider", err.getvalue())


class TestConfigure(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in ("TRADIER_TOKEN", "TRADIER_ENV")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_token_is_stored_and_never_printed_in_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            args = argparse.Namespace(
                token="tok_abcdefghijklmnop", env="sandbox", reset=False,
                no_doctor=True, non_interactive=True, stream_seconds=0.0,
                env_file=str(path),
            )
            out = io.StringIO()
            with redirect_stdout(out):
                code = run_configure(args)
            self.assertEqual(code, 0)
            self.assertIn("TRADIER_TOKEN=tok_abcdefghijklmnop", path.read_text())
            self.assertIn("TRADIER_ENV=sandbox", path.read_text())
            self.assertNotIn("tok_abcdefghijklmnop", out.getvalue())
            self.assertEqual(os.environ["TRADIER_TOKEN"], "tok_abcdefghijklmnop")

    def test_non_interactive_without_a_token_refuses(self):
        os.environ.pop("TRADIER_TOKEN", None)
        args = argparse.Namespace(
            token=None, env=None, reset=True, no_doctor=True,
            non_interactive=True, stream_seconds=0.0, env_file=None,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run_configure(args), 2)


if __name__ == "__main__":
    unittest.main()
