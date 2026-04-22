"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

PATCH v9 — Quality hardening + account size fix
════════════════════════════════════════════════════════════════
Changes from v8:
  - ACCOUNT now reads live equity from Alpaca instead of hardcoded $1k
  - _data_missing → quality_score returns 0 (hard reject, not free pass)
  - MIN_QUALITY_SCORE raised: 45 (was 25)
  - MIN_PRICE hard filter: $10.00 — penny/micro stocks rejected before analysis
  - MIN_MARKET_CAP: $500M — filters nano/micro caps (when data available)
  - Short setups now require trend == "down" only (neutral removed)
  - Wave 2 Short Fib window tightened: 50–78.6% only (was 50–78.6% but firing too often)
  - Added min_volume check in analyze_ticker: 500k avg daily volume
  - Score threshold for BUY/SHORT raised: vol_confirmed still required,
    but also signal_score >= 50 required to auto-execute

All v8 patches preserved (shorts, soft trend filter for longs, loosened RSI/wave params).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import math
import pathlib
import random
import time
import traceback
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta

# ── Keys ──────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

if not FINNHUB_KEY:
    print("[strategy] WARNING: FINNHUB_KEY is not set — fundamentals will always be missing")
else:
    print(f"[strategy] FINNHUB_KEY present (starts with: {FINNHUB_KEY[:4]}...)")

ALPACA_URL        = "https://data.alpaca.markets/v2"
ALPACA_SCREEN_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
ALPACA_ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"
ALPACA_PAPER_URL  = "https://paper-api.alpaca.markets/v2"
FINNHUB_URL       = "https://finnhub.io/api/v1"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# ── Account (live from Alpaca) ────────────────────────────────────────────────
_ACCOUNT_CACHE      = {"value": None, "ts": None}
_ACCOUNT_CACHE_SECS = 300  # refresh every 5 min

def get_account_equity() -> float:
    """Fetch live equity from Alpaca paper account. Cached for 5 min."""
    now = time.time()
    if (
        _ACCOUNT_CACHE["value"] is not None
        and _ACCOUNT_CACHE["ts"] is not None
        and now - _ACCOUNT_CACHE["ts"] < _ACCOUNT_CACHE_SECS
    ):
        return _ACCOUNT_CACHE["value"]
    try:
        r = requests.get(
            f"{ALPACA_PAPER_URL}/account",
            headers={
                "APCA-API-KEY-ID":     ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            },
            timeout=10,
        )
        if r.ok:
            equity = float(r.json().get("equity", 100_000))
            _ACCOUNT_CACHE["value"] = equity
            _ACCOUNT_CACHE["ts"]    = now
            print(f"[strategy] Live account equity: ${equity:,.2f}")
            return equity
    except Exception as e:
        print(f"[strategy] Could not fetch account equity: {e}")
    return _ACCOUNT_CACHE["value"] or 100_000


RISK_PCT = 0.01   # Risk 1% of account per trade (was 10% of fake $1k = $100, now 1% of real equity)

# ── Fibonacci levels ──────────────────────────────────────────────────────────
FIB_382 = 0.382
FIB_500 = 0.500
FIB_618 = 0.618
FIB_786 = 0.786

EXT_1272 = 1.272
EXT_1618 = 1.618
EXT_2618 = 2.618

# ── Strategy thresholds (v9 — hardened quality) ───────────────────────────────
VOL_CONFIRM_RATIO  = 1.1
MIN_QUALITY_SCORE  = 45     # was 25
MIN_WAVE1_MOVE     = 0.03
MIN_RR_TP2         = 1.5
MIN_SIGNAL_SCORE   = 50     # new: auto-execute only if score >= 50
MIN_PRICE          = 10.0   # new: reject penny/micro stocks
MIN_AVG_VOLUME     = 500_000  # new: reject illiquid stocks
MIN_MARKET_CAP     = 500_000_000  # new: $500M minimum market cap

# ── Universe config ───────────────────────────────────────────────────────────
UNIVERSE_SIZE = 500

FALLBACK_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","JPM","V","UNH",
    "LLY","XOM","JNJ","PG","MA","HD","MRK","ABBV","CVX","AVGO",
    "PEP","KO","COST","WMT","MCD","ADBE","CRM","ACN","TMO","NFLX",
    "QCOM","INTC","AMD","TXN","HON","UPS","CAT","GS","MS","AXP",
    "BA","GE","MMM","LMT","RTX","DE","EMR","ITW","ETN","PH",
    "SHOP","ETSY","UBER","LYFT","ABNB","BKNG","COIN","HOOD","PLTR","SNOW",
    "CRWD","NET","DDOG","ZS","OKTA","MDB","GTLB","CFLT","APP","SOUN",
    "ENPH","FSLR","PLUG","BE","CHPT","RIVN","NIO","LI","XPEV","LCID",
    "MARA","RIOT","MSTR","CLSK","IREN","WULF","HUT","CIFR","BTBT","CORZ",
    "BABA","JD","PDD","BIDU","TCOM","TME","BILI",
]

