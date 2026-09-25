# 0DTE bias scanner

One button. Press it an hour after the open and every scanned symbol comes back
as **BULL 8/10** or **BEAR 5/10**, with the market-structure setups that earned
the score printed underneath. Press it again later and each read is graded
against the first one: CONFIRMED, HOLDING, FADED or FLIPPED.

```bash
./bullbear setup    # once: paste your Tradier token, it verifies itself
./bullbear live     # every morning: records, reads, re-confirms, all day
```

That is the whole thing. `live` records classified trade side from the moment
you start it, prints the opening read at 10:05 ET, and re-confirms every 45
minutes until you stop it. Everything else is optional:

```bash
./bullbear          # one press, right now
./bullbear web      # the same button in a browser tab
./bullbear grade    # what past reads were actually worth
./bullbear doctor   # check the live wiring end to end
./bullbear demo     # fake data, any time, no token needed
```

```
0DTE OPENING READ ==================================================== 10:31:04 ET
  tradier:production  |  329 min to the close  |  5 symbols shown

-- SPY ------------------------------------------------------- BULL 8/10  STRONG
   589.88  +0.83%   bias +53   conf 75
   + Short-gamma breakout
       net GEX -333mm, above OR high 588.24, +1.8s over VWAP
   + Pressing the call wall
       spot 589.88 into call wall 590.00, flow +0.22
   levels  VWAP 586.72  OR 584.19-588.24  max pain 590.00  flip 587.34  EM +/-1.16
   chain   ATM IV 9.8%  net GEX -333.1mm/1%  P/C vol 0.33  classified +28.2mm net delta (72%)
   trade   calls  ATM 590.00 / 1EM 595.00  target 591.04  invalidate 586.72
```

Under the rating sits the full engine: relative strength, VWAP and opening-range
position, implied vol, skew, open interest, dealer gamma structure, and — via
streamed, classified time & sales — **who was the aggressor on every print**.
`./bullbear scan` shows all of it as one row per symbol.

Data comes from **Tradier**: real-time REST quotes and chains with the broker's
own greeks, plus an HTTP event stream for time & sales. Stdlib only — no
`requests`, no websocket library.

## Install

Nothing to install — the scanner runs on the Python 3.11 standard library.
Point it at a Tradier token:

```bash
export TRADIER_TOKEN=...          # brokerage token, market data enabled
export TRADIER_ENV=production     # or: sandbox (delayed, good for wiring)
```

`production` with a funded brokerage account gives real-time quotes and the
streaming endpoint. Sandbox tokens return delayed data and are flagged
`sandbox-delayed` in the output, so you never mistake one for the other.

(The optional Yahoo fallback — `--provider yahoo` — needs
`pip install -r requirements.txt`.)

## Going live

**1. Get a Tradier token.** tradier.com → dashboard → **API Access**. A sandbox
token works in two minutes and returns delayed data; a **production** token
needs a funded brokerage account with the market-data agreements accepted, and
that is what gives you real-time quotes and the streaming endpoint the trade-side
classification depends on.

**2. Run setup, once:**

```bash
./bullbear setup
```

It asks for the token (hidden input), asks production or sandbox, writes
`scanner/.env` with owner-only permissions (gitignored, never committed), and
then immediately runs the doctor against it:

```
  [ ok ] token           44-char token, env=production
  [ ok ] auth            market is open (Market is open from 09:30 to 16:00)
  [ ok ] quotes          SPY 589.88 (589.87/589.89), last print 0 min ago
  [ ok ] 0DTE expiry     2026-09-23 is listed for SPY
  [ ok ] chain           348 contracts, 348 with greeks, 291 traded today
  [ ok ] stream session  a1b2c3d4... issued

  You're set. Tomorrow morning, one command:

      ./bullbear live
```

Every command loads `.env` on startup, so nothing depends on remembering to
export anything. A real environment variable always wins over the file.

