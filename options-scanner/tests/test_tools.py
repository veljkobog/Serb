"""Tests for the preflight doctor and the journal reviewer. All offline."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odte import doctor, review as review_mod
from odte.calendar_utils import now_et
from odte.config import Config
from odte.providers.base import Bar
from odte.providers.finra import FinraOffExchange
from tests.fixtures import FakeProvider, make_offex

TODAY = dt.date(2026, 8, 21)


def _cfg(**kw):
    cfg = Config()
    cfg.cache_dir = kw.pop("cache_dir", "")
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


# ------------------------------------------------------------------ doctor --
class TestDoctor(unittest.TestCase):
    def _healthy(self):
        today = now_et().date()
        symbols = ["SPY", "AAPL"]
        provider = FakeProvider(symbols, today)
        bars = {s: provider.daily_bars(s) for s in symbols}
        return provider, make_offex(symbols, bars)

    def test_healthy_provider_produces_no_failures(self):
        provider, offex = self._healthy()
        checks = doctor.run_checks(_cfg(), provider=provider, offex=offex)
        failures = [c for c in checks if c.status == doctor.FAIL]
        self.assertEqual(failures, [], [c.detail for c in failures])
        names = {c.name for c in checks}
        self.assertIn("Option chain", names)
        self.assertIn("Dark pool (FINRA)", names)

    def test_dead_provider_fails_on_price_history_with_a_fix(self):
        provider, offex = self._healthy()

        def boom(*a, **k):
            raise ConnectionError("tunnel refused")

        provider.daily_bars = boom
        checks = doctor.run_checks(_cfg(), provider=provider, offex=offex)
        history = next(c for c in checks if c.name == "Price history")
        self.assertEqual(history.status, doctor.FAIL)
        self.assertIn("tunnel refused", history.detail)
        self.assertTrue(history.fix, "a failure must carry an actionable fix")
        self.assertEqual(doctor.verdict(checks), doctor.FAIL)

    def test_missing_market_cap_is_a_failure_because_the_size_gate_needs_it(self):
        provider, offex = self._healthy()
        original = provider.fundamentals

        def capless(symbol):
            fund = original(symbol)
            fund.market_cap = None
            return fund

        provider.fundamentals = capless
        checks = doctor.run_checks(_cfg(), provider=provider, offex=offex)
        fundamentals = next(c for c in checks if c.name == "Fundamentals")
        self.assertEqual(fundamentals.status, doctor.FAIL)
        self.assertIn("size gate", fundamentals.fix)

    def test_no_near_expiry_warns_rather_than_failing(self):
        today = now_et().date()
        provider = FakeProvider(["SPY", "AAPL"], today, expiry_offset_days=30)
        checks = doctor.run_checks(_cfg(), provider=provider, offex=FinraOffExchange(days=0))
        expiries = next(c for c in checks if c.name == "Option expiries")
        self.assertEqual(expiries.status, doctor.WARN)
        self.assertIn("outside", expiries.detail)

    def test_chain_without_greeks_warns_but_still_passes(self):
        today = now_et().date()
        symbols = ["SPY", "AAPL"]
        provider = FakeProvider(symbols, today)
        original = provider.chain

        def greekless(symbol, expiry):
            chain = original(symbol, expiry)
            for c in chain.contracts:
                c.delta = c.gamma = None
            return chain

        provider.chain = greekless
        checks = doctor.run_checks(_cfg(), provider=provider, offex=FinraOffExchange(days=0))
        greeks = next(c for c in checks if c.name == "Greeks")
        self.assertEqual(greeks.status, doctor.WARN)
        self.assertIn("Tradier", greeks.fix)
        self.assertNotEqual(doctor.verdict(checks), doctor.FAIL)

    def test_unknown_provider_reports_instead_of_raising(self):
        checks = doctor.run_checks(_cfg(provider="not-a-provider", darkpool_days=0))
        self.assertTrue(any(c.status == doctor.FAIL for c in checks))
        self.assertEqual(doctor.verdict(checks), doctor.FAIL)

    def test_disabled_darkpool_warns_not_fails(self):
        provider, _ = self._healthy()
        checks = doctor.run_checks(_cfg(darkpool_days=0), provider=provider)
        dark = next(c for c in checks if c.name.startswith("Dark pool"))
        self.assertEqual(dark.status, doctor.WARN)

    def test_terminal_render_is_plain_and_shows_fixes(self):
        provider, offex = self._healthy()
        text = doctor.render_terminal(
            doctor.run_checks(_cfg(), provider=provider, offex=offex), colour=False)
        self.assertNotIn("\033[", text)
        self.assertIn("Preflight", text)


# ------------------------------------------------------------------ review --
def _entry(symbol="AAA", side="CALLS", spot=100.0, expiry="2026-08-21", score=60.0,
           stop=99.0, t1=101.0, t2=102.0, ts="2026-08-21T09:35:00", rungs=None,
           blocks=None):
    return {
        "ts": ts, "symbol": symbol, "side": side, "score": score, "spot": spot,
        "expiry": expiry, "dte": 0,
        "blocks": blocks or {"trend": {"direction": 0.5, "quality": 0.6},
                             "darkpool": {"direction": -0.4, "quality": 0.5}},
        "plan": {
            "underlying_stop": stop, "target_1": t1, "target_2": t2,
            "premium_stop_pct": 0.35,
            "ladder": rungs if rungs is not None else [
                {"rung": "anchor", "strike": 99.0, "mid": 2.00, "delta": 0.60},
                {"rung": "core", "strike": 100.0, "mid": 1.00, "delta": 0.45},
                {"rung": "runner", "strike": 101.0, "mid": 0.50, "delta": 0.30},
            ],
        },
    }


def _bars(close, high, low, day=TODAY):
    return {day: Bar(day, close, high, low, close, 1_000_000)}


class TestJournalLoading(unittest.TestCase):
    def test_malformed_trailing_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_entry("AAA")) + "\n")
                fh.write(json.dumps(_entry("BBB")) + "\n")
                fh.write('{"symbol": "CCC", "partial')      # killed mid-write
            entries = review_mod.load_journal(path)
        self.assertEqual([e["symbol"] for e in entries], ["AAA", "BBB"])

    def test_missing_journal_raises_a_useful_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            review_mod.load_journal("/nonexistent/journal.jsonl")
        self.assertIn("run a scan first", str(ctx.exception))

    def test_dedupe_keeps_the_first_sighting_per_symbol_and_expiry(self):
        entries = [
            _entry("AAA", ts="2026-08-21T10:30:00", score=80.0),
            _entry("AAA", ts="2026-08-21T09:35:00", score=55.0),
            _entry("AAA", expiry="2026-08-24", ts="2026-08-24T09:35:00"),
            _entry("BBB", ts="2026-08-21T09:40:00"),
        ]
        kept = review_mod.dedupe(entries)
        self.assertEqual(len(kept), 3)
        first = next(e for e in kept if e["symbol"] == "AAA" and e["expiry"] == "2026-08-21")
        self.assertEqual(first["score"], 55.0, "should keep the earliest scan, not the best")


class TestEvaluateEntry(unittest.TestCase):
    def test_no_bar_for_the_expiry_is_skipped_not_guessed(self):
        out = review_mod.evaluate_entry(_entry(), {})
        self.assertIsNotNone(out.skipped)
        self.assertIn("no bar", out.skipped)

    def test_flat_entry_with_no_side_is_skipped(self):
        out = review_mod.evaluate_entry(_entry(side="NONE"), _bars(100, 101, 99))
        self.assertEqual(out.skipped, "no directional side")

    def test_call_levels_and_excursions(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=101.5, high=102.5, low=99.5))
        self.assertTrue(out.hit_t1)
        self.assertTrue(out.hit_t2)
        self.assertFalse(out.hit_stop)
        self.assertFalse(out.ambiguous)
        self.assertAlmostEqual(out.mfe_pct, 2.5)
        self.assertAlmostEqual(out.mae_pct, -0.5)

    def test_put_levels_invert(self):
        entry = _entry(side="PUTS", stop=101.0, t1=99.0, t2=98.0)
        out = review_mod.evaluate_entry(entry, _bars(close=98.5, high=100.2, low=97.5))
        self.assertTrue(out.hit_t1)
        self.assertTrue(out.hit_t2)
        self.assertFalse(out.hit_stop)
        self.assertAlmostEqual(out.mfe_pct, 2.5)

    def test_touching_both_levels_is_ambiguous_not_a_win(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=100.0, high=101.5, low=98.5))
        self.assertTrue(out.hit_t1)
        self.assertTrue(out.hit_stop)
        self.assertTrue(out.ambiguous)
        self.assertIsNone(out.managed_return)
        self.assertEqual(out.exit_reason, "ambiguous")

    def test_expiry_value_is_intrinsic_at_the_close(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=101.0, high=101.2, low=100.0))
        core = next(r for r in out.rungs if r["rung"] == "core")
        self.assertAlmostEqual(core["value_at_expiry"], 1.0)      # 101 - 100 strike
        self.assertAlmostEqual(core["return_pct"], 0.0)           # paid 1.00, worth 1.00
        runner = next(r for r in out.rungs if r["rung"] == "runner")
        self.assertTrue(runner["expired_worthless"])              # 101 strike, 101 close
        self.assertAlmostEqual(runner["return_pct"], -100.0)

    def test_managed_exit_at_target_uses_delta(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=100.2, high=101.4, low=99.8))
        self.assertEqual(out.exit_reason, "target")
        core = next(r for r in out.rungs if r["rung"] == "core")
        # premium 1.00, delta 0.45, +1.00 move to T1 -> ~1.45
        self.assertAlmostEqual(core["managed_return_pct"], 45.0, places=0)
        # Held to expiry the same trade returns far less.
        self.assertLess(core["return_pct"], core["managed_return_pct"])

    def test_managed_stop_is_the_premium_stop(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=99.2, high=100.4, low=98.6))
        self.assertEqual(out.exit_reason, "stop")
        core = next(r for r in out.rungs if r["rung"] == "core")
        self.assertAlmostEqual(core["managed_return_pct"], -35.0)

    def test_managed_falls_through_to_expiry_when_nothing_is_touched(self):
        out = review_mod.evaluate_entry(_entry(), _bars(close=100.3, high=100.6, low=99.4))
        self.assertEqual(out.exit_reason, "expiry")
        core = next(r for r in out.rungs if r["rung"] == "core")
        self.assertAlmostEqual(core["managed_return_pct"], core["return_pct"])

    def test_missing_greeks_still_produce_a_managed_number(self):
        rungs = [{"rung": "core", "strike": 100.0, "mid": 1.00, "delta": None}]
        out = review_mod.evaluate_entry(_entry(rungs=rungs),
                                        _bars(close=100.2, high=101.4, low=99.8))
        core = out.rungs[0]
        self.assertIsNotNone(core["managed_return_pct"])


class TestSummarise(unittest.TestCase):
    def _outcomes(self):
        rows = [
            (_entry("AAA", score=75.0), _bars(close=101.2, high=101.6, low=99.9)),
            (_entry("BBB", score=72.0), _bars(close=101.0, high=101.3, low=99.8)),
            (_entry("CCC", score=48.0), _bars(close=99.2, high=100.2, low=98.5)),
            (_entry("DDD", score=46.0), _bars(close=99.4, high=100.1, low=98.7)),
        ]
        return [review_mod.evaluate_entry(e, b) for e, b in rows]

    def test_buckets_split_by_score(self):
        summary = review_mod.summarise(self._outcomes())
        self.assertEqual(summary["evaluated"], 4)
        self.assertIn("70+", summary["by_score_bucket"])
        self.assertIn("<50", summary["by_score_bucket"])
        self.assertEqual(summary["by_score_bucket"]["70+"]["n"], 2)

    def test_higher_bucket_beats_lower_when_the_signal_works(self):
        summary = review_mod.summarise(self._outcomes())
        self.assertGreater(summary["by_score_bucket"]["70+"]["mean"],
                           summary["by_score_bucket"]["<50"]["mean"])

    def test_block_edge_is_reported_per_block(self):
        summary = review_mod.summarise(self._outcomes())
        self.assertIn("trend", summary["by_block"])
        self.assertIn("agreed", summary["by_block"]["trend"])

    def test_levels_and_exit_reasons_are_counted(self):
        summary = review_mod.summarise(self._outcomes())
        self.assertIsNotNone(summary["levels"]["reached_t1_pct"])
        self.assertTrue(summary["exit_reasons"])

    def test_empty_input_does_not_explode(self):
        summary = review_mod.summarise([])
        self.assertEqual(summary["evaluated"], 0)
        self.assertEqual(summary["overall"]["n"], 0)

    def test_render_handles_an_empty_review(self):
        text = review_mod.render_terminal([], review_mod.summarise([]))
        self.assertIn("Journal review", text)


class TestReviewEndToEnd(unittest.TestCase):
    def test_review_joins_the_journal_against_price_history(self):
        symbols = ["AAA", "BBB"]
        provider = FakeProvider(symbols, TODAY)
        bars = {s: provider.daily_bars(s) for s in symbols}
        last = {s: bars[s][-1] for s in symbols}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                for symbol in symbols:
                    bar = last[symbol]
                    fh.write(json.dumps(_entry(
                        symbol, spot=bar.open, expiry=str(bar.date),
                        stop=bar.open * 0.99, t1=bar.open * 1.005, t2=bar.open * 1.01,
                        rungs=[{"rung": "core", "strike": round(bar.open),
                                "mid": max(0.05, bar.open * 0.004), "delta": 0.45}],
                    )) + "\n")
            outcomes, summary = review_mod.review(path, provider)
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(summary["skipped"], 0)
        for out in outcomes:
            self.assertIsNotNone(out.close_at_expiry)
            self.assertIsNotNone(out.mfe_pct)

    def test_since_filter_excludes_older_expiries(self):
        provider = FakeProvider(["AAA"], TODAY)
        bar = provider.daily_bars("AAA")[-1]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_entry("AAA", expiry="2020-01-03")) + "\n")
                fh.write(json.dumps(_entry("AAA", expiry=str(bar.date))) + "\n")
            outcomes, _ = review_mod.review(path, provider, since=dt.date(2026, 1, 1))
        self.assertEqual(len(outcomes), 1)

    def test_a_provider_failure_is_recorded_not_raised(self):
        provider = FakeProvider(["AAA"], TODAY)

        def boom(*a, **k):
            raise ConnectionError("no route")

        provider.daily_bars = boom
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_entry("AAA")) + "\n")
            outcomes, summary = review_mod.review(path, provider)
        self.assertEqual(len(outcomes), 1)
        self.assertIn("price fetch failed", outcomes[0].skipped)
        self.assertEqual(summary["evaluated"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