# ── Deduplication (v3) ────────────────────────────────────────────────────────
SEEN_FILE        = pathlib.Path("seen_setups.json")
SEEN_EXPIRY_DAYS = 10
PRICE_TOLERANCE  = 0.02


def load_seen() -> dict:
    if not SEEN_FILE.exists():
        return {}
    try:
        seen   = json.loads(SEEN_FILE.read_text())
        cutoff = datetime.now() - timedelta(days=SEEN_EXPIRY_DAYS)
        return {
            k: v for k, v in seen.items()
            if datetime.fromisoformat(v["ts"]) > cutoff
        }
    except Exception:
        return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def already_alerted(ticker: str, setup_name: str, price: float, seen: dict) -> bool:
    key = f"{ticker}::{setup_name}"
    if key not in seen:
        return False
    prev_price = seen[key]["price"]
    return abs(price - prev_price) / prev_price < PRICE_TOLERANCE


def mark_seen(ticker: str, setup_name: str, price: float, seen: dict) -> None:
    key = f"{ticker}::{setup_name}"
    seen[key] = {"price": price, "ts": datetime.now().isoformat()}


# ── Dynamic Universe via Alpaca ───────────────────────────────────────────────

def _get_most_actives() -> list:
    try:
        r = requests.get(
            ALPACA_SCREEN_URL,
            headers=HEADERS,
            params={"by": "volume", "top": 100},
            timeout=15
        )
        if not r.ok:
            print(f"[universe] most-actives error {r.status_code}: {r.text[:100]}")
            return []
        tickers = [
            item["symbol"] for item in r.json().get("most_actives", [])
            if item.get("symbol") and len(item["symbol"]) <= 5
        ]
        print(f"[universe] most-actives: {len(tickers)} tickers")
        return tickers
    except Exception as e:
        print(f"[universe] most-actives exception: {e}")
        return []


def _get_alpaca_assets() -> list:
    try:
        r = requests.get(
            ALPACA_ASSETS_URL,
            headers=HEADERS,
            params={"status": "active", "asset_class": "us_equity"},
            timeout=30
        )
        if not r.ok:
            print(f"[universe] assets error {r.status_code}")
            return []
        assets = r.json()
        tickers = [
            a["symbol"] for a in assets
            if isinstance(a, dict)
            and a.get("tradable")
            and a.get("status") == "active"
            and len(a.get("symbol", "")) <= 5
            and "." not in a.get("symbol", "")
            and "/" not in a.get("symbol", "")
        ]
        print(f"[universe] alpaca assets: {len(tickers)} tradable symbols")
        return tickers
    except Exception as e:
        print(f"[universe] assets exception: {e}")
        return []


def _snapshot_filter(
    tickers: list,
    min_price: float = MIN_PRICE,
    min_volume: float = MIN_AVG_VOLUME,
) -> list:
    """
    v9: min_price raised to $10 (was $5), min_volume raised to 500k (was 300k).
    """
    filtered   = []
    chunk_size = 100

    for i in range(0, len(tickers), chunk_size):
        chunk     = tickers[i: i + chunk_size]
        snap_data = {}

        for feed in ["sip", "iex"]:
            try:
                r = requests.get(
                    f"{ALPACA_URL}/stocks/snapshots",
                    headers=HEADERS,
                    params={"symbols": ",".join(chunk), "feed": feed},
                    timeout=20
                )
                if r.ok and r.json():
                    snap_data = r.json()
                    break
            except Exception as e:
                print(f"[universe] snapshot chunk error ({feed}): {e}")

        for sym, snap in snap_data.items():
            try:
                daily  = snap.get("dailyBar") or snap.get("prevDailyBar") or {}
                close  = float(daily.get("c", 0) or 0)
                volume = float(daily.get("v", 0) or 0)
                if close >= min_price and volume >= min_volume:
                    filtered.append(sym)
            except Exception:
                continue

    print(f"[universe] after price/volume filter: {len(filtered)} tickers")
    return filtered


def get_universe() -> list:
    result = []

    most_actives = _get_most_actives()
    result.extend(most_actives)

    assets = _get_alpaca_assets()
    if assets:
        sample_size = min(len(assets), UNIVERSE_SIZE * 4)
        sample      = random.sample(assets, sample_size)
        filtered    = _snapshot_filter(sample)
        result.extend(filtered)

    seen_set = set()
    deduped  = []
    for t in result:
        t = t.upper().strip()
        if t and t not in seen_set:
            seen_set.add(t)
            deduped.append(t)

    if len(deduped) < 20:
        print(f"[universe] only {len(deduped)} tickers — using fallback")
        return list(FALLBACK_TICKERS)

    n_front = len(most_actives)
    front   = deduped[:n_front]
    rest    = deduped[n_front:]
    random.shuffle(rest)
    final   = (front + rest)[:UNIVERSE_SIZE]

    print(f"[universe] final universe: {len(final)} tickers")
    return final


def load_universe() -> list:
    return get_universe()


# ── Price Data ────────────────────────────────────────────────────────────────

