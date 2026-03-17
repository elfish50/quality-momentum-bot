scheduler = AsyncIOScheduler(timezone="America/New_York")
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron",
        day_of_week="mon-fri",
        hour="10",
        minute="0",
        id="scan_10am",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron",
        day_of_week="mon-fri",
        hour="12",
        minute="0",
        id="scan_12pm",
    )
    scheduler.add_job(
        lambda: asyncio.create_task(scheduled_scan(bot_app.bot)),
        "cron",
        day_of_week="mon-fri",
        hour="14",
        minute="0",
        id="scan_2pm",
    )
