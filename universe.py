
Copy

"""
universe.py
Loads NASDAQ + NYSE ticker lists with 24h cache.
Hardcoded fallbacks if APIs fail.
"""
import json
import os
import time
import pandas as pd
import requests

CACHE_FILE        = "universe_cache.json"
CACHE_MAX_AGE_HRS = 24

SP500_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","JPM",
    "LLY","V","UNH","XOM","MA","COST","HD","PG","WMT","NFLX","BAC","ABBV",
    "CRM","CVX","MRK","KO","ADBE","AMD","PEP","TMO","ACN","MCD","ABT","CSCO",
    "GE","DHR","TXN","CAT","INTU","WFC","AXP","MS","GS","BLK","SPGI","MMC",
    "RTX","HON","LMT","UPS","DE","NOC","GD","NEE","DUK","JNJ","PFE","AMGN",
    "GILD","VRTX","REGN","ISRG","SYK","MDT","LOW","TGT","BKNG","SBUX","NKE",
    "TJX","ORLY","PANW","CRWD","FTNT","NOW","WDAY","SNOW","QCOM","AMAT","LRCX",
    "KLAC","MRVL","SNPS","CDNS","AMT","PLD","CCI","EQIX","PSA","LIN","APD",
    "ECL","SHW","NEM","FCX","UNP","CSX","NSC","SCHW","COF","SYF","BMY","ELV",
]

NASDAQ_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","TSLA","AVGO","COST","NFLX",
    "AMD","ADBE","QCOM","INTC","CSCO","INTU","AMAT","LRCX","KLAC","SNPS",
    "CDNS","MRVL","PANW","FTNT","CRWD","ZS","DDOG","SNOW","MDB","NET",
    "HUBS","TEAM","WDAY","NOW","ABNB","UBER","RBLX","COIN","PYPL","SHOP",
    "MELI","BKNG","VRSK","IDXX","ILMN","BIIB","VRTX","REGN","GILD","AMGN",
    "MRNA","ISRG","FAST","ODFL","PCAR","CPRT","CSGP","ANSS","TTWO","EA",
    "LULU","MNST","MDLZ","PEP","DLTR","ROST","ORLY","TSCO","NXPI","ADI",
    "MPWR","FSLR","SOFI","HOOD","AFRM","UPST","ZM","DOCU","TWLO","VEEV",
]

NYSE_FALLBACK = [
    "JPM","BAC","WFC","GS","MS","C","BLK","AXP","V","MA","BRK-B","JNJ",
    "UNH","PFE","ABT","TMO","DHR","SYK","MDT","BSX","XOM","CVX","COP",
    "SLB","EOG","MPC","VLO","PSX","OXY","WMT","HD","LOW","TGT","KO","PEP",
    "MCD","SBUX","NKE","DIS","T","VZ","NEE","DUK","SO","D","AEP","EXC",
    "BA","CAT","GE","HON","MMM","RTX","LMT","NOC","GD","UPS","FDX","CSX",
    "UNP","NSC","DE","EMR","ETN","PG","CL","KMB","LIN","APD","ECL","SHW",
    "NEM","FCX","NUE","AMT","PLD","CCI","EQIX","PSA","O","SPG","SCHW",
    "COF","DFS","AIG","MET","PRU","AFL","CVS","ELV","CI","HUM","MCK","IBM",
    "DAL","UAL","AAL","LUV","CCL","RCL","MAR","HLT","F","GM",
]


def _valid(t):
    return bool(t) and len(t) <= 6 and all(c.isalpha() or c == "-" for c in t)


def _fetch_nasdaq_api(exchange: str) -> list:
    try:
        url     = f"https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=5000&exchange={exchange}"
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                   "Referer": "https://www.nasdaq.com/"}
        r    = requests.get(url, headers=headers, timeout=15)
        rows = r.json()["data"]["table"]["rows"]
        t    = [row["symbol"].strip() for row in rows if row.get("symbol")]
        t    = [x for x in t if _valid(x)]
        if len(t) > 100:
            print(f"  {exchange}: {len(t)} tickers (API)")
            return t
    except Exception as e:
        print(f"  {exchange} API failed: {e}")
    return []


def build_universe() -> dict:
    print("Building universe...")
    nasdaq = _fetch_nasdaq_api("NASDAQ") or NASDAQ_FALLBACK
    nyse   = _fetch_nasdaq_api("NYSE")   or NYSE_FALLBACK

    # SP500 via Wikipedia
    sp500 = []
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", timeout=10)
        sp500  = tables[0]["Symbol"].str.replace(".", "-", regex=False).tolist()
        sp500  = [t for t in sp500 if _valid(t)]
        print(f"  SP500: {len(sp500)} tickers (Wikipedia)")
    except Exception as e:
        print(f"  SP500 Wikipedia failed: {e}")
        sp500 = SP500_FALLBACK

    all_tickers = sorted(set(nasdaq + nyse + sp500))
    universe = {"NASDAQ": nasdaq, "NYSE": nyse, "SP500": sp500, "ALL": all_tickers}
    print(f"Total unique: {len(all_tickers)}")
    return universe


def load_universe(force_refresh=False) -> dict:
    if not force_refresh and os.path.exists(CACHE_FILE):
        age = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
        if age < CACHE_MAX_AGE_HRS:
            with open(CACHE_FILE) as f:
                cached = json.load(f)
            if len(cached.get("ALL", [])) > 0:
                print(f"Universe from cache: {len(cached['ALL'])} tickers")
                return cached
    u = build_universe()
    with open(CACHE_FILE, "w") as f:
        json.dump(u, f)
    return u


def get_all_tickers() -> list:
    return load_universe().get("ALL", [])
