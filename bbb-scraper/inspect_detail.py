#!/usr/bin/env python3
"""Fetch one BBB profile page and show how it actually presents each field.

The parser was written against a fixture. Real pages load fine through a
browser but every detail field comes back empty, which means the markup does
not look the way the parser expects. Guessing at that from here is what
produced the fixture in the first place, so this prints the real thing.

It writes the full page to a file and, for each field the size screen depends
on, prints the surrounding markup so the extractor can be written against what
BBB actually sends rather than against a memory of it.

    python inspect_detail.py https://www.bbb.org/us/tx/houston/profile/...
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

#: What the screen needs, and the words BBB might label them with.
WANTED = {
    "employees": ["employee", "number of employees", "staff"],
    "years_in_business": ["years in business", "business started",
                          "date of incorporation", "years"],
    "website": ["website", "business website", "visit website"],
    "accredited": ["accredited", "accreditation"],
    "bbb_reviews": ["customer review", "reviews"],
    "bbb_complaints": ["complaint"],
}

CONTEXT = 320


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).strip()


def show_matches(page: str, label: str, needles) -> None:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    seen = 0
    for needle in needles:
        for match in re.finditer(re.escape(needle), page, re.I):
            start = max(0, match.start() - CONTEXT // 2)
            end = min(len(page), match.end() + CONTEXT)
            fragment = page[start:end]
            text = strip_tags(fragment)
            if not text:
                continue
            seen += 1
            print(f"\n  [{seen}] ...{text[:300]}...")
            if seen >= 4:
                return
    if not seen:
        print("  (the page never mentions this)")


def show_jsonld(page: str) -> None:
    print(f"\n{'=' * 70}\nJSON-LD blocks\n{'=' * 70}")
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page, re.S | re.I)
    if not blocks:
        print("  (none)")
        return
    for index, raw in enumerate(blocks, 1):
        try:
            data = json.loads(raw.strip())
        except ValueError:
            print(f"  [{index}] (does not parse as JSON)")
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            print(f"  [{index}] @type={item.get('@type')!r} "
                  f"keys={sorted(item)[:14]}")
            for key in ("numberOfEmployees", "foundingDate", "url",
                        "aggregateRating", "telephone", "address"):
                if key in item:
                    print(f"        {key} = {json.dumps(item[key])[:160]}")


def show_next_data(page: str) -> None:
    """Server-rendered React pages usually park their state in a JSON blob."""
    print(f"\n{'=' * 70}\nEmbedded state blobs\n{'=' * 70}")
    found = False
    for pattern in (r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                    r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
                    r'window\.__DATA__\s*=\s*(\{.*?\});'):
        for raw in re.findall(pattern, page, re.S):
            found = True
            print(f"  blob of {len(raw):,} chars")
            for key in ("numberOfEmployees", "employees", "yearsInBusiness",
                        "businessStarted", "accreditation", "isAccredited",
                        "websiteUrl", "reviewCount", "complaintCount"):
                for m in re.finditer(r'"' + key + r'"\s*:\s*([^,}]{1,80})', raw):
                    print(f'    "{key}": {m.group(1).strip()}')
                    break
    if not found:
        print("  (none)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url", help="a BBB profile URL")
    p.add_argument("--out", default="detail-sample.html")
    p.add_argument("--from-file", default=None,
                   help="analyse a page already saved, instead of fetching")
    args = p.parse_args(argv)

    if args.from_file:
        page = open(args.from_file, encoding="utf-8", errors="replace").read()
    else:
        import browser_client
        client = browser_client.BrowserClient(headless=True, min_delay=0, max_delay=0)
        client.start()
        try:
            client._page.goto(args.url, wait_until="domcontentloaded")
            page = client._page.content()
        finally:
            client.close()
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(page)
        print(f"saved {len(page):,} chars to {args.out}")

    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    print(f"page title: {strip_tags(title.group(1)) if title else '(none)'}")

    low = page.lower()
    for marker in ("just a moment", "cf_chl_opt", "access denied",
                   "enable javascript and cookies"):
        if marker in low:
            print(f"\n(!) page looks like a challenge -- found {marker!r}")

    show_jsonld(page)
    show_next_data(page)
    for field, needles in WANTED.items():
        show_matches(page, field, needles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
