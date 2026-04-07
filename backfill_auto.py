"""
backfill_auto.py - One-Time Automatic Position Backfill for Railway

Run this ONCE by temporarily setting Railway's Start Command to:
    python backfill_auto.py

It will:
  1. Pull all current Alpaca positions
  2. Calculate stop/TP levels from entry price (7% stop default)
  3. Skip tickers already tracked in open_positions.json
  4. Skip tickers that already have a stop order in Alpaca
  5. Place GTC stop + TP1 limit + TP2 limit orders
  6. Write open_positions.json
  7. Print a full summary and EXIT

After confirming it worked:
  1. Change Start Command back to: python bot.py
  2. Delete this file from the repo

Fibonacci levels:
  Stop:  entry * 0.93  (7% below entry)
  TP1:   entry * 1.272
  TP2:   entry * 1.618
  TP3:   entry * 2.618
"""

import json
import os
import pathlib
import requests
import traceback
from datetime import datetime

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
PAPER_URL     = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

POSITIONS_FILE   = pathlib.Path("open_positions.json")
DEFAULT_STOP_PCT = 0.07


def get_alpaca_positions():
    r    = requests.get(f"{PAPER_URL}/positions", headers=HEADERS, timeout=15)
    data = r.json()
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def get_open_orders():
    r    = requests.get(
        f"{PAPER_URL}/orders",
        headers=HEADERS,
        params={"status": "open", "limit": 100},
        timeout=15,
    )
    data = r.json()
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def place_order(payload: dict, label: str) -> str:
    try:
        r     = requests.post(f"{PAPER_URL}/orders", headers=HEADERS, json=payload, timeout=15)
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            print(f"  ✅ {label}: {order['id']}")
            return order["id"]
        msg = order.get("message", str(order)[:200]) if isinstance(order, dict) else str(order)[:200]
        print(f"  ❌ {label} failed: {msg}")
        return ""
    except Exception:
        print(f"  ❌ {label} exception: {traceback.format_exc()[-150:]}")
        return ""


def build_entry(ticker, qty, entry, stop, tp1_id, stop_id, tp2_id, tp1, tp2, tp3, shares_at_tp1):
    return {
        "ticker":           ticker,
        "seen_key":         f"{ticker}::backfill",
        "entry":            entry,
        "shares":           qty,
        "shares_at_tp1":    shares_at_tp1,
        "shares_remaining": qty,
        "stop":             stop,
        "tp1":              tp1,
        "tp2":              tp2,
        "tp3":              tp3,
        "tp1_hit":          False,
        "tp1_order_id":     tp1_id,
        "tp2_order_id":     tp2_id,
        "stop_order_id":    stop_id,
        "bracket_id":       "",
        "setup":            "backfill",
        "signal_score":     0,
        "opened_at":        datetime.now().isoformat(),
        "closed_at":        None,
        "close_reason":     None,
    }


