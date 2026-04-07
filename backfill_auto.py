"""
backfill_auto.py - One-Time Automatic Position Backfill for Railway

Run this ONCE by temporarily setting Railway's Start Command to:
    python backfill_auto.py

It will:
  1. Pull all current Alpaca positions
  2. Calculate stop/TP levels from entry price (7% stop default)
  3. Skip any ticker where a stop order already exists
  4. Place GTC stop + TP1 limit + TP2 limit orders
  5. Write open_positions.json
  6. Print a full summary and EXIT

After confirming it worked, change Start Command back to:
    python bot.py
...and delete this file from the repo.

Fibonacci levels used:
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

POSITIONS_FILE  = pathlib.Path("open_positions.json")
DEFAULT_STOP_PCT = 0.07  # 7% below entry


def get_alpaca_positions():
    r = requests.get(f"{PAPER_URL}/positions", headers=HEADERS, timeout=15)
    data = r.json()
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    print(f"[backfill] Could not fetch positions: {data}")
    return []


def get_open_orders():
    r = requests.get(
        f"{PAPER_URL}/orders",
        headers=HEADERS,
        params={"status": "open", "limit": 100},
        timeout=15,
    )
    data = r.json()
    return [o for o in data if isinstance(o, dict)] if isinstance(data, list) else []


def place_order(payload: dict, label: str) -> str:
    """Place an order and return its ID, or '' on failure."""
    try:
        r = requests.post(f"{PAPER_URL}/orders", headers=HEADERS, json=payload, timeout=15)
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


def main():
    print("=" * 55)
    print("BACKFILL AUTO — placing stops + TP orders for all positions")
    print("=" * 55)

    if not ALPACA_KEY or not ALPACA_SECRET:
        print("ERROR: ALPACA_KEY or ALPACA_SECRET not set in environment.")
        return

    # Load existing positions.json to avoid overwriting already-tracked entries
    existing = {}
    if POSITIONS_FILE.exists():
        try:
            existing = json.loads(POSITIONS_FILE.read_text())
            already_tracked = [t for t, v in existing.items() if v.get("closed_at") is None]
            print(f"\nExisting open_positions.json has {len(already_tracked)} open entries — will skip these.")
        except Exception:
            pass

    positions = get_alpaca_positions()
    if not positions:
        print("No open positions found in Alpaca. Exiting.")
        return

    print(f"\nFound {len(positions)} open position(s) in Alpaca.")

    # Build a map of existing open orders per ticker to avoid duplicates
    open_orders = get_open_orders()
    stop_orders_by_ticker = {}
    for o in open_orders:
        sym  = o.get("symbol", "")
        otype = o.get("type", "")
        side  = o.get("side", "")
        if side == "sell" and otype == "stop":
            stop_orders_by_ticker[sym] = o
            print(f"  [skip] {sym} already has a stop order — will not double-place")

    new_entries = dict(existing)
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

        print(f"\n{'─'*55}")
        print(f"  {ticker} | {qty} sh | entry ${entry:.2f} | now ${cur_price:.2f} | {pl_pct:+.1f}%")

        # Skip if already tracked and open
        if ticker in existing and existing[ticker].get("closed_at") is None:
            print(f"  ⏭  Already in positions.json — skipping")
            skip_count += 1
            continue

        # Skip if stop order already exists in Alpaca
        if ticker in stop_orders_by_ticker:
            print(f"  ⏭  Stop order already in Alpaca — skipping")
            skip_count += 1
            # Still record it in positions.json if missing
            if ticker not in new_entries:
                existing_stop = stop_orders_by_ticker[ticker]
                stop_price    = float(existing_stop.get("stop_price", entry * (1 - DEFAULT_STOP_PCT)) or 0)
                new_entries[ticker] = _build_entry(ticker, qty, entry, stop_price, "", existing_stop["id"], "")
            continue

        # Calculate levels
        stop          = round(entry * (1 - DEFAULT_STOP_PCT), 2)
        tp1           = round(entry * 1.272, 2)
        tp2           = round(entry * 1.618, 2)
        tp3           = round(entry * 2.618, 2)
        shares_at_tp1 = max(1, qty // 3)
        shares_remain = qty - shares_at_tp1

        print(f"  Stop: ${stop:.2f} | TP1: ${tp1:.2f} | TP2: ${tp2:.2f}")

        # Warn if stop is above current price (would trigger immediately)
        if cur_price > 0 and stop >= cur_price:
            print(f"  ⚠️  WARNING: stop ${stop:.2f} >= current price ${cur_price:.2f}")
            print(f"      Adjusting stop to 2% below current price to avoid immediate trigger")
            stop = round(cur_price * 0.98, 2)
            print(f"      New stop: ${stop:.2f}")

        # Place orders
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

        new_entries[ticker] = _build_entry(ticker, qty, entry, stop, tp1_id, stop_id, tp2_id, tp1, tp2, tp3, shares_at_tp1)

        if stop_id:
            success_count += 1
        else:
            fail_count += 1
            print(f"  ⚠️  Stop order failed for {ticker} — position unprotected!")

    # Save positions.json
    POSITIONS_FILE.write_text(json.dumps(new_entries, indent=2))

    print(f"\n{'='*55}")
    print(f"BACKFILL COMPLETE")
    print(f"  ✅ Processed: {success_count}")
    print(f"  ⏭  Skipped:   {skip_count}")
    print(f"  ❌ Failed:    {fail_count}")
    print(f"  📄 open_positions.json written ({len(new_entries)} entries)")
    print(f"\nNEXT STEPS:")
    print(f"  1. Check Alpaca paper account → Orders → Open Orders")
    print(f"  2. Confirm stop + TP orders exist for each position")
    print(f"  3. Change Railway Start Command back to: python bot.py")
    print(f"  4. Delete backfill_auto.py from your repo")
    print("=" * 55)


def _build_entry(
    ticker, qty, entry, stop,
    tp1_id="", stop_id="", tp2_id="",
    tp1=0.0, tp2=0.0, tp3=0.0,
    shares_at_tp1=None,
):
    if shares_at_tp1 is None:
        shares_at_tp1 = max(1, qty // 3)
    shares_remain = qty - shares_at_tp1
    if tp1 == 0.0:
        tp1 = round(entry * 1.272, 2)
    if tp2 == 0.0:
        tp2 = round(entry * 1.618, 2)
    if tp3 == 0.0:
        tp3 = round(entry * 2.618, 2)
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


if __name__ == "__main__":
    main()
