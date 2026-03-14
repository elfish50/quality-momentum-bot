"""
scanner.py — Quality Momentum Scanner
Uses Alpha Vantage for price + fundamental data.
Free tier: 25 calls/day — use /check for individual stocks,
/scan for full universe (uses calls budget).
"""
import gc
import time
import asyncio
import traceback
from datetime import datetime

from universe import get_all_tickers
from strategy import analyze_ticker

BATCH_SIZE  = 5
BATCH_DELAY = 15  # Alpha Vantage free = 25 calls/min max


def run_scan(tickers: list = None) -> tuple[list, float]:
    start   = time.time()
    tickers = tickers or get_all_tickers()
    alerts  = []

    print(f"Scanning {len(tickers)} tickers...")

    for i, ticker in enumerate(tickers):
        try:
            sig = analyze_ticker(ticker)
            if sig:
                alerts.append(sig)
                print(f"[ALERT] BUY {ticker} | Score {sig['signal_score']}")
        except Exception:
            pass
        finally:
            gc.collect()

        # Rate limiting — free tier 25 req/min
        if (i + 1) % BATCH_SIZE == 0:
            time.sleep(BATCH_DELAY)

    alerts.sort(key=lambda x: x["signal_score"], reverse=True)
    elapsed = time.time() - start
    print(f"Scan done: {len(alerts)} alerts in {elapsed:.0f}s")
    return alerts, elapsed


def format_alert(sig: dict) -> str:
    hold  = sig["hold_time"]
    score = sig["signal_score"]
    label = "POSITION" if "POSITION" in hold else "SWING"

    lines = [
        f"{'='*36}",
        f"BUY  {sig['ticker']}  —  {sig['name']}",
        f"{'='*36}",
        f"Signal:   {score:.0f}/100  |  {label}",
        f"Hold:     {hold}",
        f"Sector:   {sig['sector']}",
        f"",
        f"--- Price ---",
        f"Price:    ${sig['price']:.2f}",
        f"SMA200:   ${sig['sma200']:.2f}  (trend UP)",
        f"RSI(14):  {sig['rsi']:.1f}",
        f"",
        f"--- Momentum ---",
        f"6-Month:  {sig['mom_6m']:+.1f}%",
        f"3-Month:  {sig['mom_3m']:+.1f}%",
        f"1-Month:  {sig['mom_1m']:+.1f}%",
        f"Score:    {sig['momentum_score']:+.1f}%",
        f"",
        f"--- Quality (Berkshire Screen) ---",
        f"ROE:          {sig['roe']:.1f}%",
        f"Gross Margin: {sig['gross_margin']:.1f}%",
        f"EPS Growth:   {sig['eps_growth']:+.1f}%",
        f"Debt/Equity:  {sig['debt_equity']:.2f}",
        f"P/E Ratio:    {sig['pe_ratio']:.1f}",
        f"Quality Score:{sig['quality_score']:.0f}/100",
    ]

    if sig.get("quality_notes"):
        lines.append(f"Warnings: {', '.join(sig['quality_notes'])}")

    lines += [
        f"",
        f"--- Risk Management (1% rule) ---",
        f"Entry:    ${sig['price']:.2f}",
        f"Stop:     ${sig['stop']:.2f}  (2x ATR)",
        f"Target 1: ${sig['tp1']:.2f}  (1:1 R/R)",
        f"Target 2: ${sig['tp2']:.2f}  (1:1.5 R/R)",
        f"Shares:   {sig['shares']}  (${sig['position_val']:,.0f}  {sig['pct_account']:.1f}% of account)",
        f"Max loss: ${sig['risk_dollars']:.0f}",
        f"{'='*36}",
    ]
    return "\n".join(lines)


def format_summary(alerts: list, elapsed: float, universe_size: int) -> str:
    positions = [a for a in alerts if "POSITION" in a["hold_time"]]
    swings    = [a for a in alerts if "SWING"    in a["hold_time"]]
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = (
        f"Quality Momentum Scan — {ts}\n"
        f"{'='*36}\n"
        f"Scanned:         {universe_size:,} tickers\n"
        f"Duration:        {elapsed:.0f}s\n"
        f"POSITION trades: {len(positions)}\n"
        f"SWING trades:    {len(swings)}\n"
        f"Total alerts:    {len(alerts)}\n"
        f"{'='*36}\n"
    )
    if positions:
        msg += "\nTop POSITION trades:\n"
        for a in positions[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | Mom {a['momentum_score']:+.1f}% | ROE {a['roe']:.0f}%\n"
    if swings:
        msg += "\nTop SWING trades:\n"
        for a in swings[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | Mom {a['momentum_score']:+.1f}% | RSI {a['rsi']:.0f}\n"
    return msg


async def run_universe_scan(bot, chat_id: str, tickers: list = None):
    from universe import load_universe

    if tickers:
        universe_size = len(tickers)
        scan_list     = tickers
    else:
        universe      = load_universe()
        universe_size = len(universe.get("ALL", []))
        scan_list     = None

    # Alpha Vantage free = 25 calls/day
    # Each stock = 2 calls (price + fundamentals)
    # So max 12 stocks per day on free tier
    MAX_FREE = 12
    if scan_list is None:
        # Use SP500 only for scheduled scans — best quality stocks
        from universe import load_universe
        u         = load_universe()
        scan_list = u.get("SP500", [])[:MAX_FREE]

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"Quality Momentum Scan starting...\n"
            f"Scanning {len(scan_list)} tickers\n"
            f"(Alpha Vantage free tier: 25 calls/day)\n"
            f"Est. time: {len(scan_list) * 2}–{len(scan_list) * 3} min"
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

    summary = format_summary(alerts, elapsed, len(scan_list))
    await bot.send_message(chat_id=chat_id, text=summary)

    if not alerts:
        await bot.send_message(
            chat_id=chat_id,
            text="No stocks passed filters today. Try again tomorrow or use /check AAPL to test a specific stock."
        )
        return

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Failed to send {sig['ticker']}: {e}")
