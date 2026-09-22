from __future__ import annotations

from typing import Any

from .base import Provider
from .snapshot import SnapshotProvider
from .synthetic import SyntheticProvider


def get_provider(name: str, **kwargs: Any) -> Provider:
    name = (name or "yahoo").lower()
    if name in ("yahoo", "yfinance", "yf"):
        from .yahoo import YahooProvider  # imported lazily: needs yfinance

        return YahooProvider(**kwargs)
    if name == "synthetic":
        return SyntheticProvider(**{k: v for k, v in kwargs.items() if k in ("seed",)})
    if name == "snapshot":
        return SnapshotProvider(**{k: v for k, v in kwargs.items() if k in ("path",)})
    raise ValueError(f"unknown provider: {name}")


__all__ = ["Provider", "SyntheticProvider", "SnapshotProvider", "get_provider"]
