from fastapi import FastAPI, Request
from aiogram.types import Update
from bot import bot, dp, supabase
import os
import logging
import asyncio
import aiohttp

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
from reports import generate_daily_report, generate_weekly_report


FASTAPI_WEBHOOK_URL = os.getenv("FASTAPI_WEBHOOK_URL","https://telegram-n8n-restaurant-bot.onrender.com")  # Replace with your actual webhook URL

# URL of  n8n Heartbeat Webhook
N8N_HEARTBEAT_URL=os.getenv("N8N_HEARTBEAT_URL", "https://n8n-atad.onrender.com/webhook/heartbeat")


app = FastAPI(title="Telegram Bot webservice", version= "1.0.0",
              description="Fastapi webservice to service Telegram bot through webhook")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone = pytz.timezone("Africa/Lagos"))

# ADD THESE FUNCTIONS

async def send_daily_reports():
    """Send daily reports to all restaurant managers"""
    try:
        restaurants = supabase.table("restaurants")\
            .select("id, name, manager_telegram_id, manager_name")\
            .eq("subscription_status", "active")\
            .not_.is_("manager_telegram_id", "null")\
            .execute()
        
        for restaurant in restaurants.data:
            manager_id = restaurant.get("manager_telegram_id")
            if not manager_id:
                continue
            
            report = await generate_daily_report(supabase, restaurant["id"])
            
            try:
                await bot.send_message(
                    manager_id,
                    report,
                    parse_mode="Markdown"
                )
                logger.info(f"Daily report sent to manager of {restaurant['name']}")
            except Exception as e:
                logger.error(f"Failed to send daily report: {e}")
                
    except Exception as e:
        logger.error(f"Error in send_daily_reports: {e}")


async def send_weekly_reports():
    """Send weekly reports to all restaurant managers"""
    try:
        restaurants = supabase.table("restaurants")\
            .select("id, name, manager_telegram_id, manager_name")\
            .eq("subscription_status", "active")\
            .not_.is_("manager_telegram_id", "null")\
            .execute()
        
        for restaurant in restaurants.data:
            manager_id = restaurant.get("manager_telegram_id")
            if not manager_id:
                continue
            
            report = await generate_weekly_report(supabase, restaurant["id"])
            
            try:
                await bot.send_message(
                    manager_id,
                    report,
                    parse_mode="Markdown"
                )
                logger.info(f"Weekly report sent to manager of {restaurant['name']}")
            except Exception as e:
                logger.error(f"Failed to send weekly report: {e}")
                
    except Exception as e:
        logger.error(f"Error in send_weekly_reports: {e}")


async def expire_subscriptions():
    try:
        supabase.table("restaurants")\
            .update({"subscription_status": "expired"})\
            .in_("subscription_status", ["trialing", "active"])\
            .lt("subscription_expires_at", datetime.now(pytz.utc).isoformat())\
            .execute()
        logger.info("✅ Subscription expiry check complete")
    except Exception as e:
        logger.error(f"Error expiring subscriptions: {e}")

async def notify_expiring_subscriptions():
    try:
        warning_date = (datetime.now(pytz.utc) + timedelta(days=3)).isoformat()
        restaurants = supabase.table("restaurants")\
            .select("name, manager_telegram_id, subscription_expires_at")\
            .in_("subscription_status", ["trialing", "active"])\
            .lt("subscription_expires_at", warning_date)\
            .not_.is_("manager_telegram_id", "null")\
            .execute()
        for r in restaurants.data:
            manager_id = r.get("manager_telegram_id")
            if not manager_id:
                continue
            expires_at = datetime.fromisoformat(r["subscription_expires_at"].replace('Z', '+00:00'))
            days_left = (expires_at - datetime.now(pytz.utc)).days
            try:
                await bot.send_message(
                    manager_id,
                    f"⚠️ *Subscription Expiring Soon*\n\n"
                    f"Your subscription for *{r['name']}* expires in *{days_left} day(s)*.\n"
                    f"Please renew to avoid service interruption.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to notify manager of {r['name']}: {e}")
    except Exception as e:
        logger.error(f"Error in notify_expiring_subscriptions: {e}")



""" async def ping_n8n_periodically():
    #Background task to keep n8n awake on Render's free tier.
    async with aiohttp.ClientSession() as session:
        while True:

            try:
                    
                    async with session.get(N8N_HEARTBEAT_URL) as response:
                        print(f"Pinged n8n: {response.status}")
            except Exception as e:
                print(f"Ping failed: {e}")
            
            # Ping every 10 minutes to stay within Render's 15-min window
            await asyncio.sleep(600)
"""

@app.post("/webhook")
async def webhook(request:Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update (bot=bot, update=update)
    return {"ok": True}


@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": f"Ok. Bot is running", "webhook": f"{FASTAPI_WEBHOOK_URL}/webhook"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(f"{FASTAPI_WEBHOOK_URL}/webhook")
    print (f"Webhook set to {FASTAPI_WEBHOOK_URL}/webhook")
    asyncio.create_task(ping_n8n_periodically())

    # SCHEDULE REPORT
    scheduler.add_job(
        send_daily_reports,
        CronTrigger(hour=22, minute=00),
        id='daily_reports'
    )
    
    scheduler.add_job(
        send_weekly_reports,
        CronTrigger(day_of_week='mon', hour=9, minute=0),
        id='weekly_reports'
    )

    # SCHEDULE SUBSCRIPTION CHECKS AND REMINDERS
    scheduler.add_job(
        expire_subscriptions,
        CronTrigger(hour=0, minute=5, timezone=pytz.timezone('Africa/Lagos')),
        id='expire_subscriptions'
    )

    scheduler.add_job(
        notify_expiring_subscriptions,
        CronTrigger(hour=9, minute=0, timezone=pytz.timezone('Africa/Lagos')),
        id='expiry_warnings'
    )
    
    scheduler.start()
    logger.info("✅ Scheduler started")
    logger.info("📊 Daily reports: Every day at 11:59 PM")
    logger.info("📊 Weekly reports: Every Monday at 9:00 AM")




@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    await bot.session.close()

    # SHUTDOWN SCHEDULER
    scheduler.shutdown()
    logger.info("🛑 Scheduler stopped")
