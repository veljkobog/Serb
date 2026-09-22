import math
import unittest

from odte import bs
from odte.models import OptionQuote
from odte.option_metrics import compute_option_metrics, max_pain

T = 5.5 / 24 / 365
SPOT = 100.0


def chain(
    skew_pts=0.0,
    call_vol=1000.0,
    put_vol=1000.0,
    call_oi=5000.0,
    put_oi=5000.0,
    atm_iv=0.20,
    step=1.0,
    width=8,
):
    """Flat-OI test chain with a linear vol skew in moneyness.

    skew_pts > 0 puts the higher vol on upside strikes (call-bid skew).
    """
    out = []
    for k in range(-width, width + 1):
        strike = SPOT + k * step
        moneyness = (strike - SPOT) / SPOT
        iv = max(0.02, atm_iv + skew_pts / 100.0 * (moneyness / 0.05))
        for right in ("C", "P"):
            px = bs.price(SPOT, strike, T, iv, right)
            mid = max(0.01, px)
            out.append(
                OptionQuote(
                    strike=strike,
                    right=right,
                    bid=round(mid * 0.98, 2),
                    ask=round(mid * 1.02, 2),
                    last=round(mid, 2),
                    volume=call_vol if right == "C" else put_vol,
                    open_interest=call_oi if right == "C" else put_oi,
                    iv=iv,
                )
            )
    return out


class TestOptionMetrics(unittest.TestCase):
    def test_expected_move_matches_the_straddle(self):
        m = compute_option_metrics(chain(), SPOT, T)
        self.assertIsNotNone(m.atm_straddle)
        self.assertAlmostEqual(m.em_dollars, m.atm_straddle, places=6)
        # ATM straddle ~ 0.8 * sigma * S for a normal distribution.
        sigma = SPOT * 0.20 * math.sqrt(T)
        self.assertAlmostEqual(m.em_dollars, 0.7979 * sigma, delta=0.02 * sigma)
        self.assertAlmostEqual(m.atm_iv, 0.20, places=3)

    def test_risk_reversal_follows_the_skew(self):
        put_bid = compute_option_metrics(chain(skew_pts=-4.0), SPOT, T)
        call_bid = compute_option_metrics(chain(skew_pts=+4.0), SPOT, T)
        flat = compute_option_metrics(chain(skew_pts=0.0), SPOT, T)
        self.assertLess(put_bid.rr25, -1.0)
        self.assertGreater(call_bid.rr25, 1.0)
        self.assertAlmostEqual(flat.rr25, 0.0, places=6)
        self.assertLess(put_bid.skew_norm, 0)

    def test_volume_and_oi_ratios(self):
        m = compute_option_metrics(chain(call_vol=1000, put_vol=3000), SPOT, T)
        self.assertAlmostEqual(m.pc_volume_ratio, 3.0, places=6)
        self.assertGreater(m.vol_oi_ratio, 0)
        self.assertLess(m.flow_tilt, 0)  # put premium dominates

    def test_flow_tilt_sign_follows_call_volume(self):
        m = compute_option_metrics(chain(call_vol=4000, put_vol=500), SPOT, T)
        self.assertGreater(m.flow_tilt, 0)
        self.assertGreater(m.call_delta_dollars, m.put_delta_dollars)

    def test_oi_tilt_measures_calls_above_vs_puts_below(self):
        heavy_calls = compute_option_metrics(chain(call_oi=9000, put_oi=1000), SPOT, T)
        heavy_puts = compute_option_metrics(chain(call_oi=1000, put_oi=9000), SPOT, T)
        self.assertGreater(heavy_calls.oi_tilt, 0)
        self.assertLess(heavy_puts.oi_tilt, 0)

    def test_max_pain_is_where_the_least_oi_pays_out(self):
        calls = [OptionQuote(strike=95, right="C", open_interest=10_000)]
        puts = [OptionQuote(strike=105, right="P", open_interest=10_000)]
        # Symmetric OI either side: pain is minimized in the middle.
        self.assertEqual(max_pain(calls, puts), 95)
        calls.append(OptionQuote(strike=105, right="C", open_interest=50_000))
        self.assertEqual(max_pain(calls, puts), 95)

    def test_gamma_structure_and_liquidity_fields(self):
        m = compute_option_metrics(chain(), SPOT, T)
        self.assertAlmostEqual(m.gamma_wall, SPOT, delta=1.0)  # gamma peaks ATM
        self.assertEqual(m.strikes_used, 17)
        self.assertGreater(m.chain_volume, 0)
        self.assertGreater(m.total_oi, 0)
        self.assertLess(m.avg_atm_spread_pct, 0.05)
        self.assertLessEqual(m.window_volume, m.chain_volume)

    def test_net_gex_sign_follows_call_vs_put_oi(self):
        self.assertGreater(compute_option_metrics(chain(call_oi=9000, put_oi=100), SPOT, T).net_gex, 0)
        self.assertLess(compute_option_metrics(chain(call_oi=100, put_oi=9000), SPOT, T).net_gex, 0)

    def test_iv_is_resolved_from_mid_when_provider_iv_is_missing(self):
        raw = chain()
        for q in raw:
            q.iv = None
        m = compute_option_metrics(raw, SPOT, T)
        self.assertIsNotNone(m.atm_iv)
        self.assertAlmostEqual(m.atm_iv, 0.20, delta=0.01)

    def test_empty_or_one_sided_chain_returns_none(self):
        self.assertIsNone(compute_option_metrics([], SPOT, T))
        calls_only = [q for q in chain() if q.right == "C"]
        self.assertIsNone(compute_option_metrics(calls_only, SPOT, T))


if __name__ == "__main__":
    unittest.main()
