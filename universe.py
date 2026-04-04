"""
trader.py - Alpaca Paper Trading Execution
Auto-executes BUY signals with two-stage exit orders.

Order structure per BUY signal:
  Stage 1 — TP1 partial exit (1/3 of shares):
    - Limit sell order at TP1 price (1.272x Fib extension)
    - monitor.py detects fill and executes this via market sell

  Stage 2 — Remaining shares (2/3) protected by bracket:
    - Stop loss at invalidation level (GTC)
    - Take profit limit at TP2 (1.618x Fib extension) (GTC)

  After TP1 hit (handled by monitor.py):
    - Stop is moved to break-even to lock in profit on remainder

Protective Put (auto-placed alongside every BUY):
  - Finds nearest put strike at or below stop price, expiry 21-60 days out
  - Contracts = floor(shares / 100)
  - If shares < 100, no put is placed
  - Put failure never blocks the stock trade

Positions are recorded in positions.py after every successful execution
so monitor.py can track TP1/TP2/stop events and clear seen_setups.json
on close (allowing re-entry on fresh setups).
"""

import os
import requests
import traceback
from datetime import datetime, timedelta, timezone

from positions import add_position

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")

PAPER_URL = "https://paper-api.alpaca.markets/v2"
DATA_URL  = "https://data.alpaca.markets/v1beta1"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

DATA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


# ── Protective Put ────────────────────────────────────────────────────────────

def get_protective_put(ticker: str, stop_price: float, entry_price: float) -> dict | None:
    try:
        today      = datetime.now().date()
        expiry_min = today + timedelta(days=21)
        expiry_max = today + timedelta(days=60)

        url = f"{DATA_URL}/options/snapshots/{ticker}"
        params = {
            "feed":                  "indicative",
            "limit":                 1000,
            "type":                  "put",
            "expiration_date_gte":   str(expiry_min),
            "expiration_date_lte":   str(expiry_max),
        }

        r = requests.get(url, headers=DATA_HEADERS, params=params, timeout=15)
        if r.status_code == 404:
            return None
        if not r.ok:
            return None

        snapshots = r.json().get("snapshots", {})
        if not snapshots:
            return None

        candidates = []
        for symbol, snap in snapshots.items():
            try:
                greeks  = snap.get("greeks", {}) or {}
                quote   = snap.get("latestQuote", {}) or {}
                details = snap.get("details", {}) or {}

                strike = float(details.get("strike_price", 0))
                expiry = details.get("expiration_date", "")
                bid    = float(quote.get("bp", 0) or 0)
                ask    = float(quote.get("ap", 0) or 0)
                iv     = float(greeks.get("impliedVolatility", 0) or 0)
                delta  = float(greeks.get("delta", 0) or 0)

                if strike > stop_price:
                    continue
                if bid <= 0:
                    continue
                if delta < -0.65 or delta > -0.03:
                    continue

                mid = round((bid + ask) / 2, 2)
                candidates.append({
                    "symbol":   symbol,
                    "strike":   strike,
                    "expiry":   expiry,
                    "bid":      round(bid, 2),
                    "ask":      round(ask, 2),
                    "mid":      mid,
                    "iv":       round(iv * 100, 1),
                    "delta":    round(delta, 3),
                    "cost":     round(mid * 100, 2),
                    "distance": abs(strike - stop_price),
                })
            except Exception:
                continue

        if not candidates:
            return None

        return min(candidates, key=lambda x: x["distance"])

    except Exception:
        print(f"get_protective_put error for {ticker}: {traceback.format_exc()[-300:]}")
        return None


def execute_put_order(put: dict, contracts: int) -> dict:
    if contracts <= 0:
        put["order_status"] = "skipped"
        return put
    try:
        order = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        put["symbol"],
                "qty":           str(contracts),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=15
        ).json()

        if isinstance(order, dict) and "id" in order:
            put["order_status"] = "placed"
            put["order_id"]     = order["id"]
        else:
            msg = order.get("message", str(order)[:200]) if isinstance(order, dict) else str(order)[:200]
            put["order_status"] = "failed"
            put["order_error"]  = msg
    except Exception:
        put["order_status"] = "failed"
        put["order_error"]  = traceback.format_exc()[-200:]

    return put


