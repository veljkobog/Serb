# Where this runs, and where the sheets land

Everything runs on your own Windows machine, in PowerShell. Nothing runs in the cloud
and no data leaves your computer except the API calls to BBB, Apollo and HubSpot.

## One-time setup

Pick a permanent home. Do **not** keep working out of `Downloads` — the folder there is
an unpacked ZIP that gets replaced every time you download a new copy, taking your output
files with it.

```powershell
cd $HOME
git clone -b claude/new-session-55i08m https://github.com/veljkobog/Serb.git
cd $HOME\Serb\bbb-scraper
pip install -r requirements.txt
```

No git? Download the ZIP, extract it, and move the `bbb-scraper` folder to `C:\Users\<you>\Serb\`
by hand. You'll re-download to update instead of running `git pull`.

You end up with:

```
C:\Users\<you>\Serb\bbb-scraper\        <- the code. You run commands from here.
C:\Users\<you>\ClaudeAssistant\exports\   <- every lead sheet lands here (BOB's source folder)
```

The code and the output live in different places on purpose: updating the code never
touches your exports, and BOB keeps reading one folder.

## Apollo (enrichment, not discovery)

BBB's profile pages return 403 to the scraper, and the website lives on the
profile, not the search card. So a raw pull has `website` blank for every row
-- which means a website filter would drop *everything*. Apollo's lookup
endpoint fills it back in, for free.

Set the key once:

```powershell
setx APOLLO_API_KEY "<your key>"
```

Close PowerShell and reopen it (setx only affects new windows), then check:

```powershell
echo $env:APOLLO_API_KEY
```

`run-leads.ps1` picks it up automatically and drops websiteless companies. If
the key isn't set it says so and skips the filter rather than silently
returning an unfiltered sheet.

If Apollo ever moves the endpoint (it has moved between `/v1` and `/api/v1`),
discover the new one instead of guessing -- this costs nothing, because it
queries for a company that does not exist:

```powershell
python scraper.py --apollo-probe
```

### What Apollo is and is not for here

Apollo is the **enrichment** layer, not the discovery layer. Discovery is BBB,
deliberately: BBB carries operating companies Apollo has never indexed, and an
Apollo-first search cannot find what Apollo does not have.

So nothing is ever dropped for being absent from Apollo. A row comes back
labelled instead:

  * `apollo_match` = `high` / `medium` / `low` -- Apollo knows this company
  * `apollo_match` = `not-in-apollo` -- Apollo looked and has no such company
  * `apollo_match` = blank -- the lookup never ran

A row marked **not-in-apollo with real headcount is a sleeper**: a company of
size that the Apollo-first tools cannot see at all. Sort by that column first.

### Sizing without EBITDA

Nobody publishes EBITDA for private companies this size -- not BBB, not
Apollo. Headcount is the proxy. At home-services margins of 10-15%, **$500K
EBITDA implies roughly $3.5-5M of revenue, or about 20-30 employees**, so
`min_employees` is 20.

That count comes from **BBB's own profile page**, not Apollo, which is what
lets a company Apollo has never heard of still qualify.

**This is a screen, not a financial.** It gets you to a conversation.

### If the size screen quietly stops working

`employees` lives on the BBB profile page, and those pages have answered 403
before. An unknown value *passes* a filter rather than failing it, so a
blocked profile fetch turns a ">= 20 employees" screen into no screen at all
-- silently, with a sheet that still looks full.

The daily run checks for this: if more than half the rows come back with no
headcount it says so in `ATTENTION-<date>.txt`. When that happens, re-run
through a real browser, which is slower but gets the page:

```powershell
.\run-leads.ps1 -Category roofing-contractors -Location houston-tx -Browser
```

## Sheet size

`-Max` caps the **raw** pull, before filtering. `-Target` caps the **finished**
sheet. Keep `-Max` well above `-Target` or filtering leaves you short:

```powershell
.\run-leads.ps1 -Category roofing-contractors -Location wichita-ks -Target 15 -Max 60
```

## Running it automatically at 9am

One-time setup, in order. Each step checks the one before it, so if something
is missing you find out now rather than at 9am on a morning nobody is watching.

**1. Keys.** Both, then close and reopen PowerShell:

```powershell
setx APOLLO_API_KEY "<your key>"
setx HUBSPOT_TOKEN "<your private-app token>"
```

**2. Pick your territory.** Copy the example config and edit the `metros`
list to the cities you actually sell into:

```powershell
copy rotation.example.json rotation.json
notepad rotation.json
```

`python scraper.py --list-metros --metros <state>` prints valid slugs. The
rotation cycles through that list and remembers where it stopped, so the same
companies do not come back on Thursday.

**3. See what tomorrow would do, without fetching anything:**

```powershell
.\daily-leads.ps1 -DryRun
```

**4. Do one real run by hand before trusting the schedule:**

```powershell
.\daily-leads.ps1
```

**5. Schedule it:**

```powershell
.\install-schedule.ps1
```

That registers a weekday 9am task that wakes the machine if it is asleep. It
**cannot** run while the machine is off; a missed run fires at your next login.

Remove it any time with `.\install-schedule.ps1 -Remove`.

### How you find out something broke

An unattended job that dies quietly looks exactly like one with nothing to
report, so this one is deliberately loud:

  * every run writes `_daily-status.json` to the exports folder
  * a failed run also drops `ATTENTION-<date>.txt` there -- the folder you
    open every morning anyway
  * a failed run raises a desktop notification. Only a failed one: a toast
    every morning trains you to dismiss it unread
  * `logs\run-<date>.log` has the full output

A clean run deletes the day's ATTENTION file, so a stale banner never reads as
today's failure.

### Where it stops

At the sheet. Nothing is pushed to Smartlead automatically. A wrong match that
costs a deleted row is a nuisance; a wrong match that sends mail is a stranger
getting a cold email from you.

## The three steps

**1. Scrape** — from `C:\Users\<you>\Serb\bbb-scraper`:

```powershell
.\run-leads.ps1 -Category plumber -Location wichita-ks -Max 100 -MinYears 10
```

Writes `C:\Users\<you>\ClaudeAssistant\exports\plumber-wichita-ks-2026-08-26-1432.csv`, plus a
`.json` run report, and opens the folder when it finishes. Filenames carry the date and
time, so repeat pulls never overwrite each other and BOB sees each batch separately.

To send somewhere else, either per run:

```powershell
.\run-leads.ps1 -Category plumber -Location wichita-ks -ExportFolder "D:\other\path"
```

or permanently (then open a new PowerShell window):

```powershell
setx LEAD_EXPORT_DIR "D:\other\path"
```

**If the export folder doesn't exist, the run stops.** It will not create one. A lead
file written to a folder nothing reads looks like success while delivering nothing, so
the script checks the usual `ClaudeAssistant` locations and tells you which ones actually
exist. If the path is right and simply missing, re-run with `-CreateFolder`.

**2. Enrich** (owner names + emails) — Apollo runs through Claude, so send the CSV to
this conversation and ask for enrichment. It comes back with owner, title, email,
email status and a match-confidence column.

**3. Check before sending** — back in PowerShell:

```powershell
$env:HUBSPOT_TOKEN = "pat-na1-..."
python crm_check.py "$env:USERPROFILE\ClaudeAssistant\exports\plumber-wichita-ks-2026-08-26-1432.csv"
```

Writes `...-checked.csv` next to it in the same exports folder, with a `crm_verdict` column: `send`, `review`,
`skip-existing` or `skip-suppressed`. Sort by that column in Excel and only the `send`
rows go to Smartlead.

## Getting a HubSpot token

HubSpot → Settings (gear) → Integrations → Private Apps → Create private app.
Under Scopes tick read access for **contacts**, **companies** and **deals**. Copy the
token; it starts `pat-na1-`. Set it once per PowerShell window as above, or permanently:

```powershell
setx HUBSPOT_TOKEN "pat-na1-..."
```

(Then open a new PowerShell window for it to take effect.)

## Updating to a newer version

```powershell
cd $HOME\Serb
git pull
```

Your exports folder is outside the code folder, so updates never touch it.
