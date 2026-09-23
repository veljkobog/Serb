import unittest
from datetime import datetime
from typing import Dict, List

from odte.models import SymbolSnapshot, OptionQuote
from odte.providers.tradier import (
    TradierClient,
    TradierError,
    TradierProvider,
    TradierTapeStream,
    _as_list,
    liquid_contracts,
    parse_bars,
    parse_chain,
)
from odte.session import to_et
from odte.sides import BUY, SELL

NOW = to_et(datetime.fromisoformat("2026-09-22T10:30:00"))

CHAIN_ROWS = [
    {
        "symbol": "SPY260922C00590000", "strike": 590.0, "option_type": "call",
        "bid": 1.10, "ask": 1.14, "last": 1.12, "volume": 12000, "open_interest": 8000,
        "greeks": {"delta": 0.52, "gamma": 0.045, "mid_iv": 0.098, "smv_vol": 0.101},
    },
    {
        "symbol": "SPY260922P00590000", "strike": 590.0, "option_type": "put",
        "bid": 1.04, "ask": 1.08, "last": 1.06, "volume": 9000, "open_interest": 7400,
        "greeks": {"delta": -0.48, "gamma": 0.045, "mid_iv": 0.0, "smv_vol": 0.102},
    },
    {"symbol": "JUNK", "strike": None, "option_type": "call"},
    {"symbol": "SPY260922X00590000", "strike": 590.0, "option_type": "other"},
]

BAR_ROWS = [
    {"time": "2026-09-22T09:25:00", "open": 585.0, "high": 585.5, "low": 584.9, "close": 585.2, "volume": 1000},
    {"time": "2026-09-22T09:30:00", "open": 585.2, "high": 586.4, "low": 585.0, "close": 586.1, "volume": 900000},
    {"time": "2026-09-22T10:25:00", "open": 586.1, "high": 588.0, "low": 586.0, "close": 587.8, "volume": 450000},
    {"time": "2026-09-21T10:25:00", "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1},
    {"time": "2026-09-22T16:05:00", "open": 588.0, "high": 588.2, "low": 587.9, "close": 588.0, "volume": 5000},
]


class FakeClient:
    """Stands in for TradierClient so the provider can be tested offline."""

    env = "production"
    token = "fake"

    def __init__(self, expirations: List[str] = None):
        self._expirations = expirations if expirations is not None else ["2026-09-22", "2026-09-23"]
        self.calls: List[str] = []

    def quotes(self, symbols, greeks=False) -> Dict[str, Dict]:
        self.calls.append("quotes")
        return {
            s: {
                "symbol": s, "last": 587.9, "bid": 587.88, "ask": 587.9,
                "prevclose": 584.6, "average_volume": 78e6,
                "trade_date": int(NOW.timestamp() * 1000),
            }
            for s in symbols
        }

    def clock(self) -> Dict[str, str]:
        self.calls.append("clock")
        return {"state": "open", "description": "Market is open from 09:30 to 16:00"}

    def stream_session(self) -> Dict[str, str]:
        self.calls.append("stream_session")
        return {"sessionid": "abcdef0123456789", "url": "https://stream.tradier.com/v1/markets/events"}

    def timesales(self, symbol, start, end, interval="5min"):
        self.calls.append("timesales")
        return BAR_ROWS

    def daily_history(self, symbol, start, end):
        self.calls.append("history")
        return [
            {"date": "2026-09-18", "open": 580, "high": 586, "low": 578, "close": 583, "volume": 7e7},
            {"date": "2026-09-21", "open": 583, "high": 588, "low": 582, "close": 584.6, "volume": 7e7},
        ]

    def expirations(self, symbol):
        self.calls.append("expirations")
        return self._expirations

    def chain(self, symbol, expiration):
        self.calls.append("chain")
        return CHAIN_ROWS


class TestParsers(unittest.TestCase):
    def test_as_list_handles_single_and_many(self):
        self.assertEqual(_as_list({"quote": {"a": 1}}, "quote"), [{"a": 1}])
        self.assertEqual(_as_list({"quote": [{"a": 1}, {"a": 2}]}, "quote"), [{"a": 1}, {"a": 2}])
        self.assertEqual(_as_list(None, "quote"), [])
        self.assertEqual(_as_list({"quote": None}, "quote"), [])

    def test_parse_chain_keeps_broker_greeks(self):
        quotes = parse_chain(CHAIN_ROWS)
        self.assertEqual(len(quotes), 2)  # junk and unknown right dropped
        call, put = quotes
        self.assertEqual(call.right, "C")
        self.assertEqual(call.occ, "SPY260922C00590000")
        self.assertAlmostEqual(call.delta, 0.52)
        self.assertAlmostEqual(call.gamma, 0.045)
        self.assertAlmostEqual(call.iv, 0.098)
        self.assertEqual(put.right, "P")
        self.assertLess(put.delta, 0)
        # mid_iv of 0 is unusable, so the next IV field is taken instead.
        self.assertAlmostEqual(put.iv, 0.102)

    def test_parse_bars_keeps_only_this_regular_session(self):
        bars = parse_bars(BAR_ROWS, NOW)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].ts.hour, 9)
        self.assertEqual(bars[0].ts.minute, 30)
        self.assertTrue(all(b.ts.date() == NOW.date() for b in bars))


