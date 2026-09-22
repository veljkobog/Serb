# 0DTE bias scanner

Scans same-day-expiry options on index ETFs and heavy-volume single names, one
hour after the cash open, and scores each symbol on a **-100 (downside) to +100
(upside)** bias scale with a separate **0-100 confidence** score.

It combines the tape (relative strength, VWAP, opening range) with the 0DTE
chain (implied vol, skew, volume, open interest, gamma structure) so the bias
is not just "what moved" but "what moved, with option positioning behind it."

```
0DTE BIAS SCAN  |  2026-09-22 10:30:00 EDT  |  provider=synthetic  |  330 min to close
SYM      BIAS  CONF  VERDICT              LAST     %PC     RS   VWZ   ORB  ATMIV    EM%   RR25   P/C  V/OI  FLAGS
SPY     +43.3    71  LEAN LONG          589.88   +0.83  +0.00  +1.8  +1.8    9.8   0.20   +0.0  0.64  0.23
IWM     -42.8    71  LEAN SHORT         220.31   -0.76  -0.76  -2.1  -1.8   15.0   0.32   +0.1  6.11  0.23
NVDA    +45.6    58  AVOID (illiquid)   182.38   +2.46  +2.19  +0.9  +0.9   33.8   0.68   -0.1  0.29  0.26  wide-spreads
QQQ     +44.5    49  LEAN LONG          510.30   +1.05  +0.98  +2.8  +3.4   12.8   0.26   +0.0  0.35  0.25  pin-risk
```

## Install

```bash
cd scanner
pip install -r requirements.txt   # yfinance + pandas, for the free data feed
```

## Run

```bash
# full universe, one hour after the open
python -m odte

# index ETFs only, with CSV + HTML output
python -m odte --universe index --csv out/scan.csv --html out/scan.html

# just the setups that clear the gates, strongest three
python -m odte --only-tradeable --top 3

# save the raw pull, then re-score it later without touching the network
python -m odte --save-snapshot out/2026-09-22.json
python -m odte --provider snapshot --snapshot-path out/2026-09-22.json --as-of 2026-09-22T10:30:00

# see it work outside market hours (deterministic fake data)
python -m odte --provider synthetic --as-of 2026-09-22T10:30:00
```

The scan refuses to run outside **10:00-11:30 ET** unless you pass `--force`:
before 10:00 the opening range isn't set and 0DTE volume is still noise.

Cron it for 10:30 ET on weekdays:

```cron
30 10 * * 1-5 cd /path/to/scanner && python -m odte --csv "out/$(date +%F).csv" --html "out/$(date +%F).html"
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
| `flow` | 0.16 | Delta- and premium-weighted 0DTE volume, calls vs puts |
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

### Confidence and gates

Confidence is deliberately separate from bias: `35%` chain liquidity (traded
0DTE volume vs the gate, penalized for wide ATM spreads), `30%` agreement
between components, `20%` freshness (today's volume vs standing OI), `15%` data
coverage — then discounted for `pin-risk` and `stale-oi`.

Flags you'll see: `thin-chain`, `wide-spreads`, `thin-tape`, `stale-oi`,
`pin-risk`, `partial-data`, `no-chain`, `no-0dte`.

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
* **Plan** — ATM strike, the strike one expected move out, an EM-based target,
  and the invalidation level (VWAP, or the far side of the opening range).

## Data

Default provider is Yahoo via `yfinance` — free, no key, carries 0DTE strikes,
volume, OI and an IV field. Know the limits before trusting a number:

* quotes are delayed ~15 minutes on many symbols;
* open interest is the prior session's official figure (true of every feed);
* the IV field is sometimes zero or stale, so it is re-solved from the mid;
* volume carries no trade side, so `flow` is a *positioning proxy*, not a
  read on whether contracts were bought or sold;
* ETFs stand in for the indices. For cash-settled SPX/NDX/RUT, point a broker
  feed at it instead.

To swap in Tradier, IBKR, Polygon or a broker feed, implement `Provider` in
`odte/providers/base.py` (one method, `snapshot(symbol, now) -> SymbolSnapshot`)
and register it in `odte/providers/__init__.py:get_provider`.

## Tuning

`--save-snapshot` writes the exact raw pull. Replay it with
`--provider snapshot` after the close, change weights in `odte/config.py`, and
re-score the same data to see what the change would have done. Keep a week of
snapshots before trusting any weight you changed.

## Tests

```bash
cd scanner && python -m unittest discover -s tests -v
```

No third-party dependency: the scoring engine and its tests are stdlib-only
(`pandas`/`yfinance` are confined to the Yahoo provider).

---

**Educational tool, not investment advice.** 0DTE options routinely lose 100%
of premium intraday. A bias score is a starting point for your own read, not a
signal to size into.
