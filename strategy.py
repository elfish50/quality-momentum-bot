"""
ELLIOTT WAVE + FIBONACCI STRATEGY
Based on Asaf Naamani's framework

Entry Scenarios (LONG ONLY):
  1. Wave 2 pullback: price retraces 50-61.8% of Wave 1 -> buy reversal zone
  2. Wave 4 pullback: price retraces 38.2% of Wave 1 -> buy shallow correction
  3. ABC correction: price completes C wave -> buy end of correction

Stop Placement:
  Wave 2: just below Wave 1 origin (-1%)
  Wave 4: just below Wave 1 high (-1%)
  ABC:    just below Wave A low (-1%)

Targets:
  TP1: 1.272x Fibonacci extension of Wave 1
  TP2: 1.618x Fibonacci extension of Wave 1
  TP3: 2.618x Fibonacci extension (stretch)

Timeframe: Daily bars
Quality: Berkshire screen (ROE, margins, EPS, D/E)
Data: Alpaca (price) + Finnhub (fundamentals)
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

# Fibonacci levels
FIB_382 = 0.382
FIB_500 = 0.500
FIB_618 = 0.618
FIB_786 = 0.786

# Extension targets
EXT_1272 = 1.272
EXT_1618 = 1.618
EXT_2618 = 2.618


def get_price_data(ticker):
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    params = {
        "start":     start,
        "end":       end,
        "timeframe": "1Day",
        "limit":     365,
        "feed":      "iex",
    }
    try:
        r    = requests.get(f"{ALPACA_URL}/stocks/{ticker}/bars",
                            headers=headers, params=params, timeout=15)
        bars = r.json().get("bars")
        if not bars or len(bars) < 60:
            return None
        df = pd.DataFrame([{
            "Date":   pd.to_datetime(b["t"]),
            "Open":   float(b["o"]),
            "High":   float(b["h"]),
            "Low":    float(b["l"]),
            "Close":  float(b["c"]),
            "Volume": float(b["v"]),
        } for b in bars])
        return df.sort_values("Date").reset_index(drop=True)
    except Exception as e:
        print(f"[{ticker}] Alpaca error: {e}")
        return None


def get_fundamentals(ticker):
    def safe(v, d=0.0):
        try: return float(v) if v not in (None, "", "N/A", "None") else d
        except: return d
    try:
        r1 = requests.get(f"{FINNHUB_URL}/stock/metric",
                          params={"symbol": ticker, "metric": "all",
                                  "token": FINNHUB_KEY}, timeout=15)
        m  = r1.json().get("metric", {})
        r2 = requests.get(f"{FINNHUB_URL}/stock/profile2",
                          params={"symbol": ticker, "token": FINNHUB_KEY},
                          timeout=15)
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
    if de <= 0.5:    score += 20
    elif de <= 2.0:  score += 20 - (de - 0.5) / 1.5 * 15
    else:            failed.append(f"D/E {de:.1f}")
    return round(score, 1), failed


def compute_indicators(df):
    df = df.copy()
    close = df["Close"]

    # RSI 14
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

    # Volume average
    df["VolAvg20"] = df["Volume"].rolling(20).mean()

    return df


def find_swing_points(df, lookback=60, min_bars=5):
    """
    Find significant swing highs and lows in the lookback period.
    A swing high is a bar whose high is the highest in min_bars on each side.
    A swing low is a bar whose low is the lowest in min_bars on each side.
    """
    recent = df.iloc[-lookback:].copy()
    highs, lows = [], []

    for i in range(min_bars, len(recent) - min_bars):
        bar = recent.iloc[i]
        window_highs = recent.iloc[i-min_bars:i+min_bars+1]["High"]
        window_lows  = recent.iloc[i-min_bars:i+min_bars+1]["Low"]

        if bar["High"] == window_highs.max():
            highs.append((recent.index[i], float(bar["High"])))
        if bar["Low"] == window_lows.min():
            lows.append((recent.index[i], float(bar["Low"])))

    return highs, lows


def detect_wave2_setup(df):
    """
    Wave 2 setup:
    - Identify Wave 1: strong impulse move up (swing low -> swing high)
    - Price retraces 50-61.8% of Wave 1
    - RSI turning up from oversold zone
    - Entry in reversal zone
    - Stop: 1% below Wave 1 origin (swing low)
    """
    df = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=5)

    if len(highs) < 1 or len(lows) < 1:
        return None

    # Find the most recent significant swing low (Wave 1 origin)
    # followed by a swing high (Wave 1 top)
    # Look for: low -> high -> pullback (current price)

    # Get last swing low and swing high
    last_low  = lows[-1]
    last_high = highs[-1]

    # Wave 1 origin must be BEFORE Wave 1 top
    low_loc  = df.index.get_loc(last_low[0])
    high_loc = df.index.get_loc(last_high[0])

    if low_loc >= high_loc:
        # Try second-to-last low
        if len(lows) < 2:
            return None
        last_low  = lows[-2]
        low_loc   = df.index.get_loc(last_low[0])
        if low_loc >= high_loc:
            return None

    wave1_origin = last_low[1]   # Wave 1 start (swing low)
    wave1_top    = last_high[1]  # Wave 1 end (swing high)
    wave1_size   = wave1_top - wave1_origin

    if wave1_size <= 0:
        return None

    # Wave 1 must be a meaningful move (at least 5%)
    if wave1_size / wave1_origin < 0.05:
        return None

    # Calculate Fibonacci retracement levels
    fib_382 = round(wave1_top - wave1_size * FIB_382, 2)
    fib_500 = round(wave1_top - wave1_size * FIB_500, 2)
    fib_618 = round(wave1_top - wave1_size * FIB_618, 2)
    fib_786 = round(wave1_top - wave1_size * FIB_786, 2)

    # Reversal zone: between 50% and 61.8% retracement
    in_reversal_zone = fib_618 <= price <= fib_500

    if not in_reversal_zone:
        return None

    # RSI should be in recovery zone (30-55) and rising
    rsi_prev = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 25 <= rsi <= 60

    if not (rsi_rising and rsi_ok):
        return None

    # Price should be above recent low (not still falling)
    recent_low = float(df.iloc[-5:]["Low"].min())
    if price < recent_low * 1.005:
        return None

    # Volume confirmation: current volume above average
    vol_ratio     = float(current["Volume"]) / float(current["VolAvg20"]) if current["VolAvg20"] > 0 else 1.0
    vol_confirmed = vol_ratio >= 1.0

    # Stop: 1% below Wave 1 origin
    stop = round(wave1_origin * 0.99, 2)

    # Targets: Fibonacci extensions of Wave 1 from Wave 2 low
    # We use current price as approximate Wave 2 low
    wave2_low = price
    ext_1272  = round(wave2_low + wave1_size * EXT_1272, 2)
    ext_1618  = round(wave2_low + wave1_size * EXT_1618, 2)
    ext_2618  = round(wave2_low + wave1_size * EXT_2618, 2)

    risk      = price - stop
    if risk <= 0:
        return None

    tp1_pct = round((ext_1272 - price) / price * 100, 1)
    tp2_pct = round((ext_1618 - price) / price * 100, 1)
    tp3_pct = round((ext_2618 - price) / price * 100, 1)
    rr_tp1  = round((ext_1272 - price) / risk, 2)
    rr_tp2  = round((ext_1618 - price) / risk, 2)

    if rr_tp2 < 2.0:
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
        "vol_ratio":     round(vol_ratio, 2),
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
    }


def detect_wave4_setup(df):
    """
    Wave 4 setup:
    - Shallower correction (38.2% retracement)
    - Stop: just below Wave 1 high
    - Wave 4 cannot overlap Wave 1 territory
    """
    df = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=120, min_bars=5)

    if len(highs) < 2 or len(lows) < 1:
        return None

    # Need: low1 -> high1 (W1) -> low2 (W2) -> high2 (W3) -> pullback (W4)
    if len(highs) < 2:
        return None

    wave1_origin = lows[0][1]  if len(lows) > 0 else None
    wave1_high   = highs[0][1] if len(highs) > 0 else None
    wave3_high   = highs[-1][1] if len(highs) > 1 else None

    if not all([wave1_origin, wave1_high, wave3_high]):
        return None

    wave3_size = wave3_high - wave1_origin
    if wave3_size <= 0:
        return None

    # Wave 4 retracement: 38.2% of Wave 3
    fib_382_w4 = round(wave3_high - wave3_size * FIB_382, 2)
    fib_500_w4 = round(wave3_high - wave3_size * FIB_500, 2)

    # Price should be in Wave 4 zone
    in_w4_zone = fib_500_w4 <= price <= fib_382_w4

    if not in_w4_zone:
        return None

    # Wave 4 cannot overlap Wave 1 high
    if price < wave1_high:
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 35 <= rsi <= 65

    if not (rsi_rising and rsi_ok):
        return None

    vol_ratio     = float(current["Volume"]) / float(current["VolAvg20"]) if current["VolAvg20"] > 0 else 1.0
    vol_confirmed = vol_ratio >= 1.0

    # Stop: 1% below Wave 1 high
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

    if rr_tp2 < 2.0:
        return None

    atr = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.02

    return {
        "setup":        "Wave 4 Pullback",
        "wave1_origin": round(wave1_origin, 2),
        "wave1_high":   round(wave1_high, 2),
        "wave3_high":   round(wave3_high, 2),
        "fib_382":      fib_382_w4,
        "fib_500":      fib_500_w4,
        "price":        round(price, 2),
        "rsi":          round(rsi, 1),
        "atr":          round(atr, 2),
        "vol_ratio":    round(vol_ratio, 2),
        "vol_confirmed": vol_confirmed,
        "stop":         stop,
        "tp1":          ext_1272,
        "tp2":          ext_1618,
        "tp3":          ext_2618,
        "tp1_pct":      tp1_pct,
        "tp2_pct":      tp2_pct,
        "tp3_pct":      tp3_pct,
        "rr_tp1":       rr_tp1,
        "rr_tp2":       rr_tp2,
        "risk":         round(risk, 2),
    }


def detect_abc_setup(df):
    """
    ABC Correction setup:
    - Price completes a 3-wave ABC correction
    - Wave C ends near Wave A low (or Fib extension of A)
    - Stop: below Wave A low
    - Entry: reversal from Wave C bottom
    """
    df = compute_indicators(df)
    current = df.iloc[-1]
    price   = float(current["Close"])
    rsi     = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50

    highs, lows = find_swing_points(df, lookback=90, min_bars=4)

    if len(highs) < 1 or len(lows) < 2:
        return None

    # ABC: high (Wave A start) -> low (Wave A end) -> high (Wave B) -> low (Wave C)
    wave_a_start = highs[-1][1]
    wave_a_end   = lows[-2][1] if len(lows) >= 2 else None
    wave_c_low   = lows[-1][1]

    if not wave_a_end:
        return None

    wave_a_size = wave_a_start - wave_a_end
    if wave_a_size <= 0:
        return None

    # Wave C should be near Wave A low (0.618 to 1.0 of Wave A)
    c_target_min = round(wave_a_end - wave_a_size * 0.2, 2)
    c_target_max = round(wave_a_end + wave_a_size * 0.1, 2)

    # Price near Wave C completion zone
    in_abc_zone = c_target_min <= price <= c_target_max

    if not in_abc_zone:
        return None

    rsi_prev   = float(df.iloc[-2]["RSI"]) if not pd.isna(df.iloc[-2]["RSI"]) else 50
    rsi_rising = rsi > rsi_prev
    rsi_ok     = 25 <= rsi <= 55

    if not (rsi_rising and rsi_ok):
        return None

    vol_ratio     = float(current["Volume"]) / float(current["VolAvg20"]) if current["VolAvg20"] > 0 else 1.0
    vol_confirmed = vol_ratio >= 1.0

    # Stop: 1% below Wave A low
    stop = round(wave_a_end * 0.99, 2)

    # Targets: return to Wave A start and beyond
    tp1 = round(wave_a_start, 2)
    tp2 = round(wave_a_start + wave_a_size * 0.618, 2)
    tp3 = round(wave_a_start + wave_a_size * 1.0,   2)

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
        "vol_ratio":     round(vol_ratio, 2),
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
    }


def position_size(price, stop):
    risk_dollars = ACCOUNT * RISK_PCT
    risk         = price - stop
    if risk <= 0:
        risk = price * 0.05
    shares       = math.floor(risk_dollars / risk)
    if shares < 1:
        shares = 1
    max_shares   = math.floor(ACCOUNT / price) if price > 0 else shares
    shares       = min(shares, max_shares)
    return {
        "shares":       shares,
        "risk_dollars": round(risk_dollars, 2),
        "position_val": round(shares * price, 2),
        "pct_account":  round(shares * price / ACCOUNT * 100, 1),
    }


def analyze_ticker(ticker):
    try:
        # 1. Price data
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
            print(f"[{ticker}] No Elliott Wave setup")
            return None

        # 3. Fundamentals + quality screen
        fund = get_fundamentals(ticker)
        if not fund:
            return None

        q_score, failed = quality_score(fund)
        if q_score < 20:
            print(f"[{ticker}] SKIP | Quality {q_score:.0f}")
            return None

        # 4. Position sizing
        price   = setup["price"]
        stop    = setup["stop"]
        sizing  = position_size(price, stop)

        # 5. Signal score
        score = 0.0
        score += q_score * 0.35
        rsi = setup["rsi"]
        if 35 <= rsi <= 55:   score += 25
        elif 55 < rsi <= 65:  score += 15
        else:                 score += 5
        if setup["vol_confirmed"]:  score += 20
        else:                       score += 8
        rr = setup.get("rr_tp2", 0)
        if rr >= 4.0:   score += 20
        elif rr >= 2.0: score += 12
        else:           score += 5

        # 6. Hold time
        if score >= 70:
            hold_time = "POSITION (3-8 weeks)"
        else:
            hold_time = "SWING (1-3 weeks)"

        print(f"[{ticker}] BUY | {setup['setup']} | Score {score:.0f} | RSI {rsi:.0f} | R:R TP2 {setup.get('rr_tp2',0):.1f}x")

        return {
            "ticker":        ticker,
            "signal":        "LONG",
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
            "setup_detail":  setup,
        }

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-300:]}")
        return None
