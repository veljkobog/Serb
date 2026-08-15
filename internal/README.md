# Internal tools

**Not published.** The Pages workflow (`.github/workflows/deploy.yml`) stages only
the public site into `_site/` and fails the build if anything from this directory
leaks in. Files here are served to nobody — open them from a local checkout.

## `partners.html`

Partner Sourcing Desk — the buyer-coverage map for our EL partner roster.
Open the file directly in a browser (`open internal/partners.html`). No build step,
no server, no dependencies, works offline.

### What it does

- **Directory** — click a vertical in the left rail to see every partner buying in
  it, with sponsor, HQ, footprint, states, brands, buy box and how to source for them.
- **Coverage** — a US tile map shaded by how many partners are active in each state
  for the selected vertical. Click a state to list them.
- **White Space** — per vertical, the states where we have no partner (red) or only
  one (yellow). That is the sourcing priority list.
- **Latest news** — every partner has a live Google News button. Nothing is cached,
  so it never goes stale.
- **Team notes** — free-text note per partner, saved in your browser. Notes are
  searchable alongside everything else.

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
