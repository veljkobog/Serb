"""`odte configure` — ask once, store it, prove it works."""

from __future__ import annotations

import argparse
import os
from getpass import getpass
from pathlib import Path
from typing import Optional

from . import __version__
from .env import PROJECT_ENV, load_env, masked, write_env


def _ask_token(current: Optional[str]) -> str:
    if current:
        print(f"  current token: {masked(current)}")
        keep = input("  keep it? [Y/n] ").strip().lower()
        if keep in ("", "y", "yes"):
            return current
    print("\n  Tradier dashboard -> API Access -> copy the access token.")
    print("  (input is hidden)")
    while True:
        token = getpass("  token: ").strip()
        if len(token) >= 12:
            return token
        print("  that looks too short - paste the whole token.")


def _ask_env(current: Optional[str]) -> str:
    default = (current or "production").lower()
    print(
        "\n  production = real-time quotes + streaming trade side (funded brokerage account)\n"
        "  sandbox    = delayed, thin, no real flow - fine for wiring"
    )
    answer = input(f"  environment [{default}]: ").strip().lower()
    answer = answer or default
    return "sandbox" if answer.startswith("s") else "production"


def run_configure(args: argparse.Namespace) -> int:
    print(f"odte {__version__} setup\n")
    load_env()
    token = args.token or (None if args.reset else os.environ.get("TRADIER_TOKEN"))
    env = args.env or (None if args.reset else os.environ.get("TRADIER_ENV"))

    if args.token:
        pass  # given on the command line, nothing to ask
    elif args.non_interactive:
        if not token:
            print("No token: pass --token, or run without --non-interactive.")
            return 2
    else:
        token = _ask_token(token)
        env = _ask_env(env)

    env = (env or "production").lower()
    target = Path(args.env_file) if getattr(args, "env_file", None) else PROJECT_ENV
    path = write_env(target, {"TRADIER_TOKEN": token, "TRADIER_ENV": env})
    os.environ["TRADIER_TOKEN"] = token
    os.environ["TRADIER_ENV"] = env
    print(f"\n  saved {masked(token)} ({env}) to {path}")
    print("  this file is gitignored and readable only by you.\n")

    if args.no_doctor:
        return 0

    from .doctor import run_doctor

    doctor_args = argparse.Namespace(
        tradier_token=None, tradier_env=None, symbol="SPY",
        stream_seconds=args.stream_seconds, as_of=None,
    )
    code = run_doctor(doctor_args)
    if code == 0:
        print("\n  You're set. Tomorrow morning, one command:\n")
        print("      ./bullbear live\n")
    return code
