# 0DTE / 1DTE Options Scanner

A same-day / next-day options scanner that ranks **liquid, large-cap, actually-tradable**
names using five independent signal families — off-exchange ("dark pool") activity,
relative volume, short interest, moving-average structure, and near-dated option flow —
then hands you a concrete contract, stop, and targets.

Pure Python standard library. No pip install, no API key required to run it.

```bash
cd options-scanner
./scan.py --demo          # see the output shape with synthetic data, no network
./scan.py                 # real scan, default universe, free Yahoo data
```

---

## Why this exists

Most "scanners" fail in one of two ways: they surface $300M-float garbage whose options
have a 40%-wide bid/ask, or they fire on a single indicator with no idea whether the
trade is actually executable. This one applies **hard gates before it ranks anything**,
and half the score is tradability rather than prediction.

Nothing gets scored until it clears all of these:

| Gate | Default | Why |
|---|---|---|
| Market cap | ≥ $2B | kills the tiny caps |
| Avg dollar volume | ≥ $100M/day | you can get filled in the stock |
| Avg share volume | ≥ 1M/day | ditto |
| Price | $15 – $2000 | below $15 the options are penny-wide junk |
| Near-expiry contract volume | ≥ 3,000 | someone is actually trading this expiry |
| Near-expiry open interest | ≥ 2,000 | there is a book, not a quote |
| ATM bid/ask | ≤ 12% of mid | the spread is not the whole edge |
| Liquid near-money strikes | ≥ 4 | you can pick a strike, not just the ATM |
| DTE | 0 or 1 **trading** sessions | the actual point |

Market cap is not enforced on ETFs (it is meaningless there), and everything is
overridable — `--min-cap 10B --min-dollar-volume 250M` if you only want the giants.

---

## The signals

Each family produces a **direction** (−1 bearish … +1 bullish) and a **quality**
(0…1, "how tradable / how much conviction"). Blocks that have no data are marked
unavailable and drop out of the weighting instead of silently scoring zero.

### 1. Dark pool — FINRA off-exchange volume (free, daily, no key)

FINRA publishes every trade in an NMS stock that printed **off-exchange** — ATSs (true
dark pools) plus wholesaler/internalizer prints. That is the same tape the paid
"dark pool" products resell. Two things come out of it:

- **`offex_share`** — off-exchange volume ÷ consolidated volume. Structurally ~40–50%
  for large caps, so the *level* means little; the scanner z-scores it against its own
  20-day history. A spike means size was worked away from the lit book.
- **`DPI`** — `1 − (off-exchange short volume ÷ off-exchange volume)`. Off-exchange
  "short" volume is dominated by market makers hedging the other side of customer
  **buys**, so a *low* short ratio reads as accumulation. Used as a level, a 5-day
  average, and a z-score.

**Be honest about what this is:** it is an end-of-day file with no trade size, no venue,
and no price. It is a next-morning signal, not an intraday one, and DPI is a proxy, not a
position report. If you want block prints tick-by-tick you need a paid feed
(Unusual Whales, Cheddar Flow, BlackBox). The scanner degrades gracefully without it.

### 2. Volume

Relative volume, projected to a full session. Comparing 10:00 a.m. volume against a
full-day average is the single most common RVOL bug; this uses an empirical intraday
volume curve (front-loaded open, dead midday, closing-auction spike) to project the
day's finish. Plus 20-day dollar volume, 5d-vs-20d volume trend, Chaikin money flow,
OBV slope, and where the close sits in the day's range — a huge-volume day that closes
on its lows is distribution, not demand.

### 3. Short interest

Short interest % of float, days-to-cover, and change vs the prior settlement. Exchange
short interest settles twice a month and publishes with roughly an eight-day lag, so it
is treated as **fuel, not direction**: it can only add to a long (`squeeze_bonus`,
capped at 8 points), never pick the side. The fast-moving cousin is the daily
off-exchange short ratio above.

### 4. Moving averages and trend

An 8/21/50/200 EMA stack scored as four independent alignment checks, 21-EMA slope
measured in ATR-per-day (so it is comparable across a $30 stock and a $900 one), Wilder
ADX/DI, RSI, position in the 20-day range, and today's move in ATR units. Extension is a
**penalty**: chasing something 3 ATR above its 21 EMA is how 0DTE calls die.

### 5. Near-dated option flow — direction *and* the tradability gate

- **Premium skew** — dollars spent on near-money OTM calls vs OTM puts. Premium-weighted,
  not contract-count-weighted, because 10,000 five-cent lottos are not a signal.
- **Volume / open interest** — above 1.0 means new positioning, not closing.
- **Expected move** — for a 0DTE chain the ATM straddle *is* the expected move; the
  scanner compares it to realised ATR, so it can tell you when premium is rich and a
  debit spread beats a naked long.
- **Call wall / put wall** — max-OI strikes, plus a dealer-gamma profile and flip strike
  (measured greeks when the provider has them, an OI-proximity proxy when it does not).
  Headroom to the nearest wall is measured in expected-move units: pinned into a wall
  with less room than the expected move means the move is capped.
- **Unusual activity** — contracts with volume > 3× open interest, ranked by premium.
- **Liquidity** — ATM spread, count of near-money strikes with tight quotes and real OI.

---

## Scoring

