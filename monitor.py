"""
monitor.py - Position Monitor

Runs 3x/day at 10:15AM, 12:45PM, 2:45PM ET (15 min after each scan).

For each open tracked position it:
  1. Checks if the position still exists in Alpaca
     → If gone: marks closed in positions.json, clears seen_key
       so the bot can re-enter on a fresh setup

  2. Checks if TP1 has been hit (current price >= tp1)
     → Cancels the GTC TP1 limit order
     → Market sells 1/3 shares at current price
     → Cancels old stop order
     → Places new stop at break-even (entry price)
     → Updates positions.json

  3. Sends a Telegram summary only when something actually happens
     (no noise on quiet checks)
"""

import json
import os
import pathlib
import requests
import traceback
from datetime import datetime

from positions import get_open_positions, mark_tp1_hit, mark_closed, POSITIONS_FILE

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
PAPER_URL     = "https://paper-api.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

SEEN_FILE = pathlib.Path("seen_setups.json")


# ── Alpaca helpers ────────────────────────────────────────────────────────────

def _get_alpaca_position(ticker: str) -> dict | None:
    try:
        r = requests.get(f"{PAPER_URL}/positions/{ticker}", headers=HEADERS, timeout=15)
        if r.status_code == 404:
            return None
        data = r.json()
        return data if isinstance(data, dict) and "symbol" in data else None
    except Exception:
        return None


def _get_current_price(ticker: str) -> float | None:
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/trades/latest",
            headers=HEADERS,
            timeout=15,
        )
        if r.ok:
            price = float(r.json().get("trade", {}).get("p", 0) or 0)
            if price > 0:
                return price
    except Exception:
        pass
    return None


def _cancel_order(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        r = requests.delete(f"{PAPER_URL}/orders/{order_id}", headers=HEADERS, timeout=15)
        return r.status_code in [200, 204]
    except Exception:
        return False


def _market_sell(ticker: str, shares: int) -> str:
    """Place a market sell. Returns order ID or empty string."""
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares),
                "side":          "sell",
                "type":          "market",
                "time_in_force": "day",
            },
            timeout=15,
        )
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            return order["id"]
        print(f"[monitor] Market sell failed for {ticker}: {order}")
        return ""
    except Exception:
        print(f"[monitor] Market sell exception for {ticker}: {traceback.format_exc()[-200:]}")
        return ""


def _place_stop_gtc(ticker: str, shares: int, stop_price: float) -> str:
    """Place a GTC stop order. Returns order ID or empty string."""
    try:
        r = requests.post(
            f"{PAPER_URL}/orders",
            headers=HEADERS,
            json={
                "symbol":        ticker,
                "qty":           str(shares),
                "side":          "sell",
                "type":          "stop",
                "stop_price":    str(round(stop_price, 2)),
                "time_in_force": "gtc",
            },
            timeout=15,
        )
        order = r.json()
        if isinstance(order, dict) and "id" in order:
            return order["id"]
        print(f"[monitor] Stop order failed for {ticker}: {order}")
        return ""
    except Exception:
        return ""


def _clear_seen_key(seen_key: str) -> None:
    """Remove a seen_key from seen_setups.json so re-entry is allowed."""
    if not seen_key or not SEEN_FILE.exists():
        return
    try:
        seen = json.loads(SEEN_FILE.read_text())
        if seen_key in seen:
            del seen[seen_key]
            SEEN_FILE.write_text(json.dumps(seen, indent=2))
            print(f"[monitor] Cleared seen_key: {seen_key}")
    except Exception:
        print(f"[monitor] Failed to clear seen_key {seen_key}: {traceback.format_exc()[-150:]}")


def _update_position_stop(ticker: str, new_stop_id: str, new_stop_price: float) -> None:
    """Update stop_order_id and stop price in positions.json after BE move."""
    try:
        data = json.loads(POSITIONS_FILE.read_text())
        if ticker in data:
            data[ticker]["stop_order_id"] = new_stop_id
            data[ticker]["stop"]          = new_stop_price
            POSITIONS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        print(f"[monitor] Failed to update stop for {ticker}: {traceback.format_exc()[-150:]}")


# ── Core monitor logic ────────────────────────────────────────────────────────

