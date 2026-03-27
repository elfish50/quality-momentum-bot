"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

Entry Scenarios (LONG ONLY):
  1. Wave 2 pullback: price retraces 50-78.6% of Wave 1 -> buy reversal zone
  2. Wave 4 pullback: price retraces 38.2% of Wave 3 -> buy shallow correction
  3. ABC correction: price completes C wave -> buy end of correction

Stop Placement:
  Wave 2: just below Wave 1 origin (-1%)
  Wave 4: just below Wave 1 high (-1%)
  ABC:    just below Wave A low (-1%)

Targets:
  TP1: 1.272x Fibonacci extension of Wave 1
  TP2: 1.618x Fibonacci extension of Wave 1
  TP3: 2.618x Fibonacci extension (stretch)

Filters:
  - Weekly trend filter: SMA10w must be above SMA20w (weekly uptrend required)
  - Volume uses PREVIOUS completed bar (not live partial intraday bar)
  - Volume threshold: 1.5x average
  - Quality score threshold: 40
  - Wave 2 reversal zone: 50-78.6%
  - Swing point detection: iloc-based, no fragile index matching
  - ABC targets: extensions beyond Wave A start
  - SIP feed fallback when IEX returns insufficient bars

KEY FIX vs prior version:
  vol_confirmed now checks df.iloc[-2] (yesterday's confirmed bar) instead of
  df.iloc[-1] (today's live partial bar). Scans run intraday (10AM/12:30/2:30 ET)
  so the current bar is never fully formed. Comparing partial intraday volume
  against a 20-day average of FULL daily bars almost never crosses 1.5x,
  suppressing all BUY signals to WATCH. Using the previous completed bar fixes this.

Timeframe: Daily bars + weekly trend confirmation
Quality: Berkshire screen (ROE, margins, EPS, D/E)
Account: $1,000 | Risk: 10% = $100 per trade
"""

import os
import math
import traceback
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timedelta

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

ALPACA_URL  = "https://data.alpaca.markets/v2"
FINNHUB_URL = "https://finnhub.io/api/v1"

ACCOUNT  = 1_000
RISK_PCT = 0.10

# Fibonacci retracement levels
FIB_382 = 0.382
FIB_500 = 0.500
FIB_618 = 0.618
FIB_786 = 0.786

# Extension targets
EXT_1272 = 1.272
EXT_1618 = 1.618
EXT_2618 = 2.618

# Thresholds
VOL_CONFIRM_RATIO = 1.5   # require meaningful volume surge on confirmed (previous) bar
MIN_QUALITY_SCORE = 40    # strict quality gate
MIN_WAVE1_MOVE    = 0.07  # wave 1 must be at least 7% move
MIN_RR_TP2        = 2.0   # minimum R:R to TP2


# ── Price Data ────────────────────────────────────────────────────────────────

def get_price_data(ticker):
    """Fetch daily bars from Alpaca. Tries IEX first, falls back to SIP."""
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
                "adjustment": "split",   # split-adjusted prices
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
    """
    Resample daily bars to weekly for trend filter.
    Returns weekly DataFrame with Open/High/Low/Close/Volume.
    """
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
    Weekly trend filter: SMA10w > SMA20w (weekly uptrend required).
    Prevents buying Wave 2 pullbacks in a weekly downtrend.
    Returns True if trend is up or insufficient data (gives benefit of doubt).
    """
    try:
        weekly = get_weekly_bars(df)
        if len(weekly) < 22:
            return True  # not enough weekly bars — don't reject
        close    = weekly["Close"]
        sma10w   = close.rolling(10).mean().iloc[-1]
        sma20w   = close.rolling(20).mean().iloc[-1]
        sma10w_1 = close.rolling(10).mean().iloc[-2]
        # Uptrend: SMA10w > SMA20w AND SMA10w is rising
        return sma10w > sma20w and sma10w > sma10w_1
    except Exception:
        return True  # don't reject on error


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

    # RSI 14 (Wilder's smoothing)
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"] = 100 - (100 / (1 + rs))

    # ATR 14
    high, low = df["High"], df["Low"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()

    # Moving averages
    df["SMA50"]  = close.rolling(50).mean()
    df["SMA200"] = close.rolling(200).mean()
    df["EMA21"]  = close.ewm(span=21, adjust=False).mean()

    # Volume average (20-day, based on ALL bars including current)
    df["VolAvg20"] = df["Volume"].rolling(20).mean()

    return df


def find_swing_points(df, lookback=90, min_bars=5):
    """
    Find significant swing highs/lows using iloc (no fragile index matching).
    Returns lists of (iloc_position, price) tuples.
    """
    n      = len(df)
    start  = max(0, n - lookback)
    highs  = []
    lows   = []

    for i in range(start + min_bars, n - min_bars):
        window = df.iloc[i - min_bars: i + min_bars + 1]
        bar    = df.iloc[i]

        if bar["High"] == window["High"].max():
            highs.append((i, float(bar["High"])))

        if bar["Low"] == window["Low"].min():
            lows.append((i, float(bar["Low"])))

    return highs, lows


# ── Volume helper ─────────────────────────────────────────────────────────────

def _vol_confirmed(df) -> tuple[bool, float]:
    """
    KEY FIX: Use the PREVIOUS completed daily bar (iloc[-2]) for volume confirmation.

    Scans run at 10AM, 12:30PM, 2:30PM ET while market is open.
    The current bar (iloc[-1]) is partially formed — its volume is a fraction
    of what it will be by close. Comparing partial intraday volume against a
    20-day average of FULL bars produces a ratio well below 1.5x even on
    high-volume days, so vol_confirmed was almost always False → WATCH, never BUY.

    Using iloc[-2] (yesterday's completed bar) gives accurate volume confirmation.
    The 20-day average (VolAvg20) at iloc[-2] is also computed from completed bars,
    so the comparison is apples-to-apples.
    """
    if len(df) < 3:
        return False, 0.0
    prev_bar = df.iloc[-2]
    vol_avg  = float(prev_bar["VolAvg20"]) if prev_bar["VolAvg20"] > 0 else 1.0
    vol_ratio = float(prev_bar["Volume"]) / vol_avg
    return vol_ratio >= VOL_CONFIRM_RATIO, round(vol_ratio, 2)


# ── Wave 2 Setup ──────────────────────────────────────────────────────────────

def detect_wave2_setup(df):
    """
    Wave 2 pullback:
    - Wave 1: swing low -> swing high (at least MIN_WAVE1_MOVE)
    - Price retraces 50–78.6% of Wave 1 (wider zone)
    - Weekly trend must be up
    - RSI turning up from 25–60, confirmed rising
    - Volume check on previous completed bar (see _vol_confirmed)
    - R:R to TP2 >= 2.0
    """
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5)

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

    in_reversal_zone = fib_786 <= price <= fib_500

    if not in_reversal_zone:
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 25 <= rsi <= 60

    if not (rsi_rising and rsi_ok):
        return None

    if price < float(df.iloc[-4:-1]["Low"].min()) * 1.002:
        return None

    # FIX: use previous completed bar for volume
    vol_confirmed, vol_ratio = _vol_confirmed(df)

    stop = round(wave1_origin * 0.99, 2)

    wave2_low = price
    ext_1272  = round(wave2_low + wave1_size * EXT_1272, 2)
    ext_1618  = round(wave2_low + wave1_size * EXT_1618, 2)
    ext_2618  = round(wave2_low + wave1_size * EXT_2618, 2)

    risk = price - stop
    if risk <= 0:
        return None

    tp1_pct = round((ext_1272 - price) / price * 100, 1)
    tp2_pct = round((ext_1618 - price) / price * 100, 1)
    tp3_pct = round((ext_2618 - price) / price * 100, 1)
    rr_tp1  = round((ext_1272 - price) / risk, 2)
    rr_tp2  = round((ext_1618 - price) / risk, 2)

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
        "tp1":           ext_1272,
        "tp2":           ext_1618,
        "tp3":           ext_2618,
        "tp1_pct":       tp1_pct,
        "tp2_pct":       tp2_pct,
        "tp3_pct":       tp3_pct,
        "rr_tp1":        rr_tp1,
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
    """
    Wave 4 pullback:
    - Need at least: low1 (W1 origin) -> high1 (W1 top) -> high2 (W3 top)
    - Price retraces 38.2% of Wave 3
    - Wave 4 cannot overlap Wave 1 territory (Elliott rule)
    - Weekly trend must be up
    - RSI 35–65, rising
    """
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5)

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
    rsi_ok     = 35 <= rsi <= 65

    if not (rsi_rising and rsi_ok):
        return None

    # FIX: use previous completed bar for volume
    vol_confirmed, vol_ratio = _vol_confirmed(df)

    stop = round(wave1_high * 0.99, 2)

    wave1_size = wave1_high - wave1_origin
    ext_1272   = round(price + wave1_size * EXT_1272, 2)
    ext_1618   = round(price + wave1_size * EXT_1618, 2)
    ext_2618   = round(price + wave1_size * EXT_2618, 2)

    risk = price - stop
    if risk <= 0:
        return None

    tp1_pct = round((ext_1272 - price) / price * 100, 1)
    tp2_pct = round((ext_1618 - price) / price * 100, 1)
    tp3_pct = round((ext_2618 - price) / price * 100, 1)
    rr_tp1  = round((ext_1272 - price) / risk, 2)
    rr_tp2  = round((ext_1618 - price) / risk, 2)

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
        "tp1_pct":       tp1_pct,
        "tp2_pct":       tp2_pct,
        "tp3_pct":       tp3_pct,
        "rr_tp1":        rr_tp1,
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
    """
    ABC Correction:
    - Wave A: impulse drop (high -> low)
    - Wave B: partial bounce
    - Wave C: second drop, ends near or below Wave A low
    - Entry at Wave C completion reversal
    - Stop: 1% below Wave A low
    - Targets extend BEYOND Wave A start (TP1 = recovery, TP2/TP3 = extension)
    """
    if not weekly_trend_is_up(df):
        return None

    df      = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4)

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

    # FIX: use previous completed bar for volume
    vol_confirmed, vol_ratio = _vol_confirmed(df)

    stop = round(wave_a_end * 0.99, 2)

    tp1 = round(wave_a_start, 2)
    tp2 = round(wave_a_start + wave_a_size * FIB_618, 2)
    tp3 = round(wave_a_start + wave_a_size * 1.0, 2)

    risk = price - stop
    if risk <= 0:
        return None

    tp1_pct = round((tp1 - price) / price * 100, 1)
    tp2_pct = round((tp2 - price) / price * 100, 1)
    tp3_pct = round((tp3 - price) / price * 100, 1)
    rr_tp1  = round((tp1 - price) / risk, 2)
    rr_tp2  = round((tp2 - price) / risk, 2)

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
        "tp1_pct":       tp1_pct,
        "tp2_pct":       tp2_pct,
        "tp3_pct":       tp3_pct,
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

