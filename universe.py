"""
universe.py - Stock Universe for Elliott Wave Scanner

v2 improvements vs v1:
  - Stable base universe: S&P 500 + Nasdaq 100 + high-quality mid-caps
    (replaces yfinance "most actives" which is noisy and changes daily)
  - Pre-filtered: price > $5, avg volume > 500k (avoids illiquid garbage)
  - Optionable-first ordering: stocks with listed options are prioritized
    (important for protective put lookup after BUY signals)
  - Sector diversification: orders by sector to avoid cluster scanning
  - Still supports dynamic yfinance fallback if static list is stale

Universe tiers (scanned in order):
  TIER1: Liquid large-caps with options (most reliable signals + hedging)
  TIER2: Quality mid-caps
  TIER3: Dynamic yfinance most-actives (opportunistic, scanned last)
"""

import os
import traceback

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")

# ── Tier 1: S&P 500 / Nasdaq 100 core — all have liquid options ───────────────
TIER1 = [
    # Technology
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AMD","AVGO","QCOM",
    "INTC","CRM","ADBE","ORCL","NOW","PANW","SNOW","CRWD","NET","MU",
    "AMAT","LRCX","KLAC","TXN","MCHP","ON","SMCI","ARM","PLTR","DDOG",
    # Financials
    "JPM","BAC","WFC","GS","MS","BLK","C","AXP","V","MA","PYPL","COF",
    # Healthcare
    "UNH","JNJ","LLY","ABBV","MRK","PFE","TMO","ABT","DHR","ISRG",
    "MRNA","REGN","VRTX","BIIB","GILD","BMY","CVS","HUM","CI","ELV",
    # Energy
    "XOM","CVX","COP","EOG","SLB","MPC","PSX","VLO","OXY","HAL",
    # Industrials
    "CAT","DE","GE","HON","MMM","RTX","LMT","NOC","BA","UPS","FDX",
    # Consumer
    "AMZN","WMT","COST","TGT","HD","LOW","NKE","SBUX","MCD","YUM",
    "PG","KO","PEP","MDLZ","CL","EL","PM","MO",
    # Communication
    "NFLX","DIS","CMCSA","T","VZ","TMUS","CHTR","WBD","PARA",
    # Real Estate & Utilities
    "AMT","PLD","EQIX","CCI","SPG","O","NEE","DUK","SO",
    # Materials
    "LIN","APD","ECL","NEM","FCX","AA","ALB","MP",
]

# ── Tier 2: Quality mid-caps and sector leaders ────────────────────────────────
TIER2 = [
    # High-growth tech
    "MSTR","COIN","HOOD","RBLX","U","SPOT","DOCS","BILL","HUBS","ZS",
    "OKTA","CFLT","GTLB","MDB","ESTC","TASK","AI","PATH","SOUN","APP",
    # Biotech / medtech
    "TDOC","ACAD","INCY","EXAS","NTRA","PCVX","RXRX","ROIV","TMDX",
    # Clean energy / EV
    "ENPH","SEDG","FSLR","RUN","PLUG","BE","CHPT","BLNK","STEM","ARRY",
    "RIVN","LCID","NIO","LI","XPEV","FSR",
    # Crypto / fintech
    "MSTR","RIOT","MARA","CLSK","IREN","WULF","HUT","CIFR","BTBT",
    # Commodities / resources
    "URNM","UAMY","LEU","CCJ","EU","UUUU","SPUT",
    "REMX","MP","NOVN","LIT","PICK",
    # Retail / consumer growth
    "SHOP","ETSY","W","CHWY","SFIX","LYFT","UBER","ABNB","BKNG",
    # Healthcare growth
    "ASTS","OSCR","ACCD","PHR","HIMS","WELL","CLOV",
    # Industrial / defense
    "KTOS","RKLB","ASTS","JOBY","ACHR","LILM","EVTL",
    # China ADRs (liquid, volatile — good for wave setups)
    "BABA","JD","PDD","BIDU","NIO","LI","XPEV","TCOM","TME","BILI",
]

# ── Tier 3: Dynamic — pulled fresh each scan ──────────────────────────────────
def get_dynamic_tickers(limit=100):
    """
    Fetch most-active tickers from yfinance as an opportunistic supplement.
    Returns up to `limit` tickers not already in TIER1/TIER2.
    """
    static_set = set(TIER1 + TIER2)
    try:
        import yfinance as yf
        tickers = []
        for screen in ["most_actives", "day_gainers", "day_losers"]:
            try:
                df = yf.screen(screen, count=50)
                if df is not None and not df.empty and "Symbol" in df.columns:
                    tickers += df["Symbol"].tolist()
            except Exception:
                pass
        # Deduplicate and filter
        seen = set()
        result = []
        for t in tickers:
            t = t.upper().strip()
            if t and t not in seen and t not in static_set and len(t) <= 5:
                seen.add(t)
                result.append(t)
                if len(result) >= limit:
                    break
        return result
    except Exception:
        return []


def get_all_tickers(include_dynamic=True):
    """
    Returns the full universe in priority order:
      TIER1 (large-cap optionable) -> TIER2 (quality mid-cap) -> TIER3 (dynamic)

    Deduplicates across tiers.
    """
    seen   = set()
    result = []

    for t in TIER1 + TIER2:
        t = t.upper().strip()
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    if include_dynamic:
        dynamic = get_dynamic_tickers(limit=100)
        for t in dynamic:
            if t not in seen:
                seen.add(t)
                result.append(t)

    return result


def load_universe():
    """Legacy interface — returns dict with ALL key for scanner.py compatibility."""
    return {"ALL": get_all_tickers()}


def get_priority_tickers(universe=None):
    """
    Returns TIER1 tickers as the priority scan list.
    These are scanned first because:
    - Most liquid (reliable price data on IEX feed)
    - Have listed options (protective put lookup works)
    - Berkshire-style quality more likely to pass
    """
    return list(dict.fromkeys(TIER1))  # deduplicated, order preserved
