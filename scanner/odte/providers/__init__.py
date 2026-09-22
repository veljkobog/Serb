from __future__ import annotations

from typing import Any

from .base import Provider
from .snapshot import SnapshotProvider
from .synthetic import SyntheticProvider

_ALLOWED = {
    "tradier": ("token", "env", "bar_interval", "client"),
    "yahoo": ("bar_interval",),
    "synthetic": ("seed", "with_sides"),
    "snapshot": ("path",),
}


def get_provider(name: str, **kwargs: Any) -> Provider:
    name = (name or "tradier").lower()
    if name in ("yahoo", "yfinance", "yf"):
        from .yahoo import YahooProvider  # imported lazily: needs yfinance

        return YahooProvider(**_only(kwargs, _ALLOWED["yahoo"]))
    if name == "tradier":
        from .tradier import TradierProvider

        return TradierProvider(**_only(kwargs, _ALLOWED["tradier"]))
    if name == "synthetic":
        return SyntheticProvider(**_only(kwargs, _ALLOWED["synthetic"]))
    if name == "snapshot":
        return SnapshotProvider(**_only(kwargs, _ALLOWED["snapshot"]))
    raise ValueError(f"unknown provider: {name}")


def _only(kwargs: dict, keys: tuple) -> dict:
    return {k: v for k, v in kwargs.items() if k in keys and v is not None}


__all__ = ["Provider", "SyntheticProvider", "SnapshotProvider", "get_provider"]
