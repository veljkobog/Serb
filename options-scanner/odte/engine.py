"""Scan orchestration: fan out over the universe, build blocks, gate, score, rank."""
from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import plan as plan_mod
from . import screen
from . import session as session_mod
from .calendar_utils import now_et, session_progress, trading_days_between
from .config import Config
from .http import Http
from .providers import FinraOffExchange, MarketDataProvider, resolve
from .providers.base import OptionChain
from .score import Candidate, compose
from .signals import darkpool, intraday, options_flow, shortinterest, trend, volume


# Rejection strings carry the offending numbers, so counting them raw gives 141
# unique "reasons". These patterns collapse them into families a human can act on.
REASON_FAMILIES: List[Tuple[str, str]] = [
    ("no near-dated option chain", "no expiry in range"),
    ("no market cap available", "no market cap data"),
    ("market cap", "market cap too small"),
    ("/day <", "dollar volume too low"),
    ("shares/day", "share volume too low"),
    ("price $", "price outside band"),
    ("contracts <", "option volume too low"),
    ("OI <", "open interest too low"),
    ("no two-sided ATM quote", "no two-sided quote"),
    ("ATM spread", "spread too wide"),
    ("liquid strikes", "too few liquid strikes"),
    ("insufficient price history", "not enough price history"),
    ("ETF excluded", "ETF excluded"),
    ("no off-exchange data", "no dark pool data"),
    ("earnings on", "earnings"),
    ("score ", "below score floor"),
]


def reason_family(text: str) -> str:
    for needle, label in REASON_FAMILIES:
        if needle in text:
            return label
    return text[:40]


@dataclass
class ScanResult:
    generated_at: dt.datetime
    provider: str
    universe: List[str]
    candidates: List[Candidate] = field(default_factory=list)
    rejected: List[Candidate] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    chain_fetches: int = 0
    darkpool_days: int = 0
    darkpool_asof: Optional[str] = None
    config: Optional[Config] = None
    brief: Optional[session_mod.SessionBrief] = None

    def reason_rollup(self) -> List[Tuple[str, int]]:
        """Why names were dropped, collapsed into families, most common first.

        This is what turns an empty table into an explanation. On a Monday the answer
        is overwhelmingly "no expiry in range", which is the calendar rather than a
        fault, and the generic "lower your score floor" advice would be actively wrong.
        """
        counts: Dict[str, int] = {}
        for cand in self.rejected:
            seen = set()
            for reason in (cand.gate_failures or ([cand.error] if cand.error else [])):
                family = reason_family(str(reason))
                if family in seen:
                    continue
                seen.add(family)
                counts[family] = counts.get(family, 0) + 1
        return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    def expiry_coverage(self) -> Dict[str, int]:
        """How many evaluated names actually had a contract in the DTE window."""
        with_expiry = sum(1 for c in self.candidates + self.rejected if c.expiry)
        return {"with_expiry": with_expiry,
                "without_expiry": len(self.candidates) + len(self.rejected) - with_expiry}

    def as_dict(self) -> Dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "provider": self.provider,
            "universe_size": len(self.universe),
            "chain_fetches": self.chain_fetches,
            "darkpool_days": self.darkpool_days,
            "darkpool_asof": self.darkpool_asof,
            "candidates": [c.as_dict() for c in self.candidates],
            "rejected": [{"symbol": c.symbol, "reasons": c.gate_failures,
                          "stage": c.stage, "error": c.error}
                         for c in self.rejected],
            "errors": self.errors,
            "reason_rollup": self.reason_rollup(),
            "expiry_coverage": self.expiry_coverage(),
            "brief": self.brief.as_dict() if self.brief else None,
            "gates": self.config.gates.__dict__ if self.config else {},
        }


def pick_expiry(expirations: List[dt.date], today: dt.date, max_dte: int) -> Optional[dt.date]:
    """The nearest expiry within ``max_dte`` *trading* sessions.

    Trading sessions, not calendar days: on a Friday the next session is Monday, so a
    Monday expiry is 1DTE from Friday even though it is three calendar days out.
    """
    best: Optional[dt.date] = None
    for exp in sorted(expirations):
        if exp < today:
            continue
        dte = 0 if exp == today else trading_days_between(today, exp)
        if dte <= max_dte:
            best = exp
            break
    return best