def run_monitor(bot=None, chat_id=None) -> None:
    """
    Main monitor function. Called 3x/day by the scheduler in bot.py.
    Checks every open position for closes and TP1 hits.
    """
    positions = get_open_positions()

    if not positions:
        print("[monitor] No open positions to monitor.")
        return

    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    actions = []
    errors  = []

    print(f"[monitor] Checking {len(positions)} open position(s) at {now}")

    for ticker, pos in positions.items():
        try:
            entry         = float(pos.get("entry", 0))
            tp1           = float(pos.get("tp1", 0))
            tp1_hit       = pos.get("tp1_hit", False)
            shares_at_tp1 = int(pos.get("shares_at_tp1", 1))
            shares_remain = int(pos.get("shares_remaining", pos.get("shares", 1)))
            tp1_order_id  = pos.get("tp1_order_id", "")
            stop_order_id = pos.get("stop_order_id", "")
            seen_key      = pos.get("seen_key", "")

            # ── 1. Check if position still exists in Alpaca ───────────────────
            alpaca_pos = _get_alpaca_position(ticker)

            if alpaca_pos is None:
                print(f"[monitor] {ticker} no longer in Alpaca — marking closed")
                mark_closed(ticker, "CLOSED")
                _clear_seen_key(seen_key)
                actions.append(
                    f"✅ {ticker} — Position closed\n"
                    f"   Exited via stop, TP2, or manual\n"
                    f"   Ready to re-enter on fresh setup"
                )
                continue

            # ── 2. Get current price ──────────────────────────────────────────
            current_price = float(alpaca_pos.get("current_price", 0) or 0)
            if current_price <= 0:
                current_price = _get_current_price(ticker)
            if not current_price:
                print(f"[monitor] Could not get price for {ticker}, skipping")
                continue

            pl_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0

            print(
                f"[monitor] {ticker}: ${current_price:.2f} "
                f"| entry ${entry:.2f} | tp1 ${tp1:.2f} "
                f"| tp1_hit={tp1_hit} | P&L {pl_pct:+.1f}%"
            )

            # ── 3. TP1 check (only if not already hit) ────────────────────────
            if not tp1_hit and tp1 > 0 and current_price >= tp1:
                print(f"[monitor] {ticker} TP1 HIT @ ${current_price:.2f}")

                # Cancel GTC TP1 limit (safety net no longer needed)
                if tp1_order_id:
                    _cancel_order(tp1_order_id)
                    print(f"[monitor] Cancelled TP1 limit {tp1_order_id}")

                # Market sell 1/3 shares
                _market_sell(ticker, shares_at_tp1)

                # Cancel old stop, place new one at break-even
                if stop_order_id:
                    _cancel_order(stop_order_id)
                    print(f"[monitor] Cancelled old stop {stop_order_id}")

                new_stop_id = _place_stop_gtc(ticker, shares_remain, entry)
                print(f"[monitor] Break-even stop @ ${entry:.2f}: {new_stop_id}")

                # Update positions.json
                mark_tp1_hit(ticker)
                _update_position_stop(ticker, new_stop_id, entry)

                actions.append(
                    f"🎯 {ticker} — TP1 HIT @ ${current_price:.2f} ({pl_pct:+.1f}%)\n"
                    f"   Sold {shares_at_tp1} sh @ market\n"
                    f"   Stop → break-even @ ${entry:.2f}\n"
                    f"   {shares_remain} sh riding to TP2"
                )

            else:
                status = "TP1 ✅ riding to TP2" if tp1_hit else f"watching TP1 @ ${tp1:.2f}"
                print(f"[monitor] {ticker} OK — {status} | P&L {pl_pct:+.1f}%")

        except Exception:
            err = traceback.format_exc()[-300:]
            print(f"[monitor] Error processing {ticker}: {err}")
            errors.append(f"⚠️ {ticker} error:\n{err}")

    # ── Send Telegram summary only if something happened ─────────────────────
    if (actions or errors) and bot and chat_id:
        try:
            lines = [f"📊 Monitor — {now}", ""]
            lines += actions
            if errors:
                lines += [""] + errors
            msg = "\n".join(lines)

            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(bot.send_message(chat_id=chat_id, text=msg))
            loop.close()
        except Exception:
            print(f"[monitor] Telegram send error: {traceback.format_exc()[-200:]}")

    print(f"[monitor] Done. {len(actions)} action(s), {len(errors)} error(s).")
