#!/usr/bin/env python3
"""Structural checks for the PowerShell scripts.

PowerShell cannot be executed where these are developed, so the failure modes
that have actually bitten are asserted statically instead. Each check exists
because the thing it looks for went wrong in a real run:

  * `$args` -- a reserved automatic variable; assigning it breaks argument
    handling in ways that surface far from the assignment.
  * stderr piped while ErrorActionPreference is Stop -- turns every warning a
    native command writes into a TERMINATING error. A daily run died on the
    scraper's first progress line and left a log containing only a timestamp.
  * unbalanced braces or parens -- a parse error the script cannot report
    about itself.
"""

import glob
import os
import sys


def code_lines(path):
    """Script lines with comment-based help and `#` comments removed."""
    out, in_help = [], False
    for line in open(path, encoding="utf-8").read().splitlines():
        stripped = line.strip()
        if stripped.startswith("<#"):
            in_help = True
        if in_help:
            if "#>" in stripped:
                in_help = False
            continue
        if stripped.startswith("#"):
            continue
        out.append(line)
    return out


def check(path):
    problems = []
    raw = open(path, encoding="utf-8").read()

    for opener, closer, name in (("{", "}", "braces"), ("(", ")", "parens")):
        if raw.count(opener) != raw.count(closer):
            problems.append(f"unbalanced {name}: "
                            f"{raw.count(opener)} vs {raw.count(closer)}")

    eap = "Continue"   # PowerShell's default
    for line in code_lines(path):
        if "$args" in line and "=" in line.split("$args")[0] + "=":
            if line.strip().startswith("$args"):
                problems.append("assigns $args, which PowerShell reserves")
        if "ErrorActionPreference" in line and "=" in line:
            eap = line.split("=", 1)[1].strip().strip('"').strip("'")
        if "2>&1" in line and "|" in line and eap == "Stop":
            problems.append(
                "pipes native stderr while ErrorActionPreference is Stop -- "
                "any warning becomes a terminating error")
    return problems


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scripts = sorted(glob.glob(os.path.join(here, "*.ps1")))
    if not scripts:
        print("no .ps1 files found", file=sys.stderr)
        return 1

    failed = False
    for path in scripts:
        problems = check(path)
        name = os.path.basename(path)
        if problems:
            failed = True
            for problem in problems:
                print(f"{name}: {problem}", file=sys.stderr)
        else:
            print(f"{name}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
