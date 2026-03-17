"""
scanner.py — VWAP Mean Reversion Scanner
Alpaca (price) + Finnhub (fundamentals)
"""
import gc
import time
import asyncio
import traceback
from datetime import datetime
from universe import get_all_tickers
from strategy import analyze_ticker

BATCH_SIZE  = 10
BATCH_DELAY = 1
MAX_STOCKS  = 500


def run_scan(tickers=None):
    start   = time.time()
    tickers = tickers or get_all_tickers()
    tickers = tickers[:MAX_STOCKS]
    alerts  = []

    print(f"Scanning {len(tickers)} tickers...")

    for i, ticker in enumerate(tickers):
        try:
            sig = analyze_ticker(ticker)
            if sig:
                alerts.append(sig)
                print(f"[ALERT] {sig['signal']} {ticker} | Score {sig['signal_score']} | R:R {sig['rr']:.1f}x | ext {sig['vwap_ext_pct']:+.1f}%")
        except Exception:
            pass
        finally:
            gc.collect()

        if (i + 1) % BATCH_SIZE == 0:
            print(f"Progress: {i+1}/{len(tickers)} | Alerts: {len(alerts)}")
            time.sleep(BATCH_DELAY)

    alerts.sort(key=lambda x: x["signal_score"], reverse=True)
    elapsed = time.time() - start
    print(f"Scan done: {len(alerts)} alerts in {elapsed:.0f}s")
    return alerts, elapsed


def format_alert(sig):
    direction = sig["signal"]
    arrow     = "UP" if direction == "LONG" else "DOWN"
    vol_note  = "Volume confirmed" if sig.get("vol_confirmed") else "Low volume"

    lines = [
        f"{'='*36}",
        f"{direction} {sig['ticker']} — {sig['name']}",
        f"{'='*36}",
        f"Strategy: VWAP Mean Reversion",
        f"Signal:   {sig['signal_score']:.0f}/100",
        f"Hold:     {sig['hold_time']}",
        f"Sector:   {sig['sector']}",
        f"",
        f"--- VWAP Analysis ---",
        f"Price:    ${sig['price']:.2f}",
        f"VWAP:     ${sig['vwap']:.2f}",
        f"Extension:{sig['vwap_ext_pct']:+.2f}% from VWAP",
        f"EMA9:     ${sig['ema9']:.2f}",
        f"RSI:      {sig['rsi']:.1f}",
        f"Volume:   {sig['vol_ratio']:.1f}x avg — {vol_note}",
        f"",
        f"--- Trade Setup ({arrow}) ---",
        f"Entry:    ${sig['price']:.2f}",
        f"Stop:     ${sig['stop']:.2f}  ({sig['stop_pct']:+.1f}%)",
        f"Target:   ${sig['target']:.2f}  ({sig['tp_pct']:+.1f}% — VWAP)",
        f"R:R:      {sig['rr']:.2f}x",
        f"",
        f"--- Position ($1k account, 10% risk) ---",
        f"Shares:   {sig['shares']}",
        f"Value:    ${sig['position_val']:,.0f} ({sig['pct_account']:.1f}% of $1k)",
        f"Max loss: ${sig['risk_dollars']:.0f}",
        f"{'='*36}",
    ]
    return "\n".join(lines)


def format_summary(alerts, elapsed, universe_size):
    longs  = [a for a in alerts if a["signal"] == "LONG"]
    shorts = [a for a in alerts if a["signal"] == "SHORT"]
    ts     = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = (
        f"VWAP Reversion Scan — {ts}\n"
        f"{'='*36}\n"
        f"Scanned:  {universe_size:,} tickers\n"
        f"Duration: {elapsed:.0f}s\n"
        f"LONG:     {len(longs)}\n"
        f"SHORT:    {len(shorts)}\n"
        f"Total:    {len(alerts)}\n"
        f"{'='*36}\n"
        f"Strategy: VWAP Mean Reversion\n"
        f"Timeframe: 15-min bars\n"
        f"Min ext:  1.5% from VWAP\n"
        f"Exit:     VWAP touch or 1.5x ATR stop\n"
    )
    if longs:
        msg += f"\nTop LONG setups:\n"
        for a in longs[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | ext {a['vwap_ext_pct']:+.1f}% | R:R {a['rr']:.1f}x\n"
    if shorts:
        msg += f"\nTop SHORT setups:\n"
        for a in shorts[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | ext {a['vwap_ext_pct']:+.1f}% | R:R {a['rr']:.1f}x\n"
    return msg


async def run_universe_scan(bot, chat_id, tickers=None):
    from universe import load_universe

    if tickers:
        scan_list     = tickers
        universe_size = len(tickers)
    else:
        u             = load_universe()
        scan_list     = u.get("ALL", [])[:MAX_STOCKS]
        universe_size = len(scan_list)

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"VWAP Reversion Scan starting...\n"
            f"Scanning {universe_size} tickers\n"
            f"Timeframe: 15-min bars\n"
            f"Est. time: ~{universe_size // 100 + 1} min"
        )
    )

    try:
        loop            = asyncio.get_event_loop()
        alerts, elapsed = await loop.run_in_executor(
            None, lambda: run_scan(scan_list)
        )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Scan error:\n{traceback.format_exc()[-500:]}"
        )
        return

    summary = format_summary(alerts, elapsed, universe_size)
    await bot.send_message(chat_id=chat_id, text=summary)

    if not alerts:
        await bot.send_message(
            chat_id=chat_id,
            text="No VWAP reversion setups found.\nMarket may be trending strongly or low volatility today."
        )
        return

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Failed to send {sig['ticker']}: {e}")
