#!/usr/bin/env python3
"""Open a visible browser so a person can clear the Cloudflare challenge once.

Headless Chromium does not get past BBB's profile-page challenge. A real
window driven by a real person does, and the clearance cookie lands in the
persistent profile directory that later headless runs reuse.

This does not pretend to solve the challenge itself -- it opens the page,
waits for you, and then verifies that what is on screen is actually a profile
rather than an interstitial. That last check matters: "the window closed
without an error" is not evidence the challenge was passed.
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import parse   # noqa: E402


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="a BBB profile URL")
    p.add_argument("--profile-dir", default=".bbb-browser-profile")
    p.add_argument("--channel", default="chrome",
                   help="installed browser to drive (default: chrome). "
                        "Playwright's own build is fingerprinted and hangs on "
                        "the challenge; pass an empty string to use it anyway")
    p.add_argument("--save", default="detail-sample.html",
                   help="where to keep the cleared page, as a parser fixture")
    args = p.parse_args(argv)

    import browser_client

    client = browser_client.BrowserClient(
        user_data_dir=args.profile_dir,
        headless=False,          # the entire point
        min_delay=0, max_delay=0,
        verbose=True,
        channel=args.channel or None,
    )
    client.start()
    try:
        print(f"[warm] opening {args.url}")
        client._page.goto(args.url, wait_until="domcontentloaded")

        print("")
        print("  Solve the challenge in the browser window if one appears.")
        print("  When you can see the BUSINESS PROFILE, press Enter here.")
        print("")
        try:
            input("  press Enter when the profile is on screen... ")
        except EOFError:
            pass

        html = client._page.content()
        title = client._page.title()
        print(f"\n[warm] page title now: {title!r}")

        if parse.looks_challenged(html):
            print("[warm] still a challenge page -- clearance NOT obtained",
                  file=sys.stderr)
            print("[warm] try again, and wait for the business name to appear",
                  file=sys.stderr)
            return 1

        with open(args.save, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"[warm] cleared page saved to {args.save} ({len(html):,} chars)")
        print(f"[warm] clearance cookie kept in {args.profile_dir}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
