"""0DTE options bias scanner.

Pulls intraday price action plus the same-day option chain for a universe of
index ETFs and heavy-volume single names, then scores each symbol on a
-100 (downside) .. +100 (upside) bias scale with a separate confidence score.

Designed to be run roughly one hour after the cash open (10:30 ET default),
once the opening range is set and 0DTE volume is real.

Data comes from Tradier: real-time quotes and chains over REST, plus streamed
time & sales classified into buyer- and seller-initiated volume, which is what
turns "flow" from a positioning proxy into actual directional pressure.
"""

__version__ = "0.2.0"
