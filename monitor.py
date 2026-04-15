"""
monitor.py — Position Monitor

Runs 3x/day (same schedule as scans). For each open position:

  LONG positions:
    - TP1 hit (price >= tp1): sell 1/3 at market, cancel TP1 limit,
      cancel old stop, place new stop at break-even
    - Closed (no Alpaca position): mark closed, clear seen_setups.json

  SHORT positions:
    - TP1 hit (price <= tp1): buy-to-cover 1/3 at market, cancel TP1 limit,
      cancel old stop, place new stop-buy at break-even (entry price)
    - Closed (no Alpaca position): mark closed, clear seen_setups.json

Called from bot.py scheduler or run standalone: python monitor.py
"""

import os
import json
import pathlib
import traceback
import requests
from datetime import datetime, timezone

from positions import (
    get_open_positions,
    mark_tp1_hit,
    mark_closed,
    POSITIONS_FILE,
)
from strategy import load_seen, save_seen

ALPACA_KEY    = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
PAPER_URL     = "https://paper-api.alpaca.markets/v2"
DATA_URL      = "https://data.alpaca.markets/v2"

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}


# ── Alpaca Helpers ────────────────────────────────────────────────────────────

def _alpaca_position(ticker: str) -> dict | None:
    try:
        r = requests.get(f"{PAPER_URL}/positions/{ticker}", headers=HEADERS, timeout=10)
        if r.status_code == 404:
            return None
        return r.json() if r.ok else None
    except Exception:
        return None


def _current_price(ticker: str) -> float | None:
    for feed in ["iex", "sip"]:
        try:
            r = requests.get(
                f"{DATA_URL}/stocks/{ticker}/trades/latest",
                headers=HEADERS,
                params={"feed": feed},
                timeout=10
            )
            if r.ok:
                p = float(r.json().get("trade", {}).get("p", 0) or 0)
                if p > 0:
                    return p
        except Exception:
            continue
    return None


def _cancel_order(order_id: str) -> bool:
    if not order_id:
        return False
    try:
        r = requests.delete(f"{PAPER_URL}/orders/{order_id}", headers=HEADERS, timeout=10)
        return r.status_code in [200, 204]
    except Exception:
        return False


def _place_order(payload: dict, ticker: str, label: str) -> str:
    """Place an order, return order_id or empty string on failure."""
    try:
        r = requests.post(f"{PAPER_URL}/orders", headers=HEADERS, json=payload, timeout=10)
        if r.ok:
            oid = r.json().get("id", "")
            print(f"[monitor] {ticker} {label} placed: {oid[:8]}")
            return oid
        print(f"[monitor] {ticker} {label} failed ({r.status_code}): {r.text[:150]}")
        return ""
    except Exception as e:
        print(f"[monitor] {ticker} {label} exception: {e}")
        return ""


def _market_sell(ticker: str, shares: int) -> str:
    """Market sell (LONG TP1 partial exit)."""
    return _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "sell",
        "type":          "market",
        "time_in_force": "day",
    }, ticker, f"market sell {shares}sh")


def _market_buy_cover(ticker: str, shares: int) -> str:
    """Market buy-to-cover (SHORT TP1 partial exit)."""
    return _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "buy",
        "type":          "market",
        "time_in_force": "day",
    }, ticker, f"buy-to-cover {shares}sh")


def _place_stop_sell_gtc(ticker: str, shares: int, stop_price: float) -> str:
    """GTC stop-market sell (LONG break-even stop)."""
    return _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "sell",
        "type":          "stop",
        "time_in_force": "gtc",
        "stop_price":    str(round(stop_price, 2)),
    }, ticker, f"stop-sell @ ${stop_price:.2f}")


def _place_stop_buy_gtc(ticker: str, shares: int, stop_price: float) -> str:
    """GTC stop-market buy-to-cover (SHORT break-even stop)."""
    return _place_order({
        "symbol":        ticker,
        "qty":           str(shares),
        "side":          "buy",
        "type":          "stop",
        "time_in_force": "gtc",
        "stop_price":    str(round(stop_price, 2)),
    }, ticker, f"stop-buy @ ${stop_price:.2f}")