def format_put_block(put: dict | None, stop: float, ticker: str, contracts: int = 0) -> str:
    if put is None:
        return (
            f"\n🛡 Protective Put\n"
            f"{'─'*36}\n"
            f"No listed puts found for {ticker}\n"
            f"(Options not available — stock trade unaffected)"
        )
    if contracts == 0:
        return (
            f"\n🛡 Protective Put — SKIPPED\n"
            f"{'─'*36}\n"
            f"Contract: {put['symbol']}\n"
            f"Strike:   ${put['strike']:.2f}  |  Expiry: {put['expiry']}\n"
            f"Mid:      ${put['mid']:.2f}  |  IV: {put['iv']:.1f}%\n"
            f"Reason:   Position < 100 shares — no full lot to hedge"
        )

    order_status = put.get("order_status", "unknown")
    order_id     = put.get("order_id", "")
    order_error  = put.get("order_error", "")

    if order_status == "placed":
        return (
            f"\n🛡 Protective Put — ORDER PLACED ✅\n"
            f"{'─'*36}\n"
            f"Contract: {put['symbol']}\n"
            f"Strike:   ${put['strike']:.2f}  |  Expiry: {put['expiry']}\n"
            f"Bid/Ask:  ${put['bid']:.2f} / ${put['ask']:.2f}  |  Mid: ${put['mid']:.2f}\n"
            f"IV:       {put['iv']:.1f}%  |  Delta: {put['delta']:.3f}\n"
            f"Qty:      {contracts} contract(s)  ({contracts * 100} shares covered)\n"
            f"Est cost: ${put['mid'] * contracts * 100:.2f}\n"
            f"Order ID: {order_id[:16]}...\n"
            f"{'─'*36}\n"
            f"Hedge active: loss capped below ${put['strike']:.2f}"
        )
    else:
        return (
            f"\n🛡 Protective Put — ORDER FAILED ⚠️\n"
            f"{'─'*36}\n"
            f"Contract: {put['symbol']}\n"
            f"Strike:   ${put['strike']:.2f}  |  Expiry: {put['expiry']}\n"
            f"Error:    {order_error[:150]}\n"
            f"Action:   Place manually on Alpaca if desired"
        )


# ── Alpaca Account helpers ────────────────────────────────────────────────────

def get_account():
    try:
        r = requests.get(f"{PAPER_URL}/account", headers=HEADERS, timeout=15)
        data = r.json()
        return data if isinstance(data, dict) else {}
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
        r    = requests.get(
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


# ── Two-stage order placement ─────────────────────────────────────────────────

def _place_tp1_limit(ticker: str, shares_at_tp1: int, tp1: float) -> str:
    """
    Place a GTC limit sell order for 1/3 of shares at TP1.
    Returns order ID or empty string on failure.
    Note: monitor.py will detect TP1 price hit and execute a market sell
    instead of relying solely on this limit (handles gaps/slippage better).
    This limit acts as a safety net.
    """
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares_at_tp1),
                "side":          "sell",
                "type":          "limit",
                "limit_price":   str(round(tp1, 2)),
                "time_in_force": "gtc",
            },
            timeout=15
        )
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            print(f"[trader] TP1 limit order placed for {ticker}: {order['id']}")
            return order["id"]
        print(f"[trader] TP1 limit order failed for {ticker}: {order}")
        return ""
    except Exception:
        print(f"[trader] TP1 limit exception for {ticker}: {traceback.format_exc()[-200:]}")
        return ""


def _place_stop_gtc(ticker: str, shares_remaining: int, stop: float) -> str:
    """GTC stop loss on the remaining 2/3 shares. Returns order ID."""
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares_remaining),
                "side":          "sell",
                "type":          "stop",
                "stop_price":    str(round(stop, 2)),
                "time_in_force": "gtc",
            },
            timeout=15
        )
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            print(f"[trader] Stop order placed for {ticker}: {order['id']}")
            return order["id"]
        print(f"[trader] Stop order failed for {ticker}: {order}")
        return ""
    except Exception:
        return ""


def _place_tp2_limit(ticker: str, shares_remaining: int, tp2: float) -> str:
    """GTC take profit limit on the remaining 2/3 shares. Returns order ID."""
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares_remaining),
                "side":          "sell",
                "type":          "limit",
                "limit_price":   str(round(tp2, 2)),
                "time_in_force": "gtc",
            },
            timeout=15
        )
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            print(f"[trader] TP2 limit order placed for {ticker}: {order['id']}")
            return order["id"]
        print(f"[trader] TP2 limit order failed for {ticker}: {order}")
        return ""
    except Exception:
        return ""


# ── Main execution ────────────────────────────────────────────────────────────

