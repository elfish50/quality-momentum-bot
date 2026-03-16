"""
Quality Momentum Bot
Berkshire-style quality screen + quantitative momentum
NASDAQ + NYSE | Alpaca + Finnhub | Railway + cron-job.org
"""
import asyncio
import json
import os
import traceback
from datetime import datetime
from threading import Thread

from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from config import BOT_TOKEN, CHAT_ID


# ── Scheduled scan ────────────────────────────────────────────────────────────

async def scheduled_scan(bot):
    from scanner import run_universe_scan
    print(f"Scheduled scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(bot, CHAT_ID)


# ── Webhook trigger (for cron-job.org) ───────────────────────────────────────

async def handle_trigger(request):
    """
    cron-job.org hits GET /trigger?secret=YOUR_SECRET
    Bot triggers the scan and returns 200.
    """
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.rel_url.query.get("secret", "")

    if secret and incoming != secret:
        return web.Response(status=403, text="Forbidden")

    app = request.app["bot_app"]
    asyncio.create_task(scheduled_scan(app.bot))
    print(f"[trigger] Scan triggered via webhook at {datetime.now().strftime('%H:%M')}")
    return web.Response(text="Scan triggered OK")


async def handle_health(request):
    return web.Response(text="OK")


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Bot\n"
        "Berkshire quality screen + momentum math\n"
        "NASDAQ + NYSE | Alpaca + Finnhub\n"
        "==============================\n\n"
        "SCAN COMMANDS:\n"
        "/scan                    Full scan\n"
        "/scan AAPL MSFT NVDA     Scan specific tickers\n\n"
        "QUICK CHECK:\n"
        "/check AAPL  Analyze one stock now\n\n"
        "WATCHLIST:\n"
        "/watch AAPL       Add to watchlist\n"
        "/unwatch AAPL     Remove from watchlist\n"
        "/list             Show watchlist\n"
        "/scan_watchlist   Scan watchlist now\n\n"
        "INFO:\n"
        "/strategy    How the strategy works\n"
        "/universe    How many tickers loaded\n"
        "/settings    Bot settings\n\n"
        "Auto-scans: Mon-Fri at 9:30 AM ET\n"
    )


# ── /strategy ─────────────────────────────────────────────────────────────────

async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "BB 3rd Touch Breakout + Fibonacci Targets\n"
        "==============================\n\n"
        "STEP 1 — QUALITY SCREEN (Berkshire DNA)\n"
        "  ROE > 15%           (profitable business)\n"
        "  Gross Margin > 40%  (pricing power)\n"
        "  EPS Growth > 15%    (growing earnings)\n"
        "  Debt/Equity < 0.5   (low debt)\n\n"
        "STEP 2 — BOLLINGER BAND PATTERN\n"
        "  Price touches lower band 3+ times\n"
        "  3rd touch: candle closes back ABOVE lower band\n"
        "  RSI turning up (30-65 range)\n"
        "  Volume 15%+ above average\n"
        "  Price above SMA50 (no downtrends)\n\n"
        "STEP 3 — FIBONACCI TARGETS\n"
        "  TP1 = 38.2% retracement\n"
        "  TP2 = 61.8% retracement\n"
        "  TP3 = 100% retracement (swing high)\n"
        "  Min R:R = 1.5x at TP2\n\n"
        "STEP 4 — RISK ($1k account, 10% risk)\n"
        "  Stop = 1.5x ATR below entry\n"
        "  Risk = $100 per trade\n"
    )


# ── /scan ─────────────────────────────────────────────────────────────────────

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from scanner import run_universe_scan
    chat_id = str(update.effective_chat.id)
    if ctx.args:
        tickers = [t.upper() for t in ctx.args]
        await update.message.reply_text(f"Scanning: {', '.join(tickers)}...")
        asyncio.create_task(run_universe_scan(ctx.bot, chat_id, tickers=tickers))
    else:
        await update.message.reply_text("Starting full scan...")
        asyncio.create_task(run_universe_scan(ctx.bot, chat_id))


# ── /check ────────────────────────────────────────────────────────────────────

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
        else:
            await update.message.reply_text(
                f"{ticker} did not pass filters.\n"
                f"Possible reasons: no BB 3rd touch, R:R too low, quality screen failed, downtrend."
            )
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


# ── /scan_watchlist ───────────────────────────────────────────────────────────

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
        await update.message.reply_text("Watchlist is empty. Use /watch AAPL")
        return
    await update.message.reply_text(f"Scanning watchlist: {', '.join(tickers)}")
    asyncio.create_task(run_universe_scan(ctx.bot, chat_id, tickers=tickers))


# ── Watchlist CRUD ────────────────────────────────────────────────────────────

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
        await update.message.reply_text(f"{ticker} added to watchlist.")
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
        await update.message.reply_text("Watchlist is empty.")


async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        with open("watchlist.json") as f:
            wl = json.load(f)
        if wl:
            await update.message.reply_text(
                "Watchlist:\n" + "\n".join(f"  {x['ticker']}" for x in wl)
            )
        else:
            await update.message.reply_text("Watchlist is empty.")
    except FileNotFoundError:
        await update.message.reply_text("Watchlist is empty.")


# ── /universe ─────────────────────────────────────────────────────────────────

async def cmd_universe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from universe import load_universe
    await update.message.reply_text("Checking universe...")
    try:
        u      = load_universe()
        all_t  = u.get("ALL", [])
        await update.message.reply_text(
            f"Universe Status\n"
            f"==============================\n"
            f"TOTAL:  {len(all_t):,} unique tickers\n"
            f"Source: Alpaca /v2/assets\n"
            f"==============================\n"
            f"{'OK — ready to scan' if len(all_t) > 100 else 'WARNING: very few tickers'}"
        )
    except Exception:
        await update.message.reply_text(f"Error:\n{traceback.format_exc()[-400:]}")


# ── /settings ─────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Bot Settings\n"
        f"==============================\n"
        f"Markets:     NASDAQ + NYSE\n"
        f"Data:        Alpaca + Finnhub\n"
        f"Account:     $1,000\n"
        f"Risk/trade:  10% = $100\n"
        f"Stop:        1.5x ATR\n"
        f"Min R:R:     1.5x at TP2\n"
        f"Targets:     Fibonacci 38.2 / 61.8 / 100%\n"
        f"Schedule:    Mon-Fri 9:30 AM ET\n"
        f"Trigger:     cron-job.org webhook\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    bot_app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

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

    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron",
        day_of_week="mon-fri",
        hour="9",
        minute="30",
        id="daily_scan",
    )

    async def on_startup(application):
        scheduler.start()
        await application.bot.delete_webhook(drop_pending_updates=True)
        print("Scheduler started — daily scan at 9:30 AM ET (Mon-Fri)")

        # Start aiohttp webhook trigger server
        web_app = web.Application()
        web_app["bot_app"] = application
        web_app.router.add_get("/trigger", handle_trigger)
        web_app.router.add_get("/health",  handle_health)

        port = int(os.getenv("PORT", 8080))
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"Webhook trigger listening on port {port}")

    bot_app.post_init = on_startup
    print("Quality Momentum Bot running!")
    bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
```

Then add `aiohttp` to `requirements.txt` on GitHub:
```
python-telegram-bot[webhooks]==21.9
apscheduler==3.10.4
python-dotenv==1.0.0
requests==2.31.0
pandas==2.2.3
numpy==1.26.4
beautifulsoup4==4.12.3
aiohttp==3.9.5
