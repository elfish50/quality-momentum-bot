"""
UNIVERSE LOADER
Fetches all active, tradeable US equity tickers from Alpaca's /v2/assets endpoint.
Covers Nasdaq, NYSE, NYSE Arca, NYSE American — no extra API key needed.

Filters applied:
  - status == "active"
  - tradable == True
  - asset_class == "us_equity"
  - exchange in (NASDAQ, NYSE, NYSE ARCA, NYSE AMERICAN)
  - no OTC / pink sheets
  - symbol is clean (letters only, no dots/slashes = no preferred shares or warrants)

Result is cached to disk for 24h so we don't hammer Alpaca on every run.
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
ALPACA_URL    = "https://paper-api.alpaca.markets/v2"   # assets endpoint works on paper too

CACHE_FILE    = Path(".universe_cache.json")
CACHE_TTL_H   = 24   # hours before refreshing

VALID_EXCHANGES = {"NASDAQ", "NYSE", "NYSE ARCA", "NYSE AMERICAN"}

MIN_PRICE     = 5.0    # skip penny stocks
MAX_PRICE     = 5000.0 # skip extreme outliers


# ── Fetch from Alpaca ─────────────────────────────────────────────────────────

def _fetch_assets() -> list[dict]:
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    params = {
        "status":      "active",
        "asset_class": "us_equity",
    }

    print("[universe] Fetching asset list from Alpaca...")
    r = requests.get(
        f"{ALPACA_URL}/assets",
        headers=headers,
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    assets = r.json()
    print(f"[universe] Raw assets returned: {len(assets)}")
    return assets


def _is_clean_symbol(sym: str) -> bool:
    """Only plain ticker symbols — no dots, slashes, or numbers (warrants/preferreds)."""
    return sym.isalpha() and len(sym) <= 5


# ── Filter ────────────────────────────────────────────────────────────────────

def _filter_assets(assets: list[dict]) -> list[str]:
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
    print(f"[universe] After filter: {len(tickers)} tickers")
    return tickers


# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache() -> list[str] | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_H):
            tickers = data["tickers"]
            print(f"[universe] Loaded {len(tickers)} tickers from cache (expires in "
                  f"{CACHE_TTL_H - int((datetime.now()-cached_at).total_seconds()/3600)}h)")
            return tickers
        print("[universe] Cache expired, refreshing...")
    except Exception:
        pass
    return None


def _save_cache(tickers: list[str]):
    CACHE_FILE.write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "tickers":   tickers,
    }, indent=2))
    print(f"[universe] Cached {len(tickers)} tickers to {CACHE_FILE}")


# ── Public API ────────────────────────────────────────────────────────────────

def get_universe(force_refresh: bool = False) -> list[str]:
    """
    Returns list of clean, tradeable US equity tickers.
    Uses 24h disk cache to avoid hammering Alpaca.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            return cached

    assets  = _fetch_assets()
    tickers = _filter_assets(assets)
    _save_cache(tickers)
    return tickers


def get_universe_batched(batch_size: int = 500) -> list[list[str]]:
    """Returns universe split into batches for parallel processing."""
    tickers = get_universe()
    return [tickers[i:i+batch_size] for i in range(0, len(tickers), batch_size)]


if __name__ == "__main__":
    tickers = get_universe(force_refresh=True)
    print(f"\nTotal tickers ready to scan: {len(tickers)}")
    print("Sample:", tickers[:20])
