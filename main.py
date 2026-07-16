from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from aiogram.types import Update
from bot import (
    bot,
    dp,
    supabase,
    delivery_bots,
    load_delivery_bots,
    create_paystack_subaccount,
    send_order_to_kitchen_from_db,
    deduct_inventory_for_order,
    send_restock_alert,
    send_receipt_to_customer,
)
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
import os
import json
import logging
import asyncio
import aiohttp
import hmac
import hashlib

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
from reports import generate_daily_report, generate_weekly_report
from whatsapp import handle_whatsapp_webhook


FASTAPI_WEBHOOK_URL = os.getenv("FASTAPI_WEBHOOK_URL","https://telegram-n8n-restaurant-bot.onrender.com")  # Replace with your actual webhook URL
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")

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



async def ping_n8n_periodically():
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


async def get_bot_for_order(order_data: dict) -> Bot:
    """Pick the correct Bot instance to reply with, based on which restaurant/channel the order came from."""
    restaurant_id = order_data.get("restaurant_id")
    if restaurant_id and restaurant_id in delivery_bots:
        return delivery_bots[restaurant_id]
    return bot  # fallback to main bot if no delivery bot is found

@app.post("/webhook")
async def webhook(request:Request):
    try:
        data = await request.json()
        update = Update(**data)
        await dp.feed_update (bot=bot, update=update)
    except Exception as e:
        logger.error(f"Error processing main webhook update: {e}", exc_info=True)

    return {"ok": True}


@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    params = request.query_params
    if params.get ("hub.mode") == "subscribe" and params.get("hub.verify_token") == os.getenv("WHATSAPP_VERIFY_TOKEN"):
        return PlainTextResponse(params.get("hub.challenge"))
    return PlainTextResponse("Verification failed", status_code=403)

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    payload = await request.json()
    try:
        await handle_whatsapp_webhook(payload, supabase, bot)  # bot = your existing Telegram bot
    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}", exc_info=True)
    return {"status": "ok"}

@app.get("/paystack/callback")
async def paystack_browser_callback(request: Request):
    reference = request.query_params.get("reference") or request.query_params.get("trxref")
    return HTMLResponse(
        f"""
        <html><body style="font-family:sans-serif;text-align:center;padding:40px">
            <h2>✅ Payment received</h2>
            <p>Reference: {reference}</p>
            <p>You can close this tab and return to Telegram —
               your order is being confirmed there.</p>
        </body></html>
        """
    )


