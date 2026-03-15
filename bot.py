"""
Quality Momentum Bot
Berkshire-style quality screen + quantitative momentum
NASDAQ + NYSE | Alpha Vantage | Render (webhook mode)
"""
import asyncio
import json
import os
import traceback
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

from config import BOT_TOKEN, CHAT_ID


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Bot\n"
        "Berkshire quality screen + momentum math\n"
        "NASDAQ + NYSE | Free data\n"
        "==============================\n\n"
        "SCAN COMMANDS:\n"
        "/scan        Full NASDAQ + NYSE scan\n"
        "/scan AAPL MSFT NVDA   Scan specific tickers\n\n"
        "QUICK CHECK:\n"
        "/check AAPL  Analyze one stock now\n\n"
        "WATCHLIST:\n"
        "/watch AAPL     Add to watchlist\n"
        "/unwatch AAPL   Remove from watchlist\n"
        "/list           Show watchlist\n"
        "/scan_watchlist Scan watchlist now\n\n"
        "INFO:\n"
        "/strategy    How the strategy works\n"
        "/universe    How many tickers loaded\n"
        "/settings    Bot settings\n\n"
        "Auto-scans: Mon-Fri at 9:30 AM ET\n"
    )


# ── /strategy ─────────────────────────────────────────────────────────────────

async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Strategy\n"
        "==============================\n\n"
        "STEP 1 — QUALITY SCREEN (Berkshire DNA)\n"
        "  ROE > 15%           (profitable business)\n"
        "  Gross Margin > 40%  (pricing power / moat)\n"
        "  EPS Growth > 10%    (growing earnings)\n"
        "  Debt/Equity < 0.5   (low debt, financial strength)\n\n"
        "STEP 2 — TREND FILTER\n"
        "  Price above 200-day SMA (uptrend only)\n"
        "  RSI(14) between 35-70   (not overbought)\n\n"
        "STEP 3 — MOMENTUM SCORE (math formula)\n"
        "  Score = 6M return x 0.6 + 3M return x 0.4\n"
        "  Must be positive (buying what works)\n\n"
        "STEP 4 — SIGNAL SCORE (0-100)\n"
        "  Quality    40%\n"
        "  Momentum   35%\n"
        "  Technical  25%\n\n"
        "STEP 5 — HOLD TIME\n"
        "  Score >= 80: POSITION trade (2-6 weeks)\n"
        "  Score 60-79: SWING trade (3-10 days)\n"
        "  Score < 60:  SKIP\n\n"
        "STEP 6 — POSITION SIZING (1% risk rule)\n"
        "  Stop loss  = 2x ATR(14) below entry\n"
        "  Target 1   = 2x ATR above entry (1:1)\n"
        "  Target 2   = 3x ATR above entry (1:1.5)\n"
        "  Shares     = (Account x 1%) / (2 x ATR)\n"
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
                f"Check Railway logs for reason (quality/momentum/trend)."
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
            await update.message.reply_text("Watchlist:\n" + "\n".join(f"  {x['ticker']}" for x in wl))
        else:
            await update.message.reply_text("Watchlist is empty.")
    except FileNotFoundError:
        await update.message.reply_text("Watchlist is empty.")


# ── /universe ─────────────────────────────────────────────────────────────────

async def cmd_universe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from universe import load_universe
    await update.message.reply_text("Checking universe...")
    try:
        u = load_universe()
        sp500  = u.get("SP500",  [])
        nasdaq = u.get("NASDAQ", [])
        nyse   = u.get("NYSE",   [])
        all_t  = u.get("ALL",    [])
        await update.message.reply_text(
            f"Universe Status\n"
            f"==============================\n"
            f"SP500:  {len(sp500):,}\n"
            f"NASDAQ: {len(nasdaq):,}\n"
            f"NYSE:   {len(nyse):,}\n"
            f"TOTAL:  {len(all_t):,} unique\n"
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
        f"Data:        yfinance (free)\n"
        f"Min price:   $10\n"
        f"Max price:   $2000\n"
        f"Min volume:  500,000/day\n"
        f"Schedule:    Mon-Fri 9:30 AM ET\n"
        f"Risk/trade:  1% of account\n"
        f"Stop loss:   2x ATR(14)\n"
        f"Target 1:    2x ATR (1:1)\n"
        f"Target 2:    3x ATR (1:1.5)\n"
    )


# ── Scheduled scan ────────────────────────────────────────────────────────────

async def scheduled_scan(bot):
    from scanner import run_universe_scan
    print(f"Scheduled scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(bot, CHAT_ID)


# ── Main ──────────────────────────────────────────────────────────────────────

from telegram.request import HTTPXRequest

def main():
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start",          start))
    app.add_handler(CommandHandler("help",           start))
    app.add_handler(CommandHandler("scan",           cmd_scan))
    app.add_handler(CommandHandler("check",          cmd_check))
    app.add_handler(CommandHandler("scan_watchlist", cmd_scan_watchlist))
    app.add_handler(CommandHandler("watch",          watch))
    app.add_handler(CommandHandler("unwatch",        unwatch))
    app.add_handler(CommandHandler("list",           list_cmd))
    app.add_handler(CommandHandler("strategy",       cmd_strategy))
    app.add_handler(CommandHandler("universe",       cmd_universe))
    app.add_handler(CommandHandler("settings",       cmd_settings))

    scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(app.bot)),
        "cron",
        day_of_week="mon-fri",
        hour="9",
        minute="30",
        id="daily_scan",
    )

    async def on_startup(application):
        scheduler.start()
        # Delete any existing webhook first
        await application.bot.delete_webhook(drop_pending_updates=True)
        # Use hardcoded Render URL
        webhook_url = os.getenv("RENDER_EXTERNAL_URL", "https://quality-momentum-bot.onrender.com")
        await application.bot.set_webhook(f"{webhook_url}/webhook")
        print(f"Webhook set: {webhook_url}/webhook")
        print("Scheduler started — daily scan at 9:30 AM ET (Mon-Fri)")

    app.post_init = on_startup
    print("Quality Momentum Bot running!")
    webhook_url = os.getenv("RENDER_EXTERNAL_URL", "https://quality-momentum-bot.onrender.com")
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        webhook_url=f"{webhook_url}/webhook",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
