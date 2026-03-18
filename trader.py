"""
trader.py - Alpaca Paper Trading Execution
Auto-executes BUY signals with bracket orders (entry + stop + take profit).

Uses Alpaca paper trading endpoint:
  https://paper-api.alpaca.markets/v2

Bracket order per BUY signal:
  - Market buy entry
  - Stop loss at invalidation level
  - Take profit limit at TP2 (1.618x Fib extension)

Telegram commands:
  /portfolio - open positions + P&L
  /trades    - recent trade history
  /cancel    - cancel all open orders
"""

import os
import requests
import traceback

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")

PAPER_URL = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}


def get_account():
    try:
        r = requests.get(f"{PAPER_URL}/account", headers=HEADERS, timeout=15)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_buying_power():
    acc = get_account()
    return float(acc.get("buying_power", 0))


def get_positions():
    try:
        r = requests.get(f"{PAPER_URL}/positions", headers=HEADERS, timeout=15)
        return r.json()
    except Exception:
        return []


def get_position(ticker):
    try:
        r = requests.get(f"{PAPER_URL}/positions/{ticker}", headers=HEADERS, timeout=15)
        if r.status_code == 404:
            return None
        return r.json()
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
        return r.json()
    except Exception:
        return []


def cancel_all_orders():
    try:
        r = requests.delete(f"{PAPER_URL}/orders", headers=HEADERS, timeout=15)
        return r.status_code in [200, 207]
    except Exception:
        return False


def execute_signal(sig: dict) -> dict:
    """
    Places a bracket order for a BUY signal:
      - Market buy
      - Stop loss at invalidation level
      - Take profit limit at TP2

    Bracket orders keep stop + TP linked to the position automatically.
    """
    ticker = sig["ticker"]
    shares = sig["shares"]
    stop   = round(sig["stop"], 2)
    tp2    = round(sig["tp2"], 2)

    result = {
        "ticker":  ticker,
        "shares":  shares,
        "entry":   sig["price"],
        "stop":    stop,
        "tp2":     tp2,
        "orders":  [],
        "success": False,
        "error":   None,
    }

    # Check if already in position
    existing = get_position(ticker)
    if existing and float(existing.get("qty", 0)) > 0:
        result["error"] = f"Already have position in {ticker}"
        return result

    # Check buying power
    bp   = get_buying_power()
    cost = shares * sig["price"]
    if cost > bp:
        shares = int(bp * 0.95 / sig["price"])
        if shares < 1:
            result["error"] = f"Insufficient buying power (${bp:,.0f})"
            return result
        result["shares"] = shares

    try:
        # Bracket order: entry + stop loss + take profit in one request
        order = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares),
                "side":          "buy",
                "type":          "market",
                "time_in_force": "day",
                "order_class":   "bracket",
                "stop_loss":   {"stop_price":  str(stop)},
                "take_profit": {"limit_price": str(tp2)},
            },
            timeout=15
        ).json()

        if "id" not in order:
            result["error"] = f"Order failed: {order.get('message', 'unknown error')}"
            return result

        result["orders"].append({
            "type": "BRACKET",
            "id":   order["id"],
        })
        result["success"] = True

    except Exception as e:
        result["error"] = traceback.format_exc()[-300:]

    return result


def format_execution_result(result: dict, sig: dict) -> str:
    if not result["success"]:
        return (
            f"Trade FAILED for {result['ticker']}\n"
            f"Reason: {result['error']}"
        )

    stop_pct = round((result["stop"] - result["entry"]) / result["entry"] * 100, 1)
    tp2_pct  = sig.get("tp2_pct", 0)

    lines = [
        f"{'='*36}",
        f"PAPER TRADE PLACED",
        f"{'='*36}",
        f"Ticker:  {result['ticker']}",
        f"Setup:   {sig.get('setup', '')}",
        f"",
        f"BUY      {result['shares']} shares @ ~${result['entry']:.2f}",
        f"STOP     @ ${result['stop']:.2f}  ({stop_pct:+.1f}%)",
        f"TP2      @ ${result['tp2']:.2f}  (+{tp2_pct:.1f}%)",
        f"",
        f"Max loss:  ${sig.get('risk_dollars', 100):.0f}",
        f"R:R TP2:   {sig.get('rr_tp2', 0):.2f}x",
        f"Order type: Bracket (stop + TP linked)",
        f"{'='*36}",
    ]
    return "\n".join(lines)


def format_portfolio() -> str:
    acc       = get_account()
    positions = get_positions()

    equity   = float(acc.get("equity", 0))
    cash     = float(acc.get("cash", 0))
    last_eq  = float(acc.get("last_equity", equity))
    pl_day   = equity - last_eq
    pl_pct   = pl_day / last_eq * 100 if last_eq > 0 else 0

    lines = [
        f"{'='*36}",
        f"PAPER PORTFOLIO",
        f"{'='*36}",
        f"Equity:    ${equity:,.2f}",
        f"Cash:      ${cash:,.2f}",
        f"Day P&L:   ${pl_day:+,.2f} ({pl_pct:+.2f}%)",
        f"Positions: {len(positions)}",
        f"{'='*36}",
    ]

    if not positions:
        lines.append("No open positions.")
    else:
        lines.append("")
        for p in positions:
            sym       = p.get("symbol", "")
            qty       = float(p.get("qty", 0))
            avg_cost  = float(p.get("avg_entry_price", 0))
            cur_price = float(p.get("current_price", 0))
            pl_pos    = float(p.get("unrealized_pl", 0))
            pl_pct_p  = float(p.get("unrealized_plpc", 0)) * 100
            lines.append(
                f"{sym:<6} {qty:.0f} sh | "
                f"Avg ${avg_cost:.2f} | "
                f"Now ${cur_price:.2f} | "
                f"P&L ${pl_pos:+.2f} ({pl_pct_p:+.1f}%)"
            )

    return "\n".join(lines)


def format_trade_history() -> str:
    orders = get_orders(status="closed")
    filled = [o for o in orders if o.get("status") == "filled"]

    if not filled:
        return "No completed trades yet."

    lines = [
        f"{'='*36}",
        f"RECENT TRADES",
        f"{'='*36}",
    ]

    for o in filled[:20]:
        sym      = o.get("symbol", "")
        side     = o.get("side", "").upper()
        qty      = o.get("filled_qty", "?")
        price    = float(o.get("filled_avg_price", 0) or 0)
        date     = o.get("filled_at", "")[:10] if o.get("filled_at") else ""
        lines.append(f"{date} {side:<5} {sym:<6} {qty} sh @ ${price:.2f}")

    return "\n".join(lines)