def get_price_data(ticker):
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    for feed in ["iex", "sip"]:
        try:
            params = {
                "start": start, "end": end,
                "timeframe": "1Day", "limit": 400,
                "feed": feed, "adjustment": "split",
            }
            r    = requests.get(
                f"{ALPACA_URL}/stocks/{ticker}/bars",
                headers=HEADERS, params=params, timeout=15
            )
            bars = r.json().get("bars")
            if not bars or len(bars) < 60:
                continue
            df = pd.DataFrame([{
                "Date":   pd.to_datetime(b["t"]),
                "Open":   float(b["o"]),
                "High":   float(b["h"]),
                "Low":    float(b["l"]),
                "Close":  float(b["c"]),
                "Volume": float(b["v"]),
            } for b in bars])
            df = df.sort_values("Date").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"[{ticker}] Alpaca {feed} error: {e}")
    return None


def get_weekly_bars(df):
    df2 = df.copy()
    df2["Date"] = pd.to_datetime(df2["Date"])
    df2 = df2.set_index("Date")
    weekly = df2.resample("W").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    }).dropna()
    return weekly.reset_index()


def _trend_context(df) -> str:
    """
    Returns 'up', 'down', or 'neutral'.
    Used as a soft filter for longs, hard filter for shorts.
    """
    try:
        closes_daily = df["Close"]
        sma200       = closes_daily.rolling(200).mean().iloc[-1]
        sma50        = closes_daily.rolling(50).mean().iloc[-1]
        price        = float(closes_daily.iloc[-1])

        weekly = get_weekly_bars(df)
        if len(weekly) < 22:
            return "neutral"

        close   = weekly["Close"]
        sma10w  = close.rolling(10).mean()
        sma20w  = close.rolling(20).mean()
        cur_10  = float(sma10w.iloc[-1])
        cur_20  = float(sma20w.iloc[-1])
        prev_10 = float(sma10w.iloc[-2])

        bullish = sum([
            not pd.isna(sma200) and price > float(sma200),
            not pd.isna(sma50)  and price > float(sma50),
            cur_10 > cur_20,
            cur_10 > prev_10,
        ])
        bearish = sum([
            not pd.isna(sma200) and price < float(sma200),
            not pd.isna(sma50)  and price < float(sma50),
            cur_10 < cur_20,
            cur_10 < prev_10,
        ])

        if bullish >= 3:   return "up"
        if bearish >= 3:   return "down"
        return "neutral"
    except Exception:
        return "neutral"


def weekly_trend_is_up(df) -> bool:
    return _trend_context(df) != "down"


# ── Fundamentals ──────────────────────────────────────────────────────────────

_FUND_DEFAULT = {
    "roe":           0.0,
    "gross_margin":  0.0,
    "debt_equity":   999.0,
    "eps_growth":    0.0,
    "pe_ratio":      0.0,
    "sector":        "Unknown",
    "name":          "",
    "market_cap":    0.0,
    "_data_missing": True,
}

_FINNHUB_CALL_DELAY = 1.2


def _finnhub_get(url: str, params: dict, ticker: str, label: str) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)

            if r.status_code == 429:
                wait = 15 * (attempt + 1)
                print(f"[{ticker}] Finnhub {label} rate-limited (429) — waiting {wait}s")
                time.sleep(wait)
                continue

            if r.status_code == 401:
                print(f"[{ticker}] Finnhub {label} UNAUTHORIZED (401) — check FINNHUB_KEY")
                return {}

            if not r.ok:
                print(f"[{ticker}] Finnhub {label} error {r.status_code}: {r.text[:120]}")
                return {}

            return r.json()

        except Exception as e:
            print(f"[{ticker}] Finnhub {label} exception (attempt {attempt+1}): {e}")
            time.sleep(2)

    return {}


def get_fundamentals(ticker: str) -> dict:
    def safe(v, d=0.0):
        try:
            return float(v) if v not in (None, "", "N/A", "None") else d
        except Exception:
            return d

    if not FINNHUB_KEY:
        default = dict(_FUND_DEFAULT)
        default["name"] = ticker
        return default

    try:
        metric_data = _finnhub_get(
            f"{FINNHUB_URL}/stock/metric",
            {"symbol": ticker, "metric": "all", "token": FINNHUB_KEY},
            ticker, "metric"
        )
        time.sleep(_FINNHUB_CALL_DELAY)

        profile_data = _finnhub_get(
            f"{FINNHUB_URL}/stock/profile2",
            {"symbol": ticker, "token": FINNHUB_KEY},
            ticker, "profile"
        )

        m = metric_data.get("metric", {})
        p = profile_data

        raw_roe = m.get("roeTTM")
        raw_gm  = m.get("grossMarginTTM")
        raw_eps = m.get("epsGrowth3Y")
        raw_de  = m.get("totalDebt/totalEquityAnnual")
        raw_pe  = m.get("peNormalizedAnnual")

        print(
            f"[{ticker}] Finnhub raw — "
            f"ROE:{raw_roe} GM:{raw_gm} EPS:{raw_eps} "
            f"D/E:{raw_de} PE:{raw_pe} "
            f"sector:{p.get('finnhubIndustry')} name:{p.get('name')}"
        )

        roe_val = safe(raw_roe) / 100.0
        gm_val  = safe(raw_gm)  / 100.0
        eps_val = safe(raw_eps) / 100.0
        de_val  = safe(raw_de, 999.0)
        pe_val  = safe(raw_pe)
        mc_val  = safe(p.get("marketCapitalization")) * 1_000_000

        numeric_empty = (
            raw_roe in (None, "", "N/A", "None") and
            raw_gm  in (None, "", "N/A", "None") and
            raw_eps in (None, "", "N/A", "None")
        )
        data_missing = numeric_empty or len(m) < 3

        if data_missing:
            print(f"[{ticker}] Finnhub metric dict empty (len={len(m)}) — marking _data_missing")

        return {
            "roe":           roe_val,
            "gross_margin":  gm_val,
            "debt_equity":   de_val,
            "eps_growth":    eps_val,
            "pe_ratio":      pe_val,
            "sector":        p.get("finnhubIndustry", "Unknown"),
            "name":          p.get("name", ticker),
            "market_cap":    mc_val,
            "_data_missing": data_missing,
        }

    except Exception as e:
        print(f"[{ticker}] Finnhub get_fundamentals exception: {e}")
        default = dict(_FUND_DEFAULT)
        default["name"] = ticker
        return default