def execute_signal(sig: dict) -> dict:
    """
    Places a two-stage exit order structure for a BUY signal:

    Entry: market buy (all shares)
    Exit stage 1: GTC limit sell at TP1 (1/3 shares) — safety net
    Exit stage 2: GTC stop loss + GTC limit sell at TP2 (2/3 shares)

    monitor.py also watches price vs TP1 every 5 minutes and executes
    a market sell at TP1 if hit (handles gaps better than limit only).
    After TP1, monitor moves the stop to break-even.

    Records position in positions.py so monitor.py can track it.
    """
    ticker = sig["ticker"]
    shares = sig["shares"]
    stop   = round(sig["stop"], 2)
    tp1    = round(sig["tp1"], 2)
    tp2    = round(sig["tp2"], 2)

    result = {
        "ticker":         ticker,
        "shares":         shares,
        "entry":          sig["price"],
        "stop":           stop,
        "tp1":            tp1,
        "tp2":            tp2,
        "tp1_order_id":   "",
        "tp2_order_id":   "",
        "stop_order_id":  "",
        "bracket_id":     "",
        "orders":         [],
        "success":        False,
        "error":          None,
        "protective_put": None,
        "put_contracts":  0,
    }

    # Already in position?
    existing = get_position(ticker)
    if existing and float(existing.get("qty", 0)) > 0:
        result["error"] = f"Already have position in {ticker}"
        return result

    # Buying power check
    bp   = get_buying_power()
    cost = shares * sig["price"]
    if cost > bp:
        shares = int(bp * 0.95 / sig["price"])
        if shares < 1:
            result["error"] = f"Insufficient buying power (${bp:,.0f})"
            return result
        result["shares"] = shares

    shares_at_tp1  = max(1, shares // 3)
    shares_remain  = shares - shares_at_tp1

    # ── 1. Market buy entry ───────────────────────────────────────────────────
    try:
        order = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=15
        ).json()

        if not isinstance(order, dict) or "id" not in order:
            result["error"] = (
                f"Entry order failed: "
                f"{order.get('message', str(order)[:200]) if isinstance(order, dict) else str(order)[:200]}"
            )
            return result

        result["bracket_id"] = order["id"]
        result["orders"].append({"type": "ENTRY", "id": order["id"]})
        result["success"] = True
        print(f"[trader] Entry placed for {ticker}: {shares} shares @ ~${sig['price']:.2f}")

    except Exception:
        result["error"] = traceback.format_exc()[-300:]
        return result

    # ── 2. TP1 limit sell (1/3) ───────────────────────────────────────────────
    tp1_id = _place_tp1_limit(ticker, shares_at_tp1, tp1)
    result["tp1_order_id"] = tp1_id
    if tp1_id:
        result["orders"].append({"type": "TP1_LIMIT", "id": tp1_id, "shares": shares_at_tp1})

    # ── 3. Stop loss GTC (2/3) ────────────────────────────────────────────────
    stop_id = _place_stop_gtc(ticker, shares_remain, stop)
    result["stop_order_id"] = stop_id
    if stop_id:
        result["orders"].append({"type": "STOP", "id": stop_id, "shares": shares_remain})

    # ── 4. TP2 limit sell GTC (2/3) ──────────────────────────────────────────
    tp2_id = _place_tp2_limit(ticker, shares_remain, tp2)
    result["tp2_order_id"] = tp2_id
    if tp2_id:
        result["orders"].append({"type": "TP2_LIMIT", "id": tp2_id, "shares": shares_remain})

    # ── 5. Record position in positions.py ────────────────────────────────────
    try:
        add_position(sig, result)
    except Exception:
        print(f"[trader] Failed to record position for {ticker}: {traceback.format_exc()[-200:]}")

    # ── 6. Protective put ─────────────────────────────────────────────────────
    try:
        put = get_protective_put(ticker, stop, sig["price"])
        if put is not None:
            contracts = shares // 100
            result["put_contracts"] = contracts
            if contracts >= 1:
                put = execute_put_order(put, contracts)
                result["orders"].append({
                    "type":   "PUT",
                    "symbol": put["symbol"],
                    "qty":    contracts,
                    "status": put.get("order_status", "unknown"),
                    "id":     put.get("order_id", ""),
                })
            else:
                put["order_status"] = "skipped"
        result["protective_put"] = put
    except Exception:
        print(f"[trader] Put error for {ticker}: {traceback.format_exc()[-200:]}")

    return result


