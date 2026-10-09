# Internal tools

**Not published.** The Pages workflow (`.github/workflows/deploy.yml`) stages only
the public site into `_site/` and fails the build if anything from this directory
leaks in. Files here are served to nobody — open them from a local checkout.

## Hosted copy

The team-facing copy lives at **https://claude.ai/code/artifact/c543de72-8dbf-41ae-b2d6-a4ee5ce57bba**

It is private until shared from the page's own share menu. `partners.html` in this
directory stays the canonical source; the hosted page is generated from it by
`build-artifact.py`, which strips the document wrapper, inlines the typefaces as
data URIs (the host blocks font CDNs), and swaps the Export download for
copy-to-clipboard (the host blocks page-initiated downloads).

To rebuild and redeploy after editing `partners.html`:

```bash
python3 internal/fetch-fonts.py fonts.css      # only if fonts.css is missing
python3 internal/build-artifact.py fonts.css desk.html
# then republish desk.html to the SAME artifact URL above
```

`fonts.css` is a build artifact, not source — it holds base64 `woff2` data URIs for
Barlow 400/600/700 and Barlow Condensed 700/800, and is too large to keep in the repo.
`fetch-fonts.py` regenerates it from Google Fonts whenever it has been cleaned up.

Publishing to a *different* URL creates a second artifact instead of updating this
one, so always pass the existing URL when redeploying.

## `partners.html`

Partner Sourcing Desk — the buyer-coverage map for our EL partner roster.
Open the file directly in a browser (`open internal/partners.html`). No build step,
no server, no dependencies, works offline.

### What it does

- **Directory** — pick a vertical from the dropdown (or the left rail) to see every
  partner buying in it, with sponsor, HQ, website, footprint, states, brands, buy box
  and how to source for them.
- **Coverage** — a US tile map shaded by how many partners are active in each state
  for the selected vertical. Click a state to list them.
- **White Space** — per vertical, the states where we have no partner (red) or only
  one (yellow). That is the sourcing priority list.
- **Latest news** — every partner has a live Google News button. Nothing is cached,
  so it never goes stale. Opened from a local file the link opens a tab normally; in
  the hosted copy the frame is sandboxed without `allow-popups`, so the page copies
  the URL to your clipboard and says so instead of failing silently.
- **Team notes** — free-text note per partner, saved in your browser. Notes are
  searchable alongside everything else.

### The vertical dropdown

Verticals are ordered by how many partners cover them and split into three groups:

- **Covered (3+ partners)** — HVAC (19), Private Equity Group (19), Roofing (16),
  Plumbing (13), Electrical (9), Landscaping (8), IT (7), Commercial MEP (6),
  Exterior Services (6), Commercial Facilities (5), Fire Protection (4),
  Restoration Services (3).
- **Thin coverage (under 3)** — Janitorial, Windows, Asphalt, Cybersecurity, Tree Care
  Services. One buyer means no competitive tension; treat a lead in these as a
  relationship call, not a process.
- **Not yet classified** — partners still awaiting research.

The threshold is the `COVERAGE_THRESHOLD` constant at the top of the script.

Multi-trade platforms are cross-referenced, not bucketed: Apex appears under HVAC,
Plumbing *and* Electrical, so picking any one trade shows every buyer who will take
that deal. Sponsors carry the verticals their platforms operate in, which is why
Alpine shows up under HVAC, Commercial MEP and IT.

### Engagement letters — the source of truth

Partner status comes from the **HubSpot Client Pipeline, `Closed (Won)` stage**
(pipeline `772899739`, stage `1128447549`). As of **9 Oct 2026** that stage holds
**73 deal records**, which the tool reconciles exactly.

Every partner carries one of three states, shown as a chip on the card and as the
first section of the drawer:

| State | Count | Meaning |
| --- | --- | --- |
| **EL signed** | 71 | Has a Closed (Won) record. Drawer shows the EL date and links to the deal. |
| **Under parent EL** | 3 | Apex and Orion (under `Alpine Investors; Apex & Orion`) and Percheron (under `Alloy Roofing (Percheron Capital)`). In scope through the parent, no standalone EL. |
| **Approved, not signed** | 1 | Heartland Paving Partners — EL approved 14 Aug 2026, no Closed (Won) record. |

71 signed partners plus 2 duplicate records = the 73 records in the stage.

**Why the tool says 71 and the deal board says ~65 partners.** It is a counting
convention, not missing data. The board groups a sponsor and its platform as one
partner; the tool keeps them as separate rows, because you route a deal to a
platform, not to a fund. Bertram/Ridgeline, O2/Harley, Gauge/Commercial Fire
Protection and Walk On/Blue Fox/Link 1 are each one relationship but several rows.