def quality_score(fund: dict):
    """
    v9: _data_missing → score 0 (hard reject). Was 30 (free pass).
    """
    score, failed = 0.0, []

    if fund.get("_data_missing"):
        # No Finnhub data = unknown quality = do not trade
        return 0.0, ["⛔ No fundamental data — rejected"]

    # Market cap filter
    mc = fund.get("market_cap", 0)
    if mc > 0 and mc < MIN_MARKET_CAP:
        return 0.0, [f"⛔ Market cap ${mc/1e6:.0f}M < $500M minimum"]

    roe = fund.get("roe", 0)
    if roe >= 0.15:   score += 30
    elif roe >= 0.08: score += 15 + (roe - 0.08) / 0.07 * 15
    elif roe > 0:     score += 8
    else:             failed.append(f"ROE {roe:.1%}")

    gm = fund.get("gross_margin", 0)
    if gm >= 0.40:   score += 25
    elif gm >= 0.15: score += 10 + (gm - 0.15) / 0.25 * 15
    elif gm > 0:     score += 5
    else:            failed.append(f"Margin {gm:.1%}")

    eg = fund.get("eps_growth", 0)
    if eg >= 0.15:  score += 25
    elif eg >= 0.0: score += 10 + eg / 0.15 * 15
    else:           failed.append(f"EPS {eg:.1%}")

    de = fund.get("debt_equity", 999)
    if de <= 0.5:   score += 20
    elif de <= 2.0: score += 20 - (de - 0.5) / 1.5 * 15
    else:           failed.append(f"D/E {de:.1f}")

    return round(score, 1), failed


# ── Indicators ────────────────────────────────────────────────────────────────

def compute_indicators(df):
    df    = df.copy()
    close = df["Close"]

    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))

    high, low = df["High"], df["Low"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR"]      = tr.rolling(14).mean()
    df["SMA50"]    = close.rolling(50).mean()
    df["SMA200"]   = close.rolling(200).mean()
    df["EMA21"]    = close.ewm(span=21, adjust=False).mean()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()

    return df


def find_swing_points(df, lookback=90, min_bars=5, max_age_bars=40):
    n     = len(df)
    start = max(0, n - lookback)
    highs, lows = [], []

    for i in range(start + min_bars, n - min_bars):
        if i < n - max_age_bars:
            continue
        window = df.iloc[i - min_bars: i + min_bars + 1]
        bar    = df.iloc[i]
        if bar["High"] == window["High"].max():
            highs.append((i, float(bar["High"])))
        if bar["Low"] == window["Low"].min():
            lows.append((i, float(bar["Low"])))

    return highs, lows


def _vol_confirmed(df):
    if len(df) < 3:
        return False, 0.0
    prev_bar  = df.iloc[-2]
    vol_avg   = float(prev_bar["VolAvg20"]) if prev_bar["VolAvg20"] > 0 else 1.0
    vol_ratio = float(prev_bar["Volume"]) / vol_avg
    return vol_ratio >= VOL_CONFIRM_RATIO, round(vol_ratio, 2)


# ══════════════════════════════════════════════════════════════════════════════
# LONG SETUPS
# ══════════════════════════════════════════════════════════════════════════════

def detect_wave2_setup(df, trend: str):
    """Wave 2 Pullback (LONG). Trend is soft — down reduces score, doesn't reject."""
    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5, max_age_bars=40)
    if len(highs) < 1 or len(lows) < 1:
        return None

    last_high_loc, wave1_top = highs[-1]
    wave1_origin = None
    for loc, val in reversed(lows):
        if loc < last_high_loc:
            wave1_origin = val
            break
    if wave1_origin is None:
        return None

    wave1_size = wave1_top - wave1_origin
    if wave1_size <= 0 or wave1_size / wave1_origin < MIN_WAVE1_MOVE:
        return None

    fib_382 = round(wave1_top - wave1_size * FIB_382, 2)
    fib_500 = round(wave1_top - wave1_size * FIB_500, 2)
    fib_618 = round(wave1_top - wave1_size * FIB_618, 2)
    fib_786 = round(wave1_top - wave1_size * FIB_786, 2)

    if not (fib_786 <= price <= fib_382):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi > rsi_prev and 20 <= rsi <= 70):
        return None
    if price < float(df.iloc[-4:-1]["Low"].min()) * 1.002:
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop     = round(wave1_origin * 0.99, 2)
    ext_1272 = round(price + wave1_size * EXT_1272, 2)
    ext_1618 = round(price + wave1_size * EXT_1618, 2)
    ext_2618 = round(price + wave1_size * EXT_2618, 2)
    risk     = price - stop

    if risk <= 0 or (ext_1618 - price) / risk < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "Wave 2 Pullback",
        "direction":    "LONG",
        "wave1_origin": round(wave1_origin, 2), "wave1_top": round(wave1_top, 2),
        "wave1_size":   round(wave1_size, 2),
        "fib_382": fib_382, "fib_500": fib_500, "fib_618": fib_618, "fib_786": fib_786,
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": ext_1272, "tp2": ext_1618, "tp3": ext_2618,
        "tp1_pct": round((ext_1272 - price) / price * 100, 1),
        "tp2_pct": round((ext_1618 - price) / price * 100, 1),
        "tp3_pct": round((ext_2618 - price) / price * 100, 1),
        "rr_tp1": round((ext_1272 - price) / risk, 2),
        "rr_tp2": round((ext_1618 - price) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave1_origin": round(wave1_origin, 2), "wave1_top": round(wave1_top, 2),
            "wave1_size":   round(wave1_size, 2),
            "fib_382": fib_382, "fib_500": fib_500, "fib_618": fib_618,
        }
    }