def format_execution_result(result: dict, sig: dict) -> str:
    if not result["success"]:
        return (
            f"Trade FAILED for {result['ticker']}\n"
            f"Reason: {result['error']}"
        )

    shares        = result["shares"]
    shares_at_tp1 = max(1, shares // 3)
    shares_remain = shares - shares_at_tp1
    stop_pct      = round((result["stop"] - result["entry"]) / result["entry"] * 100, 1)
    tp1_pct       = sig.get("tp1_pct", 0)
    tp2_pct       = sig.get("tp2_pct", 0)

    lines = [
        f"{'='*36}",
        f"PAPER TRADE PLACED ✅",
        f"{'='*36}",
        f"Ticker:  {result['ticker']}",
        f"Setup:   {sig.get('setup', '')}",
        f"",
        f"ENTRY    {shares} shares @ ~${result['entry']:.2f}",
        f"",
        f"── Exit plan ─────────────────────────",
        f"TP1 (1/3 = {shares_at_tp1} sh)  @ ${result['tp1']:.2f}  (+{tp1_pct:.1f}%)",
        f"  → Stop moves to break-even after TP1",
        f"TP2 (2/3 = {shares_remain} sh)  @ ${result['tp2']:.2f}  (+{tp2_pct:.1f}%)",
        f"TP3 (stretch)        @ ${sig.get('tp3', 0):.2f}  (+{sig.get('tp3_pct', 0):.1f}%)",
        f"STOP     @ ${result['stop']:.2f}  ({stop_pct:+.1f}%)",
        f"",
        f"── Risk ──────────────────────────────",
        f"Max loss:  ${sig.get('risk_dollars', 100):.0f}",
        f"R:R TP2:   {sig.get('rr_tp2', 0):.2f}x",
        f"{'='*36}",
    ]

    put_block = format_put_block(
        result.get("protective_put"),
        result["stop"],
        result["ticker"],
        contracts=result.get("put_contracts", 0),
    )
    lines.append(put_block)

    return "\n".join(lines)


# ── Portfolio & history (unchanged from original) ─────────────────────────────

def _fetch_portfolio_history(period: str, timeframe: str) -> list[float]:
    try:
        url    = f"{PAPER_URL}/account/portfolio/history"
        params = {"period": period, "timeframe": timeframe, "extended_hours": False}
        r      = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if not r.ok:
            return []
        equity = r.json().get("equity", [])
        return [float(e) for e in equity if e and float(e) > 0]
    except Exception:
        return []


def _calc_gain(equity_list: list[float]) -> tuple[float, float] | tuple[None, None]:
    if len(equity_list) < 2:
        return None, None
    start = equity_list[0]
    end   = equity_list[-1]
    gain  = end - start
    pct   = (gain / start * 100) if start else 0.0
    return gain, pct


def _fmt_gain_line(label: str, gain: float | None, pct: float | None) -> str:
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

    week_equity    = _fetch_portfolio_history("1W", "1D")
    month_equity   = _fetch_portfolio_history("1M", "1D")
    alltime_equity = _fetch_portfolio_history("all", "1D")

    week_gain,    week_pct    = _calc_gain(week_equity)
    month_gain,   month_pct   = _calc_gain(month_equity)
    alltime_gain, alltime_pct = _calc_gain(alltime_equity)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{'='*36}",
        f"PAPER PORTFOLIO",
        f"{'='*36}",
        f"Updated:   {now}",
        f"",
        f"Equity:    ${equity:,.2f}",
        f"Cash:      ${cash:,.2f}",
        f"",
        f"── Performance ──────────────────────",
        _fmt_gain_line("Day   ", pl_day, pl_pct),
        _fmt_gain_line("1-Week", week_gain, week_pct),
        _fmt_gain_line("1-Month", month_gain, month_pct),
        _fmt_gain_line("All-Time", alltime_gain, alltime_pct),
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
            sym      = p.get("symbol", "")
            qty      = float(p.get("qty", 0))
            avg_cost = float(p.get("avg_entry_price", 0) or 0)
            cur_price= float(p.get("current_price", 0) or 0)
            pl_pos   = float(p.get("unrealized_pl", 0) or 0)
            pl_pct_p = float(p.get("unrealized_plpc", 0) or 0) * 100
            lines.append(
                f"{sym:<6} {qty:.0f} sh | "
                f"Avg ${avg_cost:.2f} | "
                f"Now ${cur_price:.2f} | "
                f"P&L ${pl_pos:+.2f} ({pl_pct_p:+.1f}%)"
            )

    return "\n".join(lines)


def format_trade_history() -> str:
    orders = get_orders(status="closed")
    filled = [o for o in orders if isinstance(o, dict) and o.get("status") == "filled"]

    if not filled:
        return "No completed trades yet."

    lines = [f"{'='*36}", "RECENT TRADES", f"{'='*36}"]
    for o in filled[:20]:
        sym   = o.get("symbol", "")
        side  = o.get("side", "").upper()
        qty   = o.get("filled_qty", "?")
        price = float(o.get("filled_avg_price", 0) or 0)
        date  = o.get("filled_at", "")[:10] if o.get("filled_at") else ""
        lines.append(f"{date} {side:<5} {sym:<6} {qty} sh @ ${price:.2f}")

    return "\n".join(lines)
