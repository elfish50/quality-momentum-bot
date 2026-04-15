"""
scanner.py — Universe Scan + Alert Formatting

Runs analyze_ticker() across the full universe, formats alerts,
and auto-executes BUY and SHORT signals via trader.py.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import traceback
from datetime import datetime

from strategy import (
    analyze_ticker,
    get_universe,
    load_seen,
    save_seen,
)


# ── Alert Formatting ──────────────────────────────────────────────────────────

def format_alert(sig: dict) -> str:
    direction = sig.get("direction", "LONG")
    signal    = sig.get("signal", "WATCH")
    trend     = sig.get("trend", "neutral")

    if signal == "BUY":
        header = "🟢 BUY — AUTO-EXECUTING"
    elif signal == "SHORT":
        header = "🔴 SHORT — AUTO-EXECUTING"
    else:
        header = "👀 WATCH — Waiting for volume"

    dir_arrow = "▲" if direction == "LONG" else "▼"
    trend_map = {"up": "📈 Up", "down": "📉 Down", "neutral": "➡️ Neutral"}
    trend_str = trend_map.get(trend, trend)

    tp_dir = "+" if direction == "LONG" else "-"

    lines = [
        f"{header}",
        f"",
        f"{'─'*30}",
        f"📊 {sig['ticker']} — {sig['setup']}",
        f"{'─'*30}",
        f"Direction: {dir_arrow} {direction}  |  Trend: {trend_str}",
        f"Score:    {sig['signal_score']:.0f}/100  |  Quality: {sig['quality_score']:.0f}",
        f"",
        f"Price:    ${sig['price']:.2f}",
        f"RSI:      {sig['rsi']:.1f}  |  ATR: ${sig['atr']:.2f}",
        f"Volume:   {sig['vol_ratio']:.1f}x avg {'✅' if sig['vol_confirmed'] else '⏳'}",
        f"",
        f"Stop:     ${sig['stop']:.2f}  (risk ${sig['risk_dollars']:.0f})",
        f"TP1:      ${sig['tp1']:.2f}  ({tp_dir}{abs(sig['tp1_pct']):.1f}%)  R:R {sig['rr_tp1']:.1f}x",
        f"TP2:      ${sig['tp2']:.2f}  ({tp_dir}{abs(sig['tp2_pct']):.1f}%)  R:R {sig['rr_tp2']:.1f}x",
        f"TP3:      ${sig['tp3']:.2f}  ({tp_dir}{abs(sig['tp3_pct']):.1f}%)  stretch",
        f"",
        f"Sizing:   {sig['shares']} shares  (${sig['position_val']:.0f} / {sig['pct_account']:.0f}% acct)",
        f"Hold:     {sig['hold_time']}",
    ]

    if not sig.get("fund_missing"):
        lines += [
            f"",
            f"--- Fundamentals ---",
            f"ROE:      {sig['roe']:.1f}%  |  Margin: {sig['gross_margin']:.1f}%",
            f"EPS Grw:  {sig['eps_growth']:.1f}%  |  D/E: {sig['debt_equity']:.1f}x",
        ]
        if sig.get("quality_notes"):
            lines.append(f"Warnings: {', '.join(sig['quality_notes'])}")
    else:
        lines.append(f"")
        lines.append(f"⚠ Fundamentals unavailable")

    if sig.get("sector"):
        lines.append(f"Sector:   {sig['sector']}")

    return "\n".join(lines)


# ── Universe Scan ─────────────────────────────────────────────────────────────

def run_scan(tickers: list = None) -> list:
    """
    Synchronous scan over tickers list (or full universe if None).
    Returns list of signal dicts sorted by signal_score descending.
    """
    if tickers is None:
        tickers = get_universe()

    print(f"[scanner] Starting scan — {len(tickers)} tickers")
    seen    = load_seen()
    results = []

    for ticker in tickers:
        try:
            result = analyze_ticker(ticker, seen)
            if result:
                results.append(result)
                print(
                    f"[scanner] ✅ {ticker} — {result['signal']} {result['setup']} "
                    f"score:{result['signal_score']:.0f}"
                )
        except Exception:
            print(f"[scanner] ERROR {ticker}: {traceback.format_exc()[-200:]}")

    save_seen(seen)
    results.sort(key=lambda x: x["signal_score"], reverse=True)
    print(f"[scanner] Done — {len(results)} signal(s) found")
    return results


async def run_universe_scan(bot, chat_id: str, tickers: list = None):
    """
    Async wrapper called by the scheduler and /scan command.
    Runs the blocking scan in an executor, then sends alerts and executes trades.
    """
    loop = asyncio.get_event_loop()

    try:
        alerts = await loop.run_in_executor(
            None,
            lambda: run_scan(tickers)
        )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Scan error:\n{traceback.format_exc()[-400:]}"
        )
        return

    if not alerts:
        ts = datetime.now().strftime("%H:%M")
        await bot.send_message(chat_id=chat_id, text=f"[{ts}] Scan complete — no setups found.")
        return

    await bot.send_message(
        chat_id=chat_id,
        text=f"📡 Scan complete — {len(alerts)} setup(s) found:"
    )

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)

            if sig["signal"] in ("BUY", "SHORT"):
                try:
                    from trader import execute_signal, format_execution_result
                    from positions import add_position

                    result = await loop.run_in_executor(
                        None, lambda s=sig: execute_signal(s)
                    )
                    await bot.send_message(
                        chat_id=chat_id,
                        text=format_execution_result(result, sig)
                    )

                    if result.get("success"):
                        add_position(sig, result)

                except Exception:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"Trade execution error for {sig['ticker']}:\n"
                             f"{traceback.format_exc()[-300:]}"
                    )

        except Exception as e:
            print(f"[scanner] Failed to send {sig['ticker']}: {e}")
