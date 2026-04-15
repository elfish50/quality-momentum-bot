"""
trader.py — Order Execution

Handles both LONG (BUY) and SHORT signals from strategy.py.

LONG flow:
  1. Market buy entry
  2. GTC limit sell at TP1 (1/3 shares)
  3. GTC stop-market sell below stop (all shares initially)
     → monitor.py adjusts after TP1 hit (move stop to breakeven, reduce qty)

SHORT flow:
  1. Market sell short entry
  2. GTC limit buy-to-cover at TP1 (1/3 shares)
  3. GTC stop-market buy-to-cover above stop (all shares initially)
     → monitor.py adjusts after TP1 hit (move stop to breakeven, reduce qty)

Alpaca paper trading endpoint is used throughout.
"""

import os
import math
import traceback
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
PAPER_URL     = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

ACCOUNT  = 1_000
RISK_PCT = 0.10


# ── Alpaca Account Helpers ────────────────────────────────────────────────────

def get_account():
    try:
        r = requests.get(f"{PAPER_URL}/account", headers=HEADERS, timeout=15)
        return r.json() if isinstance(r.json(), dict) else {}
    except Exception:
        return {}


def get_buying_power():
    return float(get_account().get("buying_power", 0))


def get_positions():
    try:
        r    = requests.get(f"{PAPER_URL}/positions", headers=HEADERS, timeout=15)
        data = r.json()
        return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def get_position(ticker):
    try:
        r = requests.get(f"{PAPER_URL}/positions/{ticker}", headers=HEADERS, timeout=15)
        if r.status_code == 404:
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_orders(status="open"):
    try:
        r = requests.get(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            params={"status": status, "limit": 50},
            timeout=15
        )
        data = r.json()
        return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def cancel_all_orders():
    try:
        r = requests.delete(f"{PAPER_URL}/orders", headers=HEADERS, timeout=15)
        return r.status_code in [200, 207]
    except Exception:
        return False


def cancel_order(order_id: str) -> bool:
    try:
        r = requests.delete(f"{PAPER_URL}/orders/{order_id}", headers=HEADERS, timeout=15)
        return r.status_code in [200, 204]
    except Exception:
        return False


# ── Order Placement ───────────────────────────────────────────────────────────

def _place_order(payload: dict, ticker: str, label: str) -> dict:
    """
    Place a single order. Returns the Alpaca order response dict,
    or {"error": "..."} on failure.
    """
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json=payload,
            timeout=15
        )
        data = r.json()
        if not r.ok:
            msg = data.get("message", r.text[:200])
            print(f"[{ticker}] {label} order failed ({r.status_code}): {msg}")
            return {"error": msg}
        print(f"[{ticker}] {label} order placed — id:{data.get('id','?')} status:{data.get('status','?')}")
        return data
    except Exception as e:
        print(f"[{ticker}] {label} order exception: {e}")
        return {"error": str(e)}


# ── LONG execution ────────────────────────────────────────────────────────────

def _execute_long(sig: dict) -> dict:
    ticker = sig["ticker"]
    price  = sig["price"]
    stop   = sig["stop"]
    tp1    = sig["tp1"]
    tp2    = sig["tp2"]
    shares = sig["shares"]

    if shares < 1:
        return {"success": False, "error": "shares < 1"}

    tp1_shares  = max(1, math.floor(shares / 3))
    stop_shares = shares  # will be reduced by monitor after TP1

    # 1. Entry: market buy
    entry = _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }, ticker, "LONG entry")

    if "error" in entry:
        return {"success": False, "error": entry["error"]}

    entry_id = entry.get("id", "")

    # 2. TP1 limit sell (1/3)
    tp1_order = _place_order({
        "symbol":        ticker,
        "qty":           str(tp1_shares),
        "side":          "sell",
        "type":          "limit",
        "time_in_force": "gtc",
        "limit_price":   str(round(tp1, 2)),
    }, ticker, "TP1 limit sell")

    tp1_order_id = tp1_order.get("id", "") if "error" not in tp1_order else ""

    # 3. Stop-market sell (full qty — monitor reduces after TP1 hit)
    stop_order = _place_order({
        "symbol":        ticker,
        "qty":           str(stop_shares),
        "side":          "sell",
        "type":          "stop",
        "time_in_force": "gtc",
        "stop_price":    str(round(stop, 2)),
    }, ticker, "stop-market sell")

    stop_order_id = stop_order.get("id", "") if "error" not in stop_order else ""

    return {
        "success":       True,
        "direction":     "LONG",
        "ticker":        ticker,
        "shares":        shares,
        "tp1_shares":    tp1_shares,
        "entry_price":   price,
        "stop":          stop,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           sig.get("tp3", 0),
        "entry_id":      entry_id,
        "tp1_order_id":  tp1_order_id,
        "stop_order_id": stop_order_id,
        "timestamp":     datetime.now().isoformat(),
    }


# ── SHORT execution ───────────────────────────────────────────────────────────

