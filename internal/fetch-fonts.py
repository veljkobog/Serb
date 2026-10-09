#!/usr/bin/env python3
"""
Regenerate fonts.css: the @font-face rules the hosted copy inlines as data URIs.

The hosted page runs under a CSP that blocks font CDNs, so the typefaces have to
travel inside the file. This fetches the latin subsets from Google Fonts and
base64-encodes them. Run it whenever fonts.css is missing - it is a build
artifact, not source, so it does not live in the repo.

Usage:  python3 internal/fetch-fonts.py <output.css>
"""
import re, sys, base64, os, urllib.request, pathlib

WANT = {("Barlow", "400"), ("Barlow", "600"), ("Barlow", "700"),
        ("Barlow Condensed", "700"), ("Barlow Condensed", "800")}
CSS_URL = ("https://fonts.googleapis.com/css2"
           "?family=Barlow+Condensed:wght@600;700;800"
           "&family=Barlow:wght@400;500;600;700&display=swap")

proxy = os.environ.get("HTTPS_PROXY")
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"https": proxy} if proxy else {}))
# Google serves woff2 only to browser-like clients.
opener.addheaders = [("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")]

css = opener.open(CSS_URL, timeout=60).read().decode()
out = []
for subset, face in re.findall(r'/\*\s*([a-z0-9\-]+)\s*\*/\s*(@font-face\s*\{.*?\})', css, re.S):
    if subset != "latin":
        continue
    fam = re.search(r"font-family:\s*'([^']+)'", face).group(1)
    wt = re.search(r"font-weight:\s*(\d+)", face).group(1)
    if (fam, wt) not in WANT:
        continue
    url = re.search(r"url\((https://[^)]+\.woff2)\)", face).group(1)
    b64 = base64.b64encode(opener.open(url, timeout=60).read()).decode()
    out.append("@font-face{font-family:'%s';font-style:normal;font-weight:%s;font-display:swap;"
               "src:url(data:font/woff2;base64,%s) format('woff2');}" % (fam, wt, b64))
    print("  %-18s %s" % (fam, wt))

if len(out) != len(WANT):
    sys.exit("expected %d faces, got %d - check the Google Fonts CSS" % (len(WANT), len(out)))
pathlib.Path(sys.argv[1]).write_text("\n".join(out))
print("wrote %s (%d bytes)" % (sys.argv[1], os.path.getsize(sys.argv[1])))
