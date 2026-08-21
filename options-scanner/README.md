# 0DTE / 1DTE Options Scanner

A same-day / next-day options scanner that ranks **liquid, large-cap, actually-tradable**
names using five independent signal families — off-exchange ("dark pool") activity,
relative volume, short interest, moving-average structure, and near-dated option flow —
then hands you a concrete contract, stop, and targets.

Pure Python standard library. No pip install, no API key required to run it.

## Press Scan

```bash
cd options-scanner
./serve.py                    # opens http://127.0.0.1:8765 — press SCAN
./serve.py --auto 09:35       # ...and scan automatically at 9:35 ET each trading day
./serve.py --demo             # synthetic data, works any time, no network
```

One button. It pulls live data at that moment — bypassing the cache, so "Scan" means
scan, not "show me what I fetched ten minutes ago" — streams progress while it runs,
and renders the top setups with a full strike ladder and every number behind the
ranking. Settings (universe, expiry, side, how many to show, account size, risk per
trade) persist in the browser. Defaults to the **top 3**.

There is also a plain CLI if you prefer it:

```bash
./scan.py --demo          # see the output shape with synthetic data, no network
./scan.py                 # real scan, default universe, free Yahoo data
./scan.py --doctor        # preflight: is the live data path actually working?
./review.py               # forward-test the journal against what happened next
```

**If a scan comes back empty, run `./scan.py --doctor` before assuming there were no
setups.** "Nothing set up today" and "the data never arrived" produce an identical
empty table, and the preflight is what tells them apart — it probes reachability, price
history, quotes, fundamentals, expiries, chain depth and greeks, and the FINRA feed,
and every failure carries the fix rather than just the error. It is also a button in
the web UI. It finishes in a couple of seconds even when the network is dead.

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
| Near-expiry contract volume | ≥ 3,000 *(session-scaled)* | someone is actually trading this expiry |
| Near-expiry open interest | ≥ 2,000 | there is a book, not a quote |
| ATM bid/ask | ≤ 12% of mid | the spread is not the whole edge |
| Liquid near-money strikes | ≥ 4 | you can pick a strike, not just the ATM |
| DTE | 0 or 1 **trading** sessions | the actual point |

Market cap is not enforced on ETFs (it is meaningless there), and everything is
overridable — `--min-cap 10B --min-dollar-volume 250M` if you only want the giants.

**Gates run in two passes.** Everything that needs no option data — market cap, dollar
volume, share volume, price, ETF and earnings handling — is checked *first*. Fetching an
expiry list, a chain, and intraday bars costs three network round trips per symbol, and
most of a 141-name universe fails on size or liquidity alone, so a name that was never
tradable never costs those calls. On a universe where half the names fail the size gate
this cut option round trips by 35%; on a real `wide` scan it is more. Rejected names are
tagged with the stage they died at (`pre-gate`, `option-gate`, or a score-floor miss),
and the scan result reports how many chains it actually fetched.

**The contract-volume gate is session-aware**, which matters if you are scanning near
the open. Option volume accumulates through the day, so at 9:35 nothing on earth has
traded 3,000 contracts yet — a flat threshold rejects the entire market at exactly the
moment a 0DTE scan is most useful. The requirement is scaled by the share of the
session's volume that has typically printed by that time (about 4% of the target at
9:31, all of it by the close), with a floor so a pre-open scan still demands a pulse
rather than nothing. Open interest, which comes from the prior session, is gated at
full strength throughout. Turn the scaling off with
`"scale_option_volume_by_session": false`.

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

### 4. Intraday — session VWAP and the opening range

The block a daily-bar-only scanner cannot produce, and the one that matters most for a
same-day trade.

- **Session VWAP** — the reference institutional algos are measured against, which on a
  0DTE timeframe makes it the line that decides who is in control: above it dip-buyers
  are defending, below it rallies get sold. Measured three ways — distance from price in
  ATR units, VWAP slope (a rising VWAP means the average fill is improving all session,
  which is real demand rather than one spike that already faded), and what share of the
  session price has spent on the right side of it.
- **Opening range** — the first 30 minutes' high and low, and whether price has broken
  out, broken down, or is still trapped inside.
- **Trend efficiency** — net progress divided by the path walked to get it. A low number
  means the session is grinding sideways while your premium bleeds; it feeds *quality*,
  not direction.
- **Session range vs ATR** — how much of a normal day's move has already happened.

VWAP is not only scored — when it sits between price and the moving average it becomes
the **trade's stop**, because that is the level a same-day trade is actually invalidated
at. Computed from today's regular session only: no pre-market, no carry-over.

Providers that cannot supply intraday bars simply return none, and the block drops out
of the weighting rather than scoring zero. Set `"use_intraday": false` to skip the extra
call per candidate.

### 5. Moving averages and trend

