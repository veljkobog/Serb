#!/usr/bin/env python3
"""
Build the hosted copy of the Partner Sourcing Desk from internal/partners.html.

partners.html is the canonical source. The hosted page runs under a strict CSP
that blocks font CDNs and any download the page starts itself, so this script:
  - strips the document wrapper (the host supplies <html>/<head>/<body>)
  - inlines the typefaces as data URIs instead of linking Google Fonts
  - swaps the Export download for copy-to-clipboard

Usage:  python3 internal/build-artifact.py <fonts.css> <output.html>
        where fonts.css holds @font-face rules with base64 data URIs.
"""
import re, sys, pathlib

src, fonts_css, dest = (pathlib.Path(p) for p in
    (pathlib.Path(__file__).parent / "partners.html", sys.argv[1], sys.argv[2]))
s = src.read_text()
fonts = fonts_css.read_text()

# 1. Drop the external font link; inline the faces in its place.
s = s.replace(
  '<link rel="preconnect" href="https://fonts.googleapis.com" />\n'
  '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\n'
  '<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700;800&family=Barlow:wght@400;500;600;700&display=swap" rel="stylesheet" />\n'
  '<style>', '<style>\n' + fonts)
assert "fonts.googleapis" not in s, "external font link survived"

# 2. Downloads are inert in the hosted sandbox - copy the JSON to the clipboard.
old_dl = """  const text = JSON.stringify(payload, null, 2);
  const blob = new Blob([text], {type:"application/json"});
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = "el-partners-export.json";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 2000);"""
new_dl = """  const text = JSON.stringify(payload, null, 2);
  const btn = document.getElementById("exportBtn");
  const done = msg => { btn.textContent = msg; setTimeout(()=>btn.textContent="Export", 2400); };
  navigator.clipboard.writeText(text)
    .then(()=>done("Copied \\u2713"))
    .catch(()=>done("Copy blocked"));"""
assert old_dl in s, "export block not found"
s = s.replace(old_dl, new_dl)
s = s.replace('title="Download the dataset with your edits and notes merged in"',
              'title="Copy the dataset, with your notes merged in, to the clipboard"')
s = s.replace("Hit <b>Export</b> in the header to produce a merged file you can commit and share with the team.",
              "Hit <b>Export</b> in the header to copy the full dataset, notes included, to your clipboard.")

# 3. Strip the document wrapper - the host wraps this content itself.
s = re.sub(r'^.*?<title>[^<]*</title>\s*', '<title>Partner Sourcing Desk</title>\n', s, count=1, flags=re.S)
s = s.replace('</head>\n<body>\n', '', 1)
s = re.sub(r'\s*</body>\s*</html>\s*$', '\n', s)
for tag in ("<!DOCTYPE", "<html", "</html>", "<body>", "</body>", "<head>", "</head>"):
    assert tag not in s, f"{tag} survived the strip"

dest.write_text(s)
print(f"built {dest}  ({len(s):,} bytes)")
