import requests, json
from bs4 import BeautifulSoup
from pathlib import Path

HEADERS = {'User-Agent': 'Mozilla/5.0'}

def get_yahoo(path):
    try:
        url = f'https://finance.yahoo.com/{path}'
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
        tickers = []
        for a in soup.find_all('a', {'data-testid': 'table-cell-ticker'}):
            sym = a.text.strip().upper()
            if sym.isalpha() and len(sym) <= 5:
                tickers.append(sym)
        print(f'[yahoo/{path}] {len(tickers)} tickers')
        return tickers
    except Exception as e:
        print(f'[yahoo/{path}] failed: {e}')
        return []

def get_priority_tickers(universe=None):
    if universe is None:
        try:
            data = json.loads(Path('universe_cache.json').read_text())
            universe = set(data.get('ALL', []))
        except Exception:
            universe = set()
    universe_set = set(universe)
    all_t = get_yahoo('trending-tickers') + get_yahoo('gainers') + get_yahoo('most-active')
    seen, priority = set(), []
    for t in all_t:
        if t not in seen:
            seen.add(t)
            if not universe_set or t in universe_set:
                priority.append(t)
    print(f'[screener] {len(priority)} priority tickers')
    return priority

if __name__ == '__main__':
    t = get_priority_tickers()
    print('Top 30:', t[:30])