def _execute_short(sig: dict) -> dict:
    """
    Short-sell flow:
    1. Market sell short (entry)
    2. GTC limit buy-to-cover at TP1 (1/3 shares) — price BELOW entry
    3. GTC stop-market buy-to-cover at stop (price ABOVE entry, loss cap)
    """
    ticker = sig["ticker"]
    price  = sig["price"]
    stop   = sig["stop"]   # above entry for shorts
    tp1    = sig["tp1"]    # below entry for shorts
    tp2    = sig["tp2"]
    shares = sig["shares"]

    if shares < 1:
        return {"success": False, "error": "shares < 1"}

    # Validate short direction — stop must be above entry
    if stop <= price:
        msg = f"SHORT stop {stop} must be > entry {price}"
        print(f"[{ticker}] {msg}")
        return {"success": False, "error": msg}

    if tp1 >= price:
        msg = f"SHORT tp1 {tp1} must be < entry {price}"
        print(f"[{ticker}] {msg}")
        return {"success": False, "error": msg}

    tp1_shares  = max(1, math.floor(shares / 3))
    stop_shares = shares

    # 1. Entry: market sell short
    entry = _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }, ticker, "SHORT entry (sell short)")

    if "error" in entry:
        return {"success": False, "error": entry["error"]}

    entry_id = entry.get("id", "")

    # 2. TP1 limit buy-to-cover (1/3 shares)
    tp1_order = _place_order({
        "symbol":        ticker,
        "qty":           str(tp1_shares),
        "side":          "buy",
        "type":          "limit",
        "time_in_force": "gtc",
        "limit_price":   str(round(tp1, 2)),
    }, ticker, "TP1 buy-to-cover")

    tp1_order_id = tp1_order.get("id", "") if "error" not in tp1_order else ""

    # 3. Stop-market buy-to-cover (full qty)
    stop_order = _place_order({
        "symbol":        ticker,
        "qty":           str(stop_shares),
        "side":          "buy",
        "type":          "stop",
        "time_in_force": "gtc",
        "stop_price":    str(round(stop, 2)),
    }, ticker, "stop-market buy-to-cover")

    stop_order_id = stop_order.get("id", "") if "error" not in stop_order else ""

    return {
        "success":       True,
        "direction":     "SHORT",
        "ticker":        ticker,
        "shares":        shares,
        "tp1_shares":    tp1_shares,
        "entry_price":   price,
        "stop":          stop,
        "tp1":           tp1,
        "tp2":           tp2,
        "tp3":           sig.get("tp3", 0),
        "entry_id":      entry_id,
        "tp1_order_id":  tp1_order_id,
        "stop_order_id": stop_order_id,
        "timestamp":     datetime.now().isoformat(),
    }


# ── Public entry point ────────────────────────────────────────────────────────

def execute_signal(sig: dict) -> dict:
    """
    Route BUY → long execution, SHORT → short execution.
    Returns a result dict with success flag and order IDs.
    """
    direction = sig.get("direction", "LONG")
    signal    = sig.get("signal", "")

    # Normalize: "BUY" signal with LONG direction, "SHORT" signal with SHORT direction
    if signal == "SHORT" or direction == "SHORT":
        return _execute_short(sig)
    else:
        return _execute_long(sig)


# ── Telegram formatting ───────────────────────────────────────────────────────

def format_execution_result(result: dict, sig: dict) -> str:
    if not result.get("success"):
        return f"❌ Order failed: {result.get('error', 'unknown error')}"

    ticker    = result["ticker"]
    direction = result.get("direction", "LONG")
    shares    = result["shares"]
    price     = result["entry_price"]
    stop      = result["stop"]
    tp1       = result["tp1"]
    tp2       = result["tp2"]
    tp3       = result.get("tp3", 0)
    risk_amt  = round(abs(price - stop) * shares, 2)

    if direction == "LONG":
        dir_icon   = "🟢 LONG"
        stop_label = "Stop (below)"
        tp_dir     = "▲"
    else:
        dir_icon   = "🔴 SHORT"
        stop_label = "Stop (above)"
        tp_dir     = "▼"

    tp1_pct = round(abs(tp1 - price) / price * 100, 1)
    tp2_pct = round(abs(tp2 - price) / price * 100, 1)
    tp3_pct = round(abs(tp3 - price) / price * 100, 1) if tp3 else 0

    tp1_shares = result.get("tp1_shares", max(1, shares // 3))
    rem_shares = shares - tp1_shares

    lines = [
        f"✅ {dir_icon} EXECUTED — {ticker}",
        f"",
        f"Entry:    ${price:.2f}  ×{shares} shares",
        f"Risk:     ${risk_amt:.2f}",
        f"",
        f"{stop_label}: ${stop:.2f}",
        f"TP1 ({tp_dir}{tp1_pct:.1f}%): ${tp1:.2f}  [{tp1_shares} shares]",
        f"TP2 ({tp_dir}{tp2_pct:.1f}%): ${tp2:.2f}  [{rem_shares} shares]",
    ]
    if tp3:
        lines.append(f"TP3 ({tp_dir}{tp3_pct:.1f}%): ${tp3:.2f}  [stretch]")

    lines += [
        f"",
        f"Orders:",
        f"  Entry:  {result.get('entry_id','?')[:8]}",
        f"  TP1:    {result.get('tp1_order_id','?')[:8] or 'failed'}",
        f"  Stop:   {result.get('stop_order_id','?')[:8] or 'failed'}",
    ]

    return "\n".join(lines)
