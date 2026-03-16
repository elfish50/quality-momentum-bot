"""
scanner.py — Quality Momentum Scanner
Alpaca (price) + Finnhub (fundamentals) + Fibonacci targets
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


def run_scan(tickers: list = None) -> tuple:
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
                print(f"[ALERT] BUY {ticker} | Score {sig['signal_score']} | R:R TP2 {sig.get('rr_tp2', 0):.1f}x")
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


def format_alert(sig: dict) -> str:
    hold     = sig["hold_time"]
    score    = sig["signal_score"]
    label    = "POSITION" if "POSITION" in hold else "SWING"
    vol_note = "Volume confirmed" if sig.get("vol_confirmed") else "Low volume — weaker signal"

    lines = [
        f"{'='*36}",
        f"BUY  {sig['ticker']}  —  {sig['name']}",
        f"{'='*36}",
        f"Signal:   {score:.0f}/100  |  {label}",
        f"Hold:     {hold}",
        f"Sector:   {sig['sector']}",
        f"",
        f"--- Bollinger Band Pattern ---",
        f"Lower Band touches: {sig['n_touches']} (min 3 needed)",
        f"BB Lower: ${sig['bb_lower']:.2f}",
        f"BB Mid:   ${sig['bb_mid']:.2f}",
        f"BB Upper: ${sig['bb_upper']:.2f}",
        f"BB Width: {sig['bb_width']:.1f}%  (volatility)",
        f"Volume:   {sig['vol_ratio']:.1f}x avg  — {vol_note}",
        f"",
        f"--- Price & Momentum ---",
        f"Price:    ${sig['price']:.2f}",
        f"RSI(14):  {sig['rsi']:.1f}",
        f"SMA50:    ${sig['sma50']:.2f}" if sig.get('sma50') else "SMA50:    N/A",
        f"SMA200:   ${sig['sma200']:.2f}" if sig.get('sma200') else "SMA200:   N/A",
        f"Swing Low:  ${sig.get('swing_low', 0):.2f}",
        f"Swing High: ${sig.get('swing_high', 0):.2f}",
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
        f"--- Risk Management ($1k account, 10% risk) ---",
        f"Entry:    ${sig['price']:.2f}",
        f"Stop:     ${sig['stop']:.2f}  (1.5x ATR)",
        f"",
        f"--- Fibonacci Targets ---",
        f"TP1 38.2%: ${sig['tp1']:.2f}  ({sig['tp1_pct']:+.1f}%)  R:R {sig.get('rr_tp1',0):.2f}x",
        f"TP2 61.8%: ${sig['tp2']:.2f}  ({sig['tp2_pct']:+.1f}%)  R:R {sig.get('rr_tp2',0):.2f}x",
        f"TP3 100%:  ${sig['tp3']:.2f}  ({sig['tp3_pct']:+.1f}%)  R:R {sig.get('rr_tp3',0):.2f}x",
        f"",
        f"Shares:   {sig['shares']}  (${sig['position_val']:,.0f}  {sig['pct_account']:.1f}% of $1k)",
        f"Max loss: ${sig['risk_dollars']:.0f}",
        f"{'='*36}",
    ]
    return "\n".join(lines)


def format_summary(alerts: list, elapsed: float, universe_size: int) -> str:
    positions = [a for a in alerts if "POSITION" in a["hold_time"]]
    swings    = [a for a in alerts if "SWING"    in a["hold_time"]]
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = (
        f"BB 3rd Touch + Fibonacci Scan — {ts}\n"
        f"{'='*36}\n"
        f"Scanned:         {universe_size:,} tickers\n"
        f"Duration:        {elapsed:.0f}s\n"
        f"POSITION setups: {len(positions)}\n"
        f"SWING setups:    {len(swings)}\n"
        f"Total alerts:    {len(alerts)}\n"
        f"{'='*36}\n"
        f"Strategy: BB 3rd Touch + Fibonacci Targets\n"
        f"          + Berkshire Quality Screen\n"
        f"Min R:R:  1.5x at TP2 (61.8% fib)\n"
    )
    if positions:
        msg += f"\nTop POSITION setups:\n"
        for a in positions[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | RSI {a['rsi']:.0f} | R:R TP2 {a.get('rr_tp2',0):.1f}x | TP3 {a.get('tp3_pct',0):+.1f}%\n"
    if swings:
        msg += f"\nTop SWING setups:\n"
        for a in swings[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | RSI {a['rsi']:.0f} | R:R TP2 {a.get('rr_tp2',0):.1f}x | TP3 {a.get('tp3_pct',0):+.1f}%\n"
    return msg


async def run_universe_scan(bot, chat_id: str, tickers: list = None):
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
            f"BB 3rd Touch + Fibonacci Scan starting...\n"
            f"Scanning {universe_size} tickers\n"
            f"Min R:R: 1.5x at TP2\n"
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
            text="No stocks passed all filters today.\nTry /check AMT to test."
        )
        return

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Failed to send {sig['ticker']}: {e}")
