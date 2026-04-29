"""
trader.py — Order Execution (LONG ONLY)

v3 — Shorts completely removed.

LONG flow:
  1. Market buy entry
  2. Wait for fill (poll up to 30s)
  3. GTC limit sell at TP1 (1/3 shares)
  4. GTC stop-market sell at stop (full shares — monitor reduces after TP1)
"""

import os
import math
import time
import traceback
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
PAPER_URL     = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

RISK_PCT = 0.01  # 1% of live equity per trade

FILL_POLL_INTERVAL = 2   # seconds between polls
FILL_POLL_MAX      = 15  # max attempts (30 seconds total)


# ── Alpaca Account Helpers ────────────────────────────────────────────────────

def get_account():
    try:
        r = requests.get(f"{PAPER_URL}/account", headers=HEADERS, timeout=15)
        return r.json() if isinstance(r.json(), dict) else {}
    except Exception:
        return {}


def get_account_equity() -> float:
    try:
        return float(get_account().get("equity", 100_000))
    except Exception:
        return 100_000


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


def _wait_for_fill(order_id: str, ticker: str) -> dict | None:
    for attempt in range(FILL_POLL_MAX):
        try:
            r = requests.get(
                f"{PAPER_URL}/orders/{order_id}",
                headers=HEADERS,
                timeout=10,
            )
            if not r.ok:
                print(f"[{ticker}] fill poll error {r.status_code}")
                time.sleep(FILL_POLL_INTERVAL)
                continue

            order  = r.json()
            status = order.get("status", "")
            print(f"[{ticker}] fill poll {attempt+1}/{FILL_POLL_MAX} — status: {status}")

            if status == "filled":
                return order
            if status in ("canceled", "expired", "rejected"):
                print(f"[{ticker}] entry order {status} — aborting")
                return None

        except Exception as e:
            print(f"[{ticker}] fill poll exception: {e}")

        time.sleep(FILL_POLL_INTERVAL)

    print(f"[{ticker}] entry not filled after {FILL_POLL_MAX * FILL_POLL_INTERVAL}s — protective orders skipped")
    return None


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

    tp1_shares = max(1, math.floor(shares / 3))

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

    # 2. Wait for fill
    filled_order = _wait_for_fill(entry_id, ticker)
    filled_price = price
    if filled_order:
        try:
            fp = filled_order.get("filled_avg_price")
            if fp:
                filled_price = float(fp)
        except Exception:
            pass
    else:
        # Entry placed but not confirmed — return without protective orders
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
            "tp1_order_id":  "",
            "stop_order_id": "",
            "fill_warning":  "Entry not confirmed filled — protective orders not placed",
            "timestamp":     datetime.now().isoformat(),
        }

    # 3. TP1 limit sell (1/3 shares)
    tp1_order = _place_order({
        "symbol":        ticker,
        "qty":           str(tp1_shares),
        "side":          "sell",
        "type":          "limit",
        "time_in_force": "gtc",
        "limit_price":   str(round(tp1, 2)),
    }, ticker, "TP1 limit sell")

    tp1_order_id = tp1_order.get("id", "") if "error" not in tp1_order else ""

    time.sleep(0.5)

    # 4. Stop-market sell (full qty — monitor reduces after TP1 hit)
    stop_order = _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
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
        "entry_price":   filled_price,
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
    """Always executes as LONG. SHORT signals are not supported."""
    return _execute_long(sig)


# ── Telegram formatting ───────────────────────────────────────────────────────

