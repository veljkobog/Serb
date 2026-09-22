"""Replay provider: re-score a saved snapshot file.

Every scan can be written to disk with --save-snapshot; feeding it back with
`--provider snapshot --snapshot-path FILE` reproduces the exact scan, which is
what makes weight tuning and after-the-fact review possible.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..models import Bar, OptionQuote, SymbolSnapshot
from .base import Provider


def save_snapshots(
    path: str | Path, snapshots: List[SymbolSnapshot], captured_at: datetime
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": captured_at.isoformat(),
        "symbols": {s.symbol: s.to_dict() for s in snapshots},
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def _to_snapshot(d: Dict) -> SymbolSnapshot:
    return SymbolSnapshot(
        symbol=d["symbol"],
        spot=d["spot"],
        prev_close=d.get("prev_close"),
        bars=[
            Bar(
                ts=datetime.fromisoformat(b["ts"]),
                open=b["open"],
                high=b["high"],
                low=b["low"],
                close=b["close"],
                volume=b["volume"],
            )
            for b in d.get("bars", [])
        ],
        chain=[OptionQuote(**q) for q in d.get("chain", [])],
        expiry=d.get("expiry"),
        adr_pct=d.get("adr_pct"),
        avg_share_volume=d.get("avg_share_volume"),
        is_benchmark=d.get("is_benchmark", False),
        notes=list(d.get("notes", [])),
    )


class SnapshotProvider(Provider):
    name = "snapshot"

    def __init__(self, path: str | Path):
        payload = json.loads(Path(path).read_text())
        self.captured_at = datetime.fromisoformat(payload["captured_at"])
        self._symbols = {k: _to_snapshot(v) for k, v in payload["symbols"].items()}

    @property
    def symbols(self) -> List[str]:
        return list(self._symbols)

    def snapshot(self, symbol: str, now: datetime) -> Optional[SymbolSnapshot]:
        return self._symbols.get(symbol)