**3. Run the day:**

```bash
./bullbear live
```

```
live session  |  8 press(es): 10:05, 10:50, 11:35, 12:20, 13:05, 13:50, 14:35, 15:20
  recording trade side on 320 contracts across 8 symbols
  ... 10:05 ET in 38m12s
```

Start it any time — before the open to catch the full tape, or at 13:10, in
which case the first press happens immediately. Ctrl-C stops it and saves the
side tape. Knobs, if you want them:

```bash
./bullbear live --first 10:30 --every 30 --until 14:00   # different rhythm
./bullbear live --presses 3                              # three reads, then stop
./bullbear live --no-record                              # skip the trade-side stream
./bullbear live --universe index                         # SPY/QQQ/IWM/DIA only
```

**4. First week, don't size on it.** The cards and every metric behind them land
in `out/` on each press (`latest.json`, `latest.csv`, `latest.html`, plus the
press history in `out/runs/`). Then let the data argue:

```bash
./bullbear grade
```

```
0DTE READ GRADE  |  5 session(s): 2026-09-28 ... 2026-10-02
  140 graded reads, held 172 min on average, settled against each session's last press

BY BAND
              reads   hit  1 EM   mean    med  worst   best
  STRONG         31   68%   39%  +0.61  +0.44  -1.80  +2.90
  MODERATE       44   52%   20%  +0.09  +0.05  -2.10  +2.10
  LEAN           38   47%   16%  -0.04  -0.02  -1.90  +1.70
  STAND DOWN     27   44%   11%  -0.12  -0.08  -2.40  +1.20
```

Every press is graded against the last press of its session: how far price went
**in the rated direction**, in expected moves. Cut by band, by setup and by
symbol, with `--csv` for the per-read detail. That is how the setup strengths in
`odte/setups.py` stop being priors — if STRONG doesn't separate from LEAN after
a month, the weights are wrong and the grader will say so.

## Run

`./bullbear live` covers a normal day. These are the pieces it is built from,
for when you want one of them on its own (`./bullbear` alone is `go`, one press):

```bash
./bullbear                       # the read, top 5 cards
./bullbear --top 8               # more cards
./bullbear --universe index      # index ETFs only
./bullbear --fresh               # re-baseline: this press becomes read #1
./bullbear demo                  # fake data, any time, no token
```

Everything lands in `out/`: `latest.json`, `latest.csv`, `latest.html`, and the
day's press history in `out/runs/`.

The full board, when you want every metric rather than a verdict:

```bash
./bullbear scan --csv out/scan.csv --html out/scan.html
./bullbear scan --only-tradeable --top 3
```

Both refuse to run outside **10:00-11:30 ET** unless you pass `--force`: before
10:00 the opening range isn't set and 0DTE volume is still noise.

Replay, for tuning after the close:

```bash
./bullbear scan --save-snapshot out/2026-09-22.json
./bullbear scan --provider snapshot --snapshot-path out/2026-09-22.json --as-of 2026-09-22T10:30:00
```

### Recording trade side from the open

Exchange volume carries no buy/sell label, and it cannot be reconstructed after
the fact — classification needs the bid and ask standing at each print, which
only the live stream carries. `./bullbear live` does this for you — it starts the stream the moment you launch
it. To run the recorder on its own instead:

```bash
# 09:30 — stream and classify 0DTE prints until 10:30
./bullbear record

# 10:00+ — the button picks up out/sides-<today>.json automatically
./bullbear
```

The recorder saves every 30 seconds, so a dropped connection costs you the last
half-minute, not the session. `--append` folds a new run into an existing tape.
No tape and no `--stream-seconds`? `flow` falls back to the unsigned proxy and
every card carries the `No trade side` blocker.

## The rating

Bias says which way and how hard. Confidence says how much the inputs can be
trusted. Setups say whether a known structure is behind it. The rating folds all
three into one number:

