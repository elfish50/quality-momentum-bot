"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

PATCH v8 — Loosened filters + SHORT (downside) setups
════════════════════════════════════════════════════════════════
Changes from v7:
  - weekly_trend_is_up() is now a soft filter: bearish trend reduces score
    instead of hard-rejecting the setup
  - Wave 2 Fib entry window widened: upper bound moved from 50% to 38.2%
    (accepts shallower pullbacks)
  - RSI range widened: 20–70 (was 25–65)
  - MIN_WAVE1_MOVE lowered: 0.03 (was 0.05) — catches smaller impulse moves
  - VOL_CONFIRM_RATIO lowered: 1.1 (was 1.2) — more BUYs vs WATCH
  - MIN_RR_TP2 lowered: 1.5 (was 2.0) — accepts tighter reward/risk

  NEW SHORT SETUPS (signal = "SHORT"):
  - Wave 2 Short: price bounces into 50–78.6% Fib after impulsive drop
  - Wave 4 Short: price bounces into 38.2% Fib in confirmed downtrend
  - ABC Short: price recovers to Wave A start zone, ready to resume down

  Short signals require weekly_trend_is_DOWN to be confirmed.
  Trader must handle SHORT signals via Alpaca short-sell orders.

All v7 patches preserved:
  v7: Finnhub fix, retry on 429, debug logging
  v6: Universe size, swing window, quality threshold
  v5: Dynamic universe via Alpaca APIs
  v4: Dynamic universe
  v3: Deduplication, swing recency filter, seen expiry
  v2: RSI/wave thresholds relaxed
