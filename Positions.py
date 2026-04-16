"""
positions.py — Open Trade Tracker

Persists open_positions.json. Records every trade (LONG or SHORT)
with all levels needed by monitor.py to manage exits.
"""

import json
import pathlib
from datetime import datetime

POSITIONS_FILE = pathlib.Path("open_positions.json")


def _load() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    POSITIONS_FILE.write_text(json.dumps(data, indent=2))


def add_position(sig: dict, result: dict) -> None:
    data   = _load()
    ticker = sig["ticker"]

    shares     = result.get("shares", sig.get("shares", 1))
    tp1_shares = result.get("tp1_shares", max(1, shares // 3))

    data[ticker] = {
        "ticker":        ticker,
        "direction":     result.get("direction", sig.get("direction", "LONG")),
        "seen_key":      f"{ticker}::{sig['setup']}",
        "entry_price":   result.get("entry_price", sig.get("price", 0)),
        "shares":        shares,
        "tp1_shares":    tp1_shares,
        "stop":          result.get("stop", sig.get("stop", 0)),
        "tp1":           result.get("tp1", sig.get("tp1", 0)),
        "tp2":           result.get("tp2", sig.get("tp2", 0)),
        "tp3":           result.get("tp3", sig.get("tp3", 0)),
        "tp1_hit":       False,
        "tp1_order_id":  result.get("tp1_order_id", ""),
        "stop_order_id": result.get("stop_order_id", ""),
        "setup":         sig.get("setup", ""),
        "signal_score":  sig.get("signal_score", 0),
        "opened_at":     datetime.now().isoformat(),
        "closed_at":     None,
        "close_reason":  None,
    }
    _save(data)
    print(f"[positions] Recorded {data[ticker]['direction']} position: {ticker}")


def get_position(ticker: str) -> dict | None:
    return _load().get(ticker)


def get_all_positions() -> dict:
    return _load()


def get_open_positions() -> dict:
    return {k: v for k, v in _load().items() if v.get("closed_at") is None}


def mark_tp1_hit(ticker: str) -> None:
    data = _load()
    if ticker not in data:
        return
    data[ticker]["tp1_hit"] = True
    _save(data)
    print(f"[positions] TP1 hit recorded: {ticker}")


def mark_closed(ticker: str, reason: str = "CLOSED") -> None:
    data = _load()
    if ticker not in data:
        return
    data[ticker]["closed_at"]    = datetime.now().isoformat()
    data[ticker]["close_reason"] = reason
    _save(data)
    print(f"[positions] Closed: {ticker} — {reason}")


def format_open_positions() -> str:
    positions = get_open_positions()
    if not positions:
        return "No open tracked positions.\n\nRun /portfolio to see Alpaca account."

    lines = [f"{'='*36}", "OPEN POSITIONS", f"{'='*36}"]
    for ticker, pos in positions.items():
        direction  = pos.get("direction", "LONG")
        dir_icon   = "🔴 SHORT" if direction == "SHORT" else "🟢 LONG"
        tp1_status = "✅ TP1 hit" if pos.get("tp1_hit") else "⏳ watching"
        entry      = float(pos.get("entry_price", 0))
        stop       = float(pos.get("stop", 0))
        tp1        = float(pos.get("tp1", 0))
        tp2        = float(pos.get("tp2", 0))
        shares     = int(pos.get("shares", 0))
        opened     = pos.get("opened_at", "")[:10]

        lines += [
            f"",
            f"{dir_icon} {ticker} — {pos.get('setup','')}",
            f"  Entry:  ${entry:.2f} × {shares} shares  [{opened}]",
            f"  Stop:   ${stop:.2f}",
            f"  TP1:    ${tp1:.2f}  {tp1_status}",
            f"  TP2:    ${tp2:.2f}",
            f"  Score:  {pos.get('signal_score', 0):.0f}",
        ]

    return "\n".join(lines)
