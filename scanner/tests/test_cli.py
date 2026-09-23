import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path

from odte import session
from odte.cli import _record_deadline, resolve_symbols, run as _run, build_parser
from odte.providers import get_provider
from odte.providers.snapshot import save_snapshots

AS_OF = "2026-09-22T10:30:00"


def run(argv):
    """Run the CLI with its console output captured, so test output stays readable."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return _run(argv)


class TestCli(unittest.TestCase):
    def test_synthetic_scan_runs(self):
        code = run(["--provider", "synthetic", "--as-of", AS_OF, "--universe", "index"])
        self.assertEqual(code, 0)

    def test_outside_window_requires_force(self):
        args = ["--provider", "synthetic", "--as-of", "2026-09-22T15:45:00", "--symbols", "SPY"]
        self.assertEqual(run(args), 2)
        self.assertEqual(run(args + ["--force"]), 0)

    def test_weekend_is_outside_the_window(self):
        self.assertFalse(session.is_trading_day(date(2026, 9, 26)))  # Saturday
        self.assertFalse(session.in_scan_window(datetime.fromisoformat("2026-09-26T10:30:00")))

    def test_outputs_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            code = run([
                "scan", "--provider", "synthetic", "--as-of", AS_OF, "--symbols", "SPY,QQQ,NVDA",
                "--csv", str(out / "scan.csv"),
                "--json", str(out / "scan.json"),
                "--html", str(out / "scan.html"),
            ])
            self.assertEqual(code, 0)
            csv_text = (out / "scan.csv").read_text()
            self.assertIn("symbol,rating,bias,confidence,verdict", csv_text)
            self.assertEqual(len(csv_text.strip().splitlines()), 4)  # header + 3
            payload = json.loads((out / "scan.json").read_text())
            self.assertEqual(len(payload["scans"]), 3)
            self.assertIn("weights", payload["meta"])
            self.assertIn("<table>", (out / "scan.html").read_text())

    def test_snapshot_roundtrip_reproduces_the_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_path = Path(tmp) / "snap.json"
            live_json = Path(tmp) / "live.json"
            replay_json = Path(tmp) / "replay.json"
            base = ["--as-of", AS_OF, "--symbols", "SPY,QQQ,IWM"]
            self.assertEqual(
                run(["scan", "--provider", "synthetic"] + base
                    + ["--save-snapshot", str(snap_path), "--json", str(live_json)]),
                0,
            )
            self.assertEqual(
                run(["scan", "--provider", "snapshot", "--snapshot-path", str(snap_path)] + base
                    + ["--json", str(replay_json)]),
                0,
            )
            live = json.loads(live_json.read_text())["scans"]
            replay = json.loads(replay_json.read_text())["scans"]
            self.assertEqual(
                {r["symbol"]: r["bias"] for r in live},
                {r["symbol"]: r["bias"] for r in replay},
            )

    def test_snapshot_provider_round_trips_quotes(self):
        now = session.to_et(datetime.fromisoformat(AS_OF))
        provider = get_provider("synthetic")
        original = provider.snapshot("SPY", now)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_snapshots(Path(tmp) / "s.json", [original], now)
            loaded = get_provider("snapshot", path=str(path)).snapshot("SPY", now)
        self.assertEqual(loaded.spot, original.spot)
        self.assertEqual(len(loaded.bars), len(original.bars))
        self.assertEqual(len(loaded.chain), len(original.chain))
        self.assertEqual(loaded.chain[0].strike, original.chain[0].strike)

    def test_universe_resolution_always_includes_the_benchmark(self):
        args = build_parser().parse_args(["scan", "--symbols", "nvda,tsla"])
        self.assertEqual(resolve_symbols(args), ["NVDA", "TSLA", "SPY"])
        index = resolve_symbols(build_parser().parse_args(["scan", "--universe", "index"]))
        self.assertIn("SPY", index)
        self.assertNotIn("NVDA", index)

    def test_only_tradeable_filters_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.json"
            run(["scan", "--provider", "synthetic", "--as-of", AS_OF, "--universe", "all",
                 "--only-tradeable", "--json", str(out)])
            for row in json.loads(out.read_text())["scans"]:
                self.assertFalse(row["verdict"].startswith(("NO TRADE", "AVOID")))

    def test_go_is_the_default_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--provider", "synthetic", "--as-of", AS_OF, "--symbols", "SPY",
                    "--out-dir", tmp]
            self.assertEqual(run(base), 0)
            self.assertEqual(run(["go"] + base), 0)
            log = json.loads(next(Path(tmp).glob("runs/runs-*.json")).read_text())
            self.assertEqual([r["press"] for r in log["runs"]], [1, 2])

    def test_sides_file_is_merged_into_the_scan(self):
        from odte.providers import get_provider
        from odte.sides import SideTape

        now = session.to_et(datetime.fromisoformat(AS_OF))
        snap = get_provider("synthetic", with_sides=False).snapshot("SPY", now)
        tape = SideTape()
        for q in snap.chain[:20]:
            # Customers lifting the offer on everything sampled.
            tape.record(q.occ, q.ask, 500, q.bid, q.ask, now)
        with tempfile.TemporaryDirectory() as tmp:
            sides_path = Path(tmp) / "sides.json"
            tape.save(sides_path)
            out = Path(tmp) / "scan.json"
            code = run(["scan", "--provider", "synthetic", "--as-of", AS_OF, "--symbols", "SPY",
                        "--sides", str(sides_path), "--json", str(out)])
            self.assertEqual(code, 0)
            row = json.loads(out.read_text())["scans"][0]
        self.assertEqual(row["flow_source"], "side")
        self.assertGreater(row["sampled_volume"], 0)

    def test_record_deadline_from_minutes_and_clock_time(self):
        now = session.to_et(datetime.fromisoformat(AS_OF))
        args = build_parser().parse_args(["record", "--out", "x.json", "--minutes", "45"])
        self.assertEqual((_record_deadline(args, now) - now).total_seconds(), 45 * 60)
        args = build_parser().parse_args(["record", "--out", "x.json", "--until", "11:15"])
        self.assertEqual(_record_deadline(args, now).strftime("%H:%M"), "11:15")
        # A time already past today rolls to the next session.
        args = build_parser().parse_args(["record", "--out", "x.json", "--until", "09:45"])
        self.assertEqual(_record_deadline(args, now).date(), now.date() + timedelta(days=1))

    def test_missing_tradier_token_exits_cleanly(self):
        import os

        saved = os.environ.pop("TRADIER_TOKEN", None)
        try:
            self.assertEqual(
                run(["--provider", "tradier", "--as-of", AS_OF, "--symbols", "SPY"]), 2
            )
        finally:
            if saved is not None:
                os.environ["TRADIER_TOKEN"] = saved

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError):
            get_provider("bloomberg")


if __name__ == "__main__":
    unittest.main()
