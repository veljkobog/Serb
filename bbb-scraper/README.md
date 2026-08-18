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

## Several trades in one go

`--category` takes a comma-separated list, or `--categories-file` a file of slugs:

```bash
python scraper.py --category plumber,roofing-contractors,electrician \
  --location nc --endpoints endpoints.json --max-results 500
```

Each category writes its own `<category>-<location>.csv`; an explicit `--output` collects
them all into one file instead (the `category` column tells them apart, and rows already
in the file are skipped). `--max-results` applies **per category**.

The endpoint is resolved once and one session serves the whole batch — categories run in
sequence, not concurrently, which keeps the request rate where the anti-bot section says
it is.

## Full-state pulls

`--location` accepts a whole state, which runs as a metro-by-metro sequence rather than
one query:

```bash
python scraper.py --category plumber --location nc \
  --endpoints endpoints.json --max-results 2000 --max-per-metro 300
```

```
[run] nc -> 12 metros (source: bundled:NC)
[run] charlotte-nc (0/2000 collected)
[run] raleigh-nc (300/2000 collected)
...
  metros pulled   : 4/5
                    charlotte-nc 15, raleigh-nc 15, greensboro-nc 15, durham-nc 15
  budget          : 60 reached -- rerun to continue
```

- `--max-results` is the budget for the **whole run**; `--max-per-metro` caps each city so
  one large metro can't eat it.
- `--metros "raleigh-nc,durham-nc"` or `--metros-file markets.txt` overrides the bundled
  list; `--max-metros N` takes the first N; `--list-metros` prints the plan and exits
  without scraping.
- Results are deduped **across** metros, so a company listed in two adjacent cities lands
  in the CSV once.
- One HTTP session and one resolved endpoint serve every metro — re-probing per city would
  cost extra requests and throw away the cookies that keep the run looking like one person
  browsing.

### Resuming a state pull

Hitting the budget mid-state is the normal case. A second run picks up where the first
stopped:

```
[run] resuming state pull -- 4 metro(s) already done
[run] charlotte-nc: already collected, skipping
[run] appending to existing plumber-nc.csv (--overwrite to start fresh)
```

Two levels of resume: `.checkpoint-<category>-<metro>.json` tracks pages *within* a metro,
`.progress-<category>-<state>.json` tracks which metros are *done*. A resumed run **appends**
to the existing CSV and drops anything already in it, so several invocations build one
clean file. `--overwrite` starts fresh; `--append` forces appending on a non-resumed run.

The bundled metro list (`data/metros.json`) is the largest cities per state — a starting
point, not an authoritative metro list. It's plain JSON; edit it, or override per run.

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
| `bbb_reviews` / `bbb_complaints` | BBB customer review + complaint counts |
| `employees` | headcount; a range like `11-50` records as `11` |
| `google_rating` / `google_reviews` | only with `--google-key` (see below) |
| `google_place_id` / `google_match` | match confidence: `high` / `medium` / `low` |

- Deduped within-run on normalized `website`, falling back to `phone`. When two rows
  collide, the richer one wins — blanks are filled from the duplicate, nothing is
  overwritten.
- Rows missing **both** website and phone go to `<output>_lowconfidence.csv`, never the
  main file.
- Every run prints a summary: pulled / deduped / per-filter drops / low-confidence /
  website coverage.

## Filtering out the noise

Two kinds of size signal, from two different places.

**On BBB, free with the pull:**

```bash
python scraper.py --category plumber --location charlotte-nc   --min-years 10 --min-bbb-reviews 15 --max-bbb-complaints 3 --min-employees 10
```

**Google reviews are not on BBB** — no scrape of bbb.org can produce them. They come from
the Places API as an opt-in second lookup, billed by Google per request:

```bash
export GOOGLE_MAPS_API_KEY=...
python scraper.py --category plumber --location charlotte-nc   --min-google-reviews 40 --min-google-rating 4.0
```

For home-services SMBs the Google review count is usually the best available proxy for job
volume — BBB review counts are much sparser, and BBB headcount is self-reported and often
missing entirely. Expect to lean on Google if size is the thing you actually care about.

### How unknowns are treated

**A missing value passes every filter by default.** A shop with no headcount on its BBB
profile is not a small shop — it's an unmeasured one, and dropping it silently would cut
good leads. `--drop-unknown` flips this to strict for every active filter.

