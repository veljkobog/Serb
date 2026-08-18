# BBB Scraper

CLI that pulls business listings from BBB.org by vertical + geography and writes a CSV
for the standard pipeline: **clean → Apollo enrich → HubSpot dedupe → Smartlead**.

Emails are not on BBB. This tool produces company + domain + phone + firmographics;
email discovery happens downstream.

```bash
python scraper.py --category plumber --location wilmington-nc --max-results 100
```

## Install

```bash
pip install -r requirements.txt
playwright install chromium      # only needed for the Approach B fallback
```

## First run: point it at the search API

The search endpoint is **not hardcoded**, on purpose. BBB's search API is undocumented,
unversioned and Cloudflare-gated — a URL baked in today is a silent breakage tomorrow.
Give the tool the endpoint once, from your own browser session:

1. Open [bbb.org](https://www.bbb.org), run the search by hand with your category + location.
2. DevTools → **Network** → filter **Fetch/XHR** → find the paginated JSON request.
3. Right-click it → **Save all as HAR with content** → `search.har`.
4. Let the tool mine it and save a reusable config:

```bash
python scraper.py --category plumber --location wilmington-nc \
  --from-har search.har --save-endpoints endpoints.json
```

Every later run just uses the config:

```bash
python scraper.py --category plumber --location wilmington-nc \
  --endpoints endpoints.json --max-results 500
```

The HAR carries the exact URL, query params and full header set (User-Agent, Referer,
cookies) of a real session, which is what the API checks. `--save-endpoints` strips
cookies unless you pass `--save-cookies`; both `*.har` and `endpoints.json` are
gitignored, since they're session credentials.

Two other ways in:

- `--discover` — fetches the search page and its JS bundles and mines them for `/api/`
  routes, then probes each until one returns business records. Best-effort bootstrap.
- `--browser` — skip the API entirely and use Playwright.

If the API rejects the session (403/429 from Cloudflare), the run falls back to
Approach B automatically unless you pass `--no-fallback`.

## Approach B: Playwright

`playwright` + `playwright-stealth`, parsing the **rendered DOM** rather than raw HTML.
The browser profile in `--profile-dir` persists between runs, so the Cloudflare clearance
cookie survives and challenges stay rare. Add `--headed` if headless gets challenged.

## Output

CSV, UTF-8, one row per company:

| column | notes |
| --- | --- |
| `company_name` | as listed |
| `website` | domain only, normalized (no scheme, no `www.`, no path) |
| `phone` | E.164 (`+19105550134`) |
| `street` / `city` / `state` / `zip` | split fields |
| `category` | BBB category |
| `years_in_business` | int; **blank if not shown — never guessed** |
| `accredited` | `true` / `false`; blank if unknown |
| `bbb_rating` | `A+` … `F`; blank if NR |
| `profile_url` | BBB listing URL |

- Deduped within-run on normalized `website`, falling back to `phone`. When two rows
  collide, the richer one wins — blanks are filled from the duplicate, nothing is
  overwritten.
- Rows missing **both** website and phone go to `<output>_lowconfidence.csv`, never the
  main file.
- `--min-years N` drops rows below the threshold but **keeps rows with unknown years** —
  unknown is not the same as "fewer than N".
- Every run prints a summary: pulled / deduped / low-confidence / website coverage.

Detail pages are visited **only** for records still missing `years_in_business` or
`accredited` after the search card, capped by `--max-detail` (default 100), and skipped
entirely with `--no-detail`. Request volume is the scarce resource.

## Rate limiting and blocks

- 1 request per 2–4s with jitter (`--min-delay` / `--max-delay`). No concurrency in v1.
- User-Agent rotates per **session**, not per request — a UA that changes mid-session is
  a stronger bot signal than a stale one.
- Exponential backoff on 403/429/503; the run aborts after **3 consecutive blocks** and
  still writes whatever it collected.

## Resume

Every page is checkpointed to `.checkpoint-<category>-<location>.json` (last page +
collected profile IDs). Rerun the same command to resume; the checkpoint is cleared once
results are exhausted, so a completed run doesn't cause the next one to skip pages.

- `--skip N` — skip N result pages manually.
- `--no-resume` — ignore an existing checkpoint.
- A checkpoint from a different category/location is ignored rather than resumed.
- Ctrl-C saves the checkpoint before exiting.

## Layout

```
bbb-scraper/
  scraper.py         # entry, CLI, orchestration, CSV output
  api_client.py      # Approach A: endpoint discovery (HAR/probe), pagination, backoff
  browser_client.py  # Approach B: Playwright + DOM extraction
  parse.py           # field extraction + normalization + dedupe
  checkpoint.py      # resume logic
  requirements.txt
  tests/
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

52 tests, no network required. `tests/fixture_server.py` stands in for the search API —
unknown JSON envelope, mixed key casing, same-company-second-profile duplicates, rows
with no contact info, and an empty final page — so the acceptance criteria (≥80% website
coverage, zero duplicate domains, clean stop at pagination end) are checked on every run,
along with HAR mining, resume, backoff and the block-abort path.

## Out of scope (v1)

Email discovery (Apollo/Outscraper downstream) · HubSpot dedupe (existing pipeline step) ·
multi-category concurrent runs · proxy rotation (add only if IP blocks become real).

## A note on access

The pacing, session reuse and volume caps above are deliberate: they keep the crawl light
enough to look like ordinary browsing. Check BBB's Terms of Use and `robots.txt` for the
paths you're pulling and confirm this use is acceptable for your account before running it
at volume.