def detect_wave4_setup(df, trend: str):
    """Wave 4 Pullback (LONG)."""
    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5, max_age_bars=40)
    if len(highs) < 2 or len(lows) < 1:
        return None

    wave1_origin_loc, wave1_origin = lows[0]
    wave1_high_loc,   wave1_high   = highs[0]
    wave3_high_loc,   wave3_high   = highs[-1]

    if not (wave1_origin_loc < wave1_high_loc < wave3_high_loc):
        return None

    wave3_size = wave3_high - wave1_origin
    if wave3_size <= 0:
        return None

    fib_382_w4 = round(wave3_high - wave3_size * FIB_382, 2)
    fib_500_w4 = round(wave3_high - wave3_size * FIB_500, 2)

    if not (fib_500_w4 <= price <= fib_382_w4) or price < wave1_high:
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi > rsi_prev and 20 <= rsi <= 70):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop       = round(wave1_high * 0.99, 2)
    wave1_size = wave1_high - wave1_origin
    ext_1272   = round(price + wave1_size * EXT_1272, 2)
    ext_1618   = round(price + wave1_size * EXT_1618, 2)
    ext_2618   = round(price + wave1_size * EXT_2618, 2)
    risk       = price - stop

    if risk <= 0 or (ext_1618 - price) / risk < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":      "Wave 4 Pullback",
        "direction":  "LONG",
        "wave1_origin": round(wave1_origin, 2), "wave1_high": round(wave1_high, 2),
        "wave3_high": round(wave3_high, 2),
        "fib_382": fib_382_w4, "fib_500": fib_500_w4,
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": ext_1272, "tp2": ext_1618, "tp3": ext_2618,
        "tp1_pct": round((ext_1272 - price) / price * 100, 1),
        "tp2_pct": round((ext_1618 - price) / price * 100, 1),
        "tp3_pct": round((ext_2618 - price) / price * 100, 1),
        "rr_tp1": round((ext_1272 - price) / risk, 2),
        "rr_tp2": round((ext_1618 - price) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave1_origin": round(wave1_origin, 2), "wave1_high": round(wave1_high, 2),
            "wave3_high":   round(wave3_high, 2),
            "fib_382": fib_382_w4, "fib_500": fib_500_w4,
        }
    }


