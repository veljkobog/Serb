import argparse
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from odte.config import Gates
from odte.server import Runner, make_handler

AS_OF = "2026-09-22T10:30:00"


def serve_args(out_dir: str) -> argparse.Namespace:
    return argparse.Namespace(
        provider="synthetic", snapshot_path=None, interval="5m", as_of=AS_OF,
        force=True, sides=None, stream_seconds=0.0, stream_contracts=40,
        min_bias=Gates.min_bias_to_trade, min_confidence=Gates.min_confidence_to_trade,
        workers=4, save_snapshot=None, universe="index", symbols="SPY,QQQ",
        benchmark="SPY", tradier_token=None, tradier_env=None,
        top=5, out_dir=out_dir, host="127.0.0.1", port=0, open=False,
    )


def wait_for(runner: Runner, job: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = runner.status(job)
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("scan never finished")


class TestRunner(unittest.TestCase):
    def test_a_press_produces_cards_and_records_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(serve_args(tmp))
            state = wait_for(runner, runner.start())
            self.assertEqual(state["state"], "done", state.get("message"))
            result = state["result"]
            self.assertEqual(result["press"], 1)
            self.assertTrue(result["cards"])
            card = result["cards"][0]
            for key in ("symbol", "side", "stars", "band", "setups", "levels", "trade"):
                self.assertIn(key, card)
            self.assertIn(card["side"], ("BULL", "BEAR"))
            self.assertTrue(0 <= card["stars"] <= 10)
            self.assertTrue((Path(tmp) / "latest.json").exists())
            self.assertTrue(list((Path(tmp) / "runs").glob("runs-*.json")))

    def test_second_press_grades_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(serve_args(tmp))
            wait_for(runner, runner.start())
            second = wait_for(runner, runner.start())["result"]
            self.assertEqual(second["press"], 2)
            self.assertIsNotNone(second["cards"][0]["change"])
            self.assertIn(
                second["cards"][0]["change"]["status"],
                ("CONFIRMED", "HOLDING", "FADED", "FLIPPED", "NEW"),
            )

    def test_only_one_read_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(serve_args(tmp))
            runner.jobs = {"busy": {"state": "running"}}
            with self.assertRaises(RuntimeError):
                runner.start()

    def test_a_broken_scan_reports_instead_of_spinning(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = serve_args(tmp)
            args.provider = "snapshot"   # no snapshot path -> ScanFailed
            runner = Runner(args)
            state = wait_for(runner, runner.start())
            self.assertEqual(state["state"], "error")
            self.assertIn("snapshot", state["message"])

    def test_unknown_job_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(Runner(serve_args(tmp)).status("nope")["state"], "error")


class TestHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = Runner(serve_args(self.tmp.name))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.runner))
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read()

    def test_page_is_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertIn("RUN THE READ", text)
        self.assertIn("RE-CONFIRM", text)

    def test_last_is_empty_before_any_press(self):
        _, body = self.get("/api/last")
        self.assertIsNone(json.loads(body)["result"])

    def test_press_runs_and_then_last_serves_it(self):
        req = urllib.request.Request(self.base + "/api/run", method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            job = json.loads(r.read())["job"]
        state = wait_for(self.runner, job)
        self.assertEqual(state["state"], "done", state.get("message"))
        _, body = self.get(f"/api/status?job={job}")
        self.assertEqual(json.loads(body)["state"], "done")
        _, body = self.get("/api/last")
        self.assertEqual(json.loads(body)["result"]["press"], 1)

    def test_unknown_paths_404(self):
        for path, method in (("/nope", "GET"), ("/api/nope", "POST")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                req = urllib.request.Request(self.base + path, method=method)
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
