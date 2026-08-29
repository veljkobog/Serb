# Where this runs, and where the sheets land

Everything runs on your own Windows machine, in PowerShell. Nothing runs in the cloud
and no data leaves your computer except the API calls to BBB, Apollo and HubSpot.

## One-time setup

Pick a permanent home. Do **not** keep working out of `Downloads` — the folder there is
an unpacked ZIP that gets replaced every time you download a new copy, taking your output
files with it.

```powershell
mkdir C:\Serb
cd C:\Serb
git clone -b claude/new-session-55i08m https://github.com/veljkobog/Serb.git .
cd bbb-scraper
pip install -r requirements.txt
```

No git? Download the ZIP, extract it, and move the `bbb-scraper` folder to `C:\Serb\`
by hand. You'll re-download to update instead of running `git pull`.

You end up with:

```
C:\Serb\
  bbb-scraper\      <- the code. You run commands from here.
  leads\            <- every CSV lands here, dated. Created on first run.
```

## The three steps

**1. Scrape** — from `C:\Serb\bbb-scraper`:

```powershell
.\run-leads.ps1 -Category plumber -Location wichita-ks -Max 100 -MinYears 10
```

Writes `C:\Serb\leads\plumber-wichita-ks-2026-08-26.csv`, plus a `.json` run report,
and opens the folder when it finishes. Each run is dated, so nothing overwrites.

**2. Enrich** (owner names + emails) — Apollo runs through Claude, so send the CSV to
this conversation and ask for enrichment. It comes back with owner, title, email,
email status and a match-confidence column.

**3. Check before sending** — back in PowerShell:

```powershell
$env:HUBSPOT_TOKEN = "pat-na1-..."
python crm_check.py "C:\Serb\leads\plumber-wichita-ks-2026-08-26.csv"
```

Writes `...-checked.csv` next to it, with a `crm_verdict` column: `send`, `review`,
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
cd C:\Serb
git pull
```

Your `leads\` folder is untouched by updates.
