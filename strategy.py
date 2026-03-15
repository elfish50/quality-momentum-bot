"""
QUALITY MOMENTUM STRATEGY
Price data:   Alpaca Markets API (free, unlimited)
Fundamentals: Finnhub API (free, 60 calls/min)
Markets:      NASDAQ + NYSE + SP500
"""

import os
import math
import time
import traceback
import pandas as pd
import requests
from datetime import datetime, timedelta

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
FINNHUB_KEY   = os.getenv("FINNHUB_KEY", "")

ALPACA_URL  = "https://data.alpaca.markets/v2"
FINNHUB_URL = "https://finnhub.io/api/v1"


# ── Price data via Alpaca ─────────────────────────────────────────────────────

def get_price_data(ticker: str) -> pd.DataFrame | None:
    """6 months of daily OHLCV from Alpaca free tier."""
    end   = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    params = {
        "start":     start,
        "end":       end,
        "timeframe": "1Day",
        "limit":     200,
        "feed":      "iex",
    }

    try:
        r = requests.get(
            f"{ALPACA_URL}/stocks/{ticker}/bars",
            headers=headers,
            params=params,
            timeout=15,
        )
        data = r.json()

        bars = data.get("bars")
        if not bars or len(bars) < 60:
            print(f"[{ticker}] Not enough price data ({len(bars) if bars else 0} bars)")
            return None

        rows = []
        for bar in bars:
            rows.append({
                "Date":   pd.to_datetime(bar["t"]),
                "Open":   float(bar["o"]),
                "High":   float(bar["h"]),
                "Low":    float(bar["l"]),
                "Close":  float(bar["c"]),
                "Volume": float(bar["v"]),
            })

        df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
        return df

    except Exception as e:
        print(f"[{ticker}] Alpaca error: {e}")
        return None


# ── Fundamentals via Finnhub ──────────────────────────────────────────────────

def get_fundamentals(ticker: str) -> dict:
    """Company metrics via Finnhub free tier."""
    def safe_float(val, default=0.0):
        try:
            return float(val) if val not in (None, "None", "", "N/A") else default
        except Exception:
            return default

    try:
        # Basic financials
        r1 = requests.get(
            f"{FINNHUB_URL}/stock/metric",
            params={"symbol": ticker, "metric": "all", "token": FINNHUB_KEY},
            timeout=15,
        )
        metrics = r1.json().get("metric", {})

        # Company profile
        r2 = requests.get(
            f"{FINNHUB_URL}/stock/profile2",
            params={"symbol": ticker, "token": FINNHUB_KEY},
            timeout=15,
        )
        profile = r2.json()

        roe          = safe_float(metrics.get("roeTTM")) / 100  # Finnhub returns % 
        gross_margin = safe_float(metrics.get("grossMarginTTM")) / 100
        debt_equity  = safe_float(metrics.get("totalDebt/totalEquityAnnual"), default=999)
        eps_growth   = safe_float(metrics.get("epsGrowth3Y")) / 100
        pe_ratio     = safe_float(metrics.get("peNormalizedAnnual"))

        return {
            "roe":          roe,
            "debt_equity":  debt_equity,
            "gross_margin": gross_margin,
            "eps_growth":   eps_growth,
            "market_cap":   safe_float(profile.get("marketCapitalization")) * 1_000_000,
            "sector":       profile.get("finnhubIndustry", "Unknown"),
            "name":         profile.get("name", ticker),
            "pe_ratio":     pe_ratio,
        }

    except Exception as e:
        print(f"[{ticker}] Finnhub error: {e}")
        return {}


# ── Technical indicators ──────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    sma100 = close.rolling(100).mean()
    sma50  = close.rolling(50).mean()
    sma20  = close.rolling(20).mean()

    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    price_now = float(close.iloc[-1])

    def pct_return(n):
        if len(close) < n:
            return 0.0
        past = float(close.iloc[-n])
        return (price_now - past) / past if past != 0 else 0.0

    mom_6m = pct_return(126) if len(close) >= 126 else pct_return(len(close) - 1)
    mom_3m = pct_return(63)  if len(close) >= 63  else pct_return(len(close) - 1)
    mom_1m = pct_return(21)
    momentum_score = mom_6m * 0.6 + mom_3m * 0.4

    vol_20    = volume.rolling(20).mean().iloc[-1]
    vol_50    = volume.rolling(50).mean().iloc[-1]
    vol_ratio = float(vol_20 / vol_50) if vol_50 and not pd.isna(vol_50) and vol_50 > 0 else 1.0

    return {
        "price":          price_now,
        "sma200":         float(sma100.iloc[-1]) if not pd.isna(sma100.iloc[-1]) else None,
        "sma50":          float(sma50.iloc[-1])  if not pd.isna(sma50.iloc[-1])  else None,
        "sma20":          float(sma20.iloc[-1])  if not pd.isna(sma20.iloc[-1])  else None,
        "rsi":            float(rsi.iloc[-1])    if not pd.isna(rsi.iloc[-1])    else None,
        "atr14":          float(atr14.iloc[-1])  if not pd.isna(atr14.iloc[-1])  else None,
        "mom_6m":         mom_6m,
        "mom_3m":         mom_3m,
        "mom_1m":         mom_1m,
        "momentum_score": momentum_score,
        "vol_ratio":      vol_ratio,
    }


# ── Quality screen ────────────────────────────────────────────────────────────

