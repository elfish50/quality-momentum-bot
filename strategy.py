"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

PATCH v5 — Universe via Alpaca APIs (replaces Finviz)
══════════════════════════════════════════════════════
Root cause of the 50-ticker problem:
  Finviz blocks requests from cloud/VPS servers (Railway, Heroku, etc.)
  with a 403 Forbidden error. finvizfinance silently returns only the
  first page (20-50 rows) before getting blocked.

  On weekends, Alpaca's live dailyBar snapshots are empty, so volume
  filters wiped everything out. Fixed by falling back to prevDailyBar
  (previous close data) when dailyBar is unavailable.

Fix: get_universe() uses only Alpaca APIs — works from Railway,
     works on weekends, works at any time of day.

Universe built in 2 layers:
  Layer 1: Alpaca most-actives endpoint (~100 high-volume stocks)
  Layer 2: Random sample from all active Alpaca assets, filtered by
           price > $5 and volume > 300k via snapshot batch calls
           (uses prevDailyBar fallback when market is closed)

All previous patches preserved:
  v4: Dynamic universe
  v3: Deduplication, swing recency filter, seen expiry
  v2: Weekly trend filter softened, RSI/wave thresholds relaxed
"""

import os
import json
import math
import pathlib
import random
import traceback
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta

# ── Keys ──────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

ALPACA_URL        = "https://data.alpaca.markets/v2"
ALPACA_SCREEN_URL = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
ALPACA_ASSETS_URL = "https://api.alpaca.markets/v2/assets"
FINNHUB_URL       = "https://finnhub.io/api/v1"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# ── Account ───────────────────────────────────────────────────────────────────
ACCOUNT  = 1_000
RISK_PCT = 0.10

# ── Fibonacci levels ──────────────────────────────────────────────────────────
FIB_382 = 0.382
FIB_500 = 0.500
FIB_618 = 0.618
FIB_786 = 0.786

EXT_1272 = 1.272
EXT_1618 = 1.618
EXT_2618 = 2.618

# ── Strategy thresholds ───────────────────────────────────────────────────────
VOL_CONFIRM_RATIO = 1.2
MIN_QUALITY_SCORE = 40
MIN_WAVE1_MOVE    = 0.05
MIN_RR_TP2        = 2.0

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


# ── Dynamic Universe via Alpaca (v5) ──────────────────────────────────────────

def _get_most_actives() -> list:
    """
    Top 100 most-active US stocks by volume from Alpaca's screener.
    Works on weekends — this endpoint returns historical data, not live.
    """
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
    """All active tradable US equity assets from Alpaca (~8000+ symbols)."""
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


def _snapshot_filter(tickers: list, min_price: float = 5.0, min_volume: float = 300_000) -> list:
    """
    Filter tickers by price and volume using Alpaca snapshots.

    Weekend/after-hours safe: tries dailyBar first (live session data),
    falls back to prevDailyBar (last completed trading day) when the
    market is closed and dailyBar is empty. This ensures the universe
    is always populated regardless of what day/time the scan runs.
    """
    filtered   = []
    chunk_size = 100

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i: i + chunk_size]
        try:
            r = requests.get(
                f"{ALPACA_URL}/stocks/snapshots",
                headers=HEADERS,
                params={"symbols": ",".join(chunk), "feed": "iex"},
                timeout=20
            )
            if not r.ok:
                continue
            for sym, snap in r.json().items():
                try:
                    # dailyBar = live today | prevDailyBar = last close (weekend safe)
                    daily  = snap.get("dailyBar") or snap.get("prevDailyBar") or {}
                    close  = float(daily.get("c", 0) or 0)
                    volume = float(daily.get("v", 0) or 0)
                    if close >= min_price and volume >= min_volume:
                        filtered.append(sym)
                except Exception:
                    continue
        except Exception as e:
            print(f"[universe] snapshot chunk error: {e}")

    print(f"[universe] after price/volume filter: {len(filtered)} tickers")
    return filtered


def get_universe() -> list:
    """
    Build a fresh, varied universe every run using only Alpaca APIs.
    No Finviz — no 403 blocks, no scraping, works from any server.

    Layer 1: most-actives (top 100 by volume — always relevant)
    Layer 2: random sample of all Alpaca assets, filtered by price+volume
             using prevDailyBar fallback so weekends work fine

    Shuffled so each scan sees stocks in a different order.
    Falls back to FALLBACK_TICKERS only if all Alpaca calls fail.
    """
    result = []

    # Layer 1 — most actives
    most_actives = _get_most_actives()
    result.extend(most_actives)

    # Layer 2 — random sample from full asset list
    assets = _get_alpaca_assets()
    if assets:
        sample_size = min(len(assets), UNIVERSE_SIZE * 4)
        sample      = random.sample(assets, sample_size)
        filtered    = _snapshot_filter(sample)
        result.extend(filtered)

    # Deduplicate, preserving order
    seen_set = set()
    deduped  = []
    for t in result:
        t = t.upper().strip()
        if t and t not in seen_set:
            seen_set.add(t)
            deduped.append(t)

    if len(deduped) < 10:
        print(f"[universe] only {len(deduped)} tickers — using fallback")
        return list(FALLBACK_TICKERS)

    # Keep most-actives at front, shuffle the rest
    n_front = len(most_actives)
    front   = deduped[:n_front]
    rest    = deduped[n_front:]
    random.shuffle(rest)
    final   = (front + rest)[:UNIVERSE_SIZE]

    print(f"[universe] final universe: {len(final)} tickers")
    return final


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


def weekly_trend_is_up(df):
    try:
        closes_daily = df["Close"]
        sma200       = closes_daily.rolling(200).mean().iloc[-1]
        price        = float(closes_daily.iloc[-1])
        above_sma200 = not pd.isna(sma200) and price > float(sma200)

        weekly = get_weekly_bars(df)
        if len(weekly) < 22:
            return True

        close    = weekly["Close"]
        sma10w   = close.rolling(10).mean()
        sma20w   = close.rolling(20).mean()
        cur_10   = float(sma10w.iloc[-1])
        cur_20   = float(sma20w.iloc[-1])
        prev_10  = float(sma10w.iloc[-2])
        prev2_10 = float(sma10w.iloc[-3])

        if cur_10 > cur_20 and cur_10 > prev_10:   return True
        if cur_10 > prev_10 > prev2_10:             return True
        if above_sma200:                             return True
        return False
    except Exception:
        return True


# ── Fundamentals ──────────────────────────────────────────────────────────────

def get_fundamentals(ticker):
    def safe(v, d=0.0):
        try: return float(v) if v not in (None, "", "N/A", "None") else d
        except: return d
    try:
        r1 = requests.get(
            f"{FINNHUB_URL}/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": FINNHUB_KEY},
            timeout=15
        )
        m  = r1.json().get("metric", {})
        r2 = requests.get(
            f"{FINNHUB_URL}/stock/profile2",
            params={"symbol": ticker, "token": FINNHUB_KEY},
            timeout=15
        )
        p  = r2.json()
        return {
            "roe":          safe(m.get("roeTTM")) / 100,
            "gross_margin": safe(m.get("grossMarginTTM")) / 100,
            "debt_equity":  safe(m.get("totalDebt/totalEquityAnnual"), 999),
            "eps_growth":   safe(m.get("epsGrowth3Y")) / 100,
            "pe_ratio":     safe(m.get("peNormalizedAnnual")),
            "sector":       p.get("finnhubIndustry", "Unknown"),
            "name":         p.get("name", ticker),
            "market_cap":   safe(p.get("marketCapitalization")) * 1_000_000,
        }
    except Exception as e:
        print(f"[{ticker}] Finnhub error: {e}")
        return {}


def quality_score(fund):
    score, failed = 0.0, []

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


def find_swing_points(df, lookback=90, min_bars=5, max_age_bars=20):
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


# ── Wave 2 Setup ──────────────────────────────────────────────────────────────

def detect_wave2_setup(df):
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5, max_age_bars=20)
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

    if not (fib_786 <= price <= fib_500):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi > rsi_prev and 25 <= rsi <= 65):
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
        "setup": "Wave 2 Pullback",
        "wave1_origin": round(wave1_origin, 2), "wave1_top": round(wave1_top, 2),
        "wave1_size": round(wave1_size, 2),
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
            "wave1_size": round(wave1_size, 2),
            "fib_382": fib_382, "fib_500": fib_500, "fib_618": fib_618,
        }
    }


# ── Wave 4 Setup ──────────────────────────────────────────────────────────────

def detect_wave4_setup(df):
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5, max_age_bars=20)
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
    if not (rsi > rsi_prev and 30 <= rsi <= 70):
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
        "setup": "Wave 4 Pullback",
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
            "wave3_high": round(wave3_high, 2),
            "fib_382": fib_382_w4, "fib_500": fib_500_w4,
        }
    }


# ── ABC Setup ─────────────────────────────────────────────────────────────────

def detect_abc_setup(df):
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4, max_age_bars=20)
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
    if not (rsi > rsi_prev and 25 <= rsi <= 55):
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
        "setup": "ABC Correction",
        "wave_a_start": round(wave_a_start, 2), "wave_a_end": round(wave_a_end, 2),
        "wave_c_low": round(wave_c_low, 2),
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
            "wave_c_low": round(wave_c_low, 2),
        }
    }


# ── Position Sizing ───────────────────────────────────────────────────────────

def position_size(price, stop):
    risk_dollars = ACCOUNT * RISK_PCT
    risk         = price - stop
    if risk <= 0:
        risk = price * 0.05
    shares     = math.floor(risk_dollars / risk)
    if shares < 1:
        shares = 1
    max_shares = math.floor(ACCOUNT / price) if price > 0 else shares
    shares     = min(shares, max_shares)
    return {
        "shares":       shares,
        "risk_dollars": round(risk_dollars, 2),
        "position_val": round(shares * price, 2),
        "pct_account":  round(shares * price / ACCOUNT * 100, 1),
    }


# ── Main Analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker, seen=None):
    if seen is None:
        seen = load_seen()

    try:
        df = get_price_data(ticker)
        if df is None or len(df) < 60:
            return None

        setup = (
            detect_wave2_setup(df) or
            detect_wave4_setup(df) or
            detect_abc_setup(df)
        )
        if setup is None:
            return None

        price      = setup["price"]
        setup_name = setup["setup"]

        if already_alerted(ticker, setup_name, price, seen):
            print(f"[{ticker}] SKIP — already alerted '{setup_name}' at similar price")
            return None
        mark_seen(ticker, setup_name, price, seen)

        fund = get_fundamentals(ticker)
        if not fund:
            return None

        q_score, failed = quality_score(fund)
        if q_score < MIN_QUALITY_SCORE:
            print(f"[{ticker}] SKIP | Quality {q_score:.0f} < {MIN_QUALITY_SCORE}")
            return None

        stop   = setup["stop"]
        sizing = position_size(price, stop)

        score = 0.0
        score += q_score * 0.35
        rsi    = setup["rsi"]
        if 35 <= rsi <= 55:  score += 25
        elif 55 < rsi <= 65: score += 15
        else:                score += 5
        if setup["vol_confirmed"]: score += 20
        else:                      score += 8
        rr = setup.get("rr_tp2", 0)
        if rr >= 4.0:   score += 20
        elif rr >= 2.0: score += 12
        else:           score += 5

        signal_type = "BUY" if setup.get("vol_confirmed", False) else "WATCH"
        hold_time   = "POSITION (3-8 weeks)" if score >= 70 else "SWING (1-3 weeks)"

        print(
            f"[{ticker}] {signal_type} | {setup_name} | "
            f"Score {score:.0f} | RSI {rsi:.0f} | "
            f"R:R TP2 {setup.get('rr_tp2', 0):.1f}x | "
            f"Vol {setup.get('vol_ratio', 0):.1f}x"
        )

        return {
            "ticker":        ticker,
            "signal":        signal_type,
            "setup":         setup_name,
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
        print(
            f"{'★' if r['signal'] == 'BUY' else '○'} {r['ticker']:6s} | "
            f"{r['setup']:18s} | Score {r['signal_score']:.0f} | "
            f"Price ${r['price']} | TP2 ${r['tp2']} ({r['tp2_pct']:+.1f}%) | "
            f"R:R {r['rr_tp2']:.1f}x | RSI {r['rsi']}"
        )
