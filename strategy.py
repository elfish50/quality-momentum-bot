"""
BOLLINGER BAND 3RD TOUCH BREAKOUT STRATEGY
+ Berkshire Quality Screen
+ Fibonacci Retracement Targets

Logic:
  1. Quality company (ROE, margins, EPS growth)
  2. Price touches lower Bollinger Band 3 times
  3. 3rd touch: candle closes BACK ABOVE lower band
  4. RSI turning up from oversold
  5. Volume spike on breakout candle
  6. Stop: 1.5x ATR below entry
  7. Targets: Fibonacci 38.2% / 61.8% / 100% retracement

Timeframe: Daily bars
Data: Alpaca (price) + Finnhub (fundamentals)
"""

import os
import math
import traceback
import pandas as pd
import requests
from datetime import datetime, timedelta

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

ALPACA_URL  = "https://data.alpaca.markets/v2"
FINNHUB_URL = "https://finnhub.io/api/v1"

BB_PERIOD = 20
BB_STD    = 2.0

ACCOUNT  = 1_000
RISK_PCT = 0.10


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


def compute_bollinger(df):
    df    = df.copy()
    close = df["Close"]
    df["BB_mid"]   = close.rolling(BB_PERIOD).mean()
    df["BB_std"]   = close.rolling(BB_PERIOD).std()
    df["BB_upper"] = df["BB_mid"] + BB_STD * df["BB_std"]
    df["BB_lower"] = df["BB_mid"] - BB_STD * df["BB_std"]
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"]      = 100 - (100 / (1 + rs))
    high, low      = df["High"], df["Low"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR"]      = tr.rolling(14).mean()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["SMA200"]   = close.rolling(200).mean()
    df["SMA50"]    = close.rolling(50).mean()
    return df


def find_lower_band_touches(df, lookback=60):
    recent  = df.iloc[-lookback:].copy()
    touches = []
    for i in range(len(recent)):
        row = recent.iloc[i]
        if pd.isna(row["BB_lower"]):
            continue
        if row["Low"] <= row["BB_lower"] * 1.005:
            touches.append(recent.index[i])
    return touches


def compute_fibonacci(df, touches):
    """
    Swing low  = lowest low among the BB touches.
    Swing high = highest high in the 60 bars BEFORE the first touch.
    This ensures targets are always above the current entry price.
    """
    if not touches:
        return None

    swing_low = min(float(df.loc[idx, "Low"]) for idx in touches)

    first_touch_loc = df.index.get_loc(touches[0])
    lookback_start  = max(0, first_touch_loc - 60)
    swing_high      = float(df.iloc[lookback_start:first_touch_loc]["High"].max())

    current_price = float(df.iloc[-1]["Close"])
    if swing_high <= current_price:
        bb_upper   = float(df.iloc[-1]["BB_upper"])
        diff       = bb_upper - swing_low
        swing_high = swing_low + diff * 1.5

    diff = swing_high - swing_low

    return {
        "swing_low":  round(swing_low, 2),
        "swing_high": round(swing_high, 2),
        "fib_382":    round(swing_low + diff * 0.382, 2),
        "fib_618":    round(swing_low + diff * 0.618, 2),
        "fib_100":    round(swing_high, 2),
    }


def is_third_touch_breakout(df):
    df = compute_bollinger(df)
    if len(df) < BB_PERIOD + 20:
        return None

    touches = find_lower_band_touches(df, lookback=60)
    if len(touches) < 3:
        return None

    last_3         = touches[-3:]
    last_touch_idx = last_3[-1]
    bars_since     = len(df) - 1 - df.index.get_loc(last_touch_idx)
    if bars_since > 5:
        return None

    current = df.iloc[-1]
    prev    = df.iloc[-2]

    if pd.isna(current["BB_lower"]) or pd.isna(current["BB_mid"]):
        return None

    # Trend filter: price must be above SMA50
    if not pd.isna(current["SMA50"]) and current["Close"] < current["SMA50"] * 0.98:
        return None

    close_above_lower = current["Close"] > current["BB_lower"]
    was_at_lower      = prev["Low"] <= prev["BB_lower"] * 1.01
    if not (close_above_lower and was_at_lower):
        if not (current["Low"] <= current["BB_lower"] * 1.005 and
                current["Close"] > current["BB_lower"]):
            return None

    rsi_now  = current["RSI"]
    rsi_prev = prev["RSI"]
    if pd.isna(rsi_now) or pd.isna(rsi_prev):
        return None

    rsi_rising = rsi_now > rsi_prev
    rsi_ok     = 30 <= rsi_now <= 65
    if not (rsi_rising and rsi_ok):
        return None

    vol_ratio     = current["Volume"] / current["VolAvg20"] if current["VolAvg20"] > 0 else 1.0
    vol_confirmed = vol_ratio >= 1.15

    touch_span = df.index.get_loc(last_3[-1]) - df.index.get_loc(last_3[0])
    if touch_span < 5:
        return None

    stop_low = min(df.loc[idx, "Low"] for idx in last_3)

    fib = compute_fibonacci(df, last_3)
    if fib is None:
        return None

    return {
        "close":         float(current["Close"]),
        "bb_lower":      float(current["BB_lower"]),
        "bb_mid":        float(current["BB_mid"]),
        "bb_upper":      float(current["BB_upper"]),
        "bb_width":      float(current["BB_width"]),
        "rsi":           float(rsi_now),
        "atr":           float(current["ATR"]) if not pd.isna(current["ATR"]) else 0,
        "vol_ratio":     float(vol_ratio),
        "vol_confirmed": vol_confirmed,
        "stop_low":      float(stop_low),
        "sma200":        float(current["SMA200"]) if not pd.isna(current["SMA200"]) else None,
        "sma50":         float(current["SMA50"])  if not pd.isna(current["SMA50"])  else None,
        "n_touches":     len(touches),
        "touch_span":    touch_span,
        "fib_382":       fib["fib_382"],
        "fib_618":       fib["fib_618"],
        "fib_100":       fib["fib_100"],
        "swing_low":     fib["swing_low"],
        "swing_high":    fib["swing_high"],
    }


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


def position_size(price, stop):
    risk_dollars = ACCOUNT * RISK_PCT
    stop_dist    = price - stop
    if stop_dist <= 0:
        stop_dist = price * 0.02
    shares = math.floor(risk_dollars / stop_dist)
    if shares < 1:
        shares = 1
    return {
        "shares":       shares,
        "stop":         round(stop, 2),
        "risk_dollars": round(risk_dollars, 2),
        "position_val": round(shares * price, 2),
        "pct_account":  round(shares * price / ACCOUNT * 100, 1),
    }


def signal_score(bb, q_score):
    score = 0.0
    score += q_score * 0.40
    rsi = bb["rsi"]
    if 35 <= rsi <= 55:   score += 25
    elif 55 < rsi <= 65:  score += 15
    else:                 score += 5
    if bb["vol_ratio"] >= 1.5:    score += 20
    elif bb["vol_ratio"] >= 1.15: score += 12
    else:                         score += 5
    if bb["n_touches"] >= 4:   score += 15
    elif bb["n_touches"] == 3: score += 10
    return round(score, 1)


def analyze_ticker(ticker):
    try:
        df = get_price_data(ticker)
        if df is None or len(df) < 40:
            return None

        bb = is_third_touch_breakout(df)
        if bb is None:
            print(f"[{ticker}] No BB 3rd touch breakout")
            return None

        fund = get_fundamentals(ticker)
        if not fund:
            return None

        q_score, failed = quality_score(fund)
        if q_score < 20:
            print(f"[{ticker}] SKIP | Quality {q_score:.0f}")
            return None

        sig = signal_score(bb, q_score)

        if sig >= 70:
            hold_time = "POSITION (3-8 weeks)"
        elif sig >= 45:
            hold_time = "SWING (1-2 weeks)"
        else:
            print(f"[{ticker}] SKIP | Signal too weak {sig:.0f}")
            return None

        price      = bb["close"]
        atr_stop   = price - (bb["atr"] * 1.5)
        touch_stop = bb["stop_low"] * 0.99
        stop       = max(atr_stop, touch_stop)
        sizing     = position_size(price, stop)

        tp1     = bb["fib_382"]
        tp2     = bb["fib_618"]
        tp3     = bb["fib_100"]
        tp1_pct = round((tp1 - price) / price * 100, 1)
        tp2_pct = round((tp2 - price) / price * 100, 1)
        tp3_pct = round((tp3 - price) / price * 100, 1)

        risk   = price - stop
        rr_tp1 = round((tp1 - price) / risk, 2) if risk > 0 else 0
        rr_tp2 = round((tp2 - price) / risk, 2) if risk > 0 else 0
        rr_tp3 = round((tp3 - price) / risk, 2) if risk > 0 else 0

        if rr_tp2 < 1.5:
            print(f"[{ticker}] SKIP | R:R too low {rr_tp2:.2f}x at TP2")
            return None

        print(f"[{ticker}] BUY | Score {sig:.0f} | {hold_time} | RSI {bb['rsi']:.0f} | {bb['n_touches']} touches | Vol {bb['vol_ratio']:.1f}x | R:R {rr_tp2:.1f}x")

        return {
            "ticker":        ticker,
            "signal":        "BUY",
            "hold_time":     hold_time,
            "signal_score":  sig,
            "quality_score": q_score,
            "price":         round(price, 2),
            "sma200":        round(bb["sma200"], 2) if bb["sma200"] else None,
            "sma50":         round(bb["sma50"], 2)  if bb["sma50"]  else None,
            "rsi":           round(bb["rsi"], 1),
            "atr":           round(bb["atr"], 2),
            "bb_lower":      round(bb["bb_lower"], 2),
            "bb_mid":        round(bb["bb_mid"], 2),
            "bb_upper":      round(bb["bb_upper"], 2),
            "bb_width":      round(bb["bb_width"] * 100, 1),
            "n_touches":     bb["n_touches"],
            "vol_ratio":     round(bb["vol_ratio"], 2),
            "vol_confirmed": bb["vol_confirmed"],
            "swing_low":     bb["swing_low"],
            "swing_high":    bb["swing_high"],
            "roe":           round(fund.get("roe", 0) * 100, 1),
            "gross_margin":  round(fund.get("gross_margin", 0) * 100, 1),
            "eps_growth":    round(fund.get("eps_growth", 0) * 100, 1),
            "debt_equity":   round(fund.get("debt_equity", 0), 2),
            "pe_ratio":      round(fund.get("pe_ratio", 0), 1),
            "sector":        fund.get("sector", ""),
            "name":          fund.get("name", ticker),
            "stop":          sizing["stop"],
            "tp1":           tp1,
            "tp2":           tp2,
            "tp3":           tp3,
            "tp1_pct":       tp1_pct,
            "tp2_pct":       tp2_pct,
            "tp3_pct":       tp3_pct,
            "rr_tp1":        rr_tp1,
            "rr_tp2":        rr_tp2,
            "rr_tp3":        rr_tp3,
            "shares":        sizing["shares"],
            "risk_dollars":  sizing["risk_dollars"],
            "position_val":  sizing["position_val"],
            "pct_account":   sizing["pct_account"],
            "quality_notes": failed,
        }

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-300:]}")
        return None
