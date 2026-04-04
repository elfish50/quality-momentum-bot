"""
universe.py - Stock Universe for Elliott Wave Scanner

v3: Delegates entirely to strategy.get_universe() (Finviz live screener).

The old TIER1/TIER2 hardcoded lists are removed. They were the root cause
of the scanner always analyzing the same ~219 stocks every run.

get_universe() in strategy.py pulls a fresh batch from Finviz on every scan
using filters that pre-qualify stocks in a price uptrend:
  - Price > SMA20 AND SMA20 > SMA50
  - RSI 50-70 (not overbought, not in freefall)
  - Average volume > 300k
  - Market cap > $300M
  - USA only

All legacy function signatures (get_all_tickers, load_universe,
get_priority_tickers) are preserved so nothing else in your bot breaks.
They all now return the live Finviz list instead of the old static one.
"""

from strategy import get_universe


def get_all_tickers(include_dynamic=True):
    """
    Returns a fresh live universe from Finviz.
    `include_dynamic` kept for backward compatibility — has no effect.
    """
    return get_universe()


def load_universe():
    """
    Legacy interface — returns dict with ALL key for scanner.py compatibility.
    Now backed by Finviz instead of the old static TIER1/TIER2 lists.
    """
    return {"ALL": get_universe()}


def get_priority_tickers(universe=None):
    """
    Legacy interface — previously returned TIER1 large-caps as priority.
    Now returns the full Finviz universe (already pre-filtered and shuffled).
    If a universe list is passed in, it is returned as-is.
    """
    if universe:
        return universe
    return get_universe()
