"""
QUALITY MOMENTUM STRATEGY
Berkshire-style quality screen + quantitative momentum ranking
Markets: NASDAQ + NYSE
Data: yfinance only (free)

FILTERS:
  Quality:  ROE > 15% | Debt/Equity < 0.5 | Gross Margin > 40% | EPS growth > 10%
  Trend:    Price > 200 SMA
  Momentum: RSI 40-65 | 6M+3M momentum score
  Risk:     ATR-based position sizing | 2xATR stop | 3xATR target

SIGNAL STRENGTH → HOLD TIME:
  Strong  (score >= 80): Position trade 2-6 weeks
  Medium  (score 60-79): Swing trade 3-10 days
  Weak    (score < 60):  HOLD/skip
"""

import math
import traceback
import numpy as np
import pandas as pd
import yfinance as yf


# ── Data fetch ────────────────────────────────────────────────────────────────

def get_price_data(ticker: str) -> pd.DataFrame | None:
    """1 year of daily OHLCV — enough for all indicators."""
    try:
        df = yf.download(ticker, period="1y", interval="1d",
                         auto_adjust=True, progress=False)
        if df.empty or len(df) < 60:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [str(c) for c in df.columns]
        return df
    except Exception:
        return None


def get_fundamentals(ticker: str) -> dict:
    """
    Pull fundamentals from yfinance info dict.
    Returns dict with ROE, debtToEquity, grossMargins, epsGrowth, marketCap.
    """
    try:
        info = yf.Ticker(ticker).info
        # EPS growth: compare trailingEps vs forwardEps as proxy
        trailing_eps = info.get("trailingEps") or 0
        forward_eps  = info.get("forwardEps")  or 0
        if trailing_eps and trailing_eps != 0 and forward_eps:
            eps_growth = (forward_eps - trailing_eps) / abs(trailing_eps)
        else:
            eps_growth = info.get("earningsGrowth") or 0

        return {
            "roe":          info.get("returnOnEquity")   or 0,   # e.g. 0.25 = 25%
            "debt_equity":  info.get("debtToEquity")     or 999, # ratio
            "gross_margin": info.get("grossMargins")     or 0,   # e.g. 0.45 = 45%
            "eps_growth":   eps_growth,                          # e.g. 0.15 = 15%
            "market_cap":   info.get("marketCap")        or 0,
            "sector":       info.get("sector")           or "Unknown",
            "name":         info.get("shortName")        or ticker,
            "pe_ratio":     info.get("trailingPE")       or 0,
        }
    except Exception:
        return {}


# ── Technical indicators ──────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["Close"].squeeze()
    high   = df["High"].squeeze()
    low    = df["Low"].squeeze()
    volume = df["Volume"].squeeze()

    # SMAs
    sma200 = close.rolling(200).mean()
    sma50  = close.rolling(50).mean()
    sma20  = close.rolling(20).mean()

    # RSI(14)
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))

    # ATR(14)
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    # Momentum scores
    price_now = float(close.iloc[-1])
    def pct_return(n):
        if len(close) < n:
            return 0.0
        past = float(close.iloc[-n])
        return (price_now - past) / past if past != 0 else 0.0

    mom_6m = pct_return(126)   # ~6 months trading days
    mom_3m = pct_return(63)    # ~3 months
    mom_1m = pct_return(21)    # ~1 month  (risk-off filter)
    momentum_score = mom_6m * 0.6 + mom_3m * 0.4

    # Volume trend (20-day avg vs 50-day avg)
    vol_ratio = (volume.rolling(20).mean() / volume.rolling(50).mean()).iloc[-1]

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
        "vol_ratio":      float(vol_ratio)       if not pd.isna(vol_ratio)       else 1.0,
    }


# ── Quality screen ────────────────────────────────────────────────────────────