"""

import os
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

# ── Strategy thresholds (v8 — loosened) ──────────────────────────────────────
VOL_CONFIRM_RATIO = 1.1    # was 1.2
MIN_QUALITY_SCORE = 25
MIN_WAVE1_MOVE    = 0.03   # was 0.05
MIN_RR_TP2        = 1.5    # was 2.0

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


def _snapshot_filter(tickers: list, min_price: float = 5.0, min_volume: float = 300_000) -> list:
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
    Used as a soft filter — doesn't reject setups, just informs signal scoring.
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


# Keep old name as alias so existing calls don't break
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
            "market_cap":    safe(p.get("marketCapitalization")) * 1_000_000,
            "_data_missing": data_missing,
        }

    except Exception as e:
        print(f"[{ticker}] Finnhub get_fundamentals exception: {e}")
        default = dict(_FUND_DEFAULT)
        default["name"] = ticker
        return default


def quality_score(fund: dict):
    score, failed = 0.0, []

    if fund.get("_data_missing"):
        return 30.0, ["⚠ Finnhub data unavailable — using neutral score"]

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
    """
    Wave 2 Pullback (LONG).
    Price retraces 38.2–78.6% of Wave 1 impulse up, then reverses.
    Trend is now soft — 'down' trend reduces score but does not reject.
    """
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

    # v8: upper bound widened from fib_500 to fib_382
    if not (fib_786 <= price <= fib_382):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    # v8: RSI range widened to 20–70
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
# SHORT SETUPS
# ══════════════════════════════════════════════════════════════════════════════

def detect_wave2_short(df, trend: str):
    """
    Wave 2 Short — mirror of Wave 2 Pullback but for downtrends.
    After an impulsive drop (Wave 1 down), price bounces 50–78.6% Fib,
    RSI rolling over from overbought — enter SHORT, stop above bounce high.
    Only fires if trend is 'down' or 'neutral'.
    """
    if trend == "up":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5, max_age_bars=40)
    if len(highs) < 1 or len(lows) < 1:
        return None

    # Wave 1 down: from a swing HIGH down to a swing LOW
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

    # Bounce into 50–78.6% retracement of the down-move
    fib_382 = round(wave1_bottom + wave1_size * FIB_382, 2)
    fib_500 = round(wave1_bottom + wave1_size * FIB_500, 2)
    fib_618 = round(wave1_bottom + wave1_size * FIB_618, 2)
    fib_786 = round(wave1_bottom + wave1_size * FIB_786, 2)

    if not (fib_500 <= price <= fib_786):
        return None

    # RSI rolling over (falling) from elevated level — confirming reversal
    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi < rsi_prev and 35 <= rsi <= 75):
        return None

    # Price making lower high — not breaking above the bounce
    if price > float(df.iloc[-4:-1]["High"].max()) * 0.998:
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)

    # Stop: just above wave1_top (bounce invalidation)
    stop     = round(wave1_top * 1.01, 2)
    ext_1272 = round(price - wave1_size * EXT_1272, 2)
    ext_1618 = round(price - wave1_size * EXT_1618, 2)
    ext_2618 = round(price - wave1_size * EXT_2618, 2)
    risk     = stop - price  # positive: how much we lose if stop hit

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
        # For shorts: tp1/tp2/tp3 are BELOW entry
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
    """
    Wave 4 Short — price bounces into 38.2% Fib of the down-impulse
    after Wave 3 down, ready for Wave 5 continuation lower.
    Only fires if trend is 'down'.
    """
    if trend != "down":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5, max_age_bars=40)
    if len(highs) < 2 or len(lows) < 1:
        return None

    # Need: high → low → lower high → lower low (simplified Wave 3 structure)
    wave1_top_loc,    wave1_top    = highs[0]
    wave3_bottom_loc, wave3_bottom = lows[-1]
    # Current bounce must have a recent local high
    bounce_high_loc, bounce_high = highs[-1]

    if not (wave1_top_loc < wave3_bottom_loc):
        return None
    if bounce_high_loc < wave3_bottom_loc:
        return None

    wave3_size  = wave1_top - wave3_bottom
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
    ABC Short — price makes A-down, B-up bounce back near A start,
    then enter SHORT for C-down continuation.
    Only fires if trend is 'down' or 'neutral'.
    """
    if trend == "up":
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4, max_age_bars=40)
    if len(highs) < 2 or len(lows) < 1:
        return None

    # Wave A: high → low (drop)
    wave_a_start_loc, wave_a_start = highs[-2]
    wave_a_end_loc,   wave_a_end   = lows[-1]
    # Wave B: bounce back toward A start
    wave_b_loc,       wave_b_high  = highs[-1]

    if not (wave_a_start_loc < wave_a_end_loc < wave_b_loc):
        return None

    wave_a_size = wave_a_start - wave_a_end
    if wave_a_size <= 0 or wave_a_size / wave_a_start < 0.05:
        return None

    # B bounce must reach 50–100% of A drop (classic ABC structure)
    b_zone_low  = round(wave_a_end + wave_a_size * FIB_500, 2)
    b_zone_high = round(wave_a_start + wave_a_size * 0.05, 2)  # slight overshoot allowed

    if not (b_zone_low <= price <= b_zone_high):
        return None

    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    if not (rsi < rsi_prev and 40 <= rsi <= 75):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(b_zone_high * 1.01, 2)
    tp1  = round(wave_a_end, 2)                              # C = A low
    tp2  = round(wave_a_end - wave_a_size * FIB_618, 2)     # C extends 161.8%
    tp3  = round(wave_a_end - wave_a_size * 1.0, 2)         # C = 2× A
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
    risk_dollars = ACCOUNT * RISK_PCT
    risk         = abs(price - stop)
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

        # Determine trend context once — used by all detectors
        trend = _trend_context(df)

        # Run all detectors — first match wins (long before short priority)
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
            print(f"[{ticker}] SKIP | Quality {q_score:.0f} < {MIN_QUALITY_SCORE}")
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
        if direction == "LONG"  and trend == "up":   score += 10
        if direction == "LONG"  and trend == "down":  score -= 15
        if direction == "SHORT" and trend == "down":  score += 10
        if direction == "SHORT" and trend == "up":    score -= 15

        score = max(0.0, score)

        # Signal type: BUY / SHORT / WATCH
        if setup.get("vol_confirmed", False):
            signal_type = direction  # "LONG" → "BUY" below, "SHORT" stays "SHORT"
            if direction == "LONG":
                signal_type = "BUY"
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
