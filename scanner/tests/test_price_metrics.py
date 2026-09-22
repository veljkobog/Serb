import unittest
from datetime import timedelta

from odte.models import Bar, SymbolSnapshot
from odte.price_metrics import (
    beta_from_bars,
    compute_price_metrics,
    opening_range,
    vwap_and_sigma,
)
from odte.session import session_open
from datetime import date


def ramp(n=12, start=100.0, step=0.25, vol=1000.0, day=date(2026, 9, 22)):
    t0 = session_open(day)
    bars = []
    for i in range(n):
        o = start + i * step
        c = o + step
        bars.append(Bar(t0 + timedelta(minutes=5 * i), o, c + 0.05, o - 0.05, c, vol))
    return bars


def from_returns(rets, start=100.0, vol=1000.0, day=date(2026, 9, 22)):
    """Bars whose closes follow an explicit return series."""
    t0 = session_open(day)
    bars = []
    px = start
    for i, r in enumerate(rets):
        o = px
        px = o * (1 + r)
        bars.append(Bar(t0 + timedelta(minutes=5 * i), o, max(o, px), min(o, px), px, vol))
    return bars


class TestPriceMetrics(unittest.TestCase):
    def test_vwap_is_volume_weighted(self):
        t0 = session_open(date(2026, 9, 22))
        bars = [
            Bar(t0, 10, 10, 10, 10, 100),
            Bar(t0 + timedelta(minutes=5), 20, 20, 20, 20, 300),
        ]
        vwap, sigma = vwap_and_sigma(bars)
        self.assertAlmostEqual(vwap, 17.5)
        self.assertGreater(sigma, 0)

    def test_opening_range_uses_only_the_first_window(self):
        bars = ramp(n=12)
        hi, lo = opening_range(bars, 30)  # first 6 of 12 five-minute bars
        self.assertAlmostEqual(lo, 99.95)
        self.assertAlmostEqual(hi, 101.55)
        self.assertLess(hi, max(b.high for b in bars))

    def test_uptrend_metrics(self):
        snap = SymbolSnapshot(
            symbol="TEST", spot=103.25, prev_close=99.5, bars=ramp(), adr_pct=2.0
        )
        m = compute_price_metrics(snap, 30)
        self.assertGreater(m.ret_open_pct, 0)
        self.assertGreater(m.vwap_z, 0)
        self.assertGreater(m.or_pos, 1.0)       # broken out above the opening range
        self.assertAlmostEqual(m.persistence, 1.0, places=2)
        self.assertGreater(m.clv, 0.9)
        self.assertAlmostEqual(m.gap_pct, (100 - 99.5) / 99.5 * 100, places=6)

    def test_beta_and_relative_strength(self):
        # Same shocks in both series (so beta ~ 1) plus a steady outperformance
        # drift in the symbol: relative strength must come out positive.
        shocks = [0.004, -0.002, 0.003, -0.001, 0.005, -0.003, 0.002, 0.001,
                  -0.004, 0.003, 0.002, -0.001]
        bench = from_returns(shocks)
        strong = from_returns([r + 0.0015 for r in shocks])
        beta = beta_from_bars(strong, bench)
        self.assertAlmostEqual(beta, 1.0, delta=0.35)
        snap = SymbolSnapshot(symbol="STRONG", spot=strong[-1].close, prev_close=100.0,
                              bars=strong)
        m = compute_price_metrics(snap, 30, bench)
        self.assertGreater(m.rs_resid_pct, 0.5)

    def test_benchmark_has_zero_relative_strength(self):
        bars = ramp()
        snap = SymbolSnapshot(symbol="SPY", spot=bars[-1].close, prev_close=100.0,
                              bars=bars, is_benchmark=True)
        m = compute_price_metrics(snap, 30, bars)
        self.assertEqual(m.rs_resid_pct, 0.0)

    def test_no_bars_returns_none(self):
        self.assertIsNone(
            compute_price_metrics(SymbolSnapshot("X", 10, 10, []), 30)
        )


if __name__ == "__main__":
    unittest.main()
