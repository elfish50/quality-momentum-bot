"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

PATCH v4 — Dynamic Ticker Universe via Finviz Screener
═══════════════════════════════════════════════════════
The core problem: a static list of ~219 tickers means the scanner always
sees the same stocks and finds the same setups.

Fix: get_universe() pulls a FRESH list of candidates from Finviz every run.
Finviz filters are intentionally minimal — only price and volume floors.
All real filtering (trend, RSI, quality) happens inside the strategy logic
on actual price data, which is far more accurate than Finviz's preset labels.

All v3 patches are preserved:
  v3: Deduplication (seen_setups.json), swing recency filter (max_age_bars),
      auto-expiry of seen entries after SEEN_EXPIRY_DAYS.
  v2: Weekly trend filter softened, RSI windows widened, MIN_WAVE1_MOVE
      lowered, VOL_CONFIRM_RATIO lowered.

Install: pip install finvizfinance
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

# ── Finviz screener ───────────────────────────────────────────────────────────
try:
    from finvizfinance.screener.ticker import Ticker as FinvizTicker
    FINVIZ_AVAILABLE = True
except ImportError:
    FINVIZ_AVAILABLE = False
    print("WARNING: finvizfinance not installed. Run: pip install finvizfinance")
    print("Falling back to FALLBACK_TICKERS list below.")

# ── Keys ──────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

ALPACA_URL  = "https://data.alpaca.markets/v2"
FINNHUB_URL = "https://finnhub.io/api/v1"

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

# ── Screener config ───────────────────────────────────────────────────────────
# Keep UNIVERSE_SIZE high — the strategy's own filters will trim it down.
UNIVERSE_SIZE = 500

# INTENTIONALLY MINIMAL — do not add SMA, RSI, or Market Cap filters here.
# Those Finviz presets map to narrow bands and cap results at ~50 tickers.
# All real filtering is done on actual Alpaca price data inside the strategy.
FINVIZ_FILTERS = {
    "Price":          "Over $5",      # avoid penny stocks
    "Average Volume": "Over 300K",    # need liquidity for clean price data
    "Country":        "USA",          # US-listed only (Alpaca coverage)
}