def main():
    print("=" * 55)
    print("BACKFILL AUTO — placing stops + TP orders")
    print("=" * 55)

    if not ALPACA_KEY or not ALPACA_SECRET:
        print("ERROR: ALPACA_KEY or ALPACA_SECRET not set.")
        return

    # Load existing positions.json
    existing = {}
    if POSITIONS_FILE.exists():
        try:
            existing = json.loads(POSITIONS_FILE.read_text())
            open_count = len([v for v in existing.values() if not v.get("closed_at")])
            print(f"Existing open_positions.json: {open_count} open entries (will skip these)")
        except Exception:
            pass

    positions = get_alpaca_positions()
    if not positions:
        print("No open positions in Alpaca. Exiting.")
        return
    print(f"Found {len(positions)} position(s) in Alpaca.\n")

    # Map existing stop orders per ticker
    open_orders = get_open_orders()
    existing_stops = {
        o["symbol"]: o for o in open_orders
        if o.get("side") == "sell" and o.get("type") == "stop"
    }

    new_entries   = dict(existing)
    success_count = 0
    skip_count    = 0
    fail_count    = 0

    for p in positions:
        ticker    = p.get("symbol", "")
        qty       = int(float(p.get("qty", 0)))
        entry     = float(p.get("avg_entry_price", 0) or 0)
        cur_price = float(p.get("current_price", 0) or 0)
        pl_pct    = float(p.get("unrealized_plpc", 0) or 0) * 100

        if not ticker or qty <= 0 or entry <= 0:
            continue

        print(f"{'─'*55}")
        print(f"{ticker} | {qty} sh | entry ${entry:.2f} | now ${cur_price:.2f} | {pl_pct:+.1f}%")

        # Skip already tracked open positions
        if ticker in existing and not existing[ticker].get("closed_at"):
            print(f"  ⏭  Already tracked — skipping")
            skip_count += 1
            continue

        # Skip if stop order already placed in Alpaca
        if ticker in existing_stops:
            print(f"  ⏭  Stop order already in Alpaca — recording without placing new orders")
            existing_stop = existing_stops[ticker]
            stop_price    = float(existing_stop.get("stop_price") or entry * (1 - DEFAULT_STOP_PCT))
            tp1 = round(entry * 1.272, 2)
            tp2 = round(entry * 1.618, 2)
            tp3 = round(entry * 2.618, 2)
            shares_at_tp1 = max(1, qty // 3)
            new_entries[ticker] = build_entry(
                ticker, qty, entry, stop_price,
                "", existing_stop["id"], "",
                tp1, tp2, tp3, shares_at_tp1
            )
            skip_count += 1
            continue

        # Calculate levels
        stop          = round(entry * (1 - DEFAULT_STOP_PCT), 2)
        tp1           = round(entry * 1.272, 2)
        tp2           = round(entry * 1.618, 2)
        tp3           = round(entry * 2.618, 2)
        shares_at_tp1 = max(1, qty // 3)
        shares_remain = qty - shares_at_tp1

        # Auto-adjust stop if it would trigger immediately
        if cur_price > 0 and stop >= cur_price:
            old_stop = stop
            stop     = round(cur_price * 0.98, 2)
            print(f"  ⚠️  Stop ${old_stop:.2f} >= current ${cur_price:.2f} — adjusted to ${stop:.2f}")

        print(f"  Stop: ${stop:.2f} | TP1: ${tp1:.2f} | TP2: ${tp2:.2f}")
        print(f"  TP1 sell: {shares_at_tp1} sh | Stop+TP2: {shares_remain} sh")

        tp1_id  = place_order({
            "symbol": ticker, "qty": str(shares_at_tp1),
            "side": "sell", "type": "limit",
            "limit_price": str(tp1), "time_in_force": "gtc",
        }, f"TP1 limit ({shares_at_tp1} sh @ ${tp1:.2f})")

        stop_id = place_order({
            "symbol": ticker, "qty": str(shares_remain),
            "side": "sell", "type": "stop",
            "stop_price": str(stop), "time_in_force": "gtc",
        }, f"Stop ({shares_remain} sh @ ${stop:.2f})")

        tp2_id  = place_order({
            "symbol": ticker, "qty": str(shares_remain),
            "side": "sell", "type": "limit",
            "limit_price": str(tp2), "time_in_force": "gtc",
        }, f"TP2 limit ({shares_remain} sh @ ${tp2:.2f})")

        new_entries[ticker] = build_entry(
            ticker, qty, entry, stop,
            tp1_id, stop_id, tp2_id,
            tp1, tp2, tp3, shares_at_tp1
        )

        if stop_id:
            success_count += 1
        else:
            fail_count += 1
            print(f"  ⚠️  Stop order failed — {ticker} unprotected!")

    POSITIONS_FILE.write_text(json.dumps(new_entries, indent=2))

    print(f"\n{'='*55}")
    print(f"BACKFILL COMPLETE")
    print(f"  ✅ Orders placed: {success_count}")
    print(f"  ⏭  Skipped:      {skip_count}")
    print(f"  ❌ Failed:       {fail_count}")
    print(f"  📄 open_positions.json: {len(new_entries)} entries")
    print(f"\nNEXT STEPS:")
    print(f"  1. Check Alpaca → Orders → Open Orders to verify")
    print(f"  2. Change Railway Start Command back to: python bot.py")
    print(f"  3. Delete backfill_auto.py from your repo")
    print("=" * 55)


if __name__ == "__main__":
    main()
