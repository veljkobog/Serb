"""Candidate universes.

The scanner never assumes which names carry same-day expiries — it asks the option
chain and keeps whatever actually lists a 0/1 DTE contract. These lists only decide
*what to ask about*, so they are seeds, not rules.

DAILY_EXPIRY seeds are index/ETF products that have historically listed expirations
every trading day, which is what makes true 0DTE possible on a Tuesday. Single names
mostly expire Friday, so for equities "0DTE" means Friday and "1DTE" means Thursday
— the scanner handles that automatically by reading the chain.
"""
from __future__ import annotations

import os
from typing import List, Optional

DAILY_EXPIRY = [
    "SPY", "QQQ", "IWM", "DIA", "GLD", "SLV", "TLT", "HYG", "EEM", "FXI",
    "USO", "UNG", "XLF", "XLE", "SMH", "ARKK",
]

MEGA_CAP = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B", "LLY",
    "JPM", "V", "MA", "XOM", "UNH", "COST", "HD", "PG", "JNJ", "ORCL", "NFLX", "CRM",
    "AMD", "ADBE", "BAC", "WFC", "CVX", "KO", "PEP", "MRK", "ABBV", "TMO", "CSCO",
    "ACN", "MCD", "LIN", "INTU", "QCOM", "TXN", "DIS", "GE", "CAT", "IBM", "NOW",
    "AMAT", "BKNG", "GS", "SPGI", "UBER", "BLK", "PFE", "RTX", "HON", "AXP", "LOW",
    "PLTR", "MU", "LRCX", "ADI", "PANW", "KLAC", "SNPS", "CDNS", "MRVL", "ANET",
]

HIGH_BETA = [
    "COIN", "MSTR", "SMCI", "SOUN", "AFRM", "RIVN", "LCID", "SOFI", "HOOD", "DKNG",
    "RBLX", "SNAP", "PINS", "SHOP", "SQ", "PYPL", "ROKU", "DASH", "ABNB", "TTD",
    "CRWD", "NET", "DDOG", "SNOW", "ZS", "OKTA", "TWLO", "U", "IONQ", "RGTI",
    "CVNA", "CELH", "ENPH", "FSLR", "PLUG", "RUN", "CHPT", "NIO", "XPEV", "LI",
    "BABA", "PDD", "JD", "TSM", "ASML", "ARM", "DELL", "WDC", "STX", "ON",
    "GME", "AMC", "BBAI", "LUNR", "ACHR", "JOBY", "OKLO", "SMR", "VST", "TLN",
]

LEVERAGED = ["TQQQ", "SQQQ", "SOXL", "SOXS", "TNA", "TZA", "UVXY", "SPXL", "LABU", "NUGT"]

PRESETS = {
    "core": DAILY_EXPIRY + MEGA_CAP,
    "daily": DAILY_EXPIRY,
    "megacap": MEGA_CAP,
    "movers": HIGH_BETA,
    "leveraged": LEVERAGED,
    "wide": DAILY_EXPIRY + MEGA_CAP + HIGH_BETA,
    "everything": DAILY_EXPIRY + MEGA_CAP + HIGH_BETA + LEVERAGED,
}

DEFAULT = "wide"


def _dedupe(symbols) -> List[str]:
    seen, out = set(), []
    for s in symbols:
        s = (s or "").strip().upper()
        if s and not s.startswith("#") and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load(spec: Optional[str] = None, explicit: Optional[List[str]] = None,
         path: Optional[str] = None) -> List[str]:
    """Resolve a universe from an explicit list, a file, a preset name, or symbols."""
    if explicit:
        return _dedupe(explicit)
    if path:
        if not os.path.exists(path):
            raise FileNotFoundError(f"universe file not found: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            return _dedupe(line.split("#")[0] for line in fh)
    spec = (spec or DEFAULT).strip()
    if spec.lower() in PRESETS:
        return _dedupe(PRESETS[spec.lower()])
    if "," in spec:
        return _dedupe(spec.split(","))
    if spec.lower() in ("all", "*"):
        return _dedupe(PRESETS["everything"])
    return _dedupe([spec])