```
score = (bias points + aligned setups - opposing setups - blockers) x confidence haircut
```

Bias alone tops out at **7/10** — the last three points have to be earned by
structure. A blocker (a pin, an exhausted move, an illiquid chain) drags the
score down no matter how clean the tape looks. The confidence haircut floors at
0.55, so a strong setup on mediocre data still beats nothing.

| Score | Band |
|---|---|
| 8.5-10 | MAX CONVICTION |
| 6.5-8.4 | STRONG |
| 4.5-6.4 | MODERATE |
| 3.0-4.4 | LEAN |
| under 3 | STAND DOWN |

## The setups

Each one is an explicit rule over the tape and the chain, so a rating can always
be explained as "these four things were true at 10:31." `+` drives the card's
side, `-` argues against it, `!` blocks it outright.

| Setup | Side | Pts | Fires when |
|---|---|---|---|
| Short-gamma breakout / breakdown | bull / bear | 1.5 | Net GEX negative, opening range broken, >0.5σ past VWAP, flow not fighting it. Dealers short gamma hedge *with* the move, so breaks extend instead of fading. |
| Pressing the call / put wall | bull / bear | 1.2 | Spot within 0.4 EM of the heaviest call- or put-gamma strike with classified flow pushing the same way. Through the wall, hedging flips from damping to chasing. |
| Gap and go / gap fade | bull / bear | 1.2 | Gap over 0.2% held and the opening range taken — or gapped up, filled, and red on the day. |
| Max-pain magnet above / below | bull / bear | 1.0 | Long-gamma tape with max pain more than half an expected move away. Pinning flow drifts toward the strike that expires the most OI worthless. |
| VWAP reclaim / rejection | bull / bear | 1.0 | Price on one side of VWAP after spending the session on the other. |
| Put unwind / call unwind | bull / bear | 1.0 | Classified flow shows customers *writing* puts (bullish) or *writing* calls (bearish) — dealers re-hedge as those positions come off. Needs a side tape. |
| Pinned | blocker | 1.5 | Long gamma, sitting on the gamma wall, near max pain. Range day: both sides bleed theta. |
| Move already used | blocker | 1.0 | More than 1.2x the 14-day average daily range is already spent. |
| Flow disagrees with tape | blocker | 1.0 | Price going one way, classified option flow the other. |
| No trade side | blocker | 0.5 | Flow is the unsigned proxy: nothing confirms who was the aggressor. |
| Illiquid chain | blocker | 2.0 | Thin 0DTE volume or wide ATM spreads — the entry costs more than the edge. |

**On "backtested":** these encode widely documented 0DTE market-structure
behavior, and they are *priors, not parameters fitted to a backtest* — none has
been backtested in this repo. Strengths are deliberately coarse for that reason.
The snapshot and side-tape replay path exists precisely so you can validate and
retune them on your own tape before leaning on them. Treat any rating as a
starting point for your read, not a substitute for it.

## Pressing again

Every press appends to `out/runs/runs-<date>.json`. The second press onward
grades each symbol against **the first read of the day**:

| Status | Means |
|---|---|
| CONFIRMED | Same side, score held, and price has moved at least 0.15 EM your way |
| HOLDING | Same side, nothing has happened yet |
| FADED | Score bled 1.5+ points, price gave back 0.35 EM, or classified flow flipped under it |
| FLIPPED | The read changed sides |
| NEW | Wasn't in the first read |

The card shows the status, the point change, how far price has travelled in
expected moves, and how long ago the first read was. `--fresh` starts a new
chain if you want to re-baseline mid-session.

## Grading the reads

```bash
./bullbear grade                       # every session recorded so far
./bullbear grade --date 2026-09-25     # one day
./bullbear grade --csv out/grades.csv  # per-read detail
```

For each press except a session's last, the grader computes

```
progress = (final spot - spot at the read) / expected move,  signed by the rated side
```