def quality_score(fund: dict) -> tuple[float, list]:
    score  = 0.0
    failed = []

    roe = fund.get("roe", 0)
    if roe >= 0.20:
        score += 30
    elif roe >= 0.08:
        score += 15 + (roe - 0.08) / 0.12 * 15
    elif roe > 0:
        score += 8
    else:
        failed.append(f"ROE {roe:.1%}")

    gm = fund.get("gross_margin", 0)
    if gm >= 0.50:
        score += 25
    elif gm >= 0.20:
        score += 10 + (gm - 0.20) / 0.30 * 15
    elif gm > 0:
        score += 5
    else:
        failed.append(f"Margin {gm:.1%}")

    eg = fund.get("eps_growth", 0)
    if eg >= 0.20:
        score += 25
    elif eg >= 0.0:
        score += 10 + eg / 0.20 * 15
    else:
        failed.append(f"EPS growth {eg:.1%}")

    de = fund.get("debt_equity", 999)
    if de <= 0.30:
        score += 20
    elif de <= 1.50:
        score += 20 - (de - 0.30) / 1.20 * 15
    else:
        failed.append(f"D/E {de:.2f}")

    return round(score, 1), failed


# ── Signal scoring ────────────────────────────────────────────────────────────

def compute_signal_score(tech: dict, q_score: float) -> float:
    mom     = tech["momentum_score"]
    mom_pts = min(35, max(0, mom * 100 * 0.35))

    tech_pts = 0.0
    rsi = tech.get("rsi")
    if rsi and 40 <= rsi <= 65:
        tech_pts += 15 if 50 <= rsi <= 60 else 8
    if tech.get("vol_ratio", 1) >= 1.1:
        tech_pts += 5
    if tech.get("sma50") and tech["price"] > tech["sma50"]:
        tech_pts += 5

    q_pts = q_score * 0.40
    return round(q_pts + mom_pts + tech_pts, 1)


# ── Position sizing ───────────────────────────────────────────────────────────

def position_size(price: float, atr: float, account: float = 100_000,
                  risk_pct: float = 0.01) -> dict:
    risk_dollars = account * risk_pct
    stop_dist    = 2.0 * atr
    shares       = math.floor(risk_dollars / stop_dist) if stop_dist > 0 else 0
    return {
        "shares":       shares,
        "stop":         round(price - stop_dist, 2),
        "tp1":          round(price + 2.0 * atr, 2),
        "tp2":          round(price + 3.0 * atr, 2),
        "risk_dollars": round(risk_dollars, 2),
        "position_val": round(shares * price, 2),
        "pct_account":  round(shares * price / account * 100, 1),
    }


def classify_hold(signal_score: float) -> str:
    if signal_score >= 70:
        return "POSITION (2-6 weeks)"
    elif signal_score >= 45:
        return "SWING (3-10 days)"
    else:
        return "SKIP"


# ── Main analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> dict | None:
    try:
        df = get_price_data(ticker)
        if df is None:
            return None

        tech = compute_indicators(df)
        if tech["sma200"] is None or tech["rsi"] is None:
            print(f"[{ticker}] Indicators not ready")
            return None

        if tech["price"] <= tech["sma200"]:
            print(f"[{ticker}] SKIP | Below 100 SMA")
            return None

        if not (30 <= tech["rsi"] <= 75):
            print(f"[{ticker}] SKIP | RSI {tech['rsi']:.0f}")
            return None

        if tech["mom_3m"] <= -0.10:
            print(f"[{ticker}] SKIP | Momentum {tech['mom_3m']:.1%}")
            return None

        fund = get_fundamentals(ticker)
        if not fund:
            return None

        q_score, failed = quality_score(fund)
        if q_score < 25:
            print(f"[{ticker}] SKIP | Quality {q_score}")
            return None

        sig_score = compute_signal_score(tech, q_score)
        hold_time = classify_hold(sig_score)

        if hold_time == "SKIP":
            print(f"[{ticker}] SKIP | Signal {sig_score}")
            return None

        atr    = tech["atr14"] or (tech["price"] * 0.02)
        sizing = position_size(tech["price"], atr)

        print(f"[{ticker}] BUY | Score {sig_score} | {hold_time} | RSI {tech['rsi']:.0f}")
        return {
            "ticker":         ticker,
            "signal":         "BUY",
            "hold_time":      hold_time,
            "signal_score":   sig_score,
            "quality_score":  q_score,
            "price":          round(tech["price"], 2),
            "sma200":         round(tech["sma200"], 2),
            "sma50":          round(tech["sma50"], 2) if tech["sma50"] else None,
            "rsi":            round(tech["rsi"], 1),
            "atr14":          round(atr, 2),
            "mom_6m":         round(tech["mom_6m"] * 100, 1),
            "mom_3m":         round(tech["mom_3m"] * 100, 1),
            "mom_1m":         round(tech["mom_1m"] * 100, 1),
            "momentum_score": round(tech["momentum_score"] * 100, 1),
            "roe":            round(fund.get("roe", 0) * 100, 1),
            "gross_margin":   round(fund.get("gross_margin", 0) * 100, 1),
            "eps_growth":     round(fund.get("eps_growth", 0) * 100, 1),
            "debt_equity":    round(fund.get("debt_equity", 0), 2),
            "pe_ratio":       round(fund.get("pe_ratio", 0), 1),
            "sector":         fund.get("sector", ""),
            "name":           fund.get("name", ticker),
            "stop":           sizing["stop"],
            "tp1":            sizing["tp1"],
            "tp2":            sizing["tp2"],
            "shares":         sizing["shares"],
            "risk_dollars":   sizing["risk_dollars"],
            "position_val":   sizing["position_val"],
            "pct_account":    sizing["pct_account"],
            "quality_notes":  failed,
        }

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-200:]}")
        return None
