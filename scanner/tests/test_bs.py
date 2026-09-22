import math
import unittest

from odte import bs

T = 5.5 / 24 / 365  # ~5.5 hours to the close


class TestBlackScholes(unittest.TestCase):
    def test_put_call_parity(self):
        c = bs.price(100, 98, T, 0.25, "C", r=0.04)
        p = bs.price(100, 98, T, 0.25, "P", r=0.04)
        expected = 100 - 98 * math.exp(-0.04 * T)
        self.assertAlmostEqual(c - p, expected, places=6)

    def test_delta_bounds_and_signs(self):
        self.assertTrue(0 < bs.delta(100, 100, T, 0.3, "C") < 1)
        self.assertTrue(-1 < bs.delta(100, 100, T, 0.3, "P") < 0)
        self.assertGreater(bs.delta(100, 90, T, 0.3, "C"), bs.delta(100, 110, T, 0.3, "C"))

    def test_expiry_degenerates_to_intrinsic(self):
        self.assertEqual(bs.price(101, 100, 0, 0.3, "C"), 1.0)
        self.assertEqual(bs.price(99, 100, 0, 0.3, "C"), 0.0)
        self.assertEqual(bs.delta(101, 100, 0, 0.3, "C"), 1.0)
        self.assertEqual(bs.delta(99, 100, 0, 0.3, "P"), -1.0)

    def test_gamma_stays_finite_at_expiry(self):
        g = bs.gamma(100, 100, 0.0, 0.3)
        self.assertTrue(math.isfinite(g))
        self.assertGreater(g, 0)

    def test_implied_vol_roundtrip(self):
        # Strikes are set in units of the option's own 1-sigma move: a strike
        # 7% away is unsolvable at 8% vol with hours left, which is correct.
        for vol in (0.08, 0.25, 0.9):
            sigma = 100 * vol * math.sqrt(T)
            for strike in (100 - sigma, 100.0, 100 + sigma):
                px = bs.price(100, strike, T, vol, "C")
                solved = bs.implied_vol(px, 100, strike, T, "C")
                self.assertIsNotNone(solved)
                self.assertAlmostEqual(solved, vol, places=3)

    def test_implied_vol_rejects_unsolvable(self):
        self.assertIsNone(bs.implied_vol(0.0, 100, 100, T, "C"))
        self.assertIsNone(bs.implied_vol(1.0, 105, 100, T, "C"))   # below intrinsic
        self.assertIsNone(bs.implied_vol(60.0, 100, 100, T, "C"))  # above the bound


if __name__ == "__main__":
    unittest.main()