A **hit** is progress above zero; **1 EM** means the read paid a full expected
move, which for a 0DTE entry is roughly the line between a scratch and a real
winner. Results are bucketed by rating band, by setup key and by symbol.

Two honest limits. It settles against the session's last press, not against an
entry and exit you actually took — no slippage, no theta, no stop. And small
samples lie: a band or setup needs 30+ reads before its numbers mean anything.
It is evidence for retuning weights, not a P&L statement.

## The browser button

```bash
./bullbear web            # http://127.0.0.1:8787, opens a tab
```

One page, one big button, cards with a 0-10 meter per symbol. It binds loopback
only on purpose: the process holds a brokerage token, and the page will run
scans for anyone who can reach it. Bull/bear is drawn as a blue/red diverging
pair rather than green/red — that is the one pair colorblind readers cannot
separate — and every card states the side in words with an arrow, so color never
carries it alone.

## How the score is built

The rating sits on top of the composite bias, which is built from ten
components, each mapped to [-1, +1] where negative is downside, then a
weighted sum scaled to -100..+100. Weights live in `odte/config.py:Weights`.

| Component | Weight | What it measures |
|---|---|---|
| `rs` | 0.17 | Beta-adjusted return vs the benchmark (SPY), standardized across the universe |
| `vwap` | 0.11 | Sigmas from session VWAP, plus VWAP slope |
| `orb` | 0.08 | Position vs the first 30 minutes' range (+1 = at the high) |
| `persistence` | 0.06 | Share of bars closing on the right side of VWAP, plus close location in range |
| `skew` | 0.13 | 25-delta risk reversal (call IV − put IV), standardized across the universe |
| `flow` | 0.16 | Net delta the customer side lifted, from classified prints (falls back to a delta-weighted volume proxy) |
| `pcr` | 0.07 | Put/call volume ratio, log-scaled around 1.0 |
| `oi_tilt` | 0.05 | Call OI above spot vs put OI below spot |
| `gamma_pull` | 0.11 | Distance from spot to max pain, in expected moves — discounted when dealers are short gamma |
| `stretch` | 0.06 | Counter-trend brake once the move has used up a normal day's range |

Three components (`rs`, `skew`, `oi_tilt`) are standardized **across the
scanned universe** rather than against fixed thresholds. Equity skew is
structurally put-bid and OI is structurally call-heavy above spot, so the
tradeable information is which names are unusual today, not the raw sign.
Scanning a single symbol falls back to absolute scaling.

Components with missing data are dropped and the remaining weights are
renormalized, so a symbol with no chain still gets a tape-only read (flagged
`partial-data`) on the same -100..100 scale.

### Flow: classified vs proxy

Each streamed print is labelled buyer- or seller-initiated against the quote
standing at the time (Lee-Ready): lifting the offer is buyer-initiated, hitting
the bid is seller-initiated, prints inside the spread go to the midpoint
comparison and then a tick test.

Signing each print by the contract's own delta collapses all four cases into
one number, since put deltas are already negative:

| Customer action | Delta sign | Reads |
|---|---|---|
| Buys calls | `+vol × +delta` | bullish |
| Sells calls | `−vol × +delta` | bearish |
| Buys puts | `+vol × −delta` | bearish |
| Sells puts | `−vol × −delta` | bullish |

`flow_tilt` is net delta over gross delta, in [-1, +1], and the board shows the
dollar figure (`NETD$`). Classified flow only overrides the proxy once it
covers ≥5% of the day's traded volume in the measured strikes; below that the
sample is too small to outvote it. The detail block always says which one you
are looking at, plus the sampled share.

This is the one thing the free feeds cannot do. Yahoo can tell you 40,000 calls
traded; only the stream tells you whether customers were buying or writing them.

### Confidence and gates

