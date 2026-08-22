"""What the expiry landscape actually looks like today.

The scanner's whole premise is a contract expiring today or tomorrow, and whether such
a contract *exists* depends entirely on the weekday:

  * Index and ETF products (SPY, QQQ, IWM, SPX and a growing handful of others) list
    expirations every trading day. Those are 0DTE any day of the week.
  * Single-name equities list weekly expirations on Fridays. So an equity is 0DTE only
    on a Friday, and 1DTE only on a Thursday. On a Monday its nearest contract is four
    sessions out.

Without this, a Monday scan returns an almost empty table and the honest reason -- "it
is Monday, equities do not expire today" -- is indistinguishable from "no setups" or
"the feed is broken". This module makes the scanner say which one it is.

Nothing here is used to *filter*: expiries are always discovered from each symbol's real
chain. This is the briefing, not the rule.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .calendar_utils import is_trading_day, next_trading_day, now_et, trading_days_between

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def equity_expiry_on_or_after(day: dt.date) -> dt.date:
    """The standard weekly equity expiry at or after ``day``: Friday, or the last
    session before it when Friday is a market holiday (Good Friday, for instance)."""
    friday = day + dt.timedelta(days=(4 - day.weekday()) % 7)
    while not is_trading_day(friday):
        friday -= dt.timedelta(days=1)
        if friday < day:                      # the whole week is gone; take the next one
            return equity_expiry_on_or_after(day + dt.timedelta(days=7 - day.weekday()))
    return friday


def is_monthly_opex(day: dt.date) -> bool:
    """True on the third Friday — where the cycle's open interest is concentrated."""
    if day.weekday() != 4:
        return False
    fridays = [d for d in (dt.date(day.year, day.month, n)
                           for n in range(1, 29))
               if d.weekday() == 4]
    return len(fridays) >= 3 and day == fridays[2]


@dataclass
class SessionBrief:
    date: dt.date
    weekday: str
    trading_day: bool
    equity_expiry: dt.date
    sessions_to_equity_expiry: int
    equities_expire_today: bool
    monthly_opex: bool
    headline: str = ""
    advice: str = ""
    suggested_max_dte: int = 1
    suggested_universe: Optional[str] = None
    counts: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "date": str(self.date), "weekday": self.weekday, "trading_day": self.trading_day,
            "equity_expiry": str(self.equity_expiry),
            "sessions_to_equity_expiry": self.sessions_to_equity_expiry,
            "equities_expire_today": self.equities_expire_today,
            "monthly_opex": self.monthly_opex, "headline": self.headline,
            "advice": self.advice, "suggested_max_dte": self.suggested_max_dte,
            "suggested_universe": self.suggested_universe, "counts": self.counts,
        }


def describe(day: Optional[dt.date] = None, max_dte: int = 1) -> SessionBrief:
    day = day or now_et().date()
    trading = is_trading_day(day)
    reference = day if trading else next_trading_day(day)
    expiry = equity_expiry_on_or_after(reference)
    sessions = 0 if expiry == reference else trading_days_between(reference, expiry)
    expires_today = trading and sessions == 0
    opex = is_monthly_opex(expiry)

    brief = SessionBrief(
        date=day, weekday=WEEKDAYS[day.weekday()], trading_day=trading,
        equity_expiry=expiry, sessions_to_equity_expiry=sessions,
        equities_expire_today=expires_today, monthly_opex=opex,
    )

    if not trading:
        brief.headline = (f"{brief.weekday} — market closed. Next session "
                          f"{reference:%a %d %b}.")
        brief.advice = "Scan results will be stale. Use --demo to exercise the tool."
        brief.suggested_max_dte = max_dte
        return brief

    if expires_today:
        brief.headline = (f"{brief.weekday} {day:%d %b} — every optionable equity has a "
                          f"0DTE contract today.")
        if opex:
            brief.headline += " Monthly OPEX."
            brief.advice = ("Open interest peaks today, so the call/put walls are at their "
                            "most meaningful of the cycle and pin risk is real. Respect "
                            "wall headroom, and don't open new longs after 14:00.")
        else:
            brief.advice = "The widest universe of the week. Same-day expiries across the board."
        brief.suggested_max_dte = 0
        return brief

    if sessions == 1:
        brief.headline = (f"{brief.weekday} {day:%d %b} — equities expire tomorrow "
                          f"({expiry:%a %d %b}), so the whole universe is 1DTE.")
        brief.advice = ("Overnight gap risk is the entire trade. Size for a gap through "
                        "your stop, not to it.")
        brief.suggested_max_dte = 1
        return brief

    # Monday through Wednesday: only index/ETF products have anything near-dated.
    brief.headline = (f"{brief.weekday} {day:%d %b} — equities don't expire until "
                      f"{expiry:%a %d %b}, {sessions} sessions out. True 0DTE today is "
                      f"index and ETF products only.")
    brief.advice = (f"Expect most single names to be dropped for 'no near-dated option "
                    f"chain' — that is the calendar, not a fault. Either scan the daily-"
                    f"expiry products (universe 'daily'), or set expiry to {sessions}DTE "
                    f"to trade this week's Friday contracts with the same signals — that "
                    f"is a swing trade, not a 0DTE trade.")
    brief.suggested_max_dte = sessions
    brief.suggested_universe = "daily"
    return brief


def dte_label(dte: int) -> str:
    return {0: "0DTE", 1: "1DTE"}.get(dte, f"{dte}DTE")


def horizon_notes(dte: int) -> List[str]:
    """Time-decay guidance that matches the actual holding period."""
    if dte <= 0:
        return ["0DTE: theta accelerates hard after ~14:00 ET — take the trade in the "
                "first half of the session or not at all."]
    if dte == 1:
        return ["1DTE: overnight gap risk is the whole trade; size for a gap through "
                "your stop, not to it."]
    return [f"{dte}DTE: this is a multi-session swing, not a same-day trade. Theta is "
            f"slower, but so is the payoff — and the intraday signals (VWAP, opening "
            f"range) only describe today, not the days you plan to hold through."]
