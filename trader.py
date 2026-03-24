"""
trader.py - Alpaca Paper Trading Execution
Auto-executes BUY signals with bracket orders (entry + stop + take profit).

Uses Alpaca paper trading endpoint:
  https://paper-api.alpaca.markets/v2

Bracket order per BUY signal:
  - Market buy entry
  - Stop loss at invalidation level
  - Take profit limit at TP2 (1.618x Fib extension)

Protective Put:
  - After each BUY, checks Alpaca options chain for a put near the stop price
  - Finds nearest strike <= stop, nearest expiry 21-60 days out
  - Appended to the trade confirmation message

Telegram commands:
  /portfolio - open positions + P&L
  /trades    - recent trade history
  /cancel    - cancel all open orders
"""

import os
import requests
import traceback
from datetime import datetime, timedelta

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


# ── Protective Put Lookup ─────────────────────────────────────────────────────

def get_protective_put(ticker: str, stop_price: float, entry_price: float) -> dict | None:
    """
    Looks up Alpaca options chain for a protective put near the stop price.

    Logic:
    - Expiry window: 21–60 days from today (enough time, not too expensive)
    - Strike: nearest PUT strike at or below stop_price (OTM put = cheaper hedge)
    - Returns: strike, expiry, bid, ask, mid, IV, delta, cost_per_contract

    Returns None if no options available (not all stocks have listed options).
    """
    try:
        today      = datetime.now().date()
        expiry_min = today + timedelta(days=21)
        expiry_max = today + timedelta(days=60)

        # Alpaca snapshots endpoint — returns all active option contracts for a ticker
        url = f"{DATA_URL}/options/snapshots/{ticker}"
        params = {
            "feed":        "indicative",
            "limit":       1000,
            "type":        "put",
            "expiration_date_gte": str(expiry_min),
            "expiration_date_lte": str(expiry_max),
        }

        r = requests.get(url, headers=DATA_HEADERS, params=params, timeout=15)

        if r.status_code == 404:
            return None  # no options listed for this ticker
        if not r.ok:
            print(f"Options API error {r.status_code} for {ticker}: {r.text[:200]}")
            return None

        data = r.json()
        snapshots = data.get("snapshots", {})

        if not snapshots:
            return None

        # Parse each contract: keep puts with strike <= stop_price and valid bid
        candidates = []
        for symbol, snap in snapshots.items():
            try:
                greeks  = snap.get("greeks", {}) or {}
                quote   = snap.get("latestQuote", {}) or {}
                details = snap.get("details", {}) or {}

                strike      = float(details.get("strike_price", 0))
                expiry_str  = details.get("expiration_date", "")
                bid         = float(quote.get("bp", 0) or 0)   # bp = bid price
                ask         = float(quote.get("ap", 0) or 0)   # ap = ask price
                iv          = float(greeks.get("impliedVolatility", 0) or 0)
                delta       = float(greeks.get("delta", 0) or 0)

                # Only puts at or below stop (protective = OTM or ATM)
                if strike > stop_price:
                    continue
                # Must have a valid market (bid > 0)
                if bid <= 0:
                    continue
                # Delta sanity check for puts (should be negative, between -0.05 and -0.6)
                if delta < -0.65 or delta > -0.03:
                    continue

                mid = round((bid + ask) / 2, 2)

                candidates.append({
                    "symbol":   symbol,
                    "strike":   strike,
                    "expiry":   expiry_str,
                    "bid":      round(bid, 2),
                    "ask":      round(ask, 2),
                    "mid":      mid,
                    "iv":       round(iv * 100, 1),   # as percentage
                    "delta":    round(delta, 3),
                    "cost":     round(mid * 100, 2),  # 1 contract = 100 shares
                    "distance": abs(strike - stop_price),  # closeness to stop
                })
            except Exception:
                continue

        if not candidates:
            return None

        # Pick the put closest to the stop price (best hedge precision)
        best = min(candidates, key=lambda x: x["distance"])
        return best

    except Exception:
        print(f"get_protective_put error for {ticker}: {traceback.format_exc()[-300:]}")
        return None


