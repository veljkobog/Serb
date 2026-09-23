import unittest

from odte.config import Gates, ScanConfig, Weights
from odte.option_metrics import OptionMetrics, StrikeGamma
from odte.price_metrics import PriceMetrics
from odte.scoring import SymbolData, score_universe

CFG = ScanConfig(weights=Weights(), gates=Gates())


def pm(**over) -> PriceMetrics:
    base = dict(
        last=100.0, open_px=99.0, prev_close=98.5, gap_pct=0.5, ret_open_pct=1.0,
        ret_prev_close_pct=1.5, vwap=99.2, vwap_z=1.5, vwap_slope_bps=12.0,
        or_high=99.8, or_low=98.8, or_pos=1.2, clv=0.8, persistence=0.85,
        session_range_pct=1.2, move_vs_adr=0.5, share_volume=5_000_000,
        beta=1.0, rs_resid_pct=0.8,
    )
    base.update(over)
    return PriceMetrics(**base)


def om(**over) -> OptionMetrics:
    base = dict(
        spot=100.0, t_years=0.0006, atm_strike=100.0, atm_iv=0.20,
        atm_straddle=1.00, em_pct=1.0, em_dollars=1.00, rr25=-1.0, skew_norm=-0.05,
        pc_volume_ratio=0.7, pc_oi_ratio=0.9, vol_oi_ratio=0.4, oi_tilt=0.2,
        flow_tilt=0.3, flow_source="proxy", proxy_flow_tilt=0.3,
        signed_flow_tilt=None, net_delta_contracts=0.0, net_delta_dollars=0.0,
        net_premium_dollars=0.0, sampled_volume=0.0, side_coverage=None,
        call_flow_tilt=None, put_flow_tilt=None, call_wall=103.0, put_wall=97.0,
        call_delta_dollars=5e6, put_delta_dollars=2e6,
        max_pain=101.0, gamma_wall=103.0, gamma_flip=99.0, net_gex=5e7,
        chain_volume=50_000, window_volume=30_000, total_oi=120_000,
        avg_atm_spread_pct=0.03, strikes_used=40,
        gamma_profile=[
            StrikeGamma(strike=99.0, net_gex=1e6, total_gex=2e6),
            StrikeGamma(strike=100.0, net_gex=2e6, total_gex=4e6),
            StrikeGamma(strike=101.0, net_gex=1e6, total_gex=2e6),
        ],
    )
    base.update(over)
    return OptionMetrics(**base)


_DEFAULT = object()


def data(symbol="TEST", price=None, options=_DEFAULT, **kw) -> SymbolData:
    return SymbolData(
        symbol=symbol,
        price=price if price is not None else pm(),
        options=om() if options is _DEFAULT else options,
        minutes_to_close=330.0,
        expiry="2026-09-22",
        **kw,
    )