An 8/21/50/200 EMA stack scored as four independent alignment checks, 21-EMA slope
measured in ATR-per-day (so it is comparable across a $30 stock and a $900 one), Wilder
ADX/DI, RSI, position in the 20-day range, and today's move in ATR units. Extension is a
**penalty**: chasing something 3 ATR above its 21 EMA is how 0DTE calls die.

### 6. Near-dated option flow — direction *and* the tradability gate

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
direction  = weighted mean of block directions
             (trend .25, options .25, dark pool .20, intraday .15, volume .15)
quality    = weighted mean of block qualities
             (options .45, volume .20, intraday .15, trend .12, dark pool .08)
confluence = share of directional weight that agrees on the side
conviction = |direction| x (0.40 + 0.60 x confluence)
score      = 100 x conviction x (0.35 + 0.65 x quality) + squeeze_bonus
```

Confluence is what stops one loud signal from carrying a name: five blocks that all
lean the same way score far above one block screaming into four that disagree.
A name with `|direction| < 0.08` is marked `NONE` and capped at 25 — no side, no trade.

---

## The strike ladder

"Which contract" is a risk decision the scanner should not make for you, so every
ranked name comes with three rungs rather than one strike:

| Rung | Target | What it is |
|---|---|---|
| **anchor** | ~0.60 delta, ITM | Costs most, decays least, moves most like the stock. The measured-move trade. |
| **core** | ~0.45 delta, ATM | The default. Best liquidity, balanced payoff. |
| **runner** | ~0.30 delta, OTM | Cheapest, highest percentage upside, needs most of the expected move. The one that goes to zero. |

Rungs only come from strikes with a real two-sided quote and real open interest, and
the three are always distinct. Each carries bid/ask/mid, delta, OI, volume, spread,
cost per contract, breakeven, the percentage move required to reach it, **payoff at
expiry at both targets** (pure intrinsic — for 0DTE that is the honest number, since
every cent of extrinsic value is gone by the close), and a position size from your
account and risk-per-trade settings.

A typical ladder reads like this — the same setup, three risk postures:

```
anchor  297.5C ITM  $5.44  d=0.658  BE 302.94 (+1.16%)   T1 +7%   T2 +38%
core      300C OTM  $3.58  d=0.455  BE 303.58 (+1.37%)   T1 -7%   T2 +40%
runner  302.5C OTM  $3.30  d=0.266  BE 305.80 (+2.12%)   T1 -75%  T2 -24%
```

## Output

Every scan writes four things to `out/`:

- **Terminal table** — ranked, with the top reasons and the trade line per name.
- **`latest.html`** — a self-contained dashboard (no scripts, no external requests):
  per-name stat grid, direction/quality bars per block, why it ranked, and the plan.
- **`scan-*.json` / `scan-*.csv`** — every signal value, for your own analysis.
- **`journal.jsonl`** — one line per candidate per scan, written *before* the outcome is
  known. `./review.py` replays it against what actually happened (see below).

Alongside the ladder, each ranked name gets a plan: entry trigger, underlying stop
(session VWAP when it is the nearer level, otherwise the nearer of a half-ATR and the
fast EMA — and never on the wrong side of spot), T1/T2 from the expected move capped at
the OI wall, a premium stop, and sizing math.

---

## Reviewing the journal

Run the scanner daily for a few weeks, then:

```bash
./review.py                     # summary
./review.py --detail            # ...plus every entry
./review.py --since 2026-08-01  # only recent expiries
./review.py --json              # machine-readable
```

It reports two returns per pick, and **the gap between them is the lesson**:

| | what it measures |
|---|---|
| **managed** | what the plan would have produced traded as written — exit at T1 when T1 was reached, take the premium stop when the stop was hit, otherwise hold to expiry. Exits priced by delta. |
| **expiry** | intrinsic value at the close. The pessimistic bound: an ATM 0DTE long expires worthless unless the stock *closes* through the strike, however far it travelled intraday. |

On a sample run those came out at **57% win rate, +29% median managed** against
**−100% median held to expiry**, on picks whose T1 was reached 70% of the time. That
spread is the single most important thing to internalise about buying 0DTE: being right
about direction and holding to the close are almost unrelated outcomes.

It also breaks results down by score bucket (does a higher score actually pay more?),
by side, by ladder rung (the anchor rung usually has the best managed mean and the
runner the worst), and — the part worth acting on — **by signal block**:

```
block                    agreed              disagreed      edge
volume            n=43 mean -9.9%     n=2 mean -100.0%    +90.1%
darkpool          n=42 mean -9.2%      n=9 mean -26.2%    +17.0%
```

`edge` is the mean return when a block agreed with the traded side minus when it
disagreed. **A block whose edge sits near zero is contributing noise, not signal** —
that is your evidence for reweighting it, and the reason the weights are configurable.

Two limits are enforced rather than papered over: daily bars cannot say whether the
stop or the target was touched first, so sessions that touched both are **excluded**
from the managed numbers instead of being resolved in the flattering direction; and the
T1 exit is priced by delta, which is an estimate, not a fill.

## Usage

### The button

```bash
./serve.py                          # localhost:8765
./serve.py --auto 09:35             # auto-scan just after the open, every trading day
./serve.py --provider tradier       # real-time chains instead of delayed Yahoo
./serve.py --port 9000 --no-browser
./serve.py --demo                   # synthetic data
```

The server binds `127.0.0.1` on purpose. It holds no credentials itself, but whatever
provider key is in your environment is usable by anything that can reach the port, so
only pass `--host 0.0.0.0` if you genuinely mean it.

### The CLI

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
./scan.py --doctor                         # preflight the live data path
./scan.py --doctor --json                  # ...machine-readable
```

