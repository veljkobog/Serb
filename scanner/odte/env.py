"""Local config file, so nothing depends on remembering to export a token.

`odte configure` writes scanner/.env (0600, gitignored). Every command loads it
before parsing arguments. Real environment variables always win, so CI and
one-off overrides behave the way you'd expect.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Optional

PROJECT_ENV = Path(__file__).resolve().parent.parent / ".env"
HOME_ENV = Path.home() / ".odte.env"
KEYS = ("TRADIER_TOKEN", "TRADIER_ENV")


def parse_env(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def load_env(paths: Optional[Iterable[Path]] = None) -> Dict[str, str]:
    """Merge config files into os.environ without clobbering what's set."""
    loaded: Dict[str, str] = {}
    for path in paths if paths is not None else (HOME_ENV, PROJECT_ENV):
        try:
            if not path.exists():
                continue
            values = parse_env(path.read_text())
        except OSError:
            continue
        for key, value in values.items():
            loaded[key] = value
            if not os.environ.get(key):
                os.environ[key] = value
    return loaded


def write_env(path: Path, values: Dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ["# odte config - private, not committed. Delete to revoke locally.", ""]
    body += [f"{k}={v}" for k, v in values.items() if v]
    path.write_text("\n".join(body) + "\n")
    try:
        path.chmod(0o600)  # a brokerage token: owner-only
    except OSError:  # pragma: no cover - filesystems that don't do modes
        pass
    return path


def masked(token: str) -> str:
    if not token:
        return "(none)"
    return f"{token[:4]}...{token[-4:]} ({len(token)} chars)"
