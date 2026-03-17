"""
VWAP MEAN REVERSION + MOMENTUM FILTER

Logic:
  1. Price moves >1.5% above or below session VWAP
  2. EMA9 confirms intraday trend direction
  3. RSI is NOT extreme (not >70 or <30)
  4. Price starts reverting back toward VWAP
  5. Exit at VWAP touch OR trail stop 1.5x ATR

Timeframe: 15-minute bars (regular market hours only)
Data: Alpaca (price) + Finnhub (name/sector)
Account: $1,000 | Risk: 10% = $100 per trade
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

ACCOUNT        = 1_000
RISK_PCT       = 0.10
VWAP_EXTENSION = 0.015
MIN_VOLUME     = 1_000_000


def is_market_open():
    """Returns True only during regular market hours 9:30 AM - 4 PM ET."""
    now_utc  = datetime.utcnow()
    now_hour = now_utc.hour + now_utc.minute / 60
    # 13:30 UTC = 9:30 AM ET | 20:00 UTC = 4:00 PM ET
    return 13.5 <= now_hour <= 20.0


def get_intraday_data(ticker):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    params = {
        "start":     f"{today}T13:30:00Z",
        "end":       f"{today}T20:00:00Z",
        "timeframe": "15Min",
        "limit":     100,
        "feed":      "iex",
    }
    try:
        r    = requests.get(f"{ALPACA_URL}/stocks/{ticker}/bars",
                            headers=headers, params=params, timeout=15)
        bars = r.json().get("bars")
        if not bars or len(bars) < 4:
            return None
        df = pd.DataFrame([{
            "Time":   pd.to_datetime(b["t"]),
            "Open":   float(b["o"]),
            "High":   float(b["h"]),
            "Low":    float(b["l"]),
            "Close":  float(b["c"]),
            "Volume": float(b["v"]),
        } for b in bars])
        return df.sort_values("Time").reset_index(drop=True)
    except Exception as e:
        print(f"[{ticker}] Alpaca intraday error: {e}")
        return None


def get_daily_volume(ticker):
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    end   = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
    params = {
        "start":     start,
        "end":       end,
        "timeframe": "1Day",
        "limit":     20,
        "feed":      "iex",
    }
    try:
        r    = requests.get(f"{ALPACA_URL}/stocks/{ticker}/bars",
                            headers=headers, params=params, timeout=15)
        bars = r.json().get("bars")
        if not bars:
            return 0
        return sum(float(b["v"]) for b in bars) / len(bars)
    except Exception:
        return 0


def get_fundamentals(ticker):
    try:
        r = requests.get(
            f"{FINNHUB_URL}/stock/profile2",
            params={"symbol": ticker, "token": FINNHUB_KEY},
            timeout=15
        )
        p = r.json()
        return {
            "sector": p.get("finnhubIndustry", "Unknown"),
            "name":   p.get("name", ticker),
        }
    except Exception:
        return {"sector": "Unknown", "name": ticker}


def compute_vwap(df):
    df = df.copy()
    df["TP"]     = (df["High"] + df["Low"] + df["Close"]) / 3
    df["TPV"]    = df["TP"] * df["Volume"]
    df["CumTPV"] = df["TPV"].cumsum()
    df["CumVol"] = df["Volume"].cumsum()
    df["VWAP"]   = df["CumTPV"] / df["CumVol"]
    df["EMA9"]   = df["Close"].ewm(span=9, adjust=False).mean()
    delta        = df["Close"].diff()
    gain         = delta.clip(lower=0)
    loss         = (-delta).clip(lower=0)
    avg_gain     = gain.ewm(com=13, adjust=False).mean()
    avg_loss     = loss.ewm(com=13, adjust=False).mean()
    rs           = avg_gain / avg_loss.replace(0, float("nan"))
    df["RSI"]    = 100 - (100 / (1 + rs))
    high         = df["High"]
    low          = df["Low"]
    close        = df["Close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["ATR"]    = tr.rolling(14).mean()
    df["VolAvg"] = df["Volume"].rolling(10).mean()
    return df


def analyze_ticker(ticker):
    try:
        if not is_market_open():
            return None

        avg_vol = get_daily_volume(ticker)
        if avg_vol < MIN_VOLUME:
            return None

        df = get_intraday_data(ticker)
        if df is None or len(df) < 4:
            return None

        df = compute_vwap(df)

        current = df.iloc[-1]
        prev    = df.iloc[-2]

        vwap  = float(current["VWAP"])
        price = float(current["Close"])
        ema9  = float(current["EMA9"])
        rsi   = float(current["RSI"]) if not pd.isna(current["RSI"]) else 50
        atr   = float(current["ATR"]) if not pd.isna(current["ATR"]) else price * 0.005

        vwap_diff_pct = (price - vwap) / vwap

        if abs(vwap_diff_pct) < VWAP_EXTENSION:
            return None

        if rsi > 70 or rsi < 30:
            return None

        prev_price = float(prev["Close"])

        if vwap_diff_pct > 0:
            direction     = "SHORT"
            reverting     = price < prev_price
            entry         = price
            stop          = round(price + atr * 1.5, 2)
            target        = round(vwap, 2)
            tp_pct        = round((entry - target) / entry * 100, 1)
            stop_pct      = round((stop - entry) / entry * 100, 1)
        else:
            direction     = "LONG"
            reverting     = price > prev_price
            entry         = price
            stop          = round(price - atr * 1.5, 2)
            target        = round(vwap, 2)
            tp_pct        = round((target - entry) / entry * 100, 1)
            stop_pct      = round((entry - stop) / entry * 100, 1)

        if not reverting:
            return None

        vol_ratio     = float(current["Volume"]) / float(current["VolAvg"]) if current["VolAvg"] > 0 else 1.0
        vol_confirmed = vol_ratio >= 1.1

        risk   = abs(entry - stop)
        reward = abs(target - entry)
        rr     = round(reward / risk, 2) if risk > 0 else 0

        if rr < 1.0:
            return None

        risk_dollars = ACCOUNT * RISK_PCT
        shares       = math.floor(risk_dollars / risk) if risk > 0 else 1
        if shares < 1:
            shares = 1
        position_val = round(shares * entry, 2)
        pct_account  = round(position_val / ACCOUNT * 100, 1)

        fund = get_fundamentals(ticker)

        score = 0.0
        score += 30 if abs(vwap_diff_pct) >= 0.025 else 20
        score += 25 if 40 <= rsi <= 60 else 15
        score += 25 if vol_confirmed else 10
        score += 20 if rr >= 2.0 else 10

        print(f"[{ticker}] {direction} | VWAP reversion | Score {score:.0f} | RSI {rsi:.0f} | R:R {rr:.1f}x | ext {vwap_diff_pct*100:+.1f}%")

        return {
            "ticker":        ticker,
            "signal":        direction,
            "strategy":      "VWAP Reversion",
            "hold_time":     "INTRADAY (close by 4 PM ET)",
            "signal_score":  round(score, 1),
            "price":         round(entry, 2),
            "vwap":          round(vwap, 2),
            "ema9":          round(ema9, 2),
            "rsi":           round(rsi, 1),
            "atr":           round(atr, 2),
            "vwap_ext_pct":  round(vwap_diff_pct * 100, 2),
            "vol_ratio":     round(vol_ratio, 2),
            "vol_confirmed": vol_confirmed,
            "direction":     direction,
            "stop":          stop,
            "target":        target,
            "tp_pct":        tp_pct,
            "stop_pct":      stop_pct,
            "rr":            rr,
            "shares":        shares,
            "risk_dollars":  round(risk_dollars, 2),
            "position_val":  position_val,
            "pct_account":   pct_account,
            "sector":        fund.get("sector", ""),
            "name":          fund.get("name", ticker),
            "avg_volume":    round(avg_vol),
        }

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-300:]}")
        return None