Confidence is deliberately separate from bias: `32%` chain liquidity (traded
0DTE volume vs the gate, penalized for wide ATM spreads), `27%` agreement
between components, `18%` freshness (today's volume vs standing OI), `13%` data
coverage, `10%` trade-side coverage — then discounted for `pin-risk` and
`stale-oi`.

Flags you'll see: `thin-chain`, `wide-spreads`, `thin-tape`, `stale-oi`,
`pin-risk`, `partial-data`, `proxy-flow`, `no-chain`, `no-0dte`,
`sandbox-delayed`.

Verdicts: `LONG (calls)` / `LEAN LONG` / `NO TRADE` / `LEAN SHORT` /
`SHORT (puts)` / `AVOID (illiquid)`. Thresholds are `--min-bias` (default 25)
and `--min-confidence` (default 45); anything illiquid is an automatic AVOID.

### Levels in the detail block

* **Expected move (EM)** — the ATM straddle. Under a normal distribution the
  ATM straddle *is* the expected absolute move (≈0.8σ), which is the ± band
  0DTE desks quote for the rest of the session.
* **Max pain** — strike that expires the most open interest worthless.
* **Gamma wall** — strike holding the most total gamma; acts as a magnet.
* **Gamma flip** — spot level where chain-wide net gamma changes sign, computed
  by re-pricing the surface at each strike. Above it dealers are long gamma
  (moves get pinned), below it short gamma (moves get chased).
* **Net GEX** — signed gamma exposure per 1% move, calls positive and puts
  negative. A proxy: real dealer positioning is not public. Read the sign and
  the levels, not the size.
* **Flow line** — either `classified` (net delta, net premium, how many of the
  day's contracts carried a side, and the resulting tilt) or `proxy only`, with
  the delta-weighted call and put premium it fell back to.
* **Plan** — ATM strike, the strike one expected move out, an EM-based target,
  and the invalidation level (VWAP, or the far side of the opening range).

## Data

**Tradier** (default). REST for 5-minute time & sales, quotes, daily history and
the 0DTE chain with greeks (`delta`, `gamma`, `mid_iv`) — the broker's greeks
are used as given, and only re-derived from the mid when a field is missing or
nonsense. Streaming for classified time & sales. Rate limiting and 429/5xx
backoff are built into the client.

Still true of every feed, Tradier included:

* open interest is the prior session's official figure, so `oi_tilt`, max pain
  and gamma levels describe positioning as of this morning, not this minute;
* trade side exists only for the window you actually streamed — start the
  recorder at 09:30 if you want the full hour;
* ETFs stand in for the indices. Tradier quotes cash-settled SPX/NDX chains too;
  add them to the universe in `odte/config.py` if you trade them.

**Yahoo** (`--provider yahoo`, needs `requirements.txt`) stays as a no-account
fallback: delayed ~15 minutes, no trade side, occasionally zero IV. Fine for
wiring and weekend poking, not for sizing.

Other feeds (IBKR, Polygon, dxFeed) plug in the same way: implement `Provider`
in `odte/providers/base.py` — one method, `snapshot(symbol, now) ->
SymbolSnapshot` — and register it in `odte/providers/__init__.py:get_provider`.
Set `OptionQuote.occ`, `.delta` and `.gamma` if your feed has them, and fill
`.buy_volume` / `.sell_volume` (or write a `SideTape`) if it carries trade side.

## Tuning

`--save-snapshot` writes the exact raw pull. Replay it with
`--provider snapshot` after the close, change weights in `odte/config.py`, and
re-score the same data to see what the change would have done. Keep a week of
snapshots before trusting any weight you changed.

## Tests

```bash
cd scanner && python -m unittest discover -s tests -v
```

143 tests, stdlib-only, no network: the Tradier client is exercised through
recorded response shapes and a fake client, the browser button through a real
loopback server, and the scoring engine through deterministic synthetic data.

---

**Educational tool, not investment advice.** 0DTE options routinely lose 100%
of premium intraday. A bias score is a starting point for your own read, not a
signal to size into.