def analyze_ticker(ticker):
    try:
        # 1. Price data (IEX -> SIP fallback)
        df = get_price_data(ticker)
        if df is None or len(df) < 60:
            return None

        # 2. Try all three Elliott Wave setups
        setup = (
            detect_wave2_setup(df) or
            detect_wave4_setup(df) or
            detect_abc_setup(df)
        )

        if setup is None:
            return None

        # 3. Fundamentals + quality screen
        fund = get_fundamentals(ticker)
        if not fund:
            return None

        q_score, failed = quality_score(fund)
        if q_score < MIN_QUALITY_SCORE:
            print(f"[{ticker}] SKIP | Quality {q_score:.0f} < {MIN_QUALITY_SCORE}")
            return None

        # 4. Position sizing
        price  = setup["price"]
        stop   = setup["stop"]
        sizing = position_size(price, stop)

        # 5. Signal score
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

        # 6. Signal type — vol_confirmed uses previous completed bar (see _vol_confirmed)
        signal_type = "BUY" if setup.get("vol_confirmed", False) else "WATCH"

        # 7. Hold time
        hold_time = "POSITION (3-8 weeks)" if score >= 70 else "SWING (1-3 weeks)"

        print(
            f"[{ticker}] {signal_type} | {setup['setup']} | "
            f"Score {score:.0f} | RSI {rsi:.0f} | "
            f"R:R TP2 {setup.get('rr_tp2',0):.1f}x | "
            f"Vol {setup.get('vol_ratio',0):.1f}x (prev bar)"
        )

        return {
            "ticker":        ticker,
            "signal":        signal_type,
            "setup":         setup["setup"],
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
