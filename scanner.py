"""
scanner.py - Elliott Wave + Fibonacci Scanner
Berkshire Quality Screen + Wave 2 / Wave 4 / ABC setups
LONG ONLY | Daily bars
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
                print(f"[ALERT] {ticker} | {sig['setup']} | Score {sig['signal_score']} | R:R TP2 {sig['rr_tp2']:.1f}x")
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
    vol_note = "Volume confirmed" if sig.get("vol_confirmed") else "Low volume"
    d        = sig.get("setup_detail", {})

    lines = [
        f"{'='*38}",
        f"LONG {sig['ticker']} -- {sig['name']}",
        f"{'='*38}",
        f"Setup:    {sig['setup']}",
        f"Signal:   {sig['signal_score']:.0f}/100 | Quality: {sig['quality_score']:.0f}/100",
        f"Hold:     {sig['hold_time']}",
        f"Sector:   {sig['sector']}",
        f"",
        f"--- Elliott Wave Analysis ---",
    ]

    if sig["setup"] == "Wave 2 Pullback":
        lines += [
            f"Wave 1 origin: ${d.get('wave1_origin', 0):.2f}",
            f"Wave 1 top:    ${d.get('wave1_top', 0):.2f}",
            f"Wave 1 size:   ${d.get('wave1_size', 0):.2f}",
            f"Fib 38.2%:     ${d.get('fib_382', 0):.2f}",
            f"Fib 50.0%:     ${d.get('fib_500', 0):.2f}  <-- reversal zone",
            f"Fib 61.8%:     ${d.get('fib_618', 0):.2f}  <-- reversal zone",
        ]
    elif sig["setup"] == "Wave 4 Pullback":
        lines += [
            f"Wave 1 origin: ${d.get('wave1_origin', 0):.2f}",
            f"Wave 1 high:   ${d.get('wave1_high', 0):.2f}  <-- stop zone",
            f"Wave 3 high:   ${d.get('wave3_high', 0):.2f}",
            f"Fib 38.2%:     ${d.get('fib_382', 0):.2f}  <-- entry zone",
            f"Fib 50.0%:     ${d.get('fib_500', 0):.2f}",
        ]
    elif sig["setup"] == "ABC Correction":
        lines += [
            f"Wave A start:  ${d.get('wave_a_start', 0):.2f}",
            f"Wave A end:    ${d.get('wave_a_end', 0):.2f}",
            f"Wave C low:    ${d.get('wave_c_low', 0):.2f}  <-- correction end",
        ]

    lines += [
        f"",
        f"--- Price & Momentum ---",
        f"Current price: ${sig['price']:.2f}",
        f"RSI(14):       {sig['rsi']:.1f}",
        f"Volume:        {sig['vol_ratio']:.1f}x avg -- {vol_note}",
        f"",
        f"--- Trade Setup (LONG) ---",
        f"Entry:  ${sig['price']:.2f}",
        f"Stop:   ${sig['stop']:.2f}  (invalidation level)",
        f"",
        f"--- Fibonacci Targets ---",
        f"TP1 (1.272x): ${sig['tp1']:.2f}  (+{sig['tp1_pct']:.1f}%)  R:R {sig['rr_tp1']:.2f}x",
        f"TP2 (1.618x): ${sig['tp2']:.2f}  (+{sig['tp2_pct']:.1f}%)  R:R {sig['rr_tp2']:.2f}x",
        f"TP3 (2.618x): ${sig['tp3']:.2f}  (+{sig['tp3_pct']:.1f}%)  stretch target",
        f"",
        f"--- Quality Screen ---",
        f"ROE:          {sig['roe']:.1f}%",
        f"Gross Margin: {sig['gross_margin']:.1f}%",
        f"EPS Growth:   {sig['eps_growth']:+.1f}%",
        f"Debt/Equity:  {sig['debt_equity']:.2f}",
        f"P/E Ratio:    {sig['pe_ratio']:.1f}x",
    ]

    if sig.get("quality_notes"):
        lines.append(f"Warnings: {', '.join(sig['quality_notes'])}")

    lines += [
        f"",
        f"--- Position ($1k account, 10% risk) ---",
        f"Shares:   {sig['shares']}",
        f"Value:    ${sig['position_val']:,.0f} ({sig['pct_account']:.1f}% of $1k)",
        f"Max loss: ${sig['risk_dollars']:.0f}",
        f"{'='*38}",
    ]
    return "\n".join(lines)


def format_summary(alerts, elapsed, universe_size):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    w2   = [a for a in alerts if a["setup"] == "Wave 2 Pullback"]
    w4   = [a for a in alerts if a["setup"] == "Wave 4 Pullback"]
    ab   = [a for a in alerts if a["setup"] == "ABC Correction"]
    buys = [a for a in alerts if a["signal"] == "BUY"]
    watch = [a for a in alerts if a["signal"] == "WATCH"]

    msg = (
        f"Elliott Wave + Fib Scan -- {ts}\n"
        f"{'='*36}\n"
        f"Scanned:       {universe_size:,} tickers\n"
        f"Duration:      {elapsed:.0f}s\n"
        f"BUY signals:   {len(buys)}  (volume confirmed)\n"
        f"WATCH signals: {len(watch)}  (waiting for volume)\n"
        f"Wave 2 setups: {len(w2)}\n"
        f"Wave 4 setups: {len(w4)}\n"
        f"ABC setups:    {len(ab)}\n"
        f"Total:         {len(alerts)}\n"w2   = [a for a in alerts if a["setup"] == "Wave 2 Pullback"]
    w4   = [a for a in alerts if a["setup"] == "Wave 4 Pullback"]
    ab   = [a for a in alerts if a["setup"] == "ABC Correction"]
    buys = [a for a in alerts if a["signal"] == "BUY"]
    watch = [a for a in alerts if a["signal"] == "WATCH"]

    msg = (
        f"Elliott Wave + Fib Scan -- {ts}\n"
        f"{'='*36}\n"
        f"Scanned:       {universe_size:,} tickers\n"
        f"Duration:      {elapsed:.0f}s\n"
        f"BUY signals:   {len(buys)}  (volume confirmed)\n"
        f"WATCH signals: {len(watch)}  (waiting for volume)\n"
        f"Wave 2 setups: {len(w2)}\n"
        f"Wave 4 setups: {len(w4)}\n"
        f"ABC setups:    {len(ab)}\n"
        f"Total:         {len(alerts)}\n"
        f"{'='*36}\n"
        f"Strategy: Elliott Wave + Fibonacci\n"
        f"Direction: LONG ONLY\n"
        f"Timeframe: Daily bars\n"
        f"Quality: Berkshire screen\n"
    )

    if alerts:
        msg += f"\nTop setups:\n"
        for a in alerts[:5]:
            msg += f"  {a['ticker']} | {a['setup']} | Score {a['signal_score']:.0f} | R:R TP2 {a['rr_tp2']:.1f}x | +{a['tp2_pct']:.1f}%\n"
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
            f"Elliott Wave + Fibonacci Scan starting...\n"
            f"Scanning {universe_size} tickers\n"
            f"Looking for: Wave 2, Wave 4, ABC setups\n"
            f"LONG ONLY | Daily bars\n"
            f"Est. time: ~{universe_size // 60 + 1} min"
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
            text=(
                "No Elliott Wave setups found today.\n"
                "Market may not have clean wave structures right now.\n"
                "Try again tomorrow or use /check TICKER for specific stocks."
            )
        )
        return

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Failed to send {sig['ticker']}: {e}")
