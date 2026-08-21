"""Tests for the intraday block, VWAP-based stops, and two-phase gating."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odte import plan as plan_mod, screen
from odte.calendar_utils import ET
from odte.config import Config
from odte.engine import Scanner
from odte.providers.base import IntradayBar
from odte.providers.finra import FinraOffExchange
from odte.score import Candidate
from odte.signals import Block, intraday
from odte.synthetic import FakeProvider, make_chain, make_intraday, make_offex

TODAY = dt.date(2026, 8, 21)
OPEN = dt.datetime.combine(TODAY, dt.time(9, 30), tzinfo=ET)


def _bars(prices, volumes=None, minutes=5):
    """One bar per price; high/low hug the close so VWAP maths stays checkable."""
    volumes = volumes or [1000.0] * len(prices)
    return [IntradayBar(OPEN + dt.timedelta(minutes=minutes * i), p, p, p, p, v)
            for i, (p, v) in enumerate(zip(prices, volumes))]


class TestVwapMath(unittest.TestCase):
    def test_vwap_is_volume_weighted_not_a_simple_average(self):
        bars = _bars([10.0, 20.0], volumes=[1000.0, 3000.0])
        self.assertAlmostEqual(intraday.vwap_series(bars)[-1], 17.5)   # not 15

    def test_vwap_uses_the_typical_price(self):
        bar = IntradayBar(OPEN, 10.0, 12.0, 8.0, 11.0, 100.0)
        self.assertAlmostEqual(bar.typical, (12.0 + 8.0 + 11.0) / 3.0)
        self.assertAlmostEqual(intraday.vwap_series([bar])[0], bar.typical)

    def test_zero_volume_bars_do_not_divide_by_zero(self):
        bars = _bars([10.0, 11.0], volumes=[0.0, 0.0])
        self.assertEqual(len(intraday.vwap_series(bars)), 2)

    def test_opening_range_covers_only_the_first_thirty_minutes(self):
        prices = [100, 101, 102, 103, 104, 105, 106, 120]   # 5m bars: 6 in the first 30m
        high, low = intraday.opening_range(_bars(prices))
        self.assertEqual(low, 100)
        self.assertEqual(high, 105, "the 120 print is at 09:65+, outside the window")

    def test_opening_range_of_nothing_is_none(self):
        self.assertEqual(intraday.opening_range([]), (None, None))


class TestIntradayBlock(unittest.TestCase):
    def test_too_few_bars_marks_the_block_unavailable(self):
        block = intraday.analyse(_bars([100.0, 101.0]))
        self.assertFalse(block.available)
        self.assertIn("no intraday bars", block.notes[0])

    def test_holding_above_vwap_all_session_reads_bullish(self):
        block = intraday.analyse(_bars([100, 101, 102, 103, 104, 105, 106, 107]), atr=2.0)
        self.assertGreater(block.direction, 0.2)
        self.assertGreater(block.detail["share_above_vwap"], 0.5)

    def test_bleeding_below_vwap_all_session_reads_bearish(self):
        block = intraday.analyse(_bars([107, 106, 105, 104, 103, 102, 101, 100]), atr=2.0)
        self.assertLess(block.direction, -0.2)
        self.assertLess(block.detail["share_above_vwap"], 0.5)

    def test_opening_range_breakout_is_detected_both_ways(self):
        up = intraday.analyse(_bars([100, 100.5, 101, 100.8, 101.2, 101, 104, 105]), atr=2.0)
        self.assertEqual(up.detail["or_state"], "broken up")
        down = intraday.analyse(_bars([100, 99.5, 100, 99.8, 100.2, 100, 96, 95]), atr=2.0)
        self.assertEqual(down.detail["or_state"], "broken down")

    def test_price_inside_the_opening_range_is_flagged_inside(self):
        block = intraday.analyse(_bars([100, 102, 98, 101, 99, 100, 100.5, 100.2]), atr=2.0)
        self.assertEqual(block.detail["or_state"], "inside")

    def test_a_clean_trend_scores_higher_quality_than_chop(self):
        trend = intraday.analyse(_bars([100, 101, 102, 103, 104, 105, 106, 107]), atr=2.0)
        chop = intraday.analyse(_bars([100, 103, 100, 103, 100, 103, 100, 100.2]), atr=2.0)
        self.assertGreater(trend.detail["efficiency"], chop.detail["efficiency"])
        self.assertGreater(trend.quality, chop.quality)

    def test_synthetic_intraday_reconstructs_the_daily_bar(self):
        provider = FakeProvider(["AAA"], TODAY)
        daily = provider.daily_bars("AAA")[-1]
        minutes = make_intraday("AAA", daily)
        self.assertAlmostEqual(max(b.high for b in minutes), daily.high, places=6)
        self.assertAlmostEqual(min(b.low for b in minutes), daily.low, places=6)
        self.assertAlmostEqual(minutes[-1].close, daily.close, places=6)
        self.assertAlmostEqual(sum(b.volume for b in minutes), daily.volume, delta=daily.volume * 0.01)

    def test_a_partial_session_yields_proportionally_fewer_bars(self):
        early = FakeProvider(["AAA"], TODAY, session_fraction=0.05).intraday_bars("AAA")
        full = FakeProvider(["AAA"], TODAY, session_fraction=1.0).intraday_bars("AAA")
        self.assertLess(len(early), 8)
        self.assertGreater(len(full), 70)
        self.assertLess(early[-1].ts, full[-1].ts)


class TestVwapStop(unittest.TestCase):
    def _candidate(self, vwap, spot=100.0, side="CALLS"):
        cand = Candidate(symbol="Z", spot=spot, side=side, dte=0)
        cand.blocks["trend"] = Block(name="trend",
                                     detail={"atr14": 4.0, "ema8": 96.0, "ema21": 94.0})
        cand.blocks["options"] = Block(name="options",
                                       detail={"expected_move_dollars": 2.0,
                                               "call_wall": 110.0, "put_wall": 90.0})
        cand.blocks["intraday"] = Block(name="intraday", detail={"vwap": vwap})
        return cand

    def test_vwap_becomes_the_stop_when_it_is_the_nearer_level(self):
        plan = plan_mod.build(self._candidate(vwap=99.0), make_chain("Z", TODAY, 100.0), Config())
        self.assertAlmostEqual(plan["underlying_stop"], 99.0)
        self.assertAlmostEqual(plan["vwap"], 99.0)
        self.assertTrue(any("VWAP" in n for n in plan["notes"]))

    def test_vwap_on_the_wrong_side_of_spot_is_not_used_as_a_stop(self):
        plan = plan_mod.build(self._candidate(vwap=104.0), make_chain("Z", TODAY, 100.0), Config())
        self.assertLess(plan["underlying_stop"], 100.0)

    def test_a_far_vwap_does_not_widen_a_tighter_stop(self):
        plan = plan_mod.build(self._candidate(vwap=90.0), make_chain("Z", TODAY, 100.0), Config())
        self.assertGreater(plan["underlying_stop"], 90.0)

    def test_puts_invert_the_vwap_test(self):
        cand = self._candidate(vwap=101.0, side="PUTS")
        cand.blocks["trend"].detail = {"atr14": 4.0, "ema8": 104.0, "ema21": 106.0}
        plan = plan_mod.build(cand, make_chain("Z", TODAY, 100.0), Config())
        self.assertAlmostEqual(plan["underlying_stop"], 101.0)


class TestTwoPhaseGating(unittest.TestCase):
    class _Counting(FakeProvider):
        def __init__(self, *a, **kw):
            self.small_caps = set(kw.pop("small_caps", ()))
            super().__init__(*a, **kw)
            self.chain_calls = 0
            self.expiry_calls = 0
            self.intraday_calls = 0

        def expirations(self, symbol):
            self.expiry_calls += 1
            return super().expirations(symbol)

        def chain(self, symbol, expiry):
            self.chain_calls += 1
            return super().chain(symbol, expiry)

        def intraday_bars(self, symbol, interval="5m"):
            self.intraday_calls += 1
            return super().intraday_bars(symbol, interval)

        def fundamentals(self, symbol):
            fund = super().fundamentals(symbol)
            if symbol in self.small_caps:
                fund.market_cap = 300e6
            return fund

    def _run(self, small_caps=(), **cfg_kw):
        cfg = Config()
        cfg.cache_dir, cfg.workers, cfg.gates.min_score = "", 2, 20.0
        for key, value in cfg_kw.items():
            setattr(cfg, key, value)
        symbols = ["AAA", "BBB", "CCC", "DDD"]
        provider = self._Counting(symbols, TODAY, drift=0.004, vol=0.009,
                                  small_caps=small_caps)
        bars = {s: provider.daily_bars(s) for s in symbols}
        result = Scanner(cfg, provider=provider,
                         offex=make_offex(symbols, bars)).run(symbols, today=TODAY)
        return provider, result

    def test_a_small_cap_never_costs_a_chain_fetch(self):
        provider, result = self._run(small_caps=("AAA", "BBB"))
        self.assertEqual(provider.chain_calls, 2)
        self.assertEqual(provider.expiry_calls, 2)
        self.assertEqual(result.chain_fetches, 2)
        dropped = {c.symbol: c for c in result.rejected}
        self.assertEqual(dropped["AAA"].stage, "pre-gate")
        self.assertTrue(any("market cap" in f for f in dropped["AAA"].gate_failures))

    def test_intraday_is_also_skipped_for_pre_gate_rejects(self):
        provider, _ = self._run(small_caps=("AAA", "BBB", "CCC"))
        self.assertEqual(provider.intraday_calls, 1)

    def test_names_that_clear_the_pre_gate_are_fully_evaluated(self):
        provider, result = self._run()
        self.assertEqual(provider.chain_calls, 4)
        self.assertEqual(result.chain_fetches, 4)
        for cand in result.candidates:
            self.assertEqual(cand.stage, "scored")
            self.assertTrue(cand.blocks["intraday"].available)

    def test_intraday_can_be_switched_off_without_breaking_scoring(self):
        provider, result = self._run(use_intraday=False)
        self.assertEqual(provider.intraday_calls, 0)
        self.assertTrue(result.candidates)
        for cand in result.candidates:
            self.assertFalse(cand.blocks["intraday"].available)

    def test_a_provider_with_no_intraday_support_still_scores(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers, cfg.gates.min_score = "", 2, 20.0
        symbols = ["AAA", "BBB"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009, intraday=False)
        result = Scanner(cfg, provider=provider,
                         offex=FinraOffExchange(days=0)).run(symbols, today=TODAY)
        self.assertEqual(result.errors, [])
        self.assertTrue(result.candidates)

    def test_intraday_fetch_failure_is_swallowed_not_fatal(self):
        cfg = Config()
        cfg.cache_dir, cfg.workers, cfg.gates.min_score = "", 2, 20.0
        symbols = ["AAA"]
        provider = FakeProvider(symbols, TODAY, drift=0.004, vol=0.009)

        def boom(*a, **k):
            raise ConnectionError("intraday feed down")

        provider.intraday_bars = boom
        result = Scanner(cfg, provider=provider,
                         offex=FinraOffExchange(days=0)).run(symbols, today=TODAY)
        self.assertEqual(result.errors, [])
        evaluated = result.candidates + [c for c in result.rejected if c.error is None]
        self.assertTrue(evaluated)
        self.assertFalse(evaluated[0].blocks["intraday"].available)


class TestGateSplit(unittest.TestCase):
    def test_apply_still_runs_both_halves(self):
        cand = Candidate(symbol="Z", spot=5.0)
        cand.blocks["volume"] = Block(name="volume", detail={"avg20_volume": 100.0,
                                                             "dollar_volume": 500.0})
        fails = screen.apply(cand, None, None, Config(), TODAY)
        self.assertTrue(any("price" in f for f in fails))
        self.assertIn("no near-dated option chain", fails)


if __name__ == "__main__":
    unittest.main(verbosity=2)
