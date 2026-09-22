"""0DTE options bias scanner.

Pulls intraday price action plus the same-day option chain for a universe of
index ETFs and heavy-volume single names, then scores each symbol on a
-100 (downside) .. +100 (upside) bias scale with a separate confidence score.

Designed to be run roughly one hour after the cash open (10:30 ET default),
once the opening range is set and 0DTE volume is real.
"""

__version__ = "0.1.0"
