import unittest

from odte.rating import MAX_SETUP_BONUS, band_for, rate
from odte.setups import SETUPS, Hit


def hits(*keys):
    return [Hit(SETUPS[k], "detail") for k in keys]


class TestRating(unittest.TestCase):
    def test_bias_alone_tops_out_at_seven(self):
        r = rate(100.0, 100.0, [])
        self.assertEqual(r.side, "BULL")
        self.assertAlmostEqual(r.score, 7.0, places=1)

    def test_setups_earn_the_last_points(self):
        bare = rate(100.0, 100.0, [])
        stacked = rate(100.0, 100.0, hits("short_gamma_breakout", "gap_and_go", "magnet_up"))
        self.assertGreater(stacked.score, bare.score)
        self.assertLessEqual(stacked.setup_bonus, MAX_SETUP_BONUS)
        self.assertEqual(stacked.score, 10.0)  # 7 + capped 3

    def test_bearish_bias_reads_bear(self):
        r = rate(-60.0, 80.0, hits("short_gamma_breakdown"))
        self.assertEqual(r.side, "BEAR")
        self.assertIn("BEAR", r.headline)
        self.assertIn("Short-gamma breakdown", r.drivers)

    def test_opposing_setups_count_against_the_side(self):
        clean = rate(60.0, 80.0, hits("gap_and_go"))
        fought = rate(60.0, 80.0, hits("gap_and_go", "vwap_rejection"))
        self.assertLess(fought.score, clean.score)
        self.assertIn("VWAP rejection", fought.blockers)

    def test_blockers_drag_any_rating_down(self):
        clean = rate(80.0, 90.0, hits("short_gamma_breakout"))
        pinned = rate(80.0, 90.0, hits("short_gamma_breakout", "pin_lock", "illiquid"))
        self.assertLess(pinned.score, clean.score - 2.0)
        self.assertIn("Pinned", pinned.blockers)
        self.assertGreater(pinned.blocker_penalty, 0)

    def test_confidence_haircut(self):
        strong = rate(70.0, 100.0, hits("gap_and_go"))
        weak = rate(70.0, 0.0, hits("gap_and_go"))
        self.assertAlmostEqual(weak.score / strong.score, 0.55, delta=0.02)

    def test_score_is_clamped_and_never_negative(self):
        floored = rate(5.0, 10.0, hits("illiquid", "pin_lock", "em_exhausted"))
        self.assertGreaterEqual(floored.score, 0.0)
        self.assertFalse(floored.actionable)
        self.assertEqual(floored.band, "STAND DOWN")

    def test_bands_and_headline(self):
        self.assertEqual(band_for(9.0), "MAX CONVICTION")
        self.assertEqual(band_for(7.0), "STRONG")
        self.assertEqual(band_for(5.0), "MODERATE")
        self.assertEqual(band_for(3.2), "LEAN")
        self.assertEqual(band_for(1.0), "STAND DOWN")
        r = rate(75.0, 85.0, hits("short_gamma_breakout"))
        self.assertEqual(r.headline, f"BULL {r.stars}/10 - {r.band}")

    def test_signed_score_lets_runs_compare_on_one_axis(self):
        self.assertGreater(rate(60.0, 80.0, []).signed_score, 0)
        self.assertLess(rate(-60.0, 80.0, []).signed_score, 0)


if __name__ == "__main__":
    unittest.main()