Presets: `./scan.py --list-presets` → `daily`, `core`, `megacap`, `movers`, `leveraged`,
`wide` (default), `everything`. Or `--universe-file my_list.txt`, one symbol per line.

Copy `config.example.json` to `config.json` and pass `--config config.json` to persist
gates and weights.

### Data providers

| Provider | Key | Options data | Notes |
|---|---|---|---|
| `yahoo` *(default)* | none | delayed, no greeks | works today, zero setup; unofficial endpoints that occasionally break. Requests try both Yahoo hosts and refresh a stale cookie/crumb once before giving up |
| `tradier` | `TRADIER_TOKEN` | real OI, greeks, IV, tight quotes | best free-tier chain; set `TRADIER_BASE=https://sandbox.tradier.com` for sandbox |
| `polygon` | `POLYGON_API_KEY` | full snapshots with greeks | paid; cleanest data |

All three supply intraday bars for VWAP and the opening range.

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
serve.py                   press-the-button UI (start here)
scan.py                    CLI entry point (also --doctor)
review.py                  forward-test the journal
odte/
  webapp.py                the Scan button: HTTP handlers, progress, auto-scan
  doctor.py                preflight checks with actionable fixes
  review.py                journal replay: managed vs expiry returns, per-block edge
  config.py                gates, weights, thresholds
  universe.py              preset symbol lists
  engine.py                fan-out, expiry selection, orchestration
  screen.py                hard gates, split into a cheap pre-pass and an option pass
  score.py                 composite scoring and confluence
  plan.py                  strike ladder, stops, targets, payoff, sizing
  report.py                terminal / JSON / CSV / journal / HTML
  indicators.py            EMA, ATR, ADX, RSI, OBV, CMF, z-score (no numpy)
  calendar_utils.py        NYSE calendar, trading-day DTE, session progress
  http.py                  retries, per-host throttle, disk cache, circuit breaker
  synthetic.py             deterministic fake data for --demo and tests
  providers/               yahoo, tradier, polygon, finra
  signals/                 trend, intraday, volume, darkpool, shortinterest, options_flow
tests/                     112 offline tests, no network needed
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Everything runs against deterministic synthetic data — no network, no keys, no market
hours. Covers indicator math, the NYSE calendar and trading-day DTE, each signal block's
direction and quality behaviour, every hard gate, error isolation (one bad symbol never
kills a scan), target/stop sidedness, all four renderers, the strike ladder's breakeven
and payoff arithmetic, session-aware volume scaling, cache-freshness behaviour, and the
web app's HTTP endpoints end to end (including malformed and oversized requests), the
preflight doctor against healthy and broken providers, the journal reviewer's level
detection, managed-exit accounting, ambiguity handling and score-bucket aggregation, the
VWAP and opening-range maths, VWAP-based stop selection, and proof that a name failing
the pre-gate never triggers a chain or intraday fetch.

---

## Limits — read these

- **Everything here is end-of-day or delayed.** FINRA off-exchange data publishes after
  the close, so the dark pool read on any given morning describes *yesterday*. Yahoo
  option quotes are delayed ~15 minutes; if you are trading off the button near the
  open, use Tradier or Polygon so the chain is real-time. This is a *setup finder*, not
  an execution system — pressing Scan gives you a ranked shortlist to go look at, not
  an order.
- **The weights are a starting hypothesis, not a backtested edge.** They were chosen to
  be reasonable, not fitted. `./review.py` is how you find out whether they are worth
  anything — forward-test before you size up, and reweight the blocks whose measured
  edge is near zero.
- **DPI is a proxy.** Off-exchange short volume is mostly market-maker hedging, not
  directional shorting.
- **Gamma exposure is approximate.** Dealer positioning is inferred with the standard
  long-calls / short-puts convention; nobody outside the dealers knows the real book.
- **0DTE options routinely expire worthless**, and a 35% premium stop on a 0DTE contract
  can gap straight through. Size for total loss on every position.

Educational tooling. Nothing here is investment advice or a recommendation.