# Fallback list if finvizfinance is not installed or Finviz is unreachable.
FALLBACK_TICKERS = [
    "AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","JPM","V","UNH",
    "LLY","XOM","JNJ","PG","MA","HD","MRK","ABBV","CVX","AVGO",
    "PEP","KO","COST","WMT","MCD","ADBE","CRM","ACN","TMO","NFLX",
    "QCOM","INTC","AMD","TXN","HON","UPS","CAT","GS","MS","AXP",
    "BA","GE","MMM","LMT","RTX","DE","EMR","ITW","ETN","PH",
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


# ── Dynamic Universe (v4) ─────────────────────────────────────────────────────

def get_universe() -> list:
    """
    Pull a fresh list of candidate tickers from Finviz.

    Uses minimal filters (price > $5, volume > 300K, USA) so Finviz returns
    a large, varied universe. All Elliott Wave and quality filtering happens
    downstream on real Alpaca price data — not on Finviz preset labels.

    Results are shuffled so each run scans stocks in a different order,
    preventing the same tickers from always being evaluated first.
    """
    if not FINVIZ_AVAILABLE:
        print(f"[universe] finvizfinance not available — using {len(FALLBACK_TICKERS)} fallback tickers")
        return list(FALLBACK_TICKERS)

    try:
        screener = FinvizTicker()
        screener.set_filter(filters_dict=FINVIZ_FILTERS)
        df = screener.screener_view()

        if df is None or len(df) == 0:
            print("[universe] Finviz returned 0 results — using fallback tickers")
            return list(FALLBACK_TICKERS)

        # Handle both possible column names
        if "Ticker" in df.columns:
            tickers = df["Ticker"].dropna().tolist()
        else:
            tickers = df.iloc[:, 0].dropna().tolist()

        tickers = [str(t).strip().upper() for t in tickers if t and str(t).strip()]

        if len(tickers) == 0:
            print("[universe] Finviz returned 0 valid tickers — using fallback")
            return list(FALLBACK_TICKERS)

        # Shuffle so each run sees stocks in a different order
        random.shuffle(tickers)
        tickers = tickers[:UNIVERSE_SIZE]

        print(f"[universe] Finviz returned {len(tickers)} candidates")
        return tickers

    except Exception as e:
        print(f"[universe] Finviz error: {e} — using fallback tickers")
        return list(FALLBACK_TICKERS)


# ── Price Data ────────────────────────────────────────────────────────────────

def get_price_data(ticker):
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    for feed in ["iex", "sip"]:
        try:
            params = {
                "start":      start,
                "end":        end,
                "timeframe":  "1Day",
                "limit":      400,
                "feed":       feed,
                "adjustment": "split",
            }
            r    = requests.get(
                f"{ALPACA_URL}/stocks/{ticker}/bars",
                headers=headers, params=params, timeout=15
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
            continue
    return None


def get_weekly_bars(df):
    df2 = df.copy()
    df2["Date"] = pd.to_datetime(df2["Date"])
    df2 = df2.set_index("Date")
    weekly = df2.resample("W").agg({
        "Open":   "first",
        "High":   "max",
        "Low":    "min",
        "Close":  "last",
        "Volume": "sum",
    }).dropna()
    return weekly.reset_index()


def weekly_trend_is_up(df):
    """
    3 ways to pass (v2):
    1. Classic: SMA10w > SMA20w AND rising
    2. Early recovery: SMA10w rising 2+ consecutive weeks
    3. Price above daily SMA200
    """
    try:
        closes_daily = df["Close"]
        sma200_daily = closes_daily.rolling(200).mean().iloc[-1]
        price_daily  = float(closes_daily.iloc[-1])
        above_sma200 = not pd.isna(sma200_daily) and price_daily > float(sma200_daily)

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

        if cur_10 > cur_20 and cur_10 > prev_10:
            return True
        if cur_10 > prev_10 > prev2_10:
            return True
        if above_sma200:
            return True

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
    df = df.copy()
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
    df["ATR"] = tr.rolling(14).mean()

    df["SMA50"]    = close.rolling(50).mean()
    df["SMA200"]   = close.rolling(200).mean()
    df["EMA21"]    = close.ewm(span=21, adjust=False).mean()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()

    return df


def find_swing_points(df, lookback=90, min_bars=5, max_age_bars=20):
    """
    v3: max_age_bars filter — only swings formed within the last N bars qualify.
    Prevents the scanner from re-firing on stale multi-week-old swing points.
    """
    n      = len(df)
    start  = max(0, n - lookback)
    highs  = []
    lows   = []

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


# ── Volume helper ─────────────────────────────────────────────────────────────

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
    if wave1_size <= 0:
        return None
    if wave1_size / wave1_origin < MIN_WAVE1_MOVE:
        return None

    fib_382 = round(wave1_top - wave1_size * FIB_382, 2)
    fib_500 = round(wave1_top - wave1_size * FIB_500, 2)
    fib_618 = round(wave1_top - wave1_size * FIB_618, 2)
    fib_786 = round(wave1_top - wave1_size * FIB_786, 2)

    if not (fib_786 <= price <= fib_500):
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 25 <= rsi <= 65

    if not (rsi_rising and rsi_ok):
        return None

    if price < float(df.iloc[-4:-1]["Low"].min()) * 1.002:
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(wave1_origin * 0.99, 2)

    ext_1272 = round(price + wave1_size * EXT_1272, 2)
    ext_1618 = round(price + wave1_size * EXT_1618, 2)
    ext_2618 = round(price + wave1_size * EXT_2618, 2)

    risk = price - stop
    if risk <= 0:
        return None

    rr_tp2 = round((ext_1618 - price) / risk, 2)
    if rr_tp2 < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":         "Wave 2 Pullback",
        "wave1_origin":  round(wave1_origin, 2),
        "wave1_top":     round(wave1_top, 2),
        "wave1_size":    round(wave1_size, 2),
        "fib_382":       fib_382,
        "fib_500":       fib_500,
        "fib_618":       fib_618,
        "fib_786":       fib_786,
        "price":         round(price, 2),
        "rsi":           round(rsi, 1),
        "atr":           round(atr, 2),
        "vol_ratio":     vol_ratio,
        "vol_confirmed": vol_confirmed,
        "stop":          stop,
        "tp1":           round(price + wave1_size * EXT_1272, 2),
        "tp2":           ext_1618,
        "tp3":           ext_2618,
        "tp1_pct":       round((price + wave1_size * EXT_1272 - price) / price * 100, 1),
        "tp2_pct":       round((ext_1618 - price) / price * 100, 1),
        "tp3_pct":       round((ext_2618 - price) / price * 100, 1),
        "rr_tp1":        round((price + wave1_size * EXT_1272 - price) / risk, 2),
        "rr_tp2":        rr_tp2,
        "risk":          round(risk, 2),
        "setup_detail": {
            "wave1_origin": round(wave1_origin, 2),
            "wave1_top":    round(wave1_top, 2),
            "wave1_size":   round(wave1_size, 2),
            "fib_382":      fib_382,
            "fib_500":      fib_500,
            "fib_618":      fib_618,
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

    if not (fib_500_w4 <= price <= fib_382_w4):
        return None
    if price < wave1_high:
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 30 <= rsi <= 70

    if not (rsi_rising and rsi_ok):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(wave1_high * 0.99, 2)

    wave1_size = wave1_high - wave1_origin
    ext_1272   = round(price + wave1_size * EXT_1272, 2)
    ext_1618   = round(price + wave1_size * EXT_1618, 2)
    ext_2618   = round(price + wave1_size * EXT_2618, 2)

    risk = price - stop
    if risk <= 0:
        return None

    rr_tp2 = round((ext_1618 - price) / risk, 2)
    if rr_tp2 < MIN_RR_TP2:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":         "Wave 4 Pullback",
        "wave1_origin":  round(wave1_origin, 2),
        "wave1_high":    round(wave1_high, 2),
        "wave3_high":    round(wave3_high, 2),
        "fib_382":       fib_382_w4,
        "fib_500":       fib_500_w4,
        "price":         round(price, 2),
        "rsi":           round(rsi, 1),
        "atr":           round(atr, 2),
        "vol_ratio":     vol_ratio,
        "vol_confirmed": vol_confirmed,
        "stop":          stop,
        "tp1":           ext_1272,
        "tp2":           ext_1618,
        "tp3":           ext_2618,
        "tp1_pct":       round((ext_1272 - price) / price * 100, 1),
        "tp2_pct":       round((ext_1618 - price) / price * 100, 1),
        "tp3_pct":       round((ext_2618 - price) / price * 100, 1),
        "rr_tp1":        round((ext_1272 - price) / risk, 2),
        "rr_tp2":        rr_tp2,
        "risk":          round(risk, 2),
        "setup_detail": {
            "wave1_origin": round(wave1_origin, 2),
            "wave1_high":   round(wave1_high, 2),
            "wave3_high":   round(wave3_high, 2),
            "fib_382":      fib_382_w4,
            "fib_500":      fib_500_w4,
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
    if wave_a_size <= 0:
        return None
    if wave_a_size / wave_a_start < 0.05:
        return None

    c_zone_low  = round(wave_a_end - wave_a_size * 0.20, 2)
    c_zone_high = round(wave_a_end + wave_a_size * 0.10, 2)

    if not (c_zone_low <= price <= c_zone_high):
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 25 <= rsi <= 55

    if not (rsi_rising and rsi_ok):
        return None

    vol_confirmed, vol_ratio = _vol_confirmed(df)
    stop = round(wave_a_end * 0.99, 2)

    tp1 = round(wave_a_start, 2)
    tp2 = round(wave_a_start + wave_a_size * FIB_618, 2)
    tp3 = round(wave_a_start + wave_a_size * 1.0, 2)

    risk = price - stop
    if risk <= 0:
        return None

    rr_tp1 = round((tp1 - price) / risk, 2)
    rr_tp2 = round((tp2 - price) / risk, 2)

    if rr_tp1 < 1.5:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":         "ABC Correction",
        "wave_a_start":  round(wave_a_start, 2),
        "wave_a_end":    round(wave_a_end, 2),
        "wave_c_low":    round(wave_c_low, 2),
        "price":         round(price, 2),
        "rsi":           round(rsi, 1),
        "atr":           round(atr, 2),
        "vol_ratio":     vol_ratio,
        "vol_confirmed": vol_confirmed,
        "stop":          stop,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           tp3,
        "tp1_pct":       round((tp1 - price) / price * 100, 1),
        "tp2_pct":       round((tp2 - price) / price * 100, 1),
        "tp3_pct":       round((tp3 - price) / price * 100, 1),
        "rr_tp1":        rr_tp1,
        "rr_tp2":        rr_tp2,
        "risk":          round(risk, 2),
        "setup_detail": {
            "wave_a_start": round(wave_a_start, 2),
            "wave_a_end":   round(wave_a_end, 2),
            "wave_c_low":   round(wave_c_low, 2),
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

    seen = load_seen()

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
            f"Price ${r['price']} | Stop ${r['stop']} | "
            f"TP2 ${r['tp2']} ({r['tp2_pct']:+.1f}%) | "
            f"R:R {r['rr_tp2']:.1f}x | RSI {r['rsi']}"
        )
