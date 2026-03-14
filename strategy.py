"""
QUALITY MOMENTUM STRATEGY
Data: Alpha Vantage API (free tier - 25 calls/day)
Markets: NASDAQ + NYSE
"""

import os
import math
import time
import traceback
import numpy as np
import pandas as pd
import requests

AV_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
AV_URL = "https://www.alphavantage.co/query"


# ── Alpha Vantage data fetch ──────────────────────────────────────────────────

def av_get(params: dict, retries: int = 3) -> dict:
    params["apikey"] = AV_KEY
    for attempt in range(retries):
        try:
            r = requests.get(AV_URL, params=params, timeout=15)
            data = r.json()
            if "Note" in data:
                print("Alpha Vantage rate limit hit — waiting 60s...")
                time.sleep(60)
                continue
            if "Information" in data:
                print(f"AV info: {data['Information'][:80]}")
                return {}
            return data
        except Exception as e:
            print(f"AV request failed attempt {attempt+1}: {e}")
            time.sleep(3)
    return {}


def get_price_data(ticker: str) -> pd.DataFrame | None:
    """Daily OHLCV — 1 year via Alpha Vantage TIME_SERIES_DAILY."""
    data = av_get({
        "function":   "TIME_SERIES_DAILY",
        "symbol":     ticker,
        "outputsize": "full",
    })

    ts = data.get("Time Series (Daily)")
    if not ts:
        print(f"[{ticker}] No price data from AV")
        return None

    rows = []
    for date_str, vals in ts.items():
        rows.append({
            "Date":   date_str,
            "Open":   float(vals["1. open"]),
            "High":   float(vals["2. high"]),
            "Low":    float(vals["3. low"]),
            "Close":  float(vals["4. close"]),
            "Volume": float(vals["5. volume"]),
        })

    df = pd.DataFrame(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").reset_index(drop=True)

    # Keep last 1 year
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=1)
    df = df[df["Date"] >= cutoff].reset_index(drop=True)

    if len(df) < 60:
        print(f"[{ticker}] Not enough price data ({len(df)} bars)")
        return None

    return df


def get_fundamentals(ticker: str) -> dict:
    """Company overview via Alpha Vantage OVERVIEW endpoint."""
    data = av_get({
        "function": "OVERVIEW",
        "symbol":   ticker,
    })

    if not data or "Symbol" not in data:
        print(f"[{ticker}] No fundamental data from AV")
        return {}

    def safe_float(val, default=0.0):
        try:
            return float(val) if val not in (None, "None", "-", "") else default
        except Exception:
            return default

    roe          = safe_float(data.get("ReturnOnEquityTTM"))
    gross_margin = safe_float(data.get("GrossProfitTTM"))
    revenue      = safe_float(data.get("RevenueTTM"))
    gross_margin_pct = (gross_margin / revenue) if revenue > 0 else 0.0

    eps           = safe_float(data.get("EPS"))
    forward_pe    = safe_float(data.get("ForwardPE"))
    trailing_pe   = safe_float(data.get("TrailingPE"))
    price         = safe_float(data.get("AnalystTargetPrice"))
    eps_growth    = safe_float(data.get("QuarterlyEarningsGrowthYOY"))

    debt_equity   = safe_float(data.get("DebtToEquityRatio"), default=999)

    return {
        "roe":          roe,
        "debt_equity":  debt_equity,
        "gross_margin": gross_margin_pct,
        "eps_growth":   eps_growth,
        "market_cap":   safe_float(data.get("MarketCapitalization")),
        "sector":       data.get("Sector", "Unknown"),
        "name":         data.get("Name", ticker),
        "pe_ratio":     trailing_pe,
    }


# ── Technical indicators ──────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    sma200 = close.rolling(200).mean()
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

    mom_6m = pct_return(126)
    mom_3m = pct_return(63)
    mom_1m = pct_return(21)
    momentum_score = mom_6m * 0.6 + mom_3m * 0.4

    vol_ratio = float(
        (volume.rolling(20).mean() / volume.rolling(50).mean()).iloc[-1]
    ) if not pd.isna((volume.rolling(20).mean() / volume.rolling(50).mean()).iloc[-1]) else 1.0

    return {
        "price":          price_now,
        "sma200":         float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None,
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
        failed.append(f"ROE {roe:.0%}")

    gm = fund.get("gross_margin", 0)
    if gm >= 0.50:
        score += 25
    elif gm >= 0.20:
        score += 10 + (gm - 0.20) / 0.30 * 15
    elif gm > 0:
        score += 5
    else:
        failed.append(f"Margin {gm:.0%}")

    eg = fund.get("eps_growth", 0)
    if eg >= 0.20:
        score += 25
    elif eg >= 0.0:
        score += 10 + eg / 0.20 * 15
    else:
        failed.append(f"EPS growth {eg:.0%}")

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
    mom      = tech["momentum_score"]
    mom_pts  = min(35, max(0, mom * 100 * 0.35))

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
            print(f"[{ticker}] SKIP | Below 200 SMA")
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
            print(f"[{ticker}] SKIP | Signal score {sig_score}")
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


















