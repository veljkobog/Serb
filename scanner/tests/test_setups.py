import unittest

from odte.setups import detect
try:  # discover runs these as top-level modules, -m runs them as a package
    from test_scoring import om, pm
except ImportError:  # pragma: no cover
    from tests.test_scoring import om, pm


def keys(hits):
    return {h.key for h in hits}


class TestSetups(unittest.TestCase):
    def test_short_gamma_breakout_needs_short_gamma_and_a_break(self):
        price = pm(last=101.0, or_high=100.0, or_low=98.0, vwap_z=1.2)
        short = om(net_gex=-5e7, flow_tilt=0.3)
        self.assertIn("short_gamma_breakout", keys(detect(price, short)))
        # Same tape, dealers long gamma: rallies get sold into, not chased.
        self.assertNotIn("short_gamma_breakout", keys(detect(price, om(net_gex=5e7))))

    def test_short_gamma_breakdown(self):
        price = pm(last=97.0, or_high=100.0, or_low=98.0, vwap_z=-1.2, persistence=0.2, clv=-0.9)
        hits = keys(detect(price, om(net_gex=-5e7, flow_tilt=-0.3)))
        self.assertIn("short_gamma_breakdown", hits)
        self.assertNotIn("short_gamma_breakout", hits)

    def test_walls_need_price_near_them_and_flow_agreeing(self):
        price = pm(last=103.0)
        near = om(spot=103.0, call_wall=103.2, em_dollars=1.0, flow_tilt=0.4)
        self.assertIn("call_wall_break", keys(detect(price, near)))
        # Same wall, flow the other way: no setup.
        away = om(spot=103.0, call_wall=103.2, em_dollars=1.0, flow_tilt=-0.4)
        self.assertNotIn("call_wall_break", keys(detect(price, away)))

    def test_max_pain_magnet_only_in_long_gamma(self):
        long_gamma = om(spot=100.0, max_pain=102.0, em_dollars=1.0, net_gex=5e7)
        self.assertIn("magnet_up", keys(detect(pm(), long_gamma)))
        below = om(spot=100.0, max_pain=98.0, em_dollars=1.0, net_gex=5e7)
        self.assertIn("magnet_down", keys(detect(pm(), below)))
        short_gamma = om(spot=100.0, max_pain=102.0, em_dollars=1.0, net_gex=-5e7)
        self.assertNotIn("magnet_up", keys(detect(pm(), short_gamma)))

    def test_vwap_reclaim_and_rejection_are_mutually_exclusive(self):
        reclaim = detect(pm(last=101.0, vwap=100.0, vwap_z=1.0, persistence=0.2), om())
        self.assertIn("vwap_reclaim", keys(reclaim))
        reject = detect(pm(last=99.0, vwap=100.0, vwap_z=-1.0, persistence=0.8, clv=-0.5), om())
        self.assertIn("vwap_rejection", keys(reject))
        self.assertNotIn("vwap_reclaim", keys(reject))

    def test_gap_and_go_vs_gap_fade(self):
        go = pm(gap_pct=0.8, open_px=99.0, last=101.0, or_high=100.0, ret_prev_close_pct=1.5)
        self.assertIn("gap_and_go", keys(detect(go, om())))
        fade = pm(gap_pct=0.8, open_px=101.0, last=98.5, or_high=101.5, or_low=99.0,
                  ret_prev_close_pct=-0.9, vwap_z=-1.0, persistence=0.7)
        hits = keys(detect(fade, om()))
        self.assertIn("gap_fade", hits)
        self.assertNotIn("gap_and_go", hits)

    def test_unwinds_need_classified_flow(self):
        sold_puts = om(flow_source="side", put_flow_tilt=0.4, call_flow_tilt=0.0)
        self.assertIn("put_unwind", keys(detect(pm(), sold_puts)))
        sold_calls = om(flow_source="side", call_flow_tilt=-0.4, put_flow_tilt=0.0)
        self.assertIn("call_unwind", keys(detect(pm(), sold_calls)))
        # Same numbers without trade side: nothing to stand on.
        proxy = om(flow_source="proxy", put_flow_tilt=0.4)
        self.assertNotIn("put_unwind", keys(detect(pm(), proxy)))

    def test_blockers(self):
        pinned = om(net_gex=5e7, gamma_wall=100.05, max_pain=100.0, spot=100.0, em_dollars=1.0)
        self.assertIn("pin_lock", keys(detect(pm(last=100.0), pinned)))
        self.assertIn("em_exhausted", keys(detect(pm(move_vs_adr=1.6), om())))
        diverging = om(flow_tilt=-0.5)
        self.assertIn("flow_divergence", keys(detect(pm(vwap_z=2.0, or_pos=1.5), diverging)))
        self.assertIn("proxy_only", keys(detect(pm(), om(flow_source="proxy"))))
        self.assertIn("illiquid", keys(detect(pm(), om(), flags=["wide-spreads"])))

    def test_hits_are_deduplicated_and_ranked(self):
        price = pm(gap_pct=-0.9, open_px=101.0, last=97.0, or_low=98.0, or_high=101.0,
                   vwap_z=-1.5, persistence=0.2, clv=-0.9)
        hits = detect(price, om(net_gex=-5e7, flow_tilt=-0.4))
        self.assertEqual(len(hits), len({h.key for h in hits}))
        strengths = [h.strength for h in hits]
        self.assertEqual(strengths, sorted(strengths, reverse=True))

    def test_every_hit_carries_a_mechanism_and_numbers(self):
        for hit in detect(pm(last=101.0, or_high=100.0, vwap_z=1.2), om(net_gex=-5e7)):
            self.assertTrue(hit.setup.why)
            self.assertTrue(hit.detail)
            self.assertIn(hit.side, (-1, 0, 1))


if __name__ == "__main__":
    unittest.main()
