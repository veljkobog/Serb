"""Offline end-to-end and unit tests. No network access required."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odte import indicators as ind
from odte import report
from odte.calendar_utils import is_trading_day, session_progress, trading_days_between, ET
from odte.config import Config
from odte.engine import Scanner, pick_expiry
from odte.providers.finra import FinraOffExchange
from odte.signals import darkpool, options_flow, trend, volume
from tests.fixtures import FakeProvider, make_bars, make_chain, make_offex

TODAY = dt.date(2026, 8, 21)   # a Friday


class TestIndicators(unittest.TestCase):
    def test_ema_matches_manual(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8]
        out = ind.ema(values, 3)
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)              # seed = SMA(1,2,3)
        self.assertAlmostEqual(out[3], 4 * 0.5 + 2 * 0.5)

    def test_atr_is_positive_and_lagging(self):
        bars = make_bars("ATR", 60, end=TODAY)
        out = ind.atr([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], 14)
        self.assertIsNone(out[13])
        self.assertGreater(out[-1], 0)

    def test_adx_bounded(self):
        bars = make_bars("ADX", 120, end=TODAY)
        a, p, m = ind.adx([b.high for b in bars], [b.low for b in bars], [b.close for b in bars])
        self.assertTrue(0 <= a[-1] <= 100)
        self.assertTrue(0 <= p[-1] <= 100 and 0 <= m[-1] <= 100)

    def test_ramp_and_squash_bounds(self):
        self.assertEqual(ind.ramp(0.0, 1.0, 3.0), 0.0)
        self.assertEqual(ind.ramp(9.0, 1.0, 3.0), 1.0)
        self.assertAlmostEqual(ind.ramp(2.0, 1.0, 3.0), 0.5)
        self.assertTrue(-1.0 <= ind.squash(50, 1) <= 1.0)
        self.assertTrue(0 < ind.squash(1, 1) < 1)
        self.assertAlmostEqual(ind.squash(-1, 1), -ind.squash(1, 1))
        self.assertEqual(ind.squash(None, 1), 0.0)

    def test_pct_rank_and_zscore(self):
        self.assertAlmostEqual(ind.pct_rank(3, [1, 2, 3, 4]), 0.75)
        self.assertGreater(ind.zscore(5, [1, 1, 1, 1, 1, 2]), 0)


class TestCalendar(unittest.TestCase):
    def test_holidays_excluded(self):
        self.assertFalse(is_trading_day(dt.date(2026, 7, 3)))   # Independence Day observed
        self.assertFalse(is_trading_day(dt.date(2026, 8, 22)))  # Saturday
        self.assertTrue(is_trading_day(TODAY))

    def test_friday_to_monday_is_one_session(self):
        self.assertEqual(trading_days_between(TODAY, dt.date(2026, 8, 24)), 1)
        self.assertEqual(trading_days_between(TODAY, dt.date(2026, 8, 25)), 2)

    def test_session_progress(self):
        self.assertEqual(session_progress(dt.datetime(2026, 8, 21, 9, 0, tzinfo=ET)), 0.0)
        self.assertEqual(session_progress(dt.datetime(2026, 8, 21, 16, 30, tzinfo=ET)), 1.0)
        mid = session_progress(dt.datetime(2026, 8, 21, 12, 45, tzinfo=ET))
        self.assertTrue(0.4 < mid < 0.6)

    def test_pick_expiry_prefers_same_day(self):
        self.assertEqual(pick_expiry([TODAY, dt.date(2026, 8, 24)], TODAY, 1), TODAY)
        self.assertEqual(pick_expiry([dt.date(2026, 8, 24)], TODAY, 1), dt.date(2026, 8, 24))
        self.assertIsNone(pick_expiry([dt.date(2026, 8, 24)], TODAY, 0))
        self.assertIsNone(pick_expiry([dt.date(2026, 9, 18)], TODAY, 1))


class TestSignals(unittest.TestCase):
    def test_uptrend_scores_bullish_downtrend_bearish(self):
        up = trend.analyse(make_bars("UP", 260, drift=0.004, vol=0.008, end=TODAY))
        down = trend.analyse(make_bars("DOWN", 260, drift=-0.004, vol=0.008, end=TODAY))
        self.assertGreater(up.direction, 0.2)
        self.assertLess(down.direction, -0.2)

    def test_volume_projection_scales_partial_session(self):
        bars = make_bars("VOL", 60, end=TODAY)
        early = volume.analyse(bars, None, progress=0.1)
        full = volume.analyse(bars, None, progress=1.0)
        self.assertGreater(early.detail["rvol"], full.detail["rvol"])

    def test_low_offex_short_ratio_reads_bullish(self):
        bars = make_bars("DP", 60, end=TODAY)
        bulls = make_offex(["DP"], {"DP": bars}, short_ratio=0.30)
        bears = make_offex(["DP"], {"DP": bars}, short_ratio=0.65)
        self.assertGreater(darkpool.analyse(bulls.history("DP"), bars).direction, 0.3)
        self.assertLess(darkpool.analyse(bears.history("DP"), bars).direction, -0.3)

    def test_missing_darkpool_marks_block_unavailable(self):
        block = darkpool.analyse([], make_bars("NONE", 60, end=TODAY))
        self.assertFalse(block.available)

    def test_option_flow_direction_follows_premium(self):
        spot = 100.0
        calls_heavy = options_flow.analyse(make_chain("X", TODAY, spot, skew=3.0), spot, 1.5, 0)
        puts_heavy = options_flow.analyse(make_chain("X", TODAY, spot, skew=1 / 3.0), spot, 1.5, 0)
        self.assertGreater(calls_heavy.direction, 0.2)
        self.assertLess(puts_heavy.direction, -0.2)

    def test_wide_spreads_destroy_option_quality(self):
        spot = 100.0
        tight = options_flow.analyse(make_chain("X", TODAY, spot, spread=0.01), spot, 1.5, 0)
        wide = options_flow.analyse(make_chain("X", TODAY, spot, spread=0.60), spot, 1.5, 0)
        self.assertGreater(tight.quality, wide.quality)

    def test_expected_move_and_walls_present(self):
        block = options_flow.analyse(make_chain("X", TODAY, 100.0), 100.0, 1.5, 0)
        self.assertIsNotNone(block.detail["expected_move_pct"])
        self.assertIsNotNone(block.detail["call_wall"])
        self.assertIsNotNone(block.detail["put_wall"])
        self.assertTrue(block.detail["gamma_measured"])


class TestScanEndToEnd(unittest.TestCase):
    def _run(self, **cfg_kwargs):
        cfg = Config(**cfg_kwargs) if cfg_kwargs else Config()
        cfg.cache_dir = ""
        cfg.workers = 2
        symbols = ["AAA", "BBB", "CCC"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009)
        bars = {s: provider.daily_bars(s) for s in symbols}
        scanner = Scanner(cfg, provider=provider, offex=make_offex(symbols, bars, short_ratio=0.32))
        return scanner.run(symbols, today=TODAY), cfg

    def test_scan_produces_ranked_candidates_with_plans(self):
        result, _ = self._run()
        self.assertTrue(result.candidates, f"no candidates; rejects={[c.gate_failures for c in result.rejected]}")
        scores = [c.score for c in result.candidates]
        self.assertEqual(scores, sorted(scores, reverse=True))
        top = result.candidates[0]
        self.assertIn(top.side, ("CALLS", "PUTS"))
        self.assertEqual(top.dte, 0)
        self.assertEqual(top.expiry, TODAY)
        self.assertIsNotNone(top.plan.get("strike"))
        self.assertIsNotNone(top.plan.get("underlying_stop"))
        self.assertIsNotNone(top.plan.get("target_1"))
        self.assertTrue(top.reasons())

    def test_stop_is_on_the_correct_side_of_spot(self):
        result, _ = self._run()
        for c in result.candidates:
            stop, t1 = c.plan["underlying_stop"], c.plan["target_1"]
            if c.side == "CALLS":
                self.assertLess(stop, c.spot)
                self.assertGreater(t1, c.spot)
            else:
                self.assertGreater(stop, c.spot)
                self.assertLess(t1, c.spot)

    def test_stop_stays_valid_when_price_is_under_both_emas(self):
        """A long signalled while price sits below its EMAs must not get a stop above spot."""
        from odte import plan as plan_mod
        from odte.score import Candidate
        from odte.signals import Block
        cand = Candidate(symbol="Z", spot=100.0, side="CALLS", dte=0)
        cand.blocks["trend"] = Block(name="trend", detail={"atr14": 2.0, "ema8": 104.0,
                                                           "ema21": 108.0})
        cand.blocks["options"] = Block(name="options",
                                       detail={"expected_move_dollars": 1.5,
                                               "call_wall": 103.0, "put_wall": 97.0,
                                               "atm_spread": 0.02})
        chain = make_chain("Z", TODAY, 100.0)
        plan = plan_mod.build(cand, chain, Config())
        self.assertLess(plan["underlying_stop"], cand.spot)
        self.assertAlmostEqual(plan["underlying_stop"], 99.0)

    def test_market_cap_gate_rejects_small_caps(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        symbols = ["TINY"]
        provider = FakeProvider(symbols, TODAY, market_cap=300e6)
        scanner = Scanner(cfg, provider=provider, offex=FinraOffExchange(days=0))
        result = scanner.run(symbols, today=TODAY)
        self.assertFalse(result.candidates)
        self.assertTrue(any("market cap" in f for f in result.rejected[0].gate_failures))

    def test_illiquid_options_gate_rejects(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        symbols = ["THIN"]
        provider = FakeProvider(symbols, TODAY, spread=0.9, chain_volume=20)
        scanner = Scanner(cfg, provider=provider, offex=FinraOffExchange(days=0))
        result = scanner.run(symbols, today=TODAY)
        self.assertFalse(result.candidates)
        failures = " ".join(result.rejected[0].gate_failures)
        self.assertTrue("contracts" in failures or "spread" in failures)

    def test_max_dte_zero_rejects_next_day_expiry(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        cfg.gates.max_dte = 0
        symbols = ["AAA"]
        provider = FakeProvider(symbols, TODAY, expiry_offset_days=3)   # Monday
        scanner = Scanner(cfg, provider=provider, offex=FinraOffExchange(days=0))
        result = scanner.run(symbols, today=TODAY)
        self.assertFalse(result.candidates)
        self.assertIn("no near-dated option chain", " ".join(result.rejected[0].gate_failures))

    def test_missing_darkpool_does_not_break_scoring(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        symbols = ["AAA", "BBB"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009)
        scanner = Scanner(cfg, provider=provider, offex=FinraOffExchange(days=0))
        result = scanner.run(symbols, today=TODAY)
        self.assertEqual(result.errors, [])
        for c in result.candidates:
            self.assertFalse(c.blocks["darkpool"].available)

    def test_broken_symbol_is_isolated(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        symbols = ["AAA", "BOOM"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009)
        original = provider.chain

        def exploding(symbol, expiry):
            if symbol == "BOOM":
                raise RuntimeError("provider blew up")
            return original(symbol, expiry)

        provider.chain = exploding
        bars = {s: provider.daily_bars(s) for s in symbols}
        result = Scanner(cfg, provider=provider,
                         offex=make_offex(symbols, bars)).run(symbols, today=TODAY)
        self.assertTrue(any("BOOM" in e for e in result.errors))
        self.assertFalse(any("AAA" in e for e in result.errors))
        evaluated = {c.symbol for c in result.candidates} | {
            c.symbol for c in result.rejected if c.error is None}
        self.assertIn("AAA", evaluated)


class TestReports(unittest.TestCase):
    def setUp(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        # These tests exercise the renderers, not the scoring calibration — pin the
        # floor so a weight change cannot turn a rendering test red.
        cfg.gates.min_score = 10.0
        symbols = ["AAA", "BBB"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009)
        bars = {s: provider.daily_bars(s) for s in symbols}
        self.result = Scanner(cfg, provider=provider,
                              offex=make_offex(symbols, bars, short_ratio=0.32)).run(symbols, today=TODAY)

    def test_setup_produced_candidates(self):
        self.assertTrue(self.result.candidates)

    def test_terminal_render_is_plain_when_colour_off(self):
        text = report.render_terminal(self.result, colour=False, verbose=True)
        self.assertNotIn("\033[", text)
        self.assertIn("SCORE", text)

    def test_html_is_self_contained(self):
        page = report.render_html(self.result)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>0/1DTE Options Scanner</title>", page)
        self.assertIn("</body>", page)
        self.assertIn("</html>", page)
        self.assertNotIn("http://", page.replace("https://", ""))
        self.assertNotIn("<script", page)

    def test_files_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            jp = report.write_json(self.result, os.path.join(tmp, "s.json"))
            cp = report.write_csv(self.result, os.path.join(tmp, "s.csv"))
            hp = report.write_html(self.result, os.path.join(tmp, "s.html"))
            jl = report.append_journal(self.result, os.path.join(tmp, "j.jsonl"))
            for path in (jp, cp, hp, jl):
                self.assertGreater(os.path.getsize(path), 100)
            with open(jp, encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertIn("candidates", payload)
            self.assertIn("gates", payload)


class TestFinraParsing(unittest.TestCase):
    def test_parses_pipe_file_and_skips_trailer(self):
        feed = FinraOffExchange(days=1)
        text = ("Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
                "20260820|AAPL|1200|5|3000|CNMS\n"
                "20260820|NVDA|6000|0|10000|CNMS\n"
                "20260820|\n")
        self.assertEqual(feed._parse(text, dt.date(2026, 8, 20)), 2)
        self.assertAlmostEqual(feed.latest("AAPL").short_ratio, 0.4)
        self.assertAlmostEqual(feed.latest("AAPL").dpi, 0.6)
        self.assertAlmostEqual(feed.latest("NVDA").dpi, 0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
