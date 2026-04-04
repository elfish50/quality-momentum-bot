"""
positions.py - Open Trade Tracker

Persists a JSON file (open_positions.json) that records every trade
the bot enters, including the full setup data (stop, TP1, TP2, TP3,
wave details, seen_key for deduplication reset).

Why this exists:
  - Alpaca tracks the position (shares, P&L) but not our strategy levels
  - monitor.py needs stop/TP1/TP2 prices to know when to act
  - When a position closes, the seen_key is cleared so the bot can
    re-enter if a fresh setup forms on the same ticker

Schema per position:
  {
    "ticker":        "AAPL",
    "seen_key":      "AAPL::Wave 2 Pullback",   # cleared on close
    "entry":         182.50,
    "shares":        5,
    "stop":          178.20,
    "tp1":           191.30,
    "tp2":           197.80,
    "tp3":           212.40,
    "tp1_hit":       false,         # true after TP1 partial fill
    "shares_at_tp1": 1,             # shares sold at TP1 (floor(shares/3))
    "shares_remaining": 4,          # shares still open after TP1
    "tp1_order_id":  "abc...",      # Alpaca order ID for TP1 limit
    "tp2_order_id":  "def...",      # Alpaca order ID for TP2 limit
    "stop_order_id": "ghi...",      # Alpaca order ID for stop loss
    "bracket_id":    "xyz...",      # parent bracket order ID (if used)
    "setup":         "Wave 2 Pullback",
    "signal_score":  72.0,
    "opened_at":     "2025-04-01T14:23:00",
    "closed_at":     null,
    "close_reason":  null,          # "TP1", "TP2", "STOP", "MANUAL"
  }
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
    """
    Record a new open position after a successful trade execution.

    Args:
        sig:    the signal dict from analyze_ticker()
        result: the execution result dict from execute_signal()
    """
    data   = _load()
    ticker = sig["ticker"]

    shares        = result["shares"]
    shares_at_tp1 = max(1, shares // 3)   # sell 1/3 at TP1

    data[ticker] = {
        "ticker":           ticker,
        "seen_key":         f"{ticker}::{sig['setup']}",
        "entry":            result["entry"],
        "shares":           shares,
        "shares_at_tp1":    shares_at_tp1,
        "shares_remaining": shares,        # updated after TP1 fill
        "stop":             result["stop"],
        "tp1":              sig["tp1"],
        "tp2":              result["tp2"],
        "tp3":              sig["tp3"],
        "tp1_hit":          False,
        "tp1_order_id":     result.get("tp1_order_id", ""),
        "tp2_order_id":     result.get("tp2_order_id", ""),
        "stop_order_id":    result.get("stop_order_id", ""),
        "bracket_id":       result.get("bracket_id", ""),
        "setup":            sig["setup"],
        "signal_score":     sig.get("signal_score", 0),
        "opened_at":        datetime.now().isoformat(),
        "closed_at":        None,
        "close_reason":     None,
    }
    _save(data)
    print(f"[positions] Recorded open position: {ticker}")


def get_position(ticker: str) -> dict | None:
    return _load().get(ticker)


def get_all_positions() -> dict:
    return _load()


def get_open_positions() -> dict:
    return {k: v for k, v in _load().items() if v.get("closed_at") is None}


def mark_tp1_hit(ticker: str) -> None:
    """Called by monitor after TP1 partial fill confirmed."""
    data = _load()
    if ticker not in data:
        return
    pos                       = data[ticker]
    pos["tp1_hit"]            = True
    pos["shares_remaining"]   = pos["shares"] - pos["shares_at_tp1"]
    _save(data)
    print(f"[positions] TP1 hit recorded for {ticker} — {pos['shares_at_tp1']} shares sold")


def mark_closed(ticker: str, reason: str) -> None:
    """
    Mark a position as closed.
    reason: 'TP1', 'TP2', 'STOP', 'MANUAL'
    """
    data = _load()
    if ticker not in data:
        return
    data[ticker]["closed_at"]    = datetime.now().isoformat()
    data[ticker]["close_reason"] = reason
    _save(data)
    print(f"[positions] Position closed: {ticker} — reason: {reason}")


def remove_position(ticker: str) -> None:
    """Fully remove a position record (used after cleanup)."""
    data = _load()
    if ticker in data:
        del data[ticker]
        _save(data)


def format_open_positions() -> str:
    """Human-readable summary of all open positions for Telegram."""
    positions = get_open_positions()
    if not positions:
        return "No open tracked positions."

    lines = [f"{'='*38}", "OPEN POSITIONS", f"{'='*38}"]
    for ticker, pos in positions.items():
        tp1_tag = " [TP1 ✅]" if pos.get("tp1_hit") else ""
        lines += [
            f"{ticker} | {pos['setup']}{tp1_tag}",
            f"  Entry:  ${pos['entry']:.2f}  |  Shares: {pos['shares']}",
            f"  Stop:   ${pos['stop']:.2f}",
            f"  TP1:    ${pos['tp1']:.2f}",
            f"  TP2:    ${pos['tp2']:.2f}",
            f"  TP3:    ${pos['tp3']:.2f}",
            f"  Opened: {pos['opened_at'][:10]}",
            "",
        ]
    return "\n".join(lines)
