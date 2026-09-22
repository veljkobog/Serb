import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from odte.models import OptionQuote
from odte.sides import BUY, MID, SELL, SideTape, classify, occ_symbol


class TestClassification(unittest.TestCase):
    def test_quote_rule(self):
        self.assertEqual(classify(1.20, 1.15, 1.20), BUY)    # lifts the offer
        self.assertEqual(classify(1.25, 1.15, 1.20), BUY)    # through the offer
        self.assertEqual(classify(1.15, 1.15, 1.20), SELL)   # hits the bid
        self.assertEqual(classify(1.10, 1.15, 1.20), SELL)

    def test_inside_the_spread_uses_the_midpoint(self):
        self.assertEqual(classify(1.18, 1.10, 1.20), BUY)
        self.assertEqual(classify(1.12, 1.10, 1.20), SELL)
        self.assertEqual(classify(1.15, 1.10, 1.20), MID)

    def test_tick_test_fallback_without_a_quote(self):
        self.assertEqual(classify(1.20, None, None, prev_price=1.10), BUY)
        self.assertEqual(classify(1.00, None, None, prev_price=1.10), SELL)
        self.assertEqual(classify(1.10, None, None, prev_price=1.10), MID)
        self.assertEqual(classify(1.10, None, None), MID)

    def test_occ_symbol_format(self):
        self.assertEqual(occ_symbol("SPY", "2026-09-22", "C", 590.0), "SPY260922C00590000")
        self.assertEqual(occ_symbol("iwm", "2026-09-22", "p", 222.5), "IWM260922P00222500")


class TestSideTape(unittest.TestCase):
    def setUp(self):
        self.occ = occ_symbol("SPY", "2026-09-22", "C", 590.0)
        self.t0 = datetime.fromisoformat("2026-09-22T10:00:00")

    def _tape(self) -> SideTape:
        tape = SideTape()
        tape.record(self.occ, 1.20, 50, 1.15, 1.20, self.t0)                     # buy
        tape.record(self.occ, 1.15, 30, 1.15, 1.20, self.t0 + timedelta(minutes=5))  # sell
        # A print exactly at the midpoint falls back to the tick test, and the
        # previous print was lower, so Lee-Ready calls this one a buy.
        tape.record(self.occ, 1.175, 10, 1.15, 1.20, self.t0 + timedelta(minutes=10))
        return tape

    def test_accumulates_and_prices_each_side(self):
        tape = self._tape()
        c = tape.contracts[self.occ]
        self.assertEqual((c.buy, c.sell, c.mid), (60, 30, 0))
        self.assertAlmostEqual(c.buy_premium, 50 * 1.20 * 100 + 10 * 1.175 * 100)
        self.assertAlmostEqual(c.sell_premium, 30 * 1.15 * 100)
        self.assertEqual(tape.trades, 3)
        self.assertEqual(tape.window_seconds, 600.0)

    def test_ignores_junk_prints(self):
        tape = SideTape()
        tape.record(self.occ, 0.0, 10, 1.15, 1.20)
        tape.record(self.occ, 1.20, 0, 1.15, 1.20)
        self.assertEqual(tape.trades, 0)
        self.assertEqual(tape.contracts, {})

    def test_merge_into_chain_only_touches_matching_contracts(self):
        tape = self._tape()
        matching = OptionQuote(strike=590.0, right="C", volume=1000, occ=self.occ)
        other = OptionQuote(strike=591.0, right="C", volume=800,
                            occ=occ_symbol("SPY", "2026-09-22", "C", 591.0))
        self.assertEqual(tape.merge_into([matching, other]), 1)
        self.assertEqual(matching.buy_volume, 60)
        self.assertEqual(matching.net_volume, 30)
        self.assertEqual(matching.sampled_volume, 90)
        self.assertTrue(matching.sampled)
        self.assertFalse(other.sampled)

    def test_save_load_roundtrip(self):
        tape = self._tape()
        with tempfile.TemporaryDirectory() as tmp:
            path = tape.save(Path(tmp) / "sides.json")
            loaded = SideTape.load(path)
        self.assertEqual(loaded.trades, tape.trades)
        self.assertEqual(loaded.window_seconds, tape.window_seconds)
        self.assertEqual(loaded.contracts[self.occ].buy, 60)

    def test_merge_tape_folds_chunks_together(self):
        a, b = self._tape(), self._tape()
        a.merge_tape(b)
        self.assertEqual(a.trades, 6)
        self.assertEqual(a.contracts[self.occ].buy, 120)
        self.assertEqual(a.window_seconds, 600.0)


if __name__ == "__main__":
    unittest.main()