```
direction  = weighted mean of block directions   (trend .30, dark pool .25, options .25, volume .20)
quality    = weighted mean of block qualities    (options .50, volume .25, trend .15, dark pool .10)
confluence = share of directional weight that agrees on the side
conviction = |direction| x (0.40 + 0.60 x confluence)
score      = 100 x conviction x (0.35 + 0.65 x quality) + squeeze_bonus
```

Confluence is what stops one loud signal from carrying a name: four blocks that all
lean the same way score far above one block screaming into three that disagree.
A name with `|direction| < 0.08` is marked `NONE` and capped at 25 — no side, no trade.

---

## Output

Every scan writes four things to `out/`:

- **Terminal table** — ranked, with the top reasons and the trade line per name.
- **`latest.html`** — a self-contained dashboard (no scripts, no external requests):
  per-name stat grid, direction/quality bars per block, why it ranked, and the plan.
- **`scan-*.json` / `scan-*.csv`** — every signal value, for your own analysis.
- **`journal.jsonl`** — one line per candidate per scan. This is the point: run the
  scanner daily for a month, then join the journal against actual outcomes and find out
  whether the weights are worth anything **before** you trade them.

Each ranked name gets a plan: contract (targeting ~0.40 delta when greeks are available,
a quarter of the expected move OTM when they are not, and only from strikes with real OI
and a tight quote), entry trigger, underlying stop (the nearer of a half-ATR and the fast
EMA), T1/T2 from the expected move capped at the OI wall, a premium stop, and sizing math.

---

## Usage

```bash
./scan.py                                  # default: 141 names, ≤1DTE, both sides
./scan.py --max-dte 0                      # same-day expiries only
./scan.py --universe movers --side calls    # high-beta names, longs only
./scan.py --symbols NVDA,AMD,SPY,QQQ
./scan.py --min-cap 10B --min-dollar-volume 250M --min-score 55 --top 10
./scan.py --explain NVDA                   # every signal for one name, as JSON
./scan.py --verbose                        # also show what was rejected and why
./scan.py --demo                           # synthetic data, no network
./scan.py --open                           # open the HTML dashboard when done
```

Presets: `./scan.py --list-presets` → `daily`, `core`, `megacap`, `movers`, `leveraged`,
`wide` (default), `everything`. Or `--universe-file my_list.txt`, one symbol per line.

Copy `config.example.json` to `config.json` and pass `--config config.json` to persist
gates and weights.

### Data providers

| Provider | Key | Options data | Notes |
|---|---|---|---|
| `yahoo` *(default)* | none | delayed, no greeks | works today, zero setup; unofficial endpoints that occasionally break |
| `tradier` | `TRADIER_TOKEN` | real OI, greeks, IV, tight quotes | best free-tier chain; set `TRADIER_BASE=https://sandbox.tradier.com` for sandbox |
| `polygon` | `POLYGON_API_KEY` | full snapshots with greeks | paid; cleanest data |

Tradier and Polygon are thin on float and short interest, so they are wrapped to fall
back to Yahoo for fundamentals — you always get a complete record. Adding a source means
implementing `MarketDataProvider` in `odte/providers/`; nothing in the signal or scoring
layers knows which vendor the numbers came from.

---

## A note on "0DTE"

Only index and ETF products (SPY, QQQ, IWM, SPX and friends) list expirations *every*
trading day. Most single stocks expire Friday — so for equities "0DTE" means Friday and
"1DTE" means Thursday. The scanner never assumes: it reads each symbol's actual chain and
keeps whatever really lists a 0/1-DTE contract. DTE is counted in **trading sessions**,
so a Monday expiry is 1DTE from Friday, not 3DTE.

---

## Layout

```
scan.py                    CLI entry point
odte/
  config.py                gates, weights, thresholds
  universe.py              preset symbol lists
  engine.py                fan-out, expiry selection, orchestration
  screen.py                hard gates
  score.py                 composite scoring and confluence
  plan.py                  strike selection, stops, targets, sizing
  report.py                terminal / JSON / CSV / journal / HTML
  indicators.py            EMA, ATR, ADX, RSI, OBV, CMF, z-score (no numpy)
  calendar_utils.py        NYSE calendar, trading-day DTE, session progress
  http.py                  retries, per-host throttle, disk cache
  synthetic.py             deterministic fake data for --demo and tests
  providers/               yahoo, tradier, polygon, finra
  signals/                 trend, volume, darkpool, shortinterest, options_flow
tests/                     28 offline tests, no network needed
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Everything runs against deterministic synthetic data — no network, no keys, no market
hours. Covers indicator math, the NYSE calendar and trading-day DTE, each signal block's
direction and quality behaviour, every hard gate, error isolation (one bad symbol never
kills a scan), target/stop sidedness, and all four renderers.

---

## Limits — read these

- **Everything here is end-of-day or delayed.** FINRA off-exchange data publishes after
  the close. Yahoo option quotes are delayed ~15 minutes. This is a *setup finder* you
  run pre-market or early in the session, not an execution system.
- **The weights are a starting hypothesis, not a backtested edge.** They were chosen to
  be reasonable, not fitted. That is what `journal.jsonl` is for — forward-test before
  you size up.
- **DPI is a proxy.** Off-exchange short volume is mostly market-maker hedging, not
  directional shorting.
- **Gamma exposure is approximate.** Dealer positioning is inferred with the standard
  long-calls / short-puts convention; nobody outside the dealers knows the real book.
- **0DTE options routinely expire worthless**, and a 35% premium stop on a 0DTE contract
  can gap straight through. Size for total loss on every position.

Educational tooling. Nothing here is investment advice or a recommendation.
