"""
Quality Momentum Bot - Elliott Wave + Fibonacci
Auto-executes BUY signals on Alpaca paper account
"""
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from config import BOT_TOKEN, CHAT_ID

# Global reference so scheduler jobs can reach the bot
_bot_app = None


async def error_handler(update, context):
    error = context.error
    if isinstance(error, Conflict):
        print("WARNING: Conflict detected, ignoring.")
        return
    print(f"Unhandled error: {error}")
    traceback.print_exc()


# ── Scheduler job functions (true async, no lambdas) ─────────────────────────

async def scheduled_scan():
    from scanner import run_universe_scan
    print(f"[scan] Starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(_bot_app.bot, CHAT_ID)


async def scheduled_monitor():
    from monitor import run_monitor
    print(f"[monitor] Check at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: run_monitor(bot=_bot_app.bot, chat_id=CHAT_ID))


# ── Command handlers ──────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Bot\n"
        "Elliott Wave + Fibonacci Strategy\n"
        "==============================\n"
        "/scan - Full universe scan\n"
        "/scan AAPL MSFT - Scan specific tickers\n"
        "/check AAPL - Analyze one stock\n"
        "/watch AAPL - Add to watchlist\n"
        "/unwatch AAPL - Remove from watchlist\n"
        "/list - Show watchlist\n"
        "/scan_watchlist - Scan watchlist\n"
        "/positions - Open tracked positions\n"
        "/universe - Show current scan universe\n"
        "/portfolio - Paper account P&L\n"
        "/trades - Recent trade history\n"
        "/cancel - Cancel all open orders\n"
        "/strategy - How it works\n"
        "/settings - Bot settings\n"
        "Scans + monitor: Mon-Fri 10AM, 12:30PM, 2:30PM ET"
    )


async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Elliott Wave + Fibonacci Strategy\n"
        "==============================\n"
        "SETUPS (LONG ONLY):\n"
        "  Wave 2: 50-61.8% Fib retracement\n"
        "  Wave 4: 38.2% Fib retracement\n"
        "  ABC: End of corrective wave\n\n"
        "STOPS (invalidation rules):\n"
        "  Wave 2: below Wave 1 origin\n"
        "  Wave 4: below Wave 1 high\n"
        "  ABC: below Wave A low\n\n"
        "TARGETS:\n"
        "  TP1: 1.272x — sell 1/3, stop → break-even\n"
        "  TP2: 1.618x — sell remaining 2/3\n"
        "  TP3: 2.618x — stretch target\n\n"
        "SIGNALS:\n"
        "  BUY = volume confirmed (auto-executed)\n"
        "  WATCH = wait for volume before entering\n\n"
        "Quality: Berkshire screen (ROE/margins/EPS)\n"
        "Account: $1k paper | Risk: $100/trade"
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from scanner import run_universe_scan
    chat_id = str(update.effective_chat.id)
    if ctx.args:
        tickers = [t.upper() for t in ctx.args]
        await update.message.reply_text(f"Scanning: {', '.join(tickers)}...")
        asyncio.create_task(run_universe_scan(ctx.bot, chat_id, tickers=tickers))
    else:
        await update.message.reply_text("Starting Elliott Wave scan...")
        asyncio.create_task(run_universe_scan(ctx.bot, chat_id))


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from strategy import analyze_ticker
    from scanner import format_alert
    if not ctx.args:
        await update.message.reply_text("Usage: /check AAPL")
        return
    ticker = ctx.args[0].upper()
    await update.message.reply_text(f"Analyzing {ticker}...")
    try:
        loop = asyncio.get_event_loop()
        sig  = await loop.run_in_executor(None, lambda: analyze_ticker(ticker))
        if sig:
            await update.message.reply_text(format_alert(sig))
            if sig["signal"] == "BUY":
                await update.message.reply_text(
                    "Volume confirmed - auto-executing paper trade..."
                )
                from trader import execute_signal, format_execution_result
                result = await loop.run_in_executor(None, lambda: execute_signal(sig))
                await update.message.reply_text(format_execution_result(result, sig))
        else:
            await update.message.reply_text(
                f"{ticker} - no signal\n"
                "Reasons: no Elliott Wave setup, quality screen failed,\n"
                "RSI not confirming, or R:R too low."
            )
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        from positions import format_open_positions
        msg = format_open_positions()
        await update.message.reply_text(msg)
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_portfolio(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from trader import format_portfolio
    await update.message.reply_text("Checking portfolio...")
    try:
        loop = asyncio.get_event_loop()
        msg  = await loop.run_in_executor(None, format_portfolio)
        await update.message.reply_text(msg)
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from trader import format_trade_history
    await update.message.reply_text("Fetching trade history...")
    try:
        loop = asyncio.get_event_loop()
        msg  = await loop.run_in_executor(None, format_trade_history)
        await update.message.reply_text(msg)
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from trader import cancel_all_orders
    try:
        loop    = asyncio.get_event_loop()
        success = await loop.run_in_executor(None, cancel_all_orders)
        if success:
            await update.message.reply_text("All open orders cancelled.")
        else:
            await update.message.reply_text("No open orders to cancel.")
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_scan_watchlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from scanner import run_universe_scan
    chat_id = str(update.effective_chat.id)
    try:
        with open("watchlist.json") as f:
            wl = json.load(f)
        tickers = [x["ticker"] for x in wl if x.get("ticker")]
    except FileNotFoundError:
        tickers = []
    if not tickers:
        await update.message.reply_text("Watchlist empty. Use /watch AAPL")
        return
    await update.message.reply_text(f"Scanning: {', '.join(tickers)}")
    asyncio.create_task(run_universe_scan(ctx.bot, chat_id, tickers=tickers))


async def watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ticker = ctx.args[0].upper() if ctx.args else None
    if not ticker:
        await update.message.reply_text("Usage: /watch AAPL")
        return
    try:
        with open("watchlist.json") as f:
            wl = json.load(f)
    except FileNotFoundError:
        wl = []
    if not any(x["ticker"] == ticker for x in wl):
        wl.append({"ticker": ticker})
        with open("watchlist.json", "w") as f:
            json.dump(wl, f)
        await update.message.reply_text(f"{ticker} added.")
    else:
        await update.message.reply_text(f"{ticker} already in watchlist.")


async def unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ticker = ctx.args[0].upper() if ctx.args else None
    if not ticker:
        await update.message.reply_text("Usage: /unwatch AAPL")
        return
    try:
        with open("watchlist.json") as f:
            wl = json.load(f)
        wl = [x for x in wl if x["ticker"] != ticker]
        with open("watchlist.json", "w") as f:
            json.dump(wl, f)
        await update.message.reply_text(f"{ticker} removed.")
    except FileNotFoundError:
        await update.message.reply_text("Watchlist empty.")


async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        with open("watchlist.json") as f:
            wl = json.load(f)
        if wl:
            await update.message.reply_text(
                "Watchlist:\n" + "\n".join(f"  {x['ticker']}" for x in wl)
            )
        else:
            await update.message.reply_text("Watchlist empty.")
    except FileNotFoundError:
        await update.message.reply_text("Watchlist empty.")


async def cmd_universe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Fetching universe from Alpaca...")
    try:
        from universe import load_universe
        loop    = asyncio.get_event_loop()
        tickers = await loop.run_in_executor(None, load_universe)
        await update.message.reply_text(
            f"Universe: {len(tickers):,} tickers (Alpaca most-actives + assets)\n"
            f"Sample: {', '.join(tickers[:10])}"
        )
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot Settings\n"
        "==============================\n"
        "Strategy:  Elliott Wave + Fibonacci\n"
        "Direction: LONG ONLY\n"
        "Timeframe: Daily bars\n"
        "Universe:  Alpaca most-actives + assets\n"
        "Data:      Alpaca + Finnhub\n"
        "Account:   $1k paper\n"
        "Risk:      $100/trade\n"
        "BUY:       volume confirmed (auto-executed)\n"
        "WATCH:     volume pending (alert only)\n"
        "Schedule:  Mon-Fri 10AM, 12:30PM, 2:30PM ET\n"
        "Monitor:   15 min after each scan (10:15, 12:45, 14:45)"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global _bot_app

    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    bot_app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()
    _bot_app = bot_app

    bot_app.add_error_handler(error_handler)

    bot_app.add_handler(CommandHandler("start",          start))
    bot_app.add_handler(CommandHandler("help",           start))
    bot_app.add_handler(CommandHandler("scan",           cmd_scan))
    bot_app.add_handler(CommandHandler("check",          cmd_check))
    bot_app.add_handler(CommandHandler("scan_watchlist", cmd_scan_watchlist))
    bot_app.add_handler(CommandHandler("watch",          watch))
    bot_app.add_handler(CommandHandler("unwatch",        unwatch))
    bot_app.add_handler(CommandHandler("list",           list_cmd))
    bot_app.add_handler(CommandHandler("strategy",       cmd_strategy))
    bot_app.add_handler(CommandHandler("universe",       cmd_universe))
    bot_app.add_handler(CommandHandler("settings",       cmd_settings))
    bot_app.add_handler(CommandHandler("portfolio",      cmd_portfolio))
    bot_app.add_handler(CommandHandler("positions",      cmd_positions))
    bot_app.add_handler(CommandHandler("trades",         cmd_trades))
    bot_app.add_handler(CommandHandler("cancel",         cmd_cancel))

    # AsyncIOScheduler runs coroutines natively — pass async def directly,
    # no lambda wrapping needed. The scheduler shares the same event loop
    # as run_polling, so coroutine dispatch is reliable.
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    # ── 10:00 AM scan → 10:15 AM monitor ─────────────────────────────────────
    scheduler.add_job(
        scheduled_scan,
        "cron", day_of_week="mon-fri", hour=10, minute=0, id="scan_10am"
    )
    scheduler.add_job(
        scheduled_monitor,
        "cron", day_of_week="mon-fri", hour=10, minute=15, id="monitor_10am"
    )

    # ── 12:30 PM scan → 12:45 PM monitor ─────────────────────────────────────
    scheduler.add_job(
        scheduled_scan,
        "cron", day_of_week="mon-fri", hour=12, minute=30, id="scan_1230pm"
    )
    scheduler.add_job(
        scheduled_monitor,
        "cron", day_of_week="mon-fri", hour=12, minute=45, id="monitor_1230pm"
    )

    # ── 2:30 PM scan → 2:45 PM monitor ───────────────────────────────────────
    scheduler.add_job(
        scheduled_scan,
        "cron", day_of_week="mon-fri", hour=14, minute=30, id="scan_230pm"
    )
    scheduler.add_job(
        scheduled_monitor,
        "cron", day_of_week="mon-fri", hour=14, minute=45, id="monitor_230pm"
    )

    async def on_startup(application):
        scheduler.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("Quality Momentum Bot running!")
        print("  Scans:   Mon-Fri 10:00, 12:30, 14:30 ET")
        print("  Monitor: Mon-Fri 10:15, 12:45, 14:45 ET")
        for job in scheduler.get_jobs():
            print(f"  Job: {job.id} → next {job.next_run_time}")

    bot_app.post_init = on_startup
    bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