# ── Positions.json helpers ────────────────────────────────────────────────────

def _update_position(ticker: str, updates: dict) -> None:
    """Patch fields in open_positions.json for a given ticker."""
    try:
        data = json.loads(POSITIONS_FILE.read_text())
        if ticker in data:
            data[ticker].update(updates)
            POSITIONS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        print(f"[monitor] Failed to update position {ticker}: {traceback.format_exc()[-150:]}")


def _clear_seen_key(seen_key: str) -> None:
    """Remove ticker from seen_setups.json so it can be re-scanned."""
    seen_file = pathlib.Path("seen_setups.json")
    if not seen_key or not seen_file.exists():
        return
    try:
        seen = json.loads(seen_file.read_text())
        if seen_key in seen:
            del seen[seen_key]
            seen_file.write_text(json.dumps(seen, indent=2))
            print(f"[monitor] Cleared seen_key: {seen_key}")
    except Exception:
        print(f"[monitor] Failed to clear seen_key {seen_key}: {traceback.format_exc()[-150:]}")


# ── Core Logic ────────────────────────────────────────────────────────────────

def _process_long(ticker: str, pos: dict, price: float, actions: list, errors: list):
    entry         = float(pos.get("entry_price", 0))
    tp1           = float(pos.get("tp1", 0))
    tp2           = float(pos.get("tp2", 0))
    stop          = float(pos.get("stop", 0))
    shares        = int(pos.get("shares", 0))
    tp1_shares    = int(pos.get("tp1_shares", max(1, shares // 3)))
    shares_remain = shares - tp1_shares
    tp1_hit       = bool(pos.get("tp1_hit", False))
    tp1_order_id  = pos.get("tp1_order_id", "")
    stop_order_id = pos.get("stop_order_id", "")
    seen_key      = pos.get("seen_key", "")

    pl_pct = round((price - entry) / entry * 100, 2) if entry else 0
    print(f"[monitor] LONG {ticker} @ ${price:.2f} | entry ${entry:.2f} | P&L {pl_pct:+.1f}%")

    if not tp1_hit and tp1 > 0 and price >= tp1:
        print(f"[monitor] {ticker} TP1 HIT @ ${price:.2f}")

        _cancel_order(tp1_order_id)
        _market_sell(ticker, tp1_shares)

        # Move stop to break-even
        _cancel_order(stop_order_id)
        new_stop_id = _place_stop_sell_gtc(ticker, shares_remain, entry)

        mark_tp1_hit(ticker)
        _update_position(ticker, {"stop_order_id": new_stop_id, "stop": entry})

        actions.append(
            f"🎯 LONG {ticker} — TP1 HIT @ ${price:.2f} ({pl_pct:+.1f}%)\n"
            f"   Sold {tp1_shares} sh @ market\n"
            f"   Stop → break-even @ ${entry:.2f}\n"
            f"   {shares_remain} sh riding to TP2 @ ${tp2:.2f}"
        )
    else:
        status = f"TP1 ✅ riding to TP2 ${tp2:.2f}" if tp1_hit else f"watching TP1 @ ${tp1:.2f}"
        print(f"[monitor] LONG {ticker} OK — {status} | P&L {pl_pct:+.1f}%")


def _process_short(ticker: str, pos: dict, price: float, actions: list, errors: list):
    entry         = float(pos.get("entry_price", 0))
    tp1           = float(pos.get("tp1", 0))   # below entry for shorts
    tp2           = float(pos.get("tp2", 0))
    stop          = float(pos.get("stop", 0))  # above entry for shorts
    shares        = int(pos.get("shares", 0))
    tp1_shares    = int(pos.get("tp1_shares", max(1, shares // 3)))
    shares_remain = shares - tp1_shares
    tp1_hit       = bool(pos.get("tp1_hit", False))
    tp1_order_id  = pos.get("tp1_order_id", "")
    stop_order_id = pos.get("stop_order_id", "")
    seen_key      = pos.get("seen_key", "")

    # For shorts: profit when price falls below entry
    pl_pct = round((entry - price) / entry * 100, 2) if entry else 0
    print(f"[monitor] SHORT {ticker} @ ${price:.2f} | entry ${entry:.2f} | P&L {pl_pct:+.1f}%")

    if not tp1_hit and tp1 > 0 and price <= tp1:
        print(f"[monitor] {ticker} SHORT TP1 HIT @ ${price:.2f}")

        _cancel_order(tp1_order_id)
        _market_buy_cover(ticker, tp1_shares)

        # Move stop-buy to break-even (entry price — if price rises back, exit flat)
        _cancel_order(stop_order_id)
        new_stop_id = _place_stop_buy_gtc(ticker, shares_remain, entry)

        mark_tp1_hit(ticker)
        _update_position(ticker, {"stop_order_id": new_stop_id, "stop": entry})

        actions.append(
            f"🎯 SHORT {ticker} — TP1 HIT @ ${price:.2f} ({pl_pct:+.1f}%)\n"
            f"   Covered {tp1_shares} sh @ market\n"
            f"   Stop-buy → break-even @ ${entry:.2f}\n"
            f"   {shares_remain} sh riding to TP2 @ ${tp2:.2f}"
        )
    else:
        status = f"TP1 ✅ riding to TP2 ${tp2:.2f}" if tp1_hit else f"watching TP1 @ ${tp1:.2f}"
        print(f"[monitor] SHORT {ticker} OK — {status} | P&L {pl_pct:+.1f}%")


def run_monitor(bot=None, chat_id: str = None) -> None:
    positions = get_open_positions()
    if not positions:
        print("[monitor] No open positions.")
        return

    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    actions = []
    errors  = []

    print(f"[monitor] Checking {len(positions)} position(s) at {now}")

    for ticker, pos in list(positions.items()):
        try:
            direction = pos.get("direction", "LONG")

            # ── Check if position is still open in Alpaca ─────────────────
            alpaca_pos = _alpaca_position(ticker)
            if alpaca_pos is None:
                print(f"[monitor] {ticker} — no Alpaca position, marking closed")
                seen_key = pos.get("seen_key", "")
                entry    = float(pos.get("entry_price", 0))
                tp1_hit  = bool(pos.get("tp1_hit", False))

                # Estimate close reason
                price = _current_price(ticker)
                if price and entry:
                    pl_pct = round(
                        ((entry - price) / entry if direction == "SHORT" else (price - entry) / entry) * 100,
                        1
                    )
                    reason = "TP2 ✅" if pl_pct > 0 else "Stop ❌"
                else:
                    pl_pct = 0.0
                    reason = "Closed"

                mark_closed(ticker)
                _clear_seen_key(seen_key)

                actions.append(
                    f"{'🔴' if direction == 'SHORT' else '🟢'} {ticker} {direction} CLOSED — {reason}\n"
                    f"   P&L estimate: {pl_pct:+.1f}%"
                )
                continue

            # ── Get current price ─────────────────────────────────────────
            price = _current_price(ticker)
            if price is None or price <= 0:
                print(f"[monitor] {ticker} — could not get price, skipping")
                continue

            # ── Route to LONG or SHORT handler ────────────────────────────
            if direction == "SHORT":
                _process_short(ticker, pos, price, actions, errors)
            else:
                _process_long(ticker, pos, price, actions, errors)

        except Exception:
            err = traceback.format_exc()[-300:]
            print(f"[monitor] Error processing {ticker}: {err}")
            errors.append(f"⚠️ {ticker} error:\n{err}")

    # ── Send Telegram summary if anything happened ────────────────────────────
    if (actions or errors) and bot and chat_id:
        try:
            lines = [f"📊 Monitor — {now}", ""]
            lines += actions
            if errors:
                lines += [""] + errors

            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(bot.send_message(chat_id=chat_id, text="\n".join(lines)))
            loop.close()
        except Exception:
            print(f"[monitor] Telegram send error: {traceback.format_exc()[-200:]}")

    print(f"[monitor] Done — {len(actions)} action(s), {len(errors)} error(s).")


# ── Standalone ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[monitor] {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    run_monitor()
    print("[monitor] Done.")
