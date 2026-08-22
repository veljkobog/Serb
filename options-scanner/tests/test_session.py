"""Tests for the expiry-calendar briefing and the rejection-reason rollup."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from odte import report, session
from odte.calendar_utils import is_trading_day
from odte.config import Config
from odte.engine import Scanner, reason_family
from odte.synthetic import FakeProvider, make_offex

FRIDAY = dt.date(2026, 8, 21)        # third Friday: monthly opex
MONDAY = dt.date(2026, 8, 24)
THURSDAY = dt.date(2026, 8, 27)
NEXT_FRIDAY = dt.date(2026, 8, 28)


class TestExpiryCalendar(unittest.TestCase):
    def test_friday_is_its_own_expiry(self):
        self.assertEqual(session.equity_expiry_on_or_after(FRIDAY), FRIDAY)

    def test_monday_looks_forward_to_that_week_s_friday(self):
        self.assertEqual(session.equity_expiry_on_or_after(MONDAY), NEXT_FRIDAY)

    def test_weekend_rolls_into_the_coming_week(self):
        saturday = dt.date(2026, 8, 22)
        self.assertEqual(session.equity_expiry_on_or_after(saturday), NEXT_FRIDAY)

    def test_a_holiday_friday_falls_back_to_the_prior_session(self):
        """Good Friday 2026-04-03 is a market holiday, so that week expires Thursday."""
        self.assertFalse(is_trading_day(dt.date(2026, 4, 3)))
        expiry = session.equity_expiry_on_or_after(dt.date(2026, 3, 30))
        self.assertEqual(expiry, dt.date(2026, 4, 2))
        self.assertTrue(is_trading_day(expiry))

    def test_third_friday_is_monthly_opex(self):
        self.assertTrue(session.is_monthly_opex(FRIDAY))
        self.assertFalse(session.is_monthly_opex(dt.date(2026, 8, 14)))
        self.assertFalse(session.is_monthly_opex(dt.date(2026, 8, 28)))

    def test_a_non_friday_is_never_opex(self):
        self.assertFalse(session.is_monthly_opex(MONDAY))


class TestBrief(unittest.TestCase):
    def test_friday_reports_a_full_zero_dte_universe(self):
        brief = session.describe(FRIDAY)
        self.assertTrue(brief.equities_expire_today)
        self.assertEqual(brief.sessions_to_equity_expiry, 0)
        self.assertEqual(brief.suggested_max_dte, 0)
        self.assertIn("0DTE contract today", brief.headline)

    def test_monthly_opex_is_called_out(self):
        brief = session.describe(FRIDAY)
        self.assertTrue(brief.monthly_opex)
        self.assertIn("OPEX", brief.headline)
        self.assertIn("pin risk", brief.advice)

    def test_thursday_is_a_one_dte_day(self):
        brief = session.describe(THURSDAY)
        self.assertEqual(brief.sessions_to_equity_expiry, 1)
        self.assertEqual(brief.suggested_max_dte, 1)
        self.assertIn("1DTE", brief.headline)

    def test_monday_says_equities_have_nothing_near_dated(self):
        brief = session.describe(MONDAY)
        self.assertFalse(brief.equities_expire_today)
        self.assertEqual(brief.sessions_to_equity_expiry, 4)
        self.assertEqual(brief.suggested_max_dte, 4)
        self.assertEqual(brief.suggested_universe, "daily")
        self.assertIn("index and ETF products only", brief.headline)
        self.assertIn("not a fault", brief.advice)

    def test_a_closed_day_says_so(self):
        brief = session.describe(dt.date(2026, 8, 22))
        self.assertFalse(brief.trading_day)
        self.assertIn("market closed", brief.headline)

    def test_brief_serialises(self):
        payload = session.describe(MONDAY).as_dict()
        self.assertEqual(payload["weekday"], "Monday")
        self.assertEqual(payload["suggested_max_dte"], 4)


class TestHorizonNotes(unittest.TestCase):
    def test_each_horizon_gets_its_own_warning(self):
        self.assertIn("14:00", session.horizon_notes(0)[0])
        self.assertIn("gap", session.horizon_notes(1)[0])
        self.assertIn("swing", session.horizon_notes(4)[0])

    def test_multi_day_note_warns_that_intraday_signals_do_not_carry(self):
        self.assertIn("only describe today", session.horizon_notes(3)[0])

    def test_labels(self):
        self.assertEqual(session.dte_label(0), "0DTE")
        self.assertEqual(session.dte_label(4), "4DTE")


class TestReasonRollup(unittest.TestCase):
    def test_families_collapse_the_numbers_out(self):
        self.assertEqual(reason_family("market cap $412.0M < $2.0B"), "market cap too small")
        self.assertEqual(reason_family("$41M/day < $100M"), "dollar volume too low")
        self.assertEqual(reason_family("no near-dated option chain"), "no expiry in range")
        self.assertEqual(reason_family("ATM spread 22.0% > 12%"), "spread too wide")
        self.assertEqual(reason_family("score 31 < 45"), "below score floor")
        self.assertEqual(reason_family("1,204 contracts < 3,000 (session-adjusted)"),
                         "option volume too low")

    def test_unknown_reasons_survive_verbatim(self):
        self.assertEqual(reason_family("something new"), "something new")

    def test_scan_title_matches_the_horizon(self):
        self.assertEqual(report.scan_title(0), "0DTE scan")
        self.assertEqual(report.scan_title(1), "0/1DTE scan")
        self.assertIn("swing", report.scan_title(4))


class _EquitiesOnly(FakeProvider):
    """Single names: nothing expires until Friday, like the real world Mon-Wed."""

    def expirations(self, symbol):
        return [NEXT_FRIDAY, NEXT_FRIDAY + dt.timedelta(days=7)]


class TestMondayScan(unittest.TestCase):
    SYMBOLS = ["AAPL", "NVDA", "TSLA", "AMD"]

    def _scan(self, max_dte):
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        cfg.gates.max_dte = max_dte
        cfg.gates.min_score = 15.0
        provider = _EquitiesOnly(self.SYMBOLS, MONDAY, drift=0.003, vol=0.01, mixed=True)
        bars = {s: provider.daily_bars(s) for s in self.SYMBOLS}
        return Scanner(cfg, provider=provider,
                       offex=make_offex(self.SYMBOLS, bars)).run(self.SYMBOLS, today=MONDAY)

    def test_monday_at_one_dte_drops_everything_for_the_right_reason(self):
        result = self._scan(max_dte=1)
        self.assertEqual(result.candidates, [])
        rollup = dict(result.reason_rollup())
        self.assertEqual(rollup.get("no expiry in range"), len(self.SYMBOLS))
        self.assertEqual(result.expiry_coverage()["with_expiry"], 0)

    def test_the_result_carries_the_briefing(self):
        result = self._scan(max_dte=1)
        self.assertIsNotNone(result.brief)
        self.assertFalse(result.brief.equities_expire_today)
        self.assertEqual(result.brief.suggested_max_dte, 4)

    def test_the_empty_report_explains_the_calendar_not_the_score_floor(self):
        text = report.render_terminal(self._scan(max_dte=1), colour=False)
        self.assertIn("no expiry in range", text)
        self.assertIn("index and ETF products only", text)
        self.assertNotIn("Loosen --min-score", text)

    def test_widening_to_four_dte_makes_monday_tradable_again(self):
        result = self._scan(max_dte=4)
        self.assertTrue(result.candidates)
        for cand in result.candidates:
            self.assertEqual(cand.expiry, NEXT_FRIDAY)
            self.assertEqual(cand.dte, 4)

    def test_a_multi_session_horizon_is_labelled_as_a_swing_not_a_zero_dte(self):
        result = self._scan(max_dte=4)
        notes = " ".join(result.candidates[0].plan.get("notes", []))
        self.assertIn("swing", notes)
        self.assertNotIn("14:00", notes)

    def test_intraday_vwap_is_not_offered_as_a_stop_for_a_multi_day_hold(self):
        result = self._scan(max_dte=4)
        notes = " ".join(result.candidates[0].plan.get("notes", []))
        self.assertNotIn("only valid while price holds", notes)

    def test_json_payload_exposes_the_brief_and_rollup(self):
        payload = self._scan(max_dte=1).as_dict()
        self.assertIn("reason_rollup", payload)
        self.assertIn("brief", payload)
        self.assertEqual(payload["brief"]["weekday"], "Monday")


class TestFridayScan(unittest.TestCase):
    def test_friday_reaches_the_whole_universe_at_zero_dte(self):
        symbols = ["AAPL", "NVDA"]
        cfg = Config()
        cfg.cache_dir, cfg.workers = "", 2
        cfg.gates.max_dte, cfg.gates.min_score = 0, 15.0
        provider = FakeProvider(symbols, FRIDAY, drift=0.004, vol=0.009)
        bars = {s: provider.daily_bars(s) for s in symbols}
        result = Scanner(cfg, provider=provider,
                         offex=make_offex(symbols, bars)).run(symbols, today=FRIDAY)
        self.assertTrue(result.brief.equities_expire_today)
        self.assertTrue(result.brief.monthly_opex)
        for cand in result.candidates:
            self.assertEqual(cand.dte, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
