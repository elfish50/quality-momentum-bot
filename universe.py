import os, json, requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
ALPACA_URL    = "https://paper-api.alpaca.markets/v2"
CACHE_FILE    = Path("universe_cache.json")
CACHE_TTL_H   = 24
VALID_EXCHANGES = {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE AMERICAN"}

def _is_clean_symbol(sym):
    return sym.isalpha() and len(sym) <= 5

def _fetch_from_alpaca():
    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    print("[universe] Fetching from Alpaca...")
    r = requests.get(
        f"{ALPACA_URL}/assets",
        headers=headers,
        params={"status": "active", "asset_class": "us_equity"},
        timeout=30,
    )
    r.raise_for_status()
    assets = r.json()
    print(f"[universe] Raw assets: {len(assets)}")
    tickers = []
    for a in assets:
        if not a.get("tradable"):
            continue
        if a.get("exchange", "") not in VALID_EXCHANGES:
            continue
        sym = a.get("symbol", "")
        if not _is_clean_symbol(sym):
            continue
        tickers.append(sym)
    tickers = sorted(set(tickers))
    print(f"[universe] Clean tickers: {len(tickers)}")
    return tickers

def build_universe():
    tickers = _fetch_from_alpaca()
    return {
        "NASDAQ": tickers,
        "NYSE": [],
        "SP500": tickers,
        "ALL": tickers,
    }

def load_universe(force_refresh=False):
    if not force_refresh and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_H):
                all_t = data.get("ALL", [])
                if len(all_t) > 100:
                    print(f"[universe] Cache hit: {len(all_t)} tickers")
                    return data
        except Exception:
            pass
    u = build_universe()
    u["cached_at"] = datetime.now().isoformat()
    CACHE_FILE.write_text(json.dumps(u, indent=2))
    return u

def get_all_tickers():
    return load_universe().get("ALL", [])

if __name__ == "__main__":
    u = load_universe(force_refresh=True)
    print(f"Total: {len(u['ALL'])} tickers")
    print("Sample:", u["ALL"][:20])