def format_execution_result(result: dict, sig: dict) -> str:
    if not result.get("success"):
        return f"❌ Order failed: {result.get('error', 'unknown error')}"

    ticker = result["ticker"]
    shares = result["shares"]
    price  = result["entry_price"]
    stop   = result["stop"]
    tp1    = result["tp1"]
    tp2    = result["tp2"]
    tp3    = result.get("tp3", 0)

    risk_amt   = round(abs(price - stop) * shares, 2)
    tp1_pct    = round((tp1 - price) / price * 100, 1)
    tp2_pct    = round((tp2 - price) / price * 100, 1)
    tp3_pct    = round((tp3 - price) / price * 100, 1) if tp3 else 0
    tp1_shares = result.get("tp1_shares", max(1, shares // 3))
    rem_shares = shares - tp1_shares

    tp1_id  = result.get("tp1_order_id", "")
    stop_id = result.get("stop_order_id", "")
    warn    = result.get("fill_warning", "")

    lines = [
        f"✅ 🟢 LONG EXECUTED — {ticker}",
        f"",
        f"Entry:    ${price:.2f}  ×{shares} shares",
        f"Risk:     ${risk_amt:.2f}",
        f"",
        f"Stop:     ${stop:.2f}",
        f"TP1 (+{tp1_pct:.1f}%): ${tp1:.2f}  [{tp1_shares} shares]",
        f"TP2 (+{tp2_pct:.1f}%): ${tp2:.2f}  [{rem_shares} shares]",
    ]
    if tp3:
        lines.append(f"TP3 (+{tp3_pct:.1f}%): ${tp3:.2f}  [stretch]")

    lines += [
        f"",
        f"Orders:",
        f"  Entry:  {result.get('entry_id','?')[:8]}",
        f"  TP1:    {tp1_id[:8] if tp1_id else '⚠ failed'}",
        f"  Stop:   {stop_id[:8] if stop_id else '⚠ failed'}",
    ]

    if warn:
        lines += ["", f"⚠ {warn}"]

    return "\n".join(lines)


# ── Portfolio & Trade History ─────────────────────────────────────────────────

def _fetch_portfolio_history(period: str, timeframe: str) -> list[float]:
    try:
        params = {"timeframe": timeframe}
        if period != "all":
            params["period"] = period
        r = requests.get(
            f"{PAPER_URL}/account/portfolio/history",
            headers=HEADERS,
            params=params,
            timeout=15,
        )
        if not r.ok:
            return []
        data   = r.json()
        equity = data.get("equity", [])
        return [float(v) for v in equity if v is not None]
    except Exception:
        return []


def _calc_gain(equity_series: list[float]) -> tuple[float | None, float | None]:
    if len(equity_series) < 2:
        return None, None
    start = equity_series[0]
    end   = equity_series[-1]
    if start == 0:
        return None, None
    gain = end - start
    pct  = gain / start * 100
    return gain, pct


def _fmt_gain(label: str, gain: float | None, pct: float | None) -> str:
    if gain is None:
        return f"{label}: N/A"
    arrow = "🟢" if gain >= 0 else "🔴"
    sign  = "+" if gain >= 0 else ""
    return f"{label}: {arrow} {sign}${gain:,.2f} ({sign}{pct:.2f}%)"


def format_portfolio() -> str:
    acc       = get_account()
    positions = get_positions()

    equity  = float(acc.get("equity", 0))
    cash    = float(acc.get("cash", 0))
    last_eq = float(acc.get("last_equity", equity) or equity)
    pl_day  = equity - last_eq
    pl_pct  = pl_day / last_eq * 100 if last_eq > 0 else 0

    week_gain,    week_pct    = _calc_gain(_fetch_portfolio_history("1W", "1D"))
    month_gain,   month_pct   = _calc_gain(_fetch_portfolio_history("1M", "1D"))
    alltime_gain, alltime_pct = _calc_gain(_fetch_portfolio_history("all", "1D"))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{'='*36}",
        "PAPER PORTFOLIO",
        f"{'='*36}",
        f"Updated:  {now}",
        "",
        f"Equity:   ${equity:,.2f}",
        f"Cash:     ${cash:,.2f}",
        "",
        "── Performance ──────────────────────",
        _fmt_gain("Day    ", pl_day, pl_pct),
        _fmt_gain("1-Week ", week_gain, week_pct),
        _fmt_gain("1-Month", month_gain, month_pct),
        _fmt_gain("All-Time", alltime_gain, alltime_pct),
        f"{'='*36}",
        f"Positions: {len(positions)}",
    ]

    if not positions:
        lines.append("No open positions.")
    else:
        lines.append("")
        for p in positions:
            if not isinstance(p, dict):
                continue
            sym       = p.get("symbol", "")
            qty       = float(p.get("qty", 0))
            avg_cost  = float(p.get("avg_entry_price", 0) or 0)
            cur_price = float(p.get("current_price", 0) or 0)
            pl_pos    = float(p.get("unrealized_pl", 0) or 0)
            pl_pct_p  = float(p.get("unrealized_plpc", 0) or 0) * 100
            sign      = "+" if pl_pos >= 0 else ""
            lines.append(
                f"{sym:<6} {qty:.0f}sh | "
                f"${avg_cost:.2f}→${cur_price:.2f} | "
                f"P&L {sign}${pl_pos:.2f} ({sign}{pl_pct_p:.1f}%)"
            )

    return "\n".join(lines)


def format_trade_history() -> str:
    orders = get_orders(status="closed")
    filled = [
        o for o in orders
        if isinstance(o, dict) and o.get("status") == "filled"
    ]

    if not filled:
        return "No completed trades yet."

    lines = [f"{'='*36}", "RECENT TRADES", f"{'='*36}"]
    for o in filled[:20]:
        sym   = o.get("symbol", "")
        side  = o.get("side", "").upper()
        qty   = o.get("filled_qty", "?")
        price = float(o.get("filled_avg_price") or 0)
        date  = (o.get("filled_at", "") or "")[:10]
        icon  = "🟢" if side == "BUY" else "🔴"
        lines.append(f"{icon} {date} {side:<5} {sym:<6} {qty}sh @ ${price:.2f}")

    return "\n".join(lines)
