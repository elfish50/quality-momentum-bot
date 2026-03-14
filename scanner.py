"""
scanner.py
Runs quality momentum screen across NASDAQ + NYSE universe.
Pre-filters by price/volume, then runs full analysis in batches.
"""
import gc
import math
import time
import asyncio
import traceback
import requests
from datetime import datetime

import pandas as pd
import yfinance as yf

from universe import get_all_tickers
from strategy import analyze_ticker, _SESSION

# ── Config ────────────────────────────────────────────────────────────────────
MIN_PRICE      = 10.0
MAX_PRICE      = 2000.0
MIN_AVG_VOLUME = 500_000
BATCH_SIZE     = 10
BATCH_DELAY    = 2.0


# ── Pre-filter ────────────────────────────────────────────────────────────────

def pre_filter(tickers: list) -> list:
    passed = []
    print(f"Pre-filtering {len(tickers)} tickers...")

    for i, ticker in enumerate(tickers):
        try:
            t  = yf.Ticker(ticker, session=_SESSION)
            df = t.history(period="5d", interval="1d", auto_adjust=True)
            if df.empty or len(df) < 2:
                continue
            price = float(df["Close"].iloc[-1])
            vol   = float(df["Volume"].mean())
            if MIN_PRICE <= price <= MAX_PRICE and vol >= MIN_AVG_VOLUME:
                passed.append(ticker)
        except Exception:
            continue

        if i > 0 and i % 50 == 0:
            print(f"Pre-filter progress: {i}/{len(tickers)}, passed: {len(passed)}")
            time.sleep(1.0)

    print(f"Pre-filter: {len(passed)}/{len(tickers)} passed")
    return passed


# ── Full scan ─────────────────────────────────────────────────────────────────

def run_scan(tickers: list = None) -> tuple[list, float]:
    start = time.time()

    if tickers is None:
        tickers = get_all_tickers()

    tickers = pre_filter(tickers)
    alerts  = []
    total   = math.ceil(len(tickers) / BATCH_SIZE)

    print(f"Scanning {len(tickers)} tickers in {total} batches...")

    for i, idx in enumerate(range(0, len(tickers), BATCH_SIZE)):
        batch = tickers[idx:idx+BATCH_SIZE]
        for t in batch:
            try:
                sig = analyze_ticker(t)
                if sig:
                    alerts.append(sig)
            except Exception:
                pass
            finally:
                gc.collect()
        if i < total - 1:
            time.sleep(BATCH_DELAY)

    # Sort by signal_score descending
    alerts.sort(key=lambda x: x["signal_score"], reverse=True)
    elapsed = time.time() - start
    print(f"Scan complete: {len(alerts)} alerts in {elapsed:.0f}s")
    return alerts, elapsed


# ── Formatting ────────────────────────────────────────────────────────────────

