"""The real HTTP path, against a mock Tradier.

The parser tests use a fake client, which leaves `TradierClient.get/post`, the
auth header, the retry ladder and the streaming reader untested — and those are
exactly the lines that run first against a live account. This serves
Tradier-shaped payloads over real HTTP on loopback and drives the actual client
through them.
"""

import json
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from odte.providers.tradier import (
    TradierClient,
    TradierError,
    TradierProvider,
    TradierTapeStream,
)
from odte.session import to_et
from odte.option_metrics import MIN_SIDE_COVERAGE
from odte.sides import BUY, SELL

NOW = to_et(datetime.fromisoformat("2026-09-25T10:30:00"))
TODAY = "2026-09-25"
STAMP = int(NOW.timestamp() * 1000)

# One quote, returned as a bare object — Tradier's single-result shape.
QUOTE_ONE = {"quotes": {"quote": {
    "symbol": "SPY", "last": 589.88, "bid": 589.87, "ask": 589.89,
    "prevclose": 584.6, "volume": 41_000_000, "average_volume": 78_000_000,
    "trade_date": STAMP,
}}}
# Two quotes plus the unmatched block that appears when a symbol is unknown.
QUOTE_MANY = {"quotes": {
    "quote": [
        {"symbol": "SPY", "last": 589.88, "bid": 589.87, "ask": 589.89, "prevclose": 584.6},
        {"symbol": "QQQ", "last": 510.30, "bid": 510.29, "ask": 510.31, "prevclose": 505.0},
    ],
    "unmatched_symbols": {"symbol": "NOPE"},
}}
SERIES = {"series": {"data": [
    {"time": f"{TODAY}T09:30:00", "open": 585.2, "high": 586.4, "low": 585.0,
     "close": 586.1, "volume": 900_000, "vwap": 585.8},
    {"time": f"{TODAY}T09:35:00", "open": 586.1, "high": 587.0, "low": 586.0,
     "close": 586.9, "volume": 600_000, "vwap": 586.5},
    {"time": f"{TODAY}T10:25:00", "open": 586.9, "high": 590.1, "low": 586.8,
     "close": 589.9, "volume": 450_000, "vwap": 588.0},
]}}
HISTORY = {"history": {"day": [
    {"date": "2026-09-23", "open": 580, "high": 586, "low": 578, "close": 583, "volume": 7e7},
    {"date": "2026-09-24", "open": 583, "high": 588, "low": 582, "close": 584.6, "volume": 7e7},
]}}
EXPIRATIONS = {"expirations": {"date": [TODAY, "2026-09-26", "2026-09-30"]}}
CHAIN = {"options": {"option": [
    {"symbol": "SPY260925C00590000", "strike": 590.0, "option_type": "call",
     "bid": 1.10, "ask": 1.14, "last": 1.12, "volume": 12000, "open_interest": 8000,
     "greeks": {"delta": 0.52, "gamma": 0.045, "mid_iv": 0.098}},
    {"symbol": "SPY260925P00590000", "strike": 590.0, "option_type": "put",
     "bid": 1.04, "ask": 1.08, "last": 1.06, "volume": 9000, "open_interest": 7400,
     "greeks": {"delta": -0.48, "gamma": 0.045, "mid_iv": 0.101}},
    {"symbol": "SPY260925C00592000", "strike": 592.0, "option_type": "call",
     "bid": 0.42, "ask": 0.45, "last": 0.44, "volume": 6000, "open_interest": 5200,
     "greeks": {"delta": 0.31, "gamma": 0.038, "mid_iv": 0.104}},
]}}
CLOCK = {"clock": {"state": "open", "description": "Market is open from 09:30 to 16:00"}}

TIMESALE_LINES = [
    {"type": "timesale", "symbol": "SPY260925C00590000", "price": "1.14",
     "size": "25", "bid": "1.10", "ask": "1.14", "date": str(STAMP)},
    {"type": "trade", "symbol": "SPY", "price": "589.9", "size": "100"},
    {"type": "timesale", "symbol": "SPY260925C00590000", "price": "1.10",
     "size": "40", "bid": "1.10", "ask": "1.14", "date": str(STAMP + 1000)},
    {"type": "timesale", "symbol": "SPY260925P00590000", "price": "1.08",
     "size": "15", "bid": "1.04", "ask": "1.08", "date": str(STAMP + 2000)},
]


