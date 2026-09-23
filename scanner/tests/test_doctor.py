import unittest
from datetime import datetime

from odte.doctor import FAIL, OK, SKIP, WARN, check_environment, check_tradier
from odte.providers.tradier import TradierError
from odte.session import to_et

try:
    from test_tradier import FakeClient
except ImportError:  # pragma: no cover
    from tests.test_tradier import FakeClient

IN_WINDOW = to_et(datetime.fromisoformat("2026-09-22T10:30:00"))
AFTER_HOURS = to_et(datetime.fromisoformat("2026-09-22T19:00:00"))


def by_name(checks):
    return {c.name: c for c in checks}


class BrokenClient(FakeClient):
    env = "production"

    def clock(self):
        raise TradierError("Tradier rejected the token (401).")


class TestEnvironment(unittest.TestCase):
    def test_python_and_window(self):
        checks = by_name(check_environment(IN_WINDOW))
        self.assertEqual(checks["python"].status, OK)
        self.assertEqual(checks["clock"].status, OK)

    def test_outside_the_window_warns_with_a_fix(self):
        clock = by_name(check_environment(AFTER_HOURS))["clock"]
        self.assertEqual(clock.status, WARN)
        self.assertIn("--force", clock.fix)


class TestTradierChecks(unittest.TestCase):
    def test_healthy_account_passes_every_step(self):
        checks = by_name(check_tradier(FakeClient(), IN_WINDOW))
        for name in ("token", "auth", "quotes", "0DTE expiry", "chain", "stream session"):
            self.assertIn(name, checks)
            self.assertNotEqual(checks[name].status, FAIL, name)
        self.assertEqual(checks["chain"].status, OK)
        self.assertIn("with greeks", checks["chain"].detail)
        self.assertEqual(checks["live prints"].status, SKIP)

    def test_bad_token_fails_fast_and_stops(self):
        checks = check_tradier(BrokenClient(), IN_WINDOW)
        named = by_name(checks)
        self.assertEqual(named["auth"].status, FAIL)
        self.assertIn("TRADIER_TOKEN", named["auth"].fix)
        # Nothing downstream is attempted once auth is gone.
        self.assertNotIn("quotes", named)

    def test_sandbox_is_flagged_but_not_blocking(self):
        client = FakeClient()
        client.env = "sandbox"
        token = by_name(check_tradier(client, IN_WINDOW))["token"]
        self.assertEqual(token.status, WARN)
        self.assertIn("production", token.fix)

    def test_no_same_day_expiry_warns_and_skips_the_chain(self):
        checks = by_name(check_tradier(FakeClient(expirations=["2026-09-25"]), IN_WINDOW))
        self.assertEqual(checks["0DTE expiry"].status, WARN)
        self.assertNotIn("chain", checks)
        self.assertEqual(checks["live prints"].status, SKIP)

    def test_missing_stream_entitlement_is_blocking_with_a_workaround(self):
        class NoStream(FakeClient):
            def stream_session(self):
                return {}

        check = by_name(check_tradier(NoStream(), IN_WINDOW))["stream session"]
        self.assertEqual(check.status, FAIL)
        self.assertIn("proxy-flow", check.fix)

    def test_closed_market_skips_the_tape_sample(self):
        class Closed(FakeClient):
            def clock(self):
                return {"state": "closed", "description": "Market is closed"}

        checks = by_name(check_tradier(Closed(), AFTER_HOURS, stream_seconds=5))
        self.assertEqual(checks["live prints"].status, SKIP)
        self.assertIn("closed", checks["live prints"].detail)

    def test_no_quote_for_the_symbol_fails(self):
        class NoQuotes(FakeClient):
            def quotes(self, symbols, greeks=False):
                return {}

        check = by_name(check_tradier(NoQuotes(), IN_WINDOW))["quotes"]
        self.assertEqual(check.status, FAIL)
        self.assertIn("market data", check.fix.lower())


if __name__ == "__main__":
    unittest.main()