class TestScoring(unittest.TestCase):
    def test_bullish_inputs_score_upside(self):
        s = score_universe([data()], CFG)[0]
        self.assertGreater(s.bias, 25)
        self.assertIn("LONG", s.verdict)
        self.assertEqual(s.plan.direction, "calls")

    def test_bearish_inputs_score_downside(self):
        bear_price = pm(
            last=98.0, ret_open_pct=-1.0, ret_prev_close_pct=-1.5, vwap_z=-1.6,
            vwap_slope_bps=-12.0, or_pos=-1.3, clv=-0.8, persistence=0.12,
            rs_resid_pct=-0.9,
        )
        bear_opts = om(
            rr25=-6.0, skew_norm=-0.30, pc_volume_ratio=2.2, oi_tilt=-0.3,
            flow_tilt=-0.4, max_pain=97.0,
        )
        s = score_universe([data(price=bear_price, options=bear_opts)], CFG)[0]
        self.assertLess(s.bias, -25)
        self.assertIn("SHORT", s.verdict)
        self.assertEqual(s.plan.direction, "puts")

    def test_bias_stays_in_range_for_extremes(self):
        extreme = pm(vwap_z=99, or_pos=99, clv=1.0, persistence=1.0, rs_resid_pct=50)
        opts = om(pc_volume_ratio=0.001, flow_tilt=1.0, oi_tilt=1.0, max_pain=200.0)
        s = score_universe([data(price=extreme, options=opts)], CFG)[0]
        self.assertLessEqual(s.bias, 100.0)
        self.assertGreaterEqual(s.bias, -100.0)
        self.assertLessEqual(s.confidence, 100.0)

    def test_missing_chain_still_scores_on_tape_alone(self):
        s = score_universe([data(options=None, notes=["no-chain"])], CFG)[0]
        self.assertGreater(s.bias, 0)
        self.assertIn("partial-data", s.flags)
        self.assertIn("no-chain", s.flags)
        self.assertIsNone(s.options)
        # Renormalization keeps the scale: tape-only still reaches full range.
        self.assertLessEqual(abs(s.bias), 100.0)

    def test_illiquid_chain_is_avoided(self):
        thin = om(chain_volume=200, avg_atm_spread_pct=0.30)
        s = score_universe([data(options=thin)], CFG)[0]
        self.assertIn("thin-chain", s.flags)
        self.assertIn("wide-spreads", s.flags)
        self.assertTrue(s.verdict.startswith("AVOID"))

    def test_weak_signal_is_no_trade(self):
        flat = pm(
            ret_open_pct=0.02, ret_prev_close_pct=0.02, vwap_z=0.05,
            vwap_slope_bps=0.1, or_pos=0.05, clv=0.0, persistence=0.5,
            rs_resid_pct=0.0, move_vs_adr=0.1,
        )
        neutral = om(
            rr25=0.0, skew_norm=0.0, pc_volume_ratio=1.0, oi_tilt=0.0,
            flow_tilt=0.0, max_pain=100.0,
        )
        s = score_universe([data(price=flat, options=neutral)], CFG)[0]
        self.assertLess(abs(s.bias), Gates().min_bias_to_trade)
        self.assertEqual(s.verdict, "NO TRADE")

    def test_pin_risk_flag_needs_long_gamma_and_a_close_wall(self):
        pinned = om(gamma_wall=100.1, net_gex=5e7)
        chased = om(gamma_wall=100.1, net_gex=-5e7)
        self.assertIn("pin-risk", score_universe([data(options=pinned)], CFG)[0].flags)
        self.assertNotIn("pin-risk", score_universe([data(options=chased)], CFG)[0].flags)

    def test_short_gamma_discounts_the_max_pain_pull(self):
        long_gamma = score_universe([data(options=om(max_pain=103.0, net_gex=5e7))], CFG)[0]
        short_gamma = score_universe([data(options=om(max_pain=103.0, net_gex=-5e7))], CFG)[0]
        self.assertGreater(
            long_gamma.components["gamma_pull"], short_gamma.components["gamma_pull"]
        )

    def test_stretched_move_pushes_back_against_the_trend(self):
        stretched = score_universe([data(price=pm(move_vs_adr=1.6))], CFG)[0]
        early = score_universe([data(price=pm(move_vs_adr=0.3))], CFG)[0]
        self.assertLess(stretched.components["stretch"], 0)  # up-move, so it brakes
        self.assertEqual(early.components["stretch"], 0.0)
        self.assertLess(stretched.bias, early.bias)

    def test_skew_is_standardized_across_the_universe(self):
        # Every name is put-bid in absolute terms; the least put-bid name should
        # still come out with a positive skew component.
        universe = [
            data(symbol=f"S{i}", options=om(skew_norm=sn))
            for i, sn in enumerate([-0.30, -0.25, -0.20, -0.15, -0.02])
        ]
        scored = {s.symbol: s for s in score_universe(universe, CFG)}
        self.assertGreater(scored["S4"].components["skew"], 0)
        self.assertLess(scored["S0"].components["skew"], 0)

    def test_plan_levels(self):
        s = score_universe([data()], CFG)[0]
        self.assertEqual(s.plan.atm_strike, 100.0)
        self.assertGreater(s.plan.otm_1em_strike, s.plan.atm_strike)
        self.assertGreater(s.plan.target, s.price.last)
        self.assertIsNotNone(s.plan.invalidation)

    def test_ranking_prefers_strong_and_confident(self):
        strong = data(symbol="STRONG")
        weak = data(
            symbol="WEAK",
            price=pm(vwap_z=0.1, or_pos=0.1, persistence=0.5, rs_resid_pct=0.0, clv=0.0),
            options=om(flow_tilt=0.02, pc_volume_ratio=1.0, max_pain=100.0),
        )
        ranked = score_universe([weak, strong], CFG)
        self.assertEqual(ranked[0].symbol, "STRONG")


if __name__ == "__main__":
    unittest.main()