def format_put_block(put: dict | None, stop: float, ticker: str) -> str:
    """
    Formats the protective put info block for the Telegram message.
    """
    if put is None:
        return (
            f"\n🛡 Protective Put\n"
            f"{'─'*36}\n"
            f"No listed puts found for {ticker}\n"
            f"(Options may not be available for this stock)"
        )

    hedge_pct = round((put["strike"] / stop - 1) * 100, 1)  # how far strike is from stop
    sign      = f"{hedge_pct:+.1f}%" if hedge_pct != 0 else "at stop"

    return (
        f"\n🛡 Protective Put Available\n"
        f"{'─'*36}\n"
        f"Contract: {put['symbol']}\n"
        f"Strike:   ${put['strike']:.2f}  ({sign} vs stop)\n"
        f"Expiry:   {put['expiry']}\n"
        f"Bid/Ask:  ${put['bid']:.2f} / ${put['ask']:.2f}\n"
        f"Mid:      ${put['mid']:.2f}\n"
        f"IV:       {put['iv']:.1f}%\n"
        f"Delta:    {put['delta']:.3f}\n"
        f"Cost:     ${put['cost']:.2f} / contract (100 shares)\n"
        f"{'─'*36}\n"
        f"Hedge: Buy 1 put to cap loss below ${put['strike']:.2f}"
    )


# ── Alpaca Trading ────────────────────────────────────────────────────────────

def get_account():
    try:
        r = requests.get(f"{PAPER_URL}/account", headers=HEADERS, timeout=15)
        data = r.json()
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def get_buying_power():
    acc = get_account()
    return float(acc.get("buying_power", 0))


def get_positions():
    try:
        r = requests.get(f"{PAPER_URL}/positions", headers=HEADERS, timeout=15)
        data = r.json()
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict)]
        return []
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
        if isinstance(data, list):
            return [o for o in data if isinstance(o, dict)]
        return []
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

    Also looks up a protective put near the stop price.
    """
    ticker = sig["ticker"]
    shares = sig["shares"]
    stop   = round(sig["stop"], 2)
    tp2    = round(sig["tp2"], 2)

    result = {
        "ticker":          ticker,
        "shares":          shares,
        "entry":           sig["price"],
        "stop":            stop,
        "tp2":             tp2,
        "orders":          [],
        "success":         False,
        "error":           None,
        "protective_put":  None,   # ← will be filled after order
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

        if not isinstance(order, dict) or "id" not in order:
            result["error"] = (
                f"Order failed: "
                f"{order.get('message', str(order)[:200]) if isinstance(order, dict) else str(order)[:200]}"
            )
            return result

        result["orders"].append({"type": "BRACKET", "id": order["id"]})
        result["success"] = True

        # ── Protective put lookup (non-blocking — won't fail the trade) ──
        try:
            put = get_protective_put(ticker, stop, sig["price"])
            result["protective_put"] = put
        except Exception:
            result["protective_put"] = None  # silent fail — trade still confirmed

    except Exception:
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

    # Append protective put block
    put_block = format_put_block(result.get("protective_put"), result["stop"], result["ticker"])
    lines.append(put_block)

    return "\n".join(lines)


# ── Portfolio & History ───────────────────────────────────────────────────────

def format_portfolio() -> str:
    acc       = get_account()
    positions = get_positions()

    equity  = float(acc.get("equity", 0))
    cash    = float(acc.get("cash", 0))
    last_eq = float(acc.get("last_equity", equity) or equity)
    pl_day  = equity - last_eq
    pl_pct  = pl_day / last_eq * 100 if last_eq > 0 else 0

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
            if not isinstance(p, dict):
                continue
            sym       = p.get("symbol", "")
            qty       = float(p.get("qty", 0))
            avg_cost  = float(p.get("avg_entry_price", 0) or 0)
            cur_price = float(p.get("current_price", 0) or 0)
            pl_pos    = float(p.get("unrealized_pl", 0) or 0)
            pl_pct_p  = float(p.get("unrealized_plpc", 0) or 0) * 100
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

    lines = [
        f"{'='*36}",
        f"RECENT TRADES",
        f"{'='*36}",
    ]

    for o in filled[:20]:
        sym   = o.get("symbol", "")
        side  = o.get("side", "").upper()
        qty   = o.get("filled_qty", "?")
        price = float(o.get("filled_avg_price", 0) or 0)
        date  = o.get("filled_at", "")[:10] if o.get("filled_at") else ""
        lines.append(f"{date} {side:<5} {sym:<6} {qty} sh @ ${price:.2f}")

    return "\n".join(lines)
