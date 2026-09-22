# 0DTE bias scanner

Scans same-day-expiry options on index ETFs and heavy-volume single names, one
hour after the cash open, and scores each symbol on a **-100 (downside) to +100
(upside)** bias scale with a separate **0-100 confidence** score.

It combines the tape (relative strength, VWAP, opening range) with the 0DTE
chain (implied vol, skew, volume, open interest, gamma structure) and — via
streamed, classified time & sales — **who was the aggressor on every print**,
so the bias is not just "what moved" but "what moved, with real buying behind
it."

Data comes from **Tradier**: real-time REST quotes and chains with the broker's
own greeks, plus an HTTP event stream for time & sales. Stdlib only — no
`requests`, no websocket library.

```
0DTE BIAS SCAN  |  2026-09-22 10:30:00 EDT  |  provider=synthetic  |  330 min to close
======================================================================================================================
SYM      BIAS  CONF  VERDICT              LAST     %PC     RS   VWZ   ORB  ATMIV    EM%   RR25   P/C  V/OI    NETD$  FLAGS
----------------------------------------------------------------------------------------------------------------------
IWM     -50.4    77  LEAN SHORT         220.31   -0.76  -0.76  -2.1  -1.8   15.0   0.32   +0.1  3.36  0.27   -8.1mm  synthetic
SPY     +51.9    75  LEAN LONG          589.88   +0.83  +0.00  +1.8  +1.8    9.8   0.20   +0.0  0.33  0.26  +28.2mm  synthetic
QQQ     +42.2    69  LEAN LONG          510.30   +1.05  +0.98  +2.8  +3.4   12.8   0.26   +0.0  0.36  0.26  +34.3mm  synthetic
DIA      +8.5    71  NO TRADE           429.47   -0.12  -0.41  -1.4  -1.1    9.0   0.21   +0.1  0.70  0.26  +14.8mm  synthetic
```

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

## Run

```bash
# full universe, one hour after the open
python -m odte

# index ETFs only, with CSV + HTML output
python -m odte --universe index --csv out/scan.csv --html out/scan.html

# just the setups that clear the gates, strongest three
python -m odte --only-tradeable --top 3

# sample classified trade flow for 90 seconds, then score
python -m odte --stream-seconds 90

# save the raw pull, then re-score it later without touching the network
python -m odte --save-snapshot out/2026-09-22.json
python -m odte --provider snapshot --snapshot-path out/2026-09-22.json --as-of 2026-09-22T10:30:00

# see it work outside market hours (deterministic fake data)
python -m odte --provider synthetic --as-of 2026-09-22T10:30:00
```

The scan refuses to run outside **10:00-11:30 ET** unless you pass `--force`:
before 10:00 the opening range isn't set and 0DTE volume is still noise.

### Recording trade side from the open

Exchange volume carries no buy/sell label, and it cannot be reconstructed after
the fact — classification needs the bid and ask standing at each print, which
only the live stream carries. So record from the open and scan against it:

```bash
# 09:30 — stream and classify 0DTE prints until 10:30
python -m odte record --until 10:30 --out out/sides-$(date +%F).json

# 10:30 — score the session with the full hour of classified flow
python -m odte --sides out/sides-$(date +%F).json
```

The recorder saves every 30 seconds, so a dropped connection costs you the last
half-minute, not the session. `--append` folds a new run into an existing tape.
Without a tape, `flow` falls back to the unsigned proxy and every row is
flagged `proxy-flow`.

Cron both on weekdays:

```cron
30 9  * * 1-5 cd /path/to/scanner && python -m odte record --until 10:30 --out "out/sides-$(date +\%F).json"
30 10 * * 1-5 cd /path/to/scanner && python -m odte --sides "out/sides-$(date +\%F).json" --csv "out/$(date +\%F).csv" --html "out/$(date +\%F).html"
```

## How the score is built

Ten components, each mapped to [-1, +1] where negative is downside, then a
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

77 tests, stdlib-only, no network: the Tradier client is exercised through
recorded response shapes and a fake client, and the scoring engine through
deterministic synthetic data.

---

**Educational tool, not investment advice.** 0DTE options routinely lose 100%
of premium intraday. A bias score is a starting point for your own read, not a
signal to size into.
