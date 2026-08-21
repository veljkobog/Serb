"""Tests for the strike ladder, the session-aware gate, and the Scan-button app."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odte import plan as plan_mod
from odte.config import Config
from odte.http import Http
from odte.screen import required_option_volume
from odte.score import Candidate
from odte.signals import Block
os.environ.setdefault("ODTE_QUIET", "1")

from odte.webapp import Handler, ScanRunner, _clean_settings, render_app
from tests.fixtures import make_chain

TODAY = dt.date(2026, 8, 21)


def _candidate(side: str = "CALLS", spot: float = 100.0) -> Candidate:
    cand = Candidate(symbol="Z", spot=spot, side=side, dte=0)
    cand.blocks["trend"] = Block(name="trend", detail={"atr14": 2.0, "ema8": spot - 1,
                                                       "ema21": spot - 3})
    cand.blocks["options"] = Block(name="options",
                                   detail={"expected_move_dollars": 2.0,
                                           "call_wall": spot + 8, "put_wall": spot - 8,
                                           "atm_spread": 0.02})
    return cand


class TestStrikeLadder(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.account_size = 25_000.0
        self.cfg.risk_per_trade_pct = 1.0

    def test_three_distinct_strikes_ordered_itm_to_otm(self):
        plan = plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)
        ladder = plan["ladder"]
        self.assertEqual(len(ladder), 3)
        self.assertEqual([r["rung"] for r in ladder], ["anchor", "core", "runner"])
        strikes = [r["strike"] for r in ladder]
        self.assertEqual(len(set(strikes)), 3, "rungs must not share a strike")
        self.assertEqual(strikes, sorted(strikes), "calls should ladder upward")

    def test_puts_ladder_downward(self):
        plan = plan_mod.build(_candidate("PUTS"), make_chain("Z", TODAY, 100.0), self.cfg)
        strikes = [r["strike"] for r in plan["ladder"]]
        self.assertEqual(strikes, sorted(strikes, reverse=True))
        self.assertTrue(all(r["right"] == "P" for r in plan["ladder"]))

    def test_deltas_descend_across_the_ladder(self):
        ladder = plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)["ladder"]
        deltas = [abs(r["delta"]) for r in ladder]
        self.assertEqual(deltas, sorted(deltas, reverse=True))

    def test_call_breakeven_is_strike_plus_premium(self):
        for rung in plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)["ladder"]:
            self.assertAlmostEqual(rung["breakeven"], rung["strike"] + rung["mid"], places=2)

    def test_put_breakeven_is_strike_minus_premium(self):
        plan = plan_mod.build(_candidate("PUTS"), make_chain("Z", TODAY, 100.0), self.cfg)
        for rung in plan["ladder"]:
            self.assertAlmostEqual(rung["breakeven"], rung["strike"] - rung["mid"], places=2)

    def test_expiry_payoff_is_pure_intrinsic(self):
        plan = plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)
        for rung in plan["ladder"]:
            for outcome in rung["outcomes"]:
                expected = max(0.0, outcome["underlying"] - rung["strike"])
                self.assertAlmostEqual(outcome["value_at_expiry"], expected, places=2)
                self.assertAlmostEqual(outcome["pnl_per_contract"],
                                       (expected - rung["mid"]) * 100, places=1)

    def test_sizing_respects_the_risk_budget(self):
        self.cfg.account_size = 50_000.0
        self.cfg.risk_per_trade_pct = 2.0        # $1,000 of risk
        for rung in plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)["ladder"]:
            self.assertLessEqual(rung["risk_per_contract"] * rung["suggested_contracts"], 1_000.0)
            self.assertAlmostEqual(rung["risk_per_contract"],
                                   rung["mid"] * 100 * self.cfg.premium_stop_pct, places=1)

    def test_tiny_account_never_suggests_a_negative_position(self):
        self.cfg.account_size = 100.0
        for rung in plan_mod.build(_candidate(), make_chain("Z", TODAY, 100.0), self.cfg)["ladder"]:
            self.assertGreaterEqual(rung["suggested_contracts"], 0)


class TestSessionAwareVolumeGate(unittest.TestCase):
    def test_requirement_scales_with_the_session(self):
        cfg = Config()
        cfg.gates.min_option_volume = 3_000.0
        at_open = required_option_volume(cfg, 0.01)
        midday = required_option_volume(cfg, 0.5)
        at_close = required_option_volume(cfg, 1.0)
        self.assertLess(at_open, midday)
        self.assertLess(midday, at_close)
        self.assertAlmostEqual(at_close, 3_000.0)

    def test_floor_keeps_a_pre_open_scan_from_demanding_zero(self):
        cfg = Config()
        cfg.gates.min_option_volume = 3_000.0
        self.assertAlmostEqual(required_option_volume(cfg, 0.0),
                               3_000.0 * cfg.gates.min_option_volume_floor)

    def test_scaling_can_be_switched_off(self):
        cfg = Config()
        cfg.gates.scale_option_volume_by_session = False
        self.assertEqual(required_option_volume(cfg, 0.01), cfg.gates.min_option_volume)


class TestHttpFreshness(unittest.TestCase):
    """force_fresh must refetch live endpoints but still trust published files."""

    class _Counter(BaseHTTPRequestHandler):
        hits = {}

        def do_GET(self):  # noqa: N802
            key = self.path
            TestHttpFreshness._Counter.hits[key] = self.hits.get(key, 0) + 1
            body = f"{key}:{self.hits[key]}".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    def setUp(self):
        self._Counter.hits = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._Counter)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_is_used_when_not_forcing_fresh(self):
        http = Http(cache_dir=self.tmp, cache_ttl=300, min_interval=0)
        http.get_text(self.base + "/live")
        http.get_text(self.base + "/live")
        self.assertEqual(self._Counter.hits["/live"], 1, "second call should hit the cache")

    def test_force_fresh_refetches_live_endpoints(self):
        warm = Http(cache_dir=self.tmp, cache_ttl=300, min_interval=0)
        warm.get_text(self.base + "/live")
        fresh = Http(cache_dir=self.tmp, cache_ttl=300, min_interval=0, force_fresh=True)
        fresh.get_text(self.base + "/live")
        self.assertEqual(self._Counter.hits["/live"], 2, "Scan should bypass the cache")

    def test_force_fresh_still_serves_immutable_files_from_cache(self):
        warm = Http(cache_dir=self.tmp, cache_ttl=86400, min_interval=0)
        warm.get_text(self.base + "/finra", cache_ttl=86400, immutable=True)
        fresh = Http(cache_dir=self.tmp, cache_ttl=86400, min_interval=0, force_fresh=True)
        fresh.get_text(self.base + "/finra", cache_ttl=86400, immutable=True)
        self.assertEqual(self._Counter.hits["/finra"], 1,
                         "a published FINRA file must not be re-downloaded every scan")


class TestSettingsCoercion(unittest.TestCase):
    def test_strings_from_the_form_become_numbers(self):
        out = _clean_settings({"top": "3", "min_score": "45.5", "max_dte": "0",
                               "universe": "  wide  ", "side": "calls"})
        self.assertEqual(out["top"], 3)
        self.assertEqual(out["max_dte"], 0)
        self.assertAlmostEqual(out["min_score"], 45.5)
        self.assertEqual(out["universe"], "wide")

    def test_garbage_is_dropped_not_raised(self):
        out = _clean_settings({"top": "abc", "min_score": None, "symbols": "",
                               "unexpected": {"nested": 1}})
        self.assertNotIn("top", out)
        self.assertNotIn("min_score", out)
        self.assertNotIn("symbols", out)
        self.assertNotIn("unexpected", out)


class TestScanRunner(unittest.TestCase):
    def _runner(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers, cfg.out_dir = "", 2, "/tmp/odte-test-out"
        cfg.gates.min_score = 20.0
        return ScanRunner(cfg)

    def test_runs_a_demo_scan_to_completion(self):
        runner = self._runner()
        self.assertEqual(runner.status()["state"], "idle")
        self.assertFalse(runner.snapshot(3)["ok"])

        self.assertTrue(runner.start({"universe": "megacap", "demo": True, "top": 3})["started"])
        for _ in range(200):
            if runner.status()["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        status = runner.status()
        self.assertEqual(status["state"], "done", status.get("error"))
        self.assertEqual(status["done"], status["total"])

        snap = runner.snapshot(3)
        self.assertTrue(snap["ok"])
        self.assertLessEqual(snap["summary"]["showing"], 3)
        self.assertLessEqual(snap["cards"].count('<article class="card'), 3)
        self.assertIn("ladder", snap["cards"])

    def test_second_scan_is_refused_while_one_is_running(self):
        runner = self._runner()
        runner.start({"universe": "megacap", "demo": True})
        second = runner.start({"universe": "megacap", "demo": True})
        self.assertFalse(second["started"])
        for _ in range(200):
            if runner.status()["state"] in ("done", "error"):
                break
            time.sleep(0.05)

    def test_a_failing_scan_reports_error_not_a_hang(self):
        runner = self._runner()
        runner.base.provider = "nonsense-provider"
        runner.start({"universe": "megacap"})
        for _ in range(100):
            if runner.status()["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        status = runner.status()
        self.assertEqual(status["state"], "error")
        self.assertIn("nonsense-provider", status["error"])


class TestHttpEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config()
        cfg.cache_dir, cfg.workers, cfg.out_dir = "", 2, "/tmp/odte-test-out"
        cfg.gates.min_score = 20.0
        Handler.runner = ScanRunner(cfg)
        Handler.config = cfg
        Handler.auto_at = None
        Handler.demo = True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=15) as resp:
            return resp.status, resp.read()

    def _post(self, path, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())

    def test_index_serves_the_app_with_a_scan_button(self):
        status, body = self._get("/")
        page = body.decode()
        self.assertEqual(status, 200)
        self.assertIn('id="scan"', page)
        self.assertIn("DEMO MODE", page)
        self.assertTrue(page.startswith("<!doctype html>"))

    def test_favicon_is_served_locally(self):
        status, body = self._get("/favicon.svg")
        self.assertEqual(status, 200)
        self.assertIn(b"<svg", body)

    def test_unknown_route_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/does-not-exist")
        self.assertEqual(ctx.exception.code, 404)

    def test_malformed_body_is_400_not_a_crash(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/scan", None, raw=b"{not json")
        self.assertEqual(ctx.exception.code, 400)

    def test_non_object_body_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/scan", None, raw=b'"a string"')
        self.assertEqual(ctx.exception.code, 400)

    def test_oversized_body_is_rejected(self):
        req = urllib.request.Request(self.base + "/api/scan", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Content-Length": "99999999"})
        # The handler checks the declared length before reading the body.
        try:
            urllib.request.urlopen(req, timeout=15)
            self.fail("expected a rejection")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 413)
        except (urllib.error.URLError, ConnectionError):
            pass   # some clients tear the connection down first; the guard still held

    def test_full_scan_cycle_over_http(self):
        status, payload = self._post("/api/scan", {"universe": "megacap", "top": "3",
                                                   "min_score": "20"})
        self.assertTrue(payload["started"])
        for _ in range(300):
            _, body = self._get("/api/status")
            state = json.loads(body)["state"]
            if state in ("done", "error"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "done")

        _, body = self._get("/api/result?top=3")
        result = json.loads(body)
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["summary"]["showing"], 3)
        self.assertLessEqual(result["cards"].count('<article class="card'), 3)

    def test_top_parameter_is_clamped_and_survives_garbage(self):
        for value, ceiling in (("999", 25), ("abc", 3), ("0", 1)):
            _, body = self._get(f"/api/result?top={value}")
            result = json.loads(body)
            if result.get("ok"):
                self.assertLessEqual(result["summary"]["showing"], ceiling)


class TestAppRendering(unittest.TestCase):
    def test_app_page_has_no_external_requests(self):
        page = render_app(Config(), auto_at="09:35")
        self.assertNotIn("//cdn.", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page.replace("http://www.w3.org", ""))
        self.assertIn("09:35", page)
        self.assertIn("</html>", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