def detect_abc_setup(df, trend: str):
    """ABC Correction (LONG)."""
    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4, max_age_bars=40)
    if len(highs) < 1 or len(lows) < 2:
        return None

    wave_a_start_loc, wave_a_start = highs[-1]
    wave_a_end_loc,   wave_a_end   = lows[-2]
    wave_c_loc,       wave_c_low   = lows[-1]

    if not (wave_a_start_loc < wave_a_end_loc < wave_c_loc):
        return None

    wave_a_size = wave_a_start - wave_a_end
    if wave_a_size <= 0 or wave_a_size / wave_a_start < 0.05:
        return None

    c_zone_low  = round(wave_a_end - wave_a_size * 0.20, 2)
    c_zone_high = round(wave_a_end + wave_a_size * 0.10, 2)

    if not (c_zone_low <= price <= c_zone_high):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi > rsi_prev and 20 <= rsi <= 60):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(wave_a_end * 0.99, 2)
    tp1  = round(wave_a_start, 2)
    tp2  = round(wave_a_start + wave_a_size * FIB_618, 2)
    tp3  = round(wave_a_start + wave_a_size * 1.0, 2)
    risk = price - stop

    if risk <= 0 or (tp1 - price) / risk < 1.5:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "ABC Correction",
        "direction":    "LONG",
        "wave_a_start": round(wave_a_start, 2), "wave_a_end": round(wave_a_end, 2),
        "wave_c_low":   round(wave_c_low, 2),
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_pct": round((tp1 - price) / price * 100, 1),
        "tp2_pct": round((tp2 - price) / price * 100, 1),
        "tp3_pct": round((tp3 - price) / price * 100, 1),
        "rr_tp1": round((tp1 - price) / risk, 2),
        "rr_tp2": round((tp2 - price) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave_a_start": round(wave_a_start, 2), "wave_a_end": round(wave_a_end, 2),
            "wave_c_low":   round(wave_c_low, 2),
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
# SHORT SETUPS — v9: all require trend == "down" (neutral removed)
# ══════════════════════════════════════════════════════════════════════════════

def detect_wave2_short(df, trend: str):
    """
    Wave 2 Short — after impulsive drop, price bounces 50–78.6% Fib.
    v9: requires trend == "down" only (was "down" or "neutral").
    """
    if trend != "down":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5, max_age_bars=40)
    if len(highs) < 1 or len(lows) < 1:
        return None

    last_low_loc, wave1_bottom = lows[-1]
    wave1_top = None
    for loc, val in reversed(highs):
        if loc < last_low_loc:
            wave1_top = val
            break
    if wave1_top is None:
        return None

    wave1_size = wave1_top - wave1_bottom
    if wave1_size <= 0 or wave1_size / wave1_top < MIN_WAVE1_MOVE:
        return None

    fib_382 = round(wave1_bottom + wave1_size * FIB_382, 2)
    fib_500 = round(wave1_bottom + wave1_size * FIB_500, 2)
    fib_618 = round(wave1_bottom + wave1_size * FIB_618, 2)
    fib_786 = round(wave1_bottom + wave1_size * FIB_786, 2)

    if not (fib_500 <= price <= fib_786):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi < rsi_prev and 35 <= rsi <= 75):
        return None

    if price > float(df.iloc[-4:-1]["High"].max()) * 0.998:
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop     = round(wave1_top * 1.01, 2)
    ext_1272 = round(price - wave1_size * EXT_1272, 2)
    ext_1618 = round(price - wave1_size * EXT_1618, 2)
    ext_2618 = round(price - wave1_size * EXT_2618, 2)
    risk     = stop - price

    if risk <= 0 or (price - ext_1618) / risk < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "Wave 2 Short",
        "direction":    "SHORT",
        "wave1_top":    round(wave1_top, 2),
        "wave1_bottom": round(wave1_bottom, 2),
        "wave1_size":   round(wave1_size, 2),
        "fib_382": fib_382, "fib_500": fib_500, "fib_618": fib_618, "fib_786": fib_786,
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": ext_1272, "tp2": ext_1618, "tp3": ext_2618,
        "tp1_pct": round((price - ext_1272) / price * 100, 1),
        "tp2_pct": round((price - ext_1618) / price * 100, 1),
        "tp3_pct": round((price - ext_2618) / price * 100, 1),
        "rr_tp1": round((price - ext_1272) / risk, 2),
        "rr_tp2": round((price - ext_1618) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave1_top":    round(wave1_top, 2),
            "wave1_bottom": round(wave1_bottom, 2),
            "fib_500": fib_500, "fib_618": fib_618, "fib_786": fib_786,
        }
    }


def detect_wave4_short(df, trend: str):
    """Wave 4 Short — requires trend == "down"."""
    if trend != "down":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5, max_age_bars=40)
    if len(highs) < 2 or len(lows) < 1:
        return None

    wave1_top_loc,    wave1_top    = highs[0]
    wave3_bottom_loc, wave3_bottom = lows[-1]
    bounce_high_loc,  bounce_high  = highs[-1]

    if not (wave1_top_loc < wave3_bottom_loc):
        return None
    if bounce_high_loc < wave3_bottom_loc:
        return None

    wave3_size = wave1_top - wave3_bottom
    if wave3_size <= 0:
        return None

    fib_382_w4 = round(wave3_bottom + wave3_size * FIB_382, 2)
    fib_500_w4 = round(wave3_bottom + wave3_size * FIB_500, 2)

    if not (fib_382_w4 <= price <= fib_500_w4):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi < rsi_prev and 35 <= rsi <= 65):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop     = round(bounce_high * 1.01, 2)
    ext_1272 = round(price - wave3_size * 0.5 * EXT_1272, 2)
    ext_1618 = round(price - wave3_size * 0.5 * EXT_1618, 2)
    ext_2618 = round(price - wave3_size * 0.5 * EXT_2618, 2)
    risk     = stop - price

    if risk <= 0 or (price - ext_1618) / risk < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "Wave 4 Short",
        "direction":    "SHORT",
        "wave1_top":    round(wave1_top, 2),
        "wave3_bottom": round(wave3_bottom, 2),
        "bounce_high":  round(bounce_high, 2),
        "fib_382": fib_382_w4, "fib_500": fib_500_w4,
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": ext_1272, "tp2": ext_1618, "tp3": ext_2618,
        "tp1_pct": round((price - ext_1272) / price * 100, 1),
        "tp2_pct": round((price - ext_1618) / price * 100, 1),
        "tp3_pct": round((price - ext_2618) / price * 100, 1),
        "rr_tp1": round((price - ext_1272) / risk, 2),
        "rr_tp2": round((price - ext_1618) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave1_top":    round(wave1_top, 2),
            "wave3_bottom": round(wave3_bottom, 2),
            "bounce_high":  round(bounce_high, 2),
            "fib_382": fib_382_w4, "fib_500": fib_500_w4,
        }
    }