class MockTradier(ThreadingHTTPServer):
    daemon_threads = True

    """Loopback Tradier. `script` maps a path to a payload or a status code."""

    def __init__(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth_ok(self) -> bool:
                header = self.headers.get("Authorization") or ""
                return header.startswith("Bearer ") and len(header) > len("Bearer ")

            def _reply(self, payload, code=200):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _route(self):
                path = self.path.split("?")[0]
                server.seen.append(path)
                if not self._auth_ok():
                    self._reply({"fault": "no token"}, 401)
                    return
                queued = server.status_queue.pop(0) if server.status_queue else None
                if queued:
                    self._reply({"fault": "try later"}, queued)
                    return
                if path == "/v1/markets/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    for line in TIMESALE_LINES:
                        self.wfile.write((json.dumps(line) + "\n").encode())
                        self.wfile.flush()
                    self.close_connection = True
                    return
                payload = server.routes.get(path)
                if payload is None:
                    self._reply({"fault": "not found"}, 404)
                else:
                    self._reply(payload() if callable(payload) else payload)

            do_GET = _route
            do_POST = _route

        super().__init__(("127.0.0.1", 0), Handler)
        self.routes = {}
        self.seen = []
        self.status_queue = []

    def __enter__(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        self.routes = {
            "/v1/markets/clock": CLOCK,
            "/v1/markets/quotes": QUOTE_ONE,
            "/v1/markets/timesales": SERIES,
            "/v1/markets/history": HISTORY,
            "/v1/markets/options/expirations": EXPIRATIONS,
            "/v1/markets/options/chains": CHAIN,
            "/v1/markets/events/session": {
                "stream": {"sessionid": "sess123", "url": f"{self.base}/markets/events"}
            },
        }
        return self

    def __exit__(self, *exc):
        self.shutdown()
        self.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"

    def client(self) -> TradierClient:
        client = TradierClient(token="test-token", env="production")
        client.base = self.base
        client._min_interval = 0.0  # no throttling in tests
        return client


class TestClientOverHttp(unittest.TestCase):
    def test_single_object_and_list_envelopes_both_parse(self):
        with MockTradier() as server:
            client = server.client()
            one = client.quotes(["SPY"])
            self.assertEqual(set(one), {"SPY"})
            self.assertAlmostEqual(one["SPY"]["last"], 589.88)

            server.routes["/v1/markets/quotes"] = QUOTE_MANY
            many = client.quotes(["SPY", "QQQ", "NOPE"])
            self.assertEqual(set(many), {"SPY", "QQQ"})

    def test_the_bearer_token_is_actually_sent(self):
        with MockTradier() as server:
            client = server.client()
            client.token = ""  # no header -> mock answers 401
            with self.assertRaises(TradierError) as ctx:
                client.clock()
            self.assertIn("TRADIER_TOKEN", str(ctx.exception))

    def test_timesales_and_empty_series(self):
        with MockTradier() as server:
            client = server.client()
            self.assertEqual(len(client.timesales("SPY", NOW, NOW)), 3)
            server.routes["/v1/markets/timesales"] = {"series": None}
            self.assertEqual(client.timesales("SPY", NOW, NOW), [])

    def test_expirations_accepts_one_date_or_many(self):
        with MockTradier() as server:
            client = server.client()
            self.assertIn(TODAY, client.expirations("SPY"))
            server.routes["/v1/markets/options/expirations"] = {
                "expirations": {"date": TODAY}
            }
            self.assertEqual(client.expirations("SPY"), [TODAY])

    def test_chain_and_empty_chain(self):
        with MockTradier() as server:
            client = server.client()
            self.assertEqual(len(client.chain("SPY", TODAY)), 3)
            server.routes["/v1/markets/options/chains"] = {"options": None}
            self.assertEqual(client.chain("SPY", TODAY), [])

    def test_rate_limit_is_retried_then_succeeds(self):
        with MockTradier() as server:
            client = server.client()
            server.status_queue = [429]
            with patch("odte.providers.tradier.time.sleep"):
                clock = client.clock()
            self.assertEqual(clock["state"], "open")
            self.assertEqual(server.seen.count("/v1/markets/clock"), 2)

    def test_server_errors_give_up_with_a_clear_message(self):
        with MockTradier() as server:
            client = server.client()
            server.status_queue = [500, 500, 500]
            with patch("odte.providers.tradier.time.sleep"):
                with self.assertRaises(TradierError) as ctx:
                    client.clock()
            self.assertIn("after 3 tries", str(ctx.exception))

    def test_unknown_path_is_not_retried(self):
        with MockTradier() as server:
            client = server.client()
            with self.assertRaises(TradierError):
                client.get("/markets/nonsense")
            self.assertEqual(server.seen.count("/v1/markets/nonsense"), 1)


class TestProviderOverHttp(unittest.TestCase):
    def test_snapshot_pulls_everything_through_real_http(self):
        with MockTradier() as server:
            snap = TradierProvider(client=server.client()).snapshot("SPY", NOW)
        self.assertEqual(snap.symbol, "SPY")
        self.assertAlmostEqual(snap.spot, 589.88)
        self.assertAlmostEqual(snap.prev_close, 584.6)
        self.assertEqual(len(snap.bars), 3)
        self.assertEqual(snap.expiry, TODAY)
        self.assertEqual(len(snap.chain), 3)
        self.assertEqual(snap.chain[0].occ, "SPY260925C00590000")
        self.assertAlmostEqual(snap.chain[0].delta, 0.52)
        self.assertIsNotNone(snap.adr_pct)
        self.assertNotIn("no-0dte", snap.notes)

    def test_a_scan_scores_data_that_arrived_over_http(self):
        from odte.option_metrics import compute_option_metrics
        from odte.price_metrics import compute_price_metrics
        from odte.scoring import SymbolData, score_universe
        from odte.config import ScanConfig
        from odte.session import t_years_to_close

        with MockTradier() as server:
            snap = TradierProvider(client=server.client()).snapshot("SPY", NOW)
        price = compute_price_metrics(snap, 30)
        options = compute_option_metrics(snap.chain, snap.spot, t_years_to_close(NOW))
        scored = score_universe(
            [SymbolData("SPY", price, options, 330.0, snap.expiry)], ScanConfig()
        )[0]
        self.assertIn(scored.rating.side, ("BULL", "BEAR"))
        self.assertTrue(0 <= scored.rating.score <= 10)


class TestStreamOverHttp(unittest.TestCase):
    def test_streamed_prints_are_classified_from_a_real_response(self):
        with MockTradier() as server:
            stream = TradierTapeStream(server.client())
            tape = stream.run_for(
                ["SPY260925C00590000", "SPY260925P00590000"], seconds=5
            )
        self.assertIsNone(stream.error)
        self.assertEqual(tape.trades, 3)          # the `trade` event is ignored
        self.assertEqual(stream.events, 3)
        call = tape.contracts["SPY260925C00590000"]
        self.assertEqual((call.buy, call.sell), (25, 40))
        self.assertEqual(tape.contracts["SPY260925P00590000"].buy, 15)
        self.assertIsNotNone(tape.window_seconds)

    def test_a_refused_session_is_reported_not_raised(self):
        with MockTradier() as server:
            server.routes["/v1/markets/events/session"] = {"stream": {}}
            stream = TradierTapeStream(server.client())
            stream.run_for(["SPY260925C00590000"], seconds=2)
        self.assertIsInstance(stream.error, TradierError)
        self.assertIn("session id", str(stream.error))


class TestClassificationEndToEnd(unittest.TestCase):
    def test_sides_from_the_stream_reach_the_metrics(self):
        from odte.option_metrics import compute_option_metrics

        with MockTradier() as server:
            client = server.client()
            snap = TradierProvider(client=client).snapshot("SPY", NOW)
            stream = TradierTapeStream(client)
            tape = stream.run_for([q.occ for q in snap.chain], seconds=5)
        merged = tape.merge_into(snap.chain)
        self.assertEqual(merged, 2)
        m = compute_option_metrics(snap.chain, snap.spot, 0.0006, em_window=10.0)

        # 25 calls bought, 40 sold, 15 puts bought: net delta comes out negative.
        self.assertIsNotNone(m.signed_flow_tilt)
        self.assertLess(m.net_delta_contracts, 0)
        self.assertEqual(BUY if m.net_delta_contracts > 0 else SELL, SELL)
        # ...but 80 classified contracts out of 27,000 traded is far too thin a
        # sample to outvote the proxy, so the scan keeps the proxy and says so.
        self.assertLess(m.side_coverage, MIN_SIDE_COVERAGE)
        self.assertEqual(m.flow_source, "proxy")

    def test_enough_classified_volume_takes_over_from_the_proxy(self):
        from odte.option_metrics import compute_option_metrics

        with MockTradier() as server:
            client = server.client()
            snap = TradierProvider(client=client).snapshot("SPY", NOW)
            for q in snap.chain:          # a quiet chain, fully sampled
                q.volume = 60
            tape = TradierTapeStream(client).run_for([q.occ for q in snap.chain], 5)
        tape.merge_into(snap.chain)
        m = compute_option_metrics(snap.chain, snap.spot, 0.0006, em_window=10.0)
        self.assertGreaterEqual(m.side_coverage, MIN_SIDE_COVERAGE)
        self.assertEqual(m.flow_source, "side")
        self.assertEqual(m.flow_tilt, m.signed_flow_tilt)


if __name__ == "__main__":
    unittest.main()
