"""Provider interface: anything that can hand back a SymbolSnapshot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models import SymbolSnapshot


class Provider(ABC):
    name = "base"

    @abstractmethod
    def snapshot(self, symbol: str, now: datetime) -> Optional[SymbolSnapshot]:
        """Intraday bars + same-day chain for one symbol, or None if unusable."""

    def close(self) -> None:  # pragma: no cover - providers may not need it
        pass