**Duplicate records** carry a yellow "N records" marker on the card and list the other
dates in the drawer: **Solidaire** (2 Mar and 18 Aug 2026 — merge, or confirm the
second is a new scope) and **Pine Services Group** (16 Oct 2025 and 4 May 2026).

Sort by **Most recent EL** to see the newest signings first. American Landscaping
Partners is the most recent (11 Aug 2026). There is no Engagement filter in the rail —
with 68 of 72 signed, the distinction did not earn a filter; it lives on the card chip
and in the drawer instead.

**Heartland Paving Partners** was approved for an EL on 14 Aug 2026 but has no Closed
(Won) record — either it was never signed or the deal was never moved. The drawer says
so plainly; its research is retained but must not be presented to a seller as a live
mandate. Separately, `Heartland Home Services` (Grand Rapids, MI) is a different
company at a different stage.

**Shore Capital** has no engagement of its own — it appears only as an associated
company on the Skycrest record, so it is not a partner row. The relationship runs
through Skycrest, and the Skycrest drawer notes this.

### HubSpot

Partner records are cross-referenced against the HubSpot company object (portal
3983452), matched on name. **59 of 72** partners are linked, each contributing:

- **Website URL** — shown on the card and in the drawer
- **Open in HubSpot** — deep link straight to the company record
- **CRM vertical and location** — displayed next to ours for comparison
- **LinkedIn page** where the CRM has one

Partners with no matching CRM record show a warning in the drawer. Where the CRM's
`vertical` tag contradicts every vertical we have a partner under, the drawer flags it
— currently **Ruppert Landscape, tagged `HVAC` in HubSpot** when it is a landscaping
business. Worth fixing at the source.

The data is a point-in-time pull, not a live sync — a static file cannot call the
HubSpot API. Re-run the cross-reference when the roster changes.

### Data confidence

Every partner carries a confidence flag, shown on the card and in the drawer:

| Flag | Meaning |
| --- | --- |
| `verified` | Sourced from a press release, sponsor site or trade publication |
| `partial` | Platform confirmed, but geography or buy box is still thin — verify before pitching |
| `unverified` | On the roster, not yet researched. No claims made. |

Currently **46 verified, 13 partial, 13 unverified**. Rows that are still unresearched
are deliberately empty rather than guessed — a wrong state list is worse than a blank
one when someone is sourcing against it.

Six operating companies still have no geography because nothing usable is published:
Solidaire, CenterPoint Companies, Netstock, Ally Services, Blue Fox and Link 1. Blue
Fox and Link 1 are both Walk On Capital portfolio companies, so one call fills three
rows. Sponsors are unmapped by design — they invest through platforms rather than
holding territory.

### Corrections found while filling in geography

Research contradicted four records that were previously wrong. All four are fixed, and
the drawer explains each so nobody re-derives the old answer:

| Partner | Was | Actually |
| --- | --- | --- |
| **APHIX** | IT / MSP | Commercial landscaping and facilities, Lexington KY, backed by Gauge Capital |
| **Clarion** | Private equity group | Operating HVAC/plumbing/electrical platform, Lake Forest IL |
| **Pine Services Group** | Field services holdco | Evergreen's ERP and software vertical (NetSuite, Sage) |
| **Comfort Connect** | HVAC acquirer | A financing and leasing platform, and the CRM places it in Markham, Ontario |
| **SILA Services** | HVAC/plumbing/electrical + water treatment | Plumbing — no water-treatment line. Corrected by the deal team; the `water` vertical is deleted. |

Two earlier flags also resolved: **Zeus** is Zeus Fire & Security (Paoli PA), and
**Nexcore** is Trinity Hunt's commercial HVAC platform, not NexCore Group the
healthcare real estate developer.

### Partners outside the U.S.

Ironclad Group (Ontario), Alphi Capital (Toronto/BC), Baxter's Bakery (Toronto) and
possibly Comfort Connect (Markham) operate outside the U.S. They carry an `intl` field
shown as a blue chip on the card and a dedicated drawer section, and they are excluded
from the coverage map, which is U.S. states only.

### Updating the data

Two options:

1. **Quick, per-person** — write into the Team Note field in the app. Saved to your
   browser only.
2. **Shared** — edit the `PARTNERS` array at the top of the `<script>` block in
   `partners.html` and commit. Each record is a plain object; `states` drives both
   the coverage map and the white-space view, so filling that in is what makes an
   unverified partner useful.

The **Export** button downloads the dataset with everyone's local notes merged in,
which is the easy way to collect research before folding it back into the file.

### Known gaps worth closing first

- **Walk On Capital, Blue Fox, Link 1** — one call fills in three rows.
- **Zeus, Nexcore, Netstock, Baxter's Bakery** — these names collide with well-known
  companies in unrelated markets. Confirm which entity is actually our partner before
  routing any deal.
- Sponsors have no geography by design; they invest through platforms.
