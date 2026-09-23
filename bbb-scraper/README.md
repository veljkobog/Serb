# BBB Scraper

CLI that pulls business listings from BBB.org by vertical + geography and writes a CSV
for the standard pipeline: **clean → Apollo enrich → HubSpot dedupe → Smartlead**.

Emails are not on BBB. This tool produces company + domain + phone + firmographics;
email discovery happens downstream.

```bash
python scraper.py --category plumber --location wilmington-nc --max-results 100
```

**New here?** [`SETUP.md`](SETUP.md) covers where to put this on a Windows machine, where
the output files land, and the three-step run.

## Install

```bash
pip install -r requirements.txt
playwright install chromium      # only needed for the Approach B fallback
```

## How it reads BBB

BBB has **no JSON search API**. Results are rendered into the page, with
schema.org JSON-LD alongside them, so Approach A is a plain GET of

```
https://www.bbb.org/search?find_country=USA&find_text=Plumber&find_loc=Wichita%2C+KS&page=2
```

parsed via that JSON-LD. Nothing to configure — this is the default:

```bash
python scraper.py --category plumber --location wichita-ks --max-results 100
```

The search page carries **name, address, phone and profile URL only**. Website, BBB rating,
accreditation and years in business live on the profile page, which is why the detail pass
exists — and why filtering on `--min-years` costs one extra request per business.

Page size is 15. `--find-entity 10113-000` pins a search to BBB's own category id instead
of matching on text, if free-text search ever proves too loose.

A Cloudflare challenge is a 200 with no results, which is otherwise indistinguishable from
"this city has no plumbers", so it's detected explicitly and raises rather than quietly
returning an empty run. That's what triggers the fallback to Playwright.

### If a JSON API ever appears

The endpoint-discovery machinery below still works and takes over when you pass
`--endpoints` or `--from-har`. It's kept because an undocumented API may exist on other
pages, or return later.

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

## Checking a capture before you trust it

Two offline modes turn "did this actually work?" into a question you can answer without
another live run.

**`--inspect-har`** reports what a capture contains — candidate endpoints and their
pagination params, how many payloads carry records, field coverage, and one fully parsed
listing:

```bash
python scraper.py --inspect-har search.har
```

**`--replay`** runs the *entire* pipeline against the capture's real payloads with no
network at all — parsing, normalization, detail pages, dedupe, filters, CSV:

```bash
python scraper.py --category plumber --location wilmington-nc --replay search.har
```

That's the fastest way to find out whether extraction actually works on live BBB data,
and it costs nothing to repeat. Profile pages in the capture are replayed too, so the
years-in-business and headcount parsing gets exercised against real markup.

## When something is wrong, the run says so

The failure mode that matters isn't a crash — it's a run that looks fine and quietly
returns less than it should. Each of these is reported rather than swallowed:

| signal | what it means |
| --- | --- |
| `field coverage ... 0% (!)` | a key name moved; the column isn't missing from BBB |
| `N record(s) had no readable company name` | the *name* key moved — every one is a dropped lead |
| `N skipped at --max-detail` | the detail pass hit its cap; raise it or narrow the pull |
| `filter settings changed since the earlier run` | a resumed pull won't re-filter metros it already collected |
| `already in file: N` | a resumed run found rows it had written before |
| `excluded: N` | your name/domain exclusions matched |

Company names are escaped before they hit the CSV if they start with `=`, `+`, `-` or `@`,
so a listing can't smuggle a formula into the spreadsheet someone opens the leads in.
Normalized fields (phone, domains, URLs) are built by our own code and are left alone.

## Field coverage

Every run ends with the share of rows carrying each field:

```
  field coverage (share of rows with a value)
    website 100%              phone 96%                 street 100%
    years_in_business 84%     accredited 100%           bbb_rating 91%
    bbb_reviews 0% (!)        bbb_complaints 0% (!)     employees 0% (!)
    (!) never populated: bbb_reviews, bbb_complaints, employees -- if BBB shows
    these, the key names moved; capture a HAR and rerun with --replay
```

A column at **0%** is the tell that a key name is wrong, not that BBB stopped publishing
the field. Without this the column just looks empty and a size filter quietly passes
everything. If you see it, `--inspect-har` will show the real key names.

## Approach B: Playwright

`playwright` + `playwright-stealth`, parsing the **rendered DOM** rather than raw HTML.
The browser profile in `--profile-dir` persists between runs, so the Cloudflare clearance
cookie survives and challenges stay rare. Add `--headed` if headless gets challenged.

Playwright expects one exact Chromium build and fails with "playwright install" if the
machine has a different one — common in containers and CI images that already ship a
browser. Point it at what's there instead:

```bash
python scraper.py ... --browser --browser-executable /opt/pw-browsers/chromium --browser-no-sandbox
```

`$BBB_BROWSER_EXECUTABLE` works too. `--browser-no-sandbox` is usually required inside a
container. A launch failure names both fixes rather than just telling you to install.

This path is tested end-to-end against a stand-in BBB site (`tests/bbb_site.py`) with a
real headless Chromium: pagination, the empty page past the end, card extraction, the
detail-page pass, profile persistence between sessions, and the stealth shim. Those tests
skip automatically where no browser is available, so CI stays green without one.

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

## Checking leads against the CRM before you send

`crm_check.py` answers the question that actually matters before outreach — *should we
contact this company* — which is four questions, not one:

```bash
export HUBSPOT_TOKEN=pat-na1-...
python crm_check.py leads.csv --output leads-checked.csv
```

```
crm check
---------
  send            : 12
  review          : 2
  skip-existing   : 5
  skip-suppressed : 1
  (!) 1 suppressed -- do not mail these
```