def detect_abc_short(df, trend: str):
    """
    ABC Short — requires trend == "down" (was "down" or "neutral").
    """
    if trend != "down":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4, max_age_bars=40)
    if len(highs) < 2 or len(lows) < 1:
        return None

    wave_a_start_loc, wave_a_start = highs[-2]
    wave_a_end_loc,   wave_a_end   = lows[-1]
    wave_b_loc,       wave_b_high  = highs[-1]

    if not (wave_a_start_loc < wave_a_end_loc < wave_b_loc):
        return None

    wave_a_size = wave_a_start - wave_a_end
    if wave_a_size <= 0 or wave_a_size / wave_a_start < 0.05:
        return None

    b_zone_low  = round(wave_a_end + wave_a_size * FIB_500, 2)
    b_zone_high = round(wave_a_start + wave_a_size * 0.05, 2)

    if not (b_zone_low <= price <= b_zone_high):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi < rsi_prev and 40 <= rsi <= 75):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(b_zone_high * 1.01, 2)
    tp1  = round(wave_a_end, 2)
    tp2  = round(wave_a_end - wave_a_size * FIB_618, 2)
    tp3  = round(wave_a_end - wave_a_size * 1.0, 2)
    risk = stop - price

    if risk <= 0 or (price - tp1) / risk < 1.5:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "ABC Short",
        "direction":    "SHORT",
        "wave_a_start": round(wave_a_start, 2), "wave_a_end": round(wave_a_end, 2),
        "wave_b_high":  round(wave_b_high, 2),
        "price": round(price, 2), "rsi": round(rsi, 1), "atr": round(atr, 2),
        "vol_ratio": vol_ratio, "vol_confirmed": vol_confirmed, "stop": stop,
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_pct": round((price - tp1) / price * 100, 1),
        "tp2_pct": round((price - tp2) / price * 100, 1),
        "tp3_pct": round((price - tp3) / price * 100, 1),
        "rr_tp1": round((price - tp1) / risk, 2),
        "rr_tp2": round((price - tp2) / risk, 2),
        "risk": round(risk, 2),
        "setup_detail": {
            "wave_a_start": round(wave_a_start, 2), "wave_a_end": round(wave_a_end, 2),
            "wave_b_high":  round(wave_b_high, 2),
        }
    }


# ── Position Sizing ───────────────────────────────────────────────────────────

def position_size(price, stop, direction="LONG"):
    account      = get_account_equity()
    risk_dollars = account * RISK_PCT
    risk         = abs(price - stop)
    if risk <= 0:
        risk = price * 0.05
    shares     = math.floor(risk_dollars / risk)
    if shares < 1:
        shares = 1
    # Cap: max 5% of account in any single position
    max_position_val = account * 0.05
    max_shares       = math.floor(max_position_val / price) if price > 0 else shares
    shares           = min(shares, max(1, max_shares))
    return {
        "shares":       shares,
        "risk_dollars": round(risk_dollars, 2),
        "position_val": round(shares * price, 2),
        "pct_account":  round(shares * price / account * 100, 1),
    }