`--rejects rejects.csv` writes what got filtered out, so a threshold can be checked
instead of trusted. The summary breaks drops down per filter:

```
  filtered min-bbb-reviews   : 25
  filtered max-bbb-complaints: 11
  filtered total  : 36 -> rejects.csv
```

### Filters drive the detail pass

Search cards rarely carry headcount or review counts, so a run that filters on them
fetches the profile page for exactly the records still missing those fields — under the
same pacing, capped by `--max-detail`. This works on both approaches. With `--no-detail`
the run warns that those values will stay unknown rather than filtering on data it never
fetched.

### Google matching and cost control

BBB and Google have no shared key, so matches are scored, not assumed:

| confidence | basis |
| --- | --- |
| `high` | website domain or phone matches the BBB record |
| `medium` | strong name overlap and the city agrees |
| `low` | anything weaker |

A `low` match is treated as **unknown** by the Google filters — a wrong match attaches
another business's 5,000 reviews to your lead. `--allow-low-match` opts into trusting them.
The value is still written to the CSV either way, with `google_match` alongside it.

**BBB-side filters run first**, so Google is only asked about the survivors — putting
`--min-years 10 --min-bbb-reviews 15` in front of `--min-google-reviews` can cut the billed
lookups by most of the pull.

**Check the bill before paying it** with `--google-dry-run`, which runs the pull and the
BBB-side filters, then reports what enrichment *would* cost and stops:

```
google preflight (dry run -- nothing was billed)
-----------------------------------------------
  candidates      : 21
  already cached  : 0
  billable lookups: 21
  estimated cost  : 0.67 at 0.032 per lookup
```

The count comes from the same cache keys and the same cap as the real pass, so it's the
number you'd actually be billed. `--google-cost-per-lookup` turns it into an estimate —
pass your current rate; nothing is assumed about Places pricing.

Cost control, since every uncached lookup is billed:

- one request per company, field-masked to just id / name / address / rating / review count
  / website / phone
- results cached in `--google-cache` (default `.google-places-cache.json`), so re-runs and
  resumed runs cost nothing twice; cached content expires after `--google-cache-ttl` days
  (default 30, matching Google's caching terms)
- `--max-google-lookups` caps billed calls per run (default 500); cache hits don't count
  against it
- the summary reports `N billed, N cached, N matched (N low-confidence)`

Check current Places API pricing before a large run — a 500-lead pull is 500 billed
requests the first time and zero on re-runs.

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
  metros.py          # state -> metro expansion for full-state pulls
  data/metros.json   # bundled metro list, editable
  api_client.py      # Approach A: endpoint discovery (HAR/probe), pagination, backoff
  browser_client.py  # Approach B: Playwright + DOM extraction
  enrich_google.py   # optional Google Places lookup (rating + review count)
  parse.py           # field extraction + normalization + dedupe
  checkpoint.py      # resume logic (per-metro pages, cross-metro progress)
  requirements.txt
  tests/
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

109 tests, no network required. `tests/fixture_server.py` stands in for the search API —
unknown JSON envelope, mixed key casing, same-company-second-profile duplicates, rows
with no contact info, and an empty final page — so the acceptance criteria (≥80% website
coverage, zero duplicate domains, clean stop at pagination end) are checked on every run,
along with HAR mining, resume, backoff and the block-abort path. `tests/places_server.py`
stands in for the Places API and counts requests, so the cache, the TTL and the lookup cap
are proven rather than assumed. The state-pull tests run a multi-metro pull against a
location-aware fixture, including a regional operator listed in every metro, so
cross-metro dedupe and cross-invocation appending are checked too. One test asserts the
dry-run count equals the number of requests the real pass makes, so the preflight can't
drift from the thing it predicts.

## Out of scope (v1)

Email discovery (Apollo/Outscraper downstream) · HubSpot dedupe (existing pipeline step) ·
concurrent runs (categories and metros go in sequence) · proxy rotation (add only if IP blocks become real) ·
revenue estimates (no honest source at this layer — Google review volume is the closest
available proxy).

## A note on access

The pacing, session reuse and volume caps above are deliberate: they keep the crawl light
enough to look like ordinary browsing. Check BBB's Terms of Use and `robots.txt` for the
paths you're pulling and confirm this use is acceptable for your account before running it
at volume.