def quality_score(fund: dict) -> tuple[float, list]:
    """
    Returns (score 0-100, list of failed checks).
    Score weights: ROE 30 | Gross Margin 25 | EPS Growth 25 | Debt/Equity 20
    """
    score  = 0.0
    passed = []
    failed = []

    # ROE > 15% (max points at 30%+)
    roe = fund.get("roe", 0)
    if roe >= 0.30:
        score += 30; passed.append(f"ROE {roe:.0%} (excellent)")
    elif roe >= 0.15:
        score += 15 + (roe - 0.15) / 0.15 * 15
        passed.append(f"ROE {roe:.0%} (good)")
    else:
        failed.append(f"ROE {roe:.0%} < 15%")

    # Gross Margin > 40%
    gm = fund.get("gross_margin", 0)
    if gm >= 0.60:
        score += 25; passed.append(f"Margin {gm:.0%} (excellent)")
    elif gm >= 0.40:
        score += 10 + (gm - 0.40) / 0.20 * 15
        passed.append(f"Margin {gm:.0%} (good)")
    else:
        failed.append(f"Gross margin {gm:.0%} < 40%")

    # EPS Growth > 10%
    eg = fund.get("eps_growth", 0)
    if eg >= 0.25:
        score += 25; passed.append(f"EPS growth {eg:.0%} (excellent)")
    elif eg >= 0.10:
        score += 10 + (eg - 0.10) / 0.15 * 15
        passed.append(f"EPS growth {eg:.0%} (good)")
    else:
        failed.append(f"EPS growth {eg:.0%} < 10%")

    # Debt/Equity < 0.5 (lower is better)
    de = fund.get("debt_equity", 999)
    if de <= 0.20:
        score += 20; passed.append(f"D/E {de:.2f} (fortress)")
    elif de <= 0.50:
        score += 20 - (de - 0.20) / 0.30 * 10
        passed.append(f"D/E {de:.2f} (healthy)")
    else:
        failed.append(f"D/E {de:.2f} > 0.5")

    return round(score, 1), failed


# ── Signal scoring ────────────────────────────────────────────────────────────

def compute_signal_score(tech: dict, q_score: float) -> float:
    """
    Combined score 0-100:
      Quality    40%
      Momentum   35%
      Technical  25%
    """
    # Momentum component (0-35)
    mom = tech["momentum_score"]
    mom_pts = min(35, max(0, mom * 100 * 0.35))

    # Technical component (0-25)
    tech_pts = 0.0
    rsi = tech.get("rsi")
    if rsi and 40 <= rsi <= 65:
        # Sweet spot: RSI 50-60 = full points
        if 50 <= rsi <= 60:
            tech_pts += 15
        else:
            tech_pts += 8
    # Volume confirmation
    if tech.get("vol_ratio", 1) >= 1.1:
        tech_pts += 5
    # Price above SMA50 (extra confirmation)
    if tech.get("sma50") and tech["price"] > tech["sma50"]:
        tech_pts += 5

    # Quality component (0-40): scale q_score (0-100) to 0-40
    q_pts = q_score * 0.40

    return round(q_pts + mom_pts + tech_pts, 1)


# ── Position sizing (ATR-based Kelly-inspired) ────────────────────────────────

def position_size(price: float, atr: float, account: float = 100_000,
                  risk_pct: float = 0.01) -> dict:
    """
    Risk 1% of account per trade.
    Stop = 2x ATR below entry.
    Target1 = 2x ATR (1:1 R/R minimum)
    Target2 = 3x ATR (full target)
    Shares = (account * risk_pct) / (2 * ATR)
    """
    risk_dollars = account * risk_pct
    stop_dist    = 2.0 * atr
    shares       = math.floor(risk_dollars / stop_dist) if stop_dist > 0 else 0
    stop         = round(price - stop_dist, 2)
    tp1          = round(price + 2.0 * atr, 2)   # 1:1 R/R
    tp2          = round(price + 3.0 * atr, 2)   # 1:1.5 R/R
    position_val = round(shares * price, 2)
    pct_account  = round(position_val / account * 100, 1)

    return {
        "shares":        shares,
        "stop":          stop,
        "tp1":           tp1,
        "tp2":           tp2,
        "risk_dollars":  round(risk_dollars, 2),
        "position_val":  position_val,
        "pct_account":   pct_account,
    }


