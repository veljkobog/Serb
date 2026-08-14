# What's My Business Worth? — Home Services EBITDA Valuation Calculator

A single-page, blue-collar-friendly EBITDA valuation calculator for home services
businesses (HVAC, plumbing, electrical, roofing, landscaping, cleaning, pest control,
and more). Owners plug in their numbers and get a plain-English ballpark of what their
company could sell for.

## What it does

- **Pick your trade** — sets a starting valuation multiple based on typical home-services deals.
- **Enter your numbers** — annual revenue, operating expenses, owner's pay/perks (add-back), and one-time costs (add-back).
- **Tune the multiple** — a slider with trade-specific guidance.
- **See the result live** — estimated sale value with a low–high range, EBITDA, adjusted EBITDA, and EBITDA margin.
- Plain-English explainers on EBITDA, add-backs, and what moves the number.
- Save / print the result, mobile-friendly, no tracking, no backend.

Everything runs client-side in a single `index.html` — no build step, no dependencies.

## How the math works

```
EBITDA          = Revenue − Operating Expenses
Adjusted EBITDA = EBITDA + Owner's Pay & Perks + One-Time Costs
Estimated Value = Adjusted EBITDA × Multiple   (shown as a ±0.5× range)
```

Multiples are general home-services benchmarks (roughly 2.5×–5×, higher for
recurring-revenue trades) and are for education only — not a formal appraisal.

## Running locally

Just open `index.html` in a browser, or serve the folder:

```bash
python3 -m http.server 8000
# then visit http://localhost:8000
```

## Deployment

Deployed to **GitHub Pages** via GitHub Actions
([`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)). The workflow
enables Pages automatically and publishes the site on each push.

> Educational estimate only — not financial, tax, or legal advice.
