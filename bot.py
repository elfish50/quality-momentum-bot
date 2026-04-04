"""
Quality Momentum Bot - Elliott Wave + Fibonacci
Auto-executes BUY signals on Alpaca paper account
"""
import asyncio
import json
import sys
import traceback
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.error import Conflict
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from config import BOT_TOKEN, CHAT_ID


async def error_handler(update, context):
    error = context.error
    if isinstance(error, Conflict):
        print("WARNING: Conflict detected, ignoring - Railway will manage restarts.")
        return
    print(f"Unhandled error: {error}")
    traceback.print_exc()


async def scheduled_scan(bot):
    from scanner import run_universe_scan
    print(f"Scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(bot, CHAT_ID)


async def scheduled_monitor(bot):
    """Runs every 5 minutes during market hours — checks TP1, stop, and closes."""
    from monitor import run_monitor
    print(f"[monitor] Check at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: run_monitor(bot=bot, chat_id=CHAT_ID))


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
        "/portfolio - Paper account P&L\n"
        "/trades - Recent trade history\n"
        "/cancel - Cancel all open orders\n"
        "/strategy - How it works\n"
        "/settings - Bot settings\n"
        "Auto-scans: Mon-Fri 10AM, 12:30PM, 2:30PM ET\n"
        "Monitor: every 5 min (TP1 exits + stop tracking)"
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
    """Show all open tracked positions with entry, stop, and TP levels."""
    from positions import format_open_positions
    try:
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
    from strategy import get_universe
    await update.message.reply_text("Fetching live universe from Finviz...")
    try:
        loop    = asyncio.get_event_loop()
        tickers = await loop.run_in_executor(None, get_universe)
        await update.message.reply_text(
            f"Universe: {len(tickers):,} tickers (Finviz live screener)\n"
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
        "Universe:  Finviz live screener (~400 tickers)\n"
        "Data:      Alpaca + Finnhub\n"
        "Account:   $1k paper\n"
        "Risk:      $100/trade\n"
        "BUY:       volume confirmed (auto-executed)\n"
        "WATCH:     volume pending (alert only)\n"
        "Schedule:  Mon-Fri 10AM, 12:30PM, 2:30PM ET\n"
        "Monitor:   every 5 min (TP1 exits + stops)"
    )


def main():
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    bot_app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

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

    loop = None

    scheduler = AsyncIOScheduler(timezone="America/New_York")

    # ── Scans ─────────────────────────────────────────────────────────────────
    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(scheduled_scan(bot_app.bot), loop),
        "cron", day_of_week="mon-fri", hour="10", minute="0", id="scan_10am"
    )
    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(scheduled_scan(bot_app.bot), loop),
        "cron", day_of_week="mon-fri", hour="12", minute="30", id="scan_1230pm"
    )
    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(scheduled_scan(bot_app.bot), loop),
        "cron", day_of_week="mon-fri", hour="14", minute="30", id="scan_230pm"
    )

    # ── Position monitor — every 5 min, market hours only ────────────────────
    # monitor.py checks _is_market_open() internally so off-hours calls are no-ops
    scheduler.add_job(
        lambda: asyncio.run_coroutine_threadsafe(scheduled_monitor(bot_app.bot), loop),
        "cron",
        day_of_week="mon-fri",
        hour="9-16",          # 9AM-4PM ET window (monitor skips if market closed)
        minute="*/5",         # every 5 minutes
        id="monitor_5min"
    )

    async def on_startup(application):
        nonlocal loop
        loop = asyncio.get_event_loop()
        scheduler.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("Scheduler started")
        print("  Scans:   Mon-Fri 10AM, 12:30PM, 2:30PM ET")
        print("  Monitor: Mon-Fri 9AM-4PM every 5 min (TP1 exits + stop tracking)")
        print(f"  Next jobs: {[str(job.next_run_time) for job in scheduler.get_jobs()]}")

    bot_app.post_init = on_startup
    print("Quality Momentum Bot running!")
    bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
