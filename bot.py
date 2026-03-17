"""
Quality Momentum Bot
VWAP Mean Reversion Strategy
NASDAQ + NYSE | Alpaca + Finnhub | Railway + cron-job.org
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
    print(f"Scheduled scan starting at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    await run_universe_scan(bot, CHAT_ID)


async def handle_trigger(request):
    secret   = os.getenv("CRON_SECRET", "")
    incoming = request.rel_url.query.get("secret", "")
    if secret and incoming != secret:
        return web.Response(status=403, text="Forbidden")
    app = request.app["bot_app"]
    asyncio.create_task(scheduled_scan(app.bot))
    print(f"[trigger] Scan triggered at {datetime.now().strftime('%H:%M')}")
    return web.Response(text="Scan triggered OK")


async def handle_health(request):
    return web.Response(text="OK")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quality Momentum Bot\n"
        "VWAP Mean Reversion Strategy\n"
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
        "Auto-scans: Mon-Fri at 10AM, 12PM, 2PM ET\n"
    )


async def cmd_strategy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "VWAP Mean Reversion + Momentum Filter\n"
        "==============================\n\n"
        "STEP 1 — VOLUME FILTER\n"
        "  Min 1M shares/day average volume\n\n"
        "STEP 2 — VWAP EXTENSION\n"
        "  Price moves >1.5% above or below VWAP\n\n"
