# Internal tools

**Not published.** The Pages workflow (`.github/workflows/deploy.yml`) stages only
the public site into `_site/` and fails the build if anything from this directory
leaks in. Files here are served to nobody — open them from a local checkout.

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
  so it never goes stale.
- **Team notes** — free-text note per partner, saved in your browser. Notes are
  searchable alongside everything else.

### The vertical dropdown

Verticals are ordered by how many partners cover them and split into three groups:

- **Covered (3+ partners)** — Private Equity Group (20), HVAC (16), Roofing (16),
  Plumbing (10), Electrical (8), IT (8), Landscaping (7), Exterior Services (6),
  Commercial MEP (4), Commercial Facilities (3), Fire Protection (3).
- **Thin coverage (under 3)** — Restoration Services, Windows, Asphalt, Cybersecurity,
  Janitorial, Tree Care Services, Water. One buyer means no competitive tension; treat
  a lead in these as a relationship call, not a process.
- **Not yet classified** — partners still awaiting research.

The threshold is the `COVERAGE_THRESHOLD` constant at the top of the script.

Multi-trade platforms are cross-referenced, not bucketed: Apex appears under HVAC,
Plumbing *and* Electrical, so picking any one trade shows every buyer who will take
that deal. Sponsors carry the verticals their platforms operate in, which is why
Alpine shows up under HVAC, Commercial MEP and IT.

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

As of the initial build: **31 verified, 14 partial, 27 unverified.** The unverified
rows are deliberately empty rather than guessed — a wrong state list is worse than a
blank one when someone is sourcing against it.

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