Each row gains `crm_verdict`, `crm_reason`, `crm_matched_on`, `crm_record_id`,
`crm_owner_id`, `crm_last_contacted` and `crm_open_deals`; the original columns are kept.

**Suppression outranks everything.** Unsubscribed, quarantined or previously-bouncing
addresses come back `skip-suppressed` with the reason, never as a mere duplicate. Mailing
them is a compliance problem and it burns the sending domain for every other campaign.

**Matching runs strongest key first** — email, then phone, then company domain, then
company name:

- **Phone catches what email misses.** A CRM record with a blank or different domain is
  invisible to a domain check, and for local trades businesses that's most of them.
- **Toll-free numbers are never matched on**, for the same reason the scraper won't dedupe
  on them: franchisees share them, so a match merges strangers.
- **A name-only match is `review`, never `skip`.** "Brown's Plumbing" is not a unique
  string, and silently dropping a real lead is worse than a second look.

**An empty portal cannot produce an all-clear.** Zero matches from the wrong token looks
exactly like a clean list, so the run counts the portal first and refuses rather than
reporting good news it can't back up.

## Handing off to the pipeline

**`--column-map map.json`** reshapes the CSV into whatever ingests it next — only the
columns you list, in your order, under your headers:

```json
{ "columns": { "company_name": "Company", "website": "Website", "phone": "Phone" } }
```

See `column-map.example.json`. An unknown column name is an error naming the offender, not
a silently missing column. Append-dedupe follows the map, so a renamed `website` header is
still recognised on a resumed run.

**`--report run.json`** writes the run summary as JSON — counts, per-filter drops, per-metro
breakdown, field coverage, Google spend — so an automated step doesn't have to scrape
stdout. On a batch run each category gets its own `run-<category>.json`.

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
| `social_url` | Facebook/Yelp/etc. page, when that's the only web presence |
| `bbb_reviews` / `bbb_complaints` | BBB customer review + complaint counts |
| `employees` | headcount; a range like `11-50` records as `11` |
| `google_rating` / `google_reviews` | only with `--google-key` (see below) |
| `google_place_id` / `google_match` | match confidence: `high` / `medium` / `low` |

- Deduped within-run on normalized `website`, falling back to `phone`. When two rows
  collide, the richer one wins — blanks are filled from the duplicate, nothing is
  overwritten.
- **Social and directory pages are not domains.** A shop whose listed site is
  `facebook.com/acme-plumbing` gets a blank `website` and the link in `social_url`. This
  matters twice: those URLs can't be enriched by domain, and treating them as one would
  make every Facebook-only business share a dedupe key — the first would be kept and the
  rest dropped as duplicates. Yelp, Angi, Thumbtack, Instagram, Linktree and friends are
  handled the same way. Expect `website` coverage to look lower and be truer; the run
  summary reports the social-only count separately.
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

### Requiring a website

`--require-website` drops listings with no company domain. A blank website is treated as a
**known absence**, not an unknown, so those rows fail rather than passing through the
unknown-passes rule. A social page doesn't satisfy it either — `facebook.com/acme` isn't a
domain you can enrich.

One caveat with teeth: **BBB publishes the website only on profile pages**, so this needs a
working detail pass. Combined with `--no-detail`, or when profile pages are blocked, it
drops every row — the run warns rather than handing you an empty file.

### Excluding chains and franchises

A category search also returns national chains, franchisors and supply houses. Nothing is
excluded by default — a bundled blocklist would quietly drop real leads — so name it:

```bash
python scraper.py ... --exclude-name "roto-rooter,home depot" --exclude-domain homedepot.com
python scraper.py ... --exclude-file exclusions.txt      # see exclusions.example.txt
```

Name fragments match case-insensitively anywhere in the company name; a domain also
matches its subdomains. Excluded rows are counted in the summary and land in `--rejects`
if you asked for one.

### What is *not* treated as identity

Dedupe collapses rows that share a website or phone, which makes both fields load-bearing.
Two cases are deliberately excluded:

- **Social and directory pages** (above) — every Facebook-only shop would share one key.
- **Toll-free numbers** (800/833/844/855/866/877/888) — franchisees, answering services and
  marketing agencies share them, so two unrelated shops can carry the same 800 number.
  The number is still written; it just isn't identity.

Both follow the same rule: dropping a real lead is permanent and invisible, while letting a
genuine duplicate through costs one row that the downstream HubSpot dedupe catches anyway.

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
  crm_check.py       # pre-send HubSpot check: suppression, prior contact, duplicates
  replay.py          # offline HAR replay, for testing against real payloads
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

167 tests. The 158 core tests need no network and no browser; 9 more drive a real
Chromium against a local stand-in site and skip when none is installed. `tests/fixture_server.py` stands in for the search API —
unknown JSON envelope, mixed key casing, same-company-second-profile duplicates, rows
with no contact info, and an empty final page — so the acceptance criteria (≥80% website
coverage, zero duplicate domains, clean stop at pagination end) are checked on every run,
along with HAR mining, resume, backoff and the block-abort path. `tests/places_server.py`
stands in for the Places API and counts requests, so the cache, the TTL and the lookup cap
are proven rather than assumed. The state-pull tests run a multi-metro pull against a
location-aware fixture, including a regional operator listed in every metro, so
cross-metro dedupe and cross-invocation appending are checked too. One test asserts the
dry-run count equals the number of requests the real pass makes, so the preflight can't
drift from the thing it predicts. The replay tests run the full pipeline from a
hand-written HAR, including a sparse card that can only be completed from its profile
page. CI runs the suite and pyflakes on every push
([`.github/workflows/bbb-scraper-tests.yml`](../.github/workflows/bbb-scraper-tests.yml)).

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