# ── Hold time classification ──────────────────────────────────────────────────

def classify_hold(signal_score: float) -> str:
    if signal_score >= 80:
        return "POSITION (2-6 weeks)"
    elif signal_score >= 60:
        return "SWING (3-10 days)"
    else:
        return "SKIP"


# ── Main analyzer ─────────────────────────────────────────────────────────────

def analyze_ticker(ticker: str) -> dict | None:
    try:
        # 1. Price data
        df = get_price_data(ticker)
        if df is None:
            print(f"[{ticker}] No price data")
            return None

        # 2. Technical indicators
        tech = compute_indicators(df)
        if tech["sma200"] is None or tech["rsi"] is None:
            print(f"[{ticker}] Indicators not ready")
            return None

        # 3. Trend filter — must be above 200 SMA
        if tech["price"] <= tech["sma200"]:
            print(f"[{ticker}] SKIP | Below 200 SMA")
            return None

        # 4. RSI filter — not overbought, not in freefall
        if not (35 <= tech["rsi"] <= 70):
            print(f"[{ticker}] SKIP | RSI {tech['rsi']:.0f} out of range")
            return None

        # 5. Momentum filter — positive 6M momentum
        if tech["mom_6m"] <= 0:
            print(f"[{ticker}] SKIP | Negative 6M momentum")
            return None

        # 6. Fundamentals
        fund = get_fundamentals(ticker)
        if not fund:
            print(f"[{ticker}] No fundamental data")
            return None

        q_score, failed = quality_score(fund)

        # Must pass at least 3 of 4 quality checks (score >= 40)
        if q_score < 40:
            print(f"[{ticker}] SKIP | Quality score {q_score} — failed: {failed}")
            return None

        # 7. Signal score
        sig_score = compute_signal_score(tech, q_score)
        hold_time = classify_hold(sig_score)

        if hold_time == "SKIP":
            print(f"[{ticker}] SKIP | Signal score {sig_score} too low")
            return None

        # 8. Position sizing
        atr   = tech["atr14"] or (tech["price"] * 0.02)
        sizing = position_size(tech["price"], atr)

        result = {
            "ticker":         ticker,
            "signal":         "BUY",
            "hold_time":      hold_time,
            "signal_score":   sig_score,
            "quality_score":  q_score,
            # Price & technicals
            "price":          round(tech["price"], 2),
            "sma200":         round(tech["sma200"], 2),
            "sma50":          round(tech["sma50"], 2) if tech["sma50"] else None,
            "rsi":            round(tech["rsi"], 1),
            "atr14":          round(atr, 2),
            # Momentum
            "mom_6m":         round(tech["mom_6m"] * 100, 1),
            "mom_3m":         round(tech["mom_3m"] * 100, 1),
            "mom_1m":         round(tech["mom_1m"] * 100, 1),
            "momentum_score": round(tech["momentum_score"] * 100, 1),
            # Fundamentals
            "roe":            round(fund.get("roe", 0) * 100, 1),
            "gross_margin":   round(fund.get("gross_margin", 0) * 100, 1),
            "eps_growth":     round(fund.get("eps_growth", 0) * 100, 1),
            "debt_equity":    round(fund.get("debt_equity", 0), 2),
            "pe_ratio":       round(fund.get("pe_ratio", 0), 1),
            "sector":         fund.get("sector", ""),
            "name":           fund.get("name", ticker),
            # Risk management
            "stop":           sizing["stop"],
            "tp1":            sizing["tp1"],
            "tp2":            sizing["tp2"],
            "shares":         sizing["shares"],
            "risk_dollars":   sizing["risk_dollars"],
            "position_val":   sizing["position_val"],
            "pct_account":    sizing["pct_account"],
            # Failed quality checks
            "quality_notes":  failed,
        }

        print(f"[{ticker}] BUY | Score {sig_score} | {hold_time} | RSI {tech['rsi']:.0f} | Mom {tech['momentum_score']*100:.1f}%")
        return result

    except Exception:
        print(f"[{ticker}] ERROR: {traceback.format_exc()[-200:]}")
        return None