@app.post("/webhook/paystack")
async def paystack_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    if not PAYSTACK_SECRET_KEY:
        return {"status": "paystack not configured"}

    expected_signature = hmac.new(
        PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        logger.warning("Invalid Paystack webhook signature")
        return {"status": "invalid signature"}

    event = json.loads(body)
    if event.get("event") != "charge.success":
        return {"status": "ignored"}

    data = event.get("data", {})
    order_id = data.get("reference")
    if not order_id:
        return {"status": "missing reference"}

    # Idempotency check: see if we've already processed this order's payment before doing any updates or notifications.
    current = supabase.table("orders").select("payment_status").eq("id", order_id).execute()
    if current.data and current.data[0]["payment_status"] == "confirmed":
        return {"status": "already processed"}  # stop here — avoid double-firing kitchen/receipt

    # Mark payment as confirmed in DB
    supabase.table("orders").update({"payment_status": "confirmed"}).eq("id", order_id).execute()
    supabase.table("payments").update({
        "status": "confirmed",
        "paystack_reference": data.get("reference")
    }).eq("order_id", order_id).eq("provider", "Paystack").execute()

    # Load order info so we can resolve which bot should be used for notifications
    order_result = supabase.table("orders")\
        .select("telegram_user_id, restaurant_id, restaurants(kitchen_chat_id)")\
        .eq("id", order_id).execute()

    order_data = order_result.data[0] if order_result.data else {}
    restaurant_id = order_data.get("restaurant_id")
    kitchen_chat_id = (order_data.get("restaurants") or {}).get("kitchen_chat_id")
    user_id = order_data.get("telegram_user_id")

    # Resolve the correct Bot instance for this order (delivery bots vs main bot)
    try:
        target_bot = await get_bot_for_order(order_data)
    except Exception:
        target_bot = bot

    # Notify kitchen using the resolved bot
    try:
        await send_order_to_kitchen_from_db(target_bot, order_id)
    except Exception as e:
        logger.error(f"Failed to send Paystack order to kitchen: {e}", exc_info=True)

    # Deduct inventory and send restock alerts using the resolved bot
    try:
        low_stock_items = await deduct_inventory_for_order(order_id)
        await send_restock_alert(target_bot, restaurant_id, kitchen_chat_id, low_stock_items)
    except Exception as e:
        logger.error(f"Failed inventory deduction/alert for Paystack order {order_id}: {e}", exc_info=True)

    # Send receipt to customer using the resolved bot
    if user_id:
        try:
            await send_receipt_to_customer(target_bot, user_id, order_id)
        except Exception as e:
            logger.error(f"Failed to send Paystack receipt for order {order_id}: {e}", exc_info=True)

    return {"status": "ok"}


@app.post("/admin/paystack/subaccount/{restaurant_id}")
async def admin_create_paystack_subaccount(restaurant_id: str, request: Request):
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key != os.getenv("ADMIN_API_KEY"):
        return {"ok": False, "error": "unauthorized"}

    try:
        subaccount = await create_paystack_subaccount(restaurant_id)
        return {"ok": True, "subaccount_code": subaccount.get("subaccount_code"), "data": subaccount}
    except Exception as e:
        logger.error(f"Failed to create Paystack subaccount for {restaurant_id}: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


@app.post("/webhook/delivery/{restaurant_id}")
async def delivery_webhook(restaurant_id: str, request: Request):
    delivery_bot = delivery_bots.get(restaurant_id)
    if not delivery_bot:
        return {"ok": False, "error": "unknown restaurant"}
    try:
        data = await request.json()
        update = Update(**data)
        await dp.feed_update(
            bot=delivery_bot,
            update=update,
            delivery_restaurant_id=restaurant_id
        )
    except Exception as e:
        logger.error(f"Error processing delivery webhook for {restaurant_id}: {e}", exc_info=True)


    return {"ok": True}

@app.post("/admin/reload-delivery-bots")
async def reload_delivery_bots(request: Request):
    admin_key = request.headers.get("X-Admin-Key")
    if admin_key != os.getenv("ADMIN_API_KEY"):
        return {"ok": False, "error": "unauthorized"}

    restaurants = supabase.table("restaurants")\
        .select("id, delivery_bot_token")\
        .not_.is_("delivery_bot_token", "null")\
        .execute()

    new_ids = set()
    for r in restaurants.data or []:
        rid, token = r["id"], r["delivery_bot_token"]
        new_ids.add(rid)
        if rid not in delivery_bots:
            new_bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
            delivery_bots[rid] = new_bot
            await new_bot.set_webhook(f"{FASTAPI_WEBHOOK_URL}/webhook/delivery/{rid}")
            logger.info(f"Registered new delivery bot for restaurant {rid}")

    removed = [rid for rid in delivery_bots if rid not in new_ids]
    for rid in removed:
        old_bot = delivery_bots.pop(rid)
        try:
            await old_bot.delete_webhook()
            await old_bot.session.close()
        except Exception as e:
            logger.error(f"Failed to clean up bot for {rid}: {e}")

    return {"ok": True, "active_delivery_bots": list(delivery_bots.keys()), "removed": removed}

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": f"Ok. Bot is running", "webhook": f"{FASTAPI_WEBHOOK_URL}/webhook"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(f"{FASTAPI_WEBHOOK_URL}/webhook")
    await load_delivery_bots()
    for restaurant_id, dbot in delivery_bots.items():
        await dbot.set_webhook(f"{FASTAPI_WEBHOOK_URL}/webhook/delivery/{restaurant_id}")
        logger.info(f"Delivery bot webhook set for restaurant {restaurant_id}")
    print (f"Webhook set to {FASTAPI_WEBHOOK_URL}/webhook")
    asyncio.create_task(ping_n8n_periodically())

    # SCHEDULE REPORT
    scheduler.add_job(
        send_daily_reports,
        CronTrigger(hour=21, minute=00),
        id='daily_reports'
    )
    
    scheduler.add_job(
        send_weekly_reports,
        CronTrigger(day_of_week='mon', hour=8, minute=0),
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
    logger.info("📊 Daily reports: Every day at 09:59 PM")
    logger.info("📊 Weekly reports: Every Monday at 9:00 AM")




@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    await bot.session.close()
    for dbot in delivery_bots.values():
        await dbot.delete_webhook()
        await dbot.session.close()

    # SHUTDOWN SCHEDULER
    scheduler.shutdown()
    logger.info("🛑 Scheduler stopped")
