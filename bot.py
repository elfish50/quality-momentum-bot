"""
Quality Momentum Bot - VWAP Mean Reversion
"""
import asyncio
import json
import os
import traceback
from datetime import datetime
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from config import BOT_TOKEN, CHAT_ID


async def scheduled_scan(bot):
    from scanner import run_universe_scan
    print(f"Scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(bot, CHAT_ID)


async def handle_trigger(request):
    secret = os.getenv("CRON_SECRET", "")
    incoming = request.rel_url.query.get("secret", "")
    if secret and incoming != secret:
        return web.Response(status=403, text="Forbidden")
    asyncio.create_task(scheduled_scan(request.app["bot_app"].bot))
    return web.Response(text="Scan triggered OK")


async def handle_health(request):
    return web.Response(text="OK")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Bot\n"
        "VWAP Mean Reversion Strategy\n"
        "==============================\n"
        "/scan - Full scan\n"
        "/scan AAPL MSFT - Scan specific tickers\n"
        "/check AAPL - Analyze one stock\n"
        "/watch AAPL - Add to watchlist\n"
        "/unwatch AAPL - Remove from watchlist\n"
        "/list - Show watchlist\n"
        "/scan_watchlist - Scan watchlist\n"
        "/strategy - How it works\n"
        "/settings - Bot settings\n"
        "Auto-scans: Mon-Fri 10AM, 12PM, 2PM ET"
    )


async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "VWAP Mean Reversion + Momentum Filter\n"
        "==============================\n"
        "1. Volume filter: min 1M shares/day\n"
        "2. Price moves >1.5% above/below VWAP\n"
        "3. RSI not extreme (30-70)\n"
        "4. Price starts reverting toward VWAP\n"
        "5. Exit at VWAP touch or 1.5x ATR stop\n"
        "Timeframe: 15-min bars\n"
        "Account: $1,000 | Risk: $100/trade"
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from scanner import run_universe_scan
    chat_id = str(update.effective_chat.id)
    if ctx.args:
        tickers = [t.upper() for t in ctx.args]
        await update.message.reply_text(f"Scanning: {', '.join(tickers)}...")
        asyncio.create_task(run_universe_scan(ctx.bot, chat_id, tickers=tickers))
    else:
        await update.message.reply_text("Starting VWAP scan...")
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
        sig = await loop.run_in_executor(None, lambda: analyze_ticker(ticker))
        if sig:
            await update.message.reply_text(format_alert(sig))
        else:
            await update.message.reply_text(
                f"{ticker} - no signal\n"
                "Reasons: not extended from VWAP, RSI extreme,\n"
                "price not reverting, low volume, market closed.\n"
                "Only works during market hours 9:30AM-4PM ET"
            )
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
    from universe import load_universe
    await update.message.reply_text("Checking universe...")
    try:
        u = load_universe()
        all_t = u.get("ALL", [])
        await update.message.reply_text(
            f"Universe: {len(all_t):,} tickers from Alpaca"
        )
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot Settings\n"
        "==============================\n"
        "Strategy:  VWAP Mean Reversion\n"
        "Timeframe: 15-min bars\n"
        "Data:      Alpaca + Finnhub\n"
        "Account:   $1,000\n"
        "Risk:      10% = $100/trade\n"
        "Stop:      1.5x ATR\n"
        "Min R:R:   1.0x\n"
        "Schedule:  Mon-Fri 10AM, 12PM, 2PM ET"
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

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("help", start))
    bot_app.add_handler(CommandHandler("scan", cmd_scan))
    bot_app.add_handler(CommandHandler("check", cmd_check))
    bot_app.add_handler(CommandHandler("scan_watchlist", cmd_scan_watchlist))
    bot_app.add_handler(CommandHandler("watch", watch))
    bot_app.add_handler(CommandHandler("unwatch", unwatch))
    bot_app.add_handler(CommandHandler("list", list_cmd))
    bot_app.add_handler(CommandHandler("strategy", cmd_strategy))
    bot_app.add_handler(CommandHandler("universe", cmd_universe))
    bot_app.add_handler(CommandHandler("settings", cmd_settings))

    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron", day_of_week="mon-fri", hour="10", minute="0", id="scan_10am"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron", day_of_week="mon-fri", hour="12", minute="0", id="scan_12pm"
    )
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron", day_of_week="mon-fri", hour="14", minute="0", id="scan_2pm"
    )

    async def on_startup(application):
        scheduler.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("Scheduler started - scans at 10AM, 12PM, 2PM ET")
        web_app = web.Application()
        web_app["bot_app"] = application
        web_app.router.add_get("/trigger", handle_trigger)
        web_app.router.add_get("/health", handle_health)
        port = int(os.getenv("PORT", 8080))
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"Webhook listening on port {port}")

    bot_app.post_init = on_startup
    print("Quality Momentum Bot running!")
    bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
