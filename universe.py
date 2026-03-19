import json
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

CACHE_FILE  = Path("universe_cache.json")
CACHE_TTL_H = 1  # refresh every scan session (was 24h for static list)

# yfinance built-in screeners — no key needed
SCREENERS = ["most_active", "day_gainers"]

def _is_clean_symbol(sym: str) -> bool:
    return isinstance(sym, str) and sym.isalpha() and len(sym) <= 5

def _fetch_yfinance_actives() -> list[str]:
    tickers = []
    for screener in SCREENERS:
        try:
            print(f"[universe] Fetching yfinance screener: {screener}")
            data = yf.screen(screener, size=50)
            quotes = data.get("quotes", [])
            for q in quotes:
                sym = q.get("symbol", "")
                if _is_clean_symbol(sym):
                    tickers.append(sym)
            print(f"[universe] {screener}: {len(quotes)} results")
        except Exception as e:
            print(f"[universe] screener {screener} failed: {e}")

    tickers = sorted(set(tickers))
    print(f"[universe] Total clean tickers: {len(tickers)}")
    return tickers

def build_universe() -> dict:
    tickers = _fetch_yfinance_actives()
    return {
        "ALL": tickers,
        "cached_at": datetime.now().isoformat(),
    }

def load_universe(force_refresh=False) -> dict:
    if not force_refresh and CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
            if datetime.now() - cached_at < timedelta(hours=CACHE_TTL_H):
                all_t = data.get("ALL", [])
                if len(all_t) > 10:
                    print(f"[universe] Cache hit: {len(all_t)} tickers")
                    return data
        except Exception:
            pass

    u = build_universe()
    CACHE_FILE.write_text(json.dumps(u, indent=2))
    return u

def get_all_tickers() -> list[str]:
    return load_universe().get("ALL", [])

if __name__ == "__main__":
    u = load_universe(force_refresh=True)
    print(f"Total: {len(u['ALL'])} tickers")
    print("Sample:", u['ALL'][:20])