# ── Main Analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker, seen=None):
    if seen is None:
        seen = load_seen()

    try:
        df = get_price_data(ticker)
        if df is None or len(df) < 60:
            return None

        # ── v9: Hard price filter before any analysis ──
        current_price = float(df.iloc[-1]["Close"])
        if current_price < MIN_PRICE:
            return None

        # ── v9: Hard avg volume filter ──
        df_ind = compute_indicators(df)
        avg_vol = float(df_ind.iloc[-1]["VolAvg20"])
        if avg_vol < MIN_AVG_VOLUME:
            return None

        trend = _trend_context(df)

        setup = (
            detect_wave2_setup(df, trend) or
            detect_wave4_setup(df, trend) or
            detect_abc_setup(df, trend)   or
            detect_wave2_short(df, trend) or
            detect_wave4_short(df, trend) or
            detect_abc_short(df, trend)
        )
        if setup is None:
            return None

        price      = setup["price"]
        setup_name = setup["setup"]
        direction  = setup.get("direction", "LONG")

        if already_alerted(ticker, setup_name, price, seen):
            print(f"[{ticker}] SKIP — already alerted '{setup_name}' at similar price")
            return None
        mark_seen(ticker, setup_name, price, seen)

        fund = get_fundamentals(ticker)
        q_score, failed = quality_score(fund)
        if q_score < MIN_QUALITY_SCORE:
            print(f"[{ticker}] SKIP | Quality {q_score:.0f} < {MIN_QUALITY_SCORE} | {failed}")
            return None

        stop   = setup["stop"]
        sizing = position_size(price, stop, direction)

        # ── Signal scoring ──
        score = 0.0
        score += q_score * 0.35

        rsi = setup["rsi"]
        if direction == "LONG":
            if 35 <= rsi <= 55:  score += 25
            elif 55 < rsi <= 65: score += 15
            else:                score += 5
        else:  # SHORT
            if 45 <= rsi <= 65:  score += 25
            elif 35 <= rsi < 45: score += 15
            else:                score += 5

        if setup["vol_confirmed"]: score += 20
        else:                      score += 8

        rr = setup.get("rr_tp2", 0)
        if rr >= 4.0:   score += 20
        elif rr >= 2.0: score += 12
        else:           score += 5

        # Trend alignment bonus/penalty
        if direction == "LONG"  and trend == "up":    score += 10
        if direction == "LONG"  and trend == "down":  score -= 15
        if direction == "SHORT" and trend == "down":  score += 10
        if direction == "SHORT" and trend == "up":    score -= 15

        score = max(0.0, score)

        # ── v9: auto-execute only if vol confirmed AND score >= MIN_SIGNAL_SCORE ──
        if setup.get("vol_confirmed", False) and score >= MIN_SIGNAL_SCORE:
            signal_type = "BUY" if direction == "LONG" else "SHORT"
        else:
            signal_type = "WATCH"

        hold_time = "POSITION (3-8 weeks)" if score >= 70 else "SWING (1-3 weeks)"

        print(
            f"[{ticker}] {signal_type} | {setup_name} | "
            f"Trend:{trend} | Score {score:.0f} | RSI {rsi:.0f} | "
            f"R:R TP2 {setup.get('rr_tp2', 0):.1f}x | "
            f"Vol {setup.get('vol_ratio', 0):.1f}x"
        )

        return {
            "ticker":        ticker,
            "signal":        signal_type,
            "direction":     direction,
            "setup":         setup_name,
            "trend":         trend,
            "hold_time":     hold_time,
            "signal_score":  round(score, 1),
            "quality_score": q_score,
            "price":         round(price, 2),
            "rsi":           setup["rsi"],
            "atr":           setup["atr"],
            "vol_ratio":     setup["vol_ratio"],
            "vol_confirmed": setup["vol_confirmed"],
            "stop":          setup["stop"],
            "tp1":           setup["tp1"],
            "tp2":           setup["tp2"],
            "tp3":           setup["tp3"],
            "tp1_pct":       setup["tp1_pct"],
            "tp2_pct":       setup["tp2_pct"],
            "tp3_pct":       setup["tp3_pct"],
            "rr_tp1":        setup.get("rr_tp1", 0),
            "rr_tp2":        setup.get("rr_tp2", 0),
            "shares":        sizing["shares"],
            "risk_dollars":  sizing["risk_dollars"],
            "position_val":  sizing["position_val"],
            "pct_account":   sizing["pct_account"],
            "roe":           round(fund.get("roe", 0) * 100, 1),
            "gross_margin":  round(fund.get("gross_margin", 0) * 100, 1),
            "eps_growth":    round(fund.get("eps_growth", 0) * 100, 1),
            "debt_equity":   round(fund.get("debt_equity", 0), 2),
            "pe_ratio":      round(fund.get("pe_ratio", 0), 1),
            "sector":        fund.get("sector", ""),
            "name":          fund.get("name", ticker),
            "quality_notes": failed,
            "fund_missing":  fund.get("_data_missing", False),
            "setup_detail":  setup.get("setup_detail", {}),
        }

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-300:]}")
        return None


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"Elliott Wave Scanner — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    tickers = get_universe()
    print(f"Scanning {len(tickers)} tickers...\n")

    seen    = load_seen()
    results = []

    for ticker in tickers:
        result = analyze_ticker(ticker, seen)
        if result:
            results.append(result)

    save_seen(seen)
    results.sort(key=lambda x: x["signal_score"], reverse=True)

    print(f"\n{'='*60}")
    print(f"Found {len(results)} setup(s)")
    print(f"{'='*60}\n")

    for r in results:
        fund_flag  = " [no Finnhub data]" if r.get("fund_missing") else ""
        dir_icon   = "▼ SHORT" if r["direction"] == "SHORT" else "▲ LONG"
        sig_icon   = "★" if r["signal"] in ("BUY", "SHORT") else "○"
        print(
            f"{sig_icon} {r['ticker']:6s} | {dir_icon} | "
            f"{r['setup']:18s} | Score {r['signal_score']:.0f} | "
            f"Price ${r['price']} | TP2 ${r['tp2']} ({r['tp2_pct']:+.1f}%) | "
            f"R:R {r['rr_tp2']:.1f}x | RSI {r['rsi']} | Trend:{r['trend']}{fund_flag}"
        )