class Scanner:
    def __init__(self, config: Config, provider: Optional[MarketDataProvider] = None,
                 offex: Optional[FinraOffExchange] = None,
                 progress_cb: Optional[Callable[[str, str], None]] = None):
        self.config = config
        self.http = Http(cache_dir=config.cache_dir, cache_ttl=300,
                         force_fresh=config.force_fresh)
        self.provider = provider or resolve(config.provider, http=self.http)
        self.offex = offex
        self.progress_cb = progress_cb or (lambda sym, msg: None)

    # -- data ---------------------------------------------------------------
    def load_darkpool(self) -> FinraOffExchange:
        if self.offex is None:
            self.offex = FinraOffExchange(http=Http(cache_dir=self.config.cache_dir,
                                                    min_interval=0.1, timeout=45),
                                          days=self.config.darkpool_days).load()
        return self.offex

    # -- per symbol ---------------------------------------------------------
    def evaluate(self, symbol: str, today: dt.date, progress: float) -> Candidate:
        cand = Candidate(symbol=symbol)
        try:
            bars = self.provider.daily_bars(symbol, self.config.lookback_days)
            if len(bars) < 60:
                cand.error = "insufficient price history"
                return cand

            quote = self.provider.quote(symbol)
            cand.spot = quote.last if quote and quote.last else bars[-1].close
            cand.change_pct = quote.change_pct if quote else None

            fundamentals = self.provider.fundamentals(symbol)
            cand.name = fundamentals.name
            cand.market_cap = fundamentals.market_cap

            cand.blocks["trend"] = trend.analyse(bars, quote)
            cand.blocks["volume"] = volume.analyse(bars, quote, progress)
            cand.blocks["short_interest"] = shortinterest.analyse(fundamentals, bars)

            offex = self.offex.history(symbol) if (self.offex and self.offex.available) else []
            cand.blocks["darkpool"] = darkpool.analyse(offex, bars)

            # Cheap gates first. A name that fails on market cap or dollar volume never
            # costs an expiry lookup or a chain fetch — two round trips it would have
            # paid for nothing.
            cand.gate_failures = screen.pre_gate(cand, fundamentals, self.config, today)
            if cand.gate_failures:
                cand.stage = "pre-gate"
                compose(cand, self.config)
                return cand

            # Intraday costs one more call, so it is fetched only for names that have
            # already earned it by clearing the cheap gates.
            atr = cand.blocks["trend"].detail.get("atr14")
            if self.config.use_intraday:
                try:
                    minutes = self.provider.intraday_bars(symbol, self.config.intraday_interval)
                except Exception:
                    minutes = []      # optional data: never fail a scan over it
                cand.blocks["intraday"] = intraday.analyse(minutes, cand.spot, atr)
            else:
                cand.blocks["intraday"] = intraday.analyse([], cand.spot, atr)
                cand.blocks["intraday"].notes = ["intraday disabled in config"]

            chain: Optional[OptionChain] = None
            expiry = pick_expiry(self.provider.expirations(symbol), today, self.config.gates.max_dte)
            if expiry:
                cand.expiry = expiry
                cand.dte = 0 if expiry == today else trading_days_between(today, expiry)
                chain = self.provider.chain(symbol, expiry)
                cand.fetched_chain = True
            atr_pct = cand.blocks["trend"].detail.get("atr_pct")
            cand.blocks["options"] = options_flow.analyse(
                chain, cand.spot, atr_pct, cand.dte,
                band_pct=self.config.band_pct,
                min_unusual_volume=self.config.min_unusual_volume)

            cand.gate_failures = screen.option_gate(cand, chain, self.config, progress)
            cand.stage = "option-gate" if cand.gate_failures else "scored"
            compose(cand, self.config)
            if cand.passed:
                cand.plan = plan_mod.build(cand, chain, self.config)
        except Exception as exc:  # a single bad symbol must never kill the scan
            cand.error = f"{type(exc).__name__}: {exc}"
        return cand

    # -- run ----------------------------------------------------------------
    def run(self, symbols: List[str], today: Optional[dt.date] = None) -> ScanResult:
        now = now_et()
        today = today or now.date()
        progress = session_progress(now)

        self.load_darkpool()
        result = ScanResult(generated_at=now, provider=self.provider.name, universe=list(symbols),
                            config=self.config,
                            brief=session_mod.describe(today, self.config.gates.max_dte))
        if self.offex:
            result.darkpool_days = len(self.offex.loaded_dates)
            result.darkpool_asof = str(self.offex.loaded_dates[-1]) if self.offex.loaded_dates else None
            result.errors.extend(self.offex.errors[:5])

        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as pool:
            futures = {pool.submit(self.evaluate, s, today, progress): s for s in symbols}
            done = 0
            for fut in as_completed(futures):
                symbol = futures[fut]
                done += 1
                try:
                    cand = fut.result()
                except Exception as exc:
                    result.errors.append(f"{symbol}: {exc}")
                    continue
                self.progress_cb(symbol, f"{done}/{len(symbols)}")
                if cand.fetched_chain:
                    result.chain_fetches += 1
                if cand.error:
                    result.errors.append(f"{symbol}: {cand.error}")
                    result.rejected.append(cand)
                elif cand.passed and cand.score >= self.config.gates.min_score:
                    result.candidates.append(cand)
                else:
                    if not cand.gate_failures:
                        cand.gate_failures.append(f"score {cand.score:.0f} < {self.config.gates.min_score:.0f}")
                    result.rejected.append(cand)

        result.candidates.sort(key=lambda c: c.score, reverse=True)
        result.rejected.sort(key=lambda c: c.symbol)
        return result
