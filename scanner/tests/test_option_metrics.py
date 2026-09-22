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


def sided(right_with_buys=None, buy=600.0, sell=200.0, sampled_strikes=3, **kw):
    """Test chain where customers lift the offer on one side of the book.

    `right_with_buys` gets net buying; the other right gets net selling.
    """
    quotes = chain(**kw)
    strikes = sorted({q.strike for q in quotes}, key=lambda k: abs(k - SPOT))[:sampled_strikes]
    for q in quotes:
        if q.strike not in strikes:
            continue
        buys = buy if q.right == right_with_buys else sell
        sells = sell if q.right == right_with_buys else buy
        q.buy_volume, q.sell_volume = buys, sells
        q.buy_premium = buys * (q.mid or 0) * 100
        q.sell_premium = sells * (q.mid or 0) * 100
    return quotes


class TestSignedFlow(unittest.TestCase):
    # Wide window so every strike in the small test chain is measured.
    WINDOW = 10.0

    def test_call_buying_reads_bullish(self):
        m = compute_option_metrics(sided("C"), SPOT, T, em_window=self.WINDOW)
        self.assertEqual(m.flow_source, "side")
        self.assertGreater(m.flow_tilt, 0)
        self.assertEqual(m.flow_tilt, m.signed_flow_tilt)
        self.assertGreater(m.net_delta_dollars, 0)
        self.assertGreater(m.net_premium_dollars, 0)

    def test_put_buying_reads_bearish(self):
        m = compute_option_metrics(sided("P"), SPOT, T, em_window=self.WINDOW)
        self.assertEqual(m.flow_source, "side")
        self.assertLess(m.flow_tilt, 0)
        self.assertLess(m.net_delta_dollars, 0)
        self.assertLess(m.net_premium_dollars, 0)

    def test_side_data_can_contradict_the_unsigned_proxy(self):
        # Heavy call volume, but the customer is the seller on every print.
        quotes = sided("P", call_vol=5000, put_vol=500)
        m = compute_option_metrics(quotes, SPOT, T, em_window=self.WINDOW)
        self.assertGreater(m.proxy_flow_tilt, 0)   # proxy sees the call volume
        self.assertLess(m.signed_flow_tilt, 0)     # side data sees who was buying
        self.assertEqual(m.flow_tilt, m.signed_flow_tilt)

    def test_thin_sampling_falls_back_to_the_proxy(self):
        quotes = sided("C", buy=5.0, sell=1.0, sampled_strikes=1, call_vol=5000, put_vol=5000)
        m = compute_option_metrics(quotes, SPOT, T, em_window=self.WINDOW)
        self.assertLess(m.side_coverage, 0.05)
        self.assertEqual(m.flow_source, "proxy")
        self.assertEqual(m.flow_tilt, m.proxy_flow_tilt)

    def test_no_side_data_is_proxy_with_empty_counters(self):
        m = compute_option_metrics(chain(), SPOT, T, em_window=self.WINDOW)
        self.assertEqual(m.flow_source, "proxy")
        self.assertIsNone(m.signed_flow_tilt)
        self.assertEqual(m.sampled_volume, 0.0)
        self.assertEqual(m.net_delta_dollars, 0.0)

    def test_coverage_is_the_sampled_share_of_traded_volume(self):
        m = compute_option_metrics(
            sided("C", buy=500, sell=500, sampled_strikes=17, call_vol=1000, put_vol=1000),
            SPOT, T, em_window=self.WINDOW,
        )
        self.assertAlmostEqual(m.side_coverage, 1.0, places=6)
        self.assertAlmostEqual(m.sampled_volume, m.window_volume, places=6)


class TestProviderGreeks(unittest.TestCase):
    def test_broker_deltas_are_used_when_supplied(self):
        quotes = sided("C", buy=1000, sell=0, sampled_strikes=1)
        atm = [q for q in quotes if q.strike == SPOT]
        for q in atm:
            q.delta = 0.60 if q.right == "C" else -0.40
        m = compute_option_metrics(quotes, SPOT, T, em_window=10.0)
        # Customers bought 1000 ATM calls and sold 1000 ATM puts. Both are
        # bullish, and delta-signing adds them: +1000*0.60 and -1000*-0.40.
        expected = 1000 * 0.60 + (-1000) * (-0.40)
        self.assertAlmostEqual(m.net_delta_contracts, expected, places=6)
        self.assertAlmostEqual(m.net_delta_dollars, expected * 100 * SPOT, places=4)

    def test_nonsense_broker_greeks_fall_back_to_black_scholes(self):
        quotes = chain()
        for q in quotes:
            q.delta = 42.0   # out of range
            q.gamma = -1.0   # impossible
        m = compute_option_metrics(quotes, SPOT, T)
        self.assertIsNotNone(m.rr25)
        self.assertGreater(m.gamma_wall, 0)


if __name__ == "__main__":
    unittest.main()