def format_alert(sig: dict) -> str:
    hold  = sig["hold_time"]
    score = sig["signal_score"]
    emoji = "POSITION" if "POSITION" in hold else "SWING"

    lines = [
        f"{'='*36}",
        f"BUY  {sig['ticker']}  —  {sig['name']}",
        f"{'='*36}",
        f"Signal:   {score:.0f}/100  |  {emoji}",
        f"Hold:     {hold}",
        f"Sector:   {sig['sector']}",
        f"",
        f"--- Price ---",
        f"Price:    ${sig['price']:.2f}",
        f"SMA200:   ${sig['sma200']:.2f}  (trend: UP)",
        f"RSI(14):  {sig['rsi']:.1f}",
        f"",
        f"--- Momentum ---",
        f"6-Month:  {sig['mom_6m']:+.1f}%",
        f"3-Month:  {sig['mom_3m']:+.1f}%",
        f"1-Month:  {sig['mom_1m']:+.1f}%",
        f"Score:    {sig['momentum_score']:+.1f}%",
        f"",
        f"--- Quality (Berkshire Screen) ---",
        f"ROE:          {sig['roe']:.1f}%  (min 15%)",
        f"Gross Margin: {sig['gross_margin']:.1f}%  (min 40%)",
        f"EPS Growth:   {sig['eps_growth']:+.1f}%  (min 10%)",
        f"Debt/Equity:  {sig['debt_equity']:.2f}  (max 0.5)",
        f"P/E Ratio:    {sig['pe_ratio']:.1f}",
        f"Quality Score:{sig['quality_score']:.0f}/100",
    ]

    if sig.get("quality_notes"):
        lines.append(f"Warnings: {', '.join(sig['quality_notes'])}")

    lines += [
        f"",
        f"--- Risk Management (1% rule) ---",
        f"Entry:    ${sig['price']:.2f}",
        f"Stop:     ${sig['stop']:.2f}  (2x ATR below)",
        f"Target 1: ${sig['tp1']:.2f}  (1:1 R/R)",
        f"Target 2: ${sig['tp2']:.2f}  (1:1.5 R/R)",
        f"ATR(14):  ${sig['atr14']:.2f}",
        f"Shares:   {sig['shares']}  (${sig['position_val']:,.0f}  {sig['pct_account']:.1f}% of account)",
        f"Max loss: ${sig['risk_dollars']:.0f}  (1% of $100k)",
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
        f"Universe scanned: {universe_size:,}\n"
        f"Duration:         {elapsed:.0f}s\n"
        f"POSITION trades:  {len(positions)}  (2-6 weeks)\n"
        f"SWING trades:     {len(swings)}  (3-10 days)\n"
        f"Total alerts:     {len(alerts)}\n"
        f"{'='*36}\n"
        f"Strategy: Quality (ROE/Margins/EPS/Debt)\n"
        f"        + Momentum (6M*0.6 + 3M*0.4)\n"
        f"        + Trend (above 200 SMA)\n"
        f"        + RSI 35-70\n"
        f"Risk: 1% per trade | 2xATR stop | 3xATR target\n"
    )

    if positions:
        msg += f"\nTop POSITION trades:\n"
        for a in positions[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | Mom {a['momentum_score']:+.1f}% | ROE {a['roe']:.0f}%\n"
    if swings:
        msg += f"\nTop SWING trades:\n"
        for a in swings[:5]:
            msg += f"  {a['ticker']} | Score {a['signal_score']:.0f} | Mom {a['momentum_score']:+.1f}% | RSI {a['rsi']:.0f}\n"

    return msg


# ── Async wrapper for bot ─────────────────────────────────────────────────────

async def run_universe_scan(bot, chat_id: str, tickers: list = None):
    from universe import load_universe

    universe      = load_universe()
    universe_size = len(universe.get("ALL", []))

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"Quality Momentum Scan starting...\n"
            f"Universe: {universe_size:,} tickers (NASDAQ + NYSE)\n"
            f"Filters: ROE > 15% | Margin > 40% | EPS growth > 10%\n"
            f"         Debt/Equity < 0.5 | Above 200 SMA | RSI 35-70\n"
            f"Est. time: 15-30 min. Will message you when done."
        )
    )

    try:
        loop            = asyncio.get_event_loop()
        alerts, elapsed = await loop.run_in_executor(
            None, lambda: run_scan(tickers)
        )
    except Exception:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Scan crashed:\n{traceback.format_exc()[-600:]}"
        )
        return

    summary = format_summary(alerts, elapsed, universe_size)
    await bot.send_message(chat_id=chat_id, text=summary)

    if not alerts:
        await bot.send_message(
            chat_id=chat_id,
            text="No stocks passed all quality + momentum filters today.\nMarket may be extended — check back tomorrow."
        )
        return

    for sig in alerts:
        try:
            await bot.send_message(chat_id=chat_id, text=format_alert(sig))
            await asyncio.sleep(0.3)
        except Exception as e:
            print(f"Failed to send {sig['ticker']}: {e}")