class TestProvider(unittest.TestCase):
    def test_snapshot_assembles_everything(self):
        provider = TradierProvider(client=FakeClient())
        snap = provider.snapshot("SPY", NOW)
        self.assertEqual(snap.spot, 587.9)
        self.assertEqual(snap.prev_close, 584.6)
        self.assertEqual(snap.expiry, "2026-09-22")
        self.assertEqual(len(snap.chain), 2)
        self.assertEqual(len(snap.bars), 2)
        self.assertAlmostEqual(snap.adr_pct, ((586 - 578) / 583 + (588 - 582) / 584.6) / 2 * 100, places=4)
        self.assertNotIn("no-0dte", snap.notes)

    def test_symbol_without_a_same_day_expiry_is_flagged(self):
        provider = TradierProvider(client=FakeClient(expirations=["2026-09-25"]))
        snap = provider.snapshot("SPY", NOW)
        self.assertIn("no-0dte", snap.notes)
        self.assertIsNone(snap.expiry)
        self.assertEqual(snap.chain, [])

    def test_sandbox_is_flagged_as_delayed(self):
        client = FakeClient()
        client.env = "sandbox"
        snap = TradierProvider(client=client).snapshot("SPY", NOW)
        self.assertIn("sandbox-delayed", snap.notes)

    def test_missing_token_is_a_clear_error(self):
        with self.assertRaises(TradierError) as ctx:
            TradierClient(token="")
        self.assertIn("TRADIER_TOKEN", str(ctx.exception))

    def test_sandbox_env_selects_the_sandbox_host(self):
        self.assertIn("sandbox", TradierClient(token="x", env="sandbox").base)
        self.assertNotIn("sandbox", TradierClient(token="x", env="production").base)


class TestTapeStream(unittest.TestCase):
    def setUp(self):
        self.stream = TradierTapeStream(client=FakeClient())

    def test_timesale_events_are_classified(self):
        line = ('{"type":"timesale","symbol":"SPY260922C00590000","price":"1.14",'
                '"size":"25","bid":"1.10","ask":"1.14","date":"1758551400000"}')
        self.assertEqual(self.stream.handle_line(line), BUY)
        sell = ('{"type":"timesale","symbol":"SPY260922C00590000","price":"1.10",'
                '"size":"40","bid":"1.10","ask":"1.14","date":"1758551460000"}')
        self.assertEqual(self.stream.handle_line(sell), SELL)
        c = self.stream.tape.contracts["SPY260922C00590000"]
        self.assertEqual((c.buy, c.sell), (25, 40))
        self.assertEqual(self.stream.events, 2)

    def test_non_timesale_and_malformed_lines_are_ignored(self):
        self.assertIsNone(self.stream.handle_line('{"type":"trade","symbol":"SPY","price":"1"}'))
        self.assertIsNone(self.stream.handle_line("not json"))
        self.assertIsNone(self.stream.handle_line(""))
        self.assertIsNone(self.stream.handle_line('{"type":"timesale","symbol":"X"}'))
        self.assertEqual(self.stream.tape.trades, 0)

    def test_stream_needs_symbols(self):
        with self.assertRaises(ValueError):
            self.stream.start([])


class TestContractSelection(unittest.TestCase):
    def test_liquid_contracts_are_nearest_the_money_first(self):
        chain = [
            OptionQuote(strike=k, right="C", volume=v, occ=f"SPY260922C{int(k*1000):08d}")
            for k, v in ((580, 100), (590, 5000), (600, 200))
        ]
        snap = SymbolSnapshot(symbol="SPY", spot=589.0, prev_close=584.0, chain=chain)
        picks = liquid_contracts(snap, per_symbol=2)
        self.assertEqual(picks[0], "SPY260922C00590000")
        self.assertEqual(len(picks), 2)

    def test_quotes_without_occ_symbols_are_skipped(self):
        snap = SymbolSnapshot(
            symbol="SPY", spot=589.0, prev_close=584.0,
            chain=[OptionQuote(strike=590, right="C", volume=10)],
        )
        self.assertEqual(liquid_contracts(snap), [])


if __name__ == "__main__":
    unittest.main()
