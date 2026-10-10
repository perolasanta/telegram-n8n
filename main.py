from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
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
    notify_order_customer,
    send_order_receipt,
    is_subscription_active,
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
import html
import re
from urllib.parse import quote

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from datetime import datetime, timedelta
from reports import generate_daily_report, generate_weekly_report
from whatsapp import handle_whatsapp_webhook, is_whatsapp_addon_active


FASTAPI_WEBHOOK_URL = os.getenv("FASTAPI_WEBHOOK_URL","https://telegram-n8n-restaurant-bot.onrender.com")  # Replace with your actual webhook URL
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")

# URL of  n8n Heartbeat Webhook
N8N_HEARTBEAT_URL=os.getenv("N8N_HEARTBEAT_URL", "https://n8n-atad.onrender.com/webhook/heartbeat")


app = FastAPI(title="Telegram Bot webservice", version= "1.0.0",
              description="Fastapi webservice to service Telegram bot through webhook")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone = pytz.timezone("Africa/Lagos"))

LANDING_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; img-src https: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
}


def sanitize_brand_color(value):
    value = str(value or "")
    return value if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else "#1F2937"


def readable_text_color(hex_color):
    color = sanitize_brand_color(hex_color).lstrip("#")
    channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    white_contrast = 1.05 / (luminance + 0.05)
    dark_luminance = 0.2126 * (17 / 255 / 12.92) + 0.7152 * (17 / 255 / 12.92) + 0.0722 * (17 / 255 / 12.92)
    dark_contrast = (luminance + 0.05) / (dark_luminance + 0.05)
    return "#FFFFFF" if white_contrast >= dark_contrast else "#111111"


def sanitize_logo_url(value):
    if isinstance(value, str) and value.startswith("https://") and len(value) < 500:
        return value
    return None


def format_display_number(digits):
    digits = re.sub(r"\D", "", str(digits or ""))
    if digits.startswith("0") and len(digits) == 11:
        digits = "234" + digits[1:]
    if digits.startswith("234") and len(digits) == 13:
        return f"+234 {digits[3:6]} {digits[6:9]} {digits[9:]}"
    return f"+{digits}" if digits else ""


def build_landing_links(table_number, public_code, display_number):
    digits = re.sub(r"[^0-9]", "", str(display_number or ""))
    if not 8 <= len(digits) <= 15:
        return None
    is_table = table_number is not None and str(table_number) != "EXTERNAL"
    message = f"Table {table_number} ref:{public_code}" if is_table else f"Hi ref:{public_code}"
    return f"https://wa.me/{quote(digits, safe='')}?text={quote(message, safe='')}"


@app.get("/t/{public_code}", response_class=HTMLResponse)
async def table_landing(public_code: str):
    no_store = LANDING_HEADERS.copy()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", public_code or ""):
        return HTMLResponse("<!doctype html><title>Not found</title><h1>Not found</h1>", status_code=404, headers=no_store)

    result = supabase.table("restaurant_tables").select(
        "id, table_number, restaurant_id, restaurants(name, whatsapp_display_number, whatsapp_phone_number_id, whatsapp_addon_enabled, whatsapp_addon_expires_at, subscription_status, subscription_expires_at, brand_color, logo_url)"
    ).eq("public_code", public_code).eq("is_active", True).limit(1).execute()
    if not result.data:
        return HTMLResponse(
            "<!doctype html><title>QR code not found</title><h1>This QR code isn't valid. Please ask the staff.</h1>",
            status_code=404,
            headers=no_store,
        )

    table = result.data[0]
    restaurant_value = table.get("restaurants") or {}
    restaurant = restaurant_value[0] if isinstance(restaurant_value, list) and restaurant_value else restaurant_value
    if not isinstance(restaurant, dict):
        restaurant = {}
    if not await is_subscription_active(table.get("restaurant_id")):
        return HTMLResponse(
            "<!doctype html><title>Ordering unavailable</title><h1>This restaurant isn't taking orders right now.</h1>",
            headers=no_store,
        )

    if not TELEGRAM_BOT_USERNAME:
        logger.error("TELEGRAM_BOT_USERNAME is missing; cannot build table landing link")
        return HTMLResponse(
            "<!doctype html><title>Temporarily unavailable</title><h1>Ordering is temporarily unavailable.</h1>",
            status_code=503,
            headers=no_store,
        )

    telegram_url = f"https://t.me/{quote(TELEGRAM_BOT_USERNAME, safe='')}?start={quote(public_code, safe='')}"
    display_number = restaurant.get("whatsapp_display_number")
    whatsapp_url = build_landing_links(
        table.get("table_number"), public_code, display_number
    )
    addon_active = is_whatsapp_addon_active(restaurant)
    whatsapp_available = (
        whatsapp_url is not None
        and restaurant.get("whatsapp_phone_number_id")
        and addon_active
    )
    if not whatsapp_available:
        failed_conditions = []
        if whatsapp_url is None:
            failed_conditions.append("display number invalid")
        if not restaurant.get("whatsapp_phone_number_id"):
            failed_conditions.append("phone id missing")
        if not addon_active:
            failed_conditions.append("add-on inactive")
        logger.warning(
            "WhatsApp button hidden for restaurant_id=%s: %s",
            table.get("restaurant_id"), ", ".join(failed_conditions),
        )
        return RedirectResponse(telegram_url, status_code=302, headers=no_store)

    table_number = table.get("table_number")
    is_table = table_number is not None and str(table_number) != "EXTERNAL"
    restaurant_name = str(restaurant.get("name") or "Restaurant")
    title = html.escape(restaurant_name, quote=True)
    brand_color = sanitize_brand_color(restaurant.get("brand_color"))
    brand_text_color = readable_text_color(brand_color)
    logo_url = sanitize_logo_url(restaurant.get("logo_url"))
    logo_html = ""
    if logo_url:
        logo_url_attr = html.escape(quote(logo_url, safe=":/?#[]@!$&'()*+,;=%"), quote=True)
        logo_html = f'<img class="logo" src="{logo_url_attr}" alt="">'
    initials = "".join(part[0] for part in restaurant_name.split()[:2]).upper() or "C"
    initials_html = html.escape(initials, quote=True)
    location_label = f"Table {table_number} · Dine-in" if is_table else "Delivery & pickup"
    phone_label = html.escape(format_display_number(display_number), quote=True)
    table_pill = html.escape(location_label, quote=True)
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
        f"<title>{title}</title><meta property=\"og:title\" content=\"{title}\">"
        "<style>*{box-sizing:border-box}body{font:16px/1.5 system-ui,-apple-system,sans-serif;"
        "margin:0;min-height:100vh;min-height:100dvh;padding:env(safe-area-inset-top) 16px env(safe-area-inset-bottom);"
        "display:flex;justify-content:center;color:#111827;background:#f8fafc}.shell{width:100%;max-width:480px;"
        "margin:auto 0;padding:16px 0}.brand{background:var(--brand);color:var(--brand-text);border-radius:24px;"
        "padding:24px 20px;display:flex;align-items:center;gap:16px}.avatar{width:68px;height:68px;flex:none;"
        "position:relative;border-radius:50%;display:grid;place-items:center;background:rgba(255,255,255,.22);"
        "font-size:22px;font-weight:800}.logo{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;"
        "border-radius:50%}.brand h1{font-size:24px;line-height:1.2;margin:0 0 10px;overflow-wrap:anywhere}"
        ".pill{display:inline-block;border:1px solid currentColor;border-radius:999px;padding:4px 10px;"
        "font-size:13px;font-weight:650}.content{padding:24px 4px}.content h2{font-size:21px;margin:0 0 16px}"
        ".action{min-height:60px;width:100%;padding:15px 18px;border:0;border-radius:16px;display:flex;"
        "align-items:center;justify-content:center;gap:12px;color:#fff;text-decoration:none;font-size:17px;"
        "font-weight:750;margin:0 0 12px}.action svg{width:24px;height:24px;flex:none}.telegram{background:#229ED9}"
        ".whatsapp{background:#25D366}.muted{text-align:center;font-size:13px;color:#64748b;margin:-4px 0 22px}"
        ".steps{border-top:1px solid #e2e8f0;padding-top:20px}.steps h2{font-size:17px;margin:0 0 12px}"
        ".steps ol{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(3,1fr);gap:8px}"
        ".steps li{font-size:12px;color:#475569;text-align:center}.step{display:grid;place-items:center;width:30px;"
        "height:30px;border-radius:50%;background:#e2e8f0;color:#0f172a;font-weight:800;margin:0 auto 7px}"
        "footer{text-align:center;color:#64748b;font-size:12px;padding:18px 0 4px}@media(prefers-color-scheme:dark){"
        "body{background:#0f172a;color:#f8fafc}.steps{border-color:#334155}.steps li,.muted,footer{color:#94a3b8}"
        ".step{background:#334155;color:#f8fafc}}</style></head><body><main class=\"shell\">"
        f"<header class=\"brand\" style=\"--brand:{brand_color};--brand-text:{brand_text_color}\">"
        f"<span class=\"avatar\" aria-hidden=\"true\">{initials_html}{logo_html}</span>"
        f"<div><h1>{title}</h1><span class=\"pill\">{table_pill}</span></div></header>"
        "<section class=\"content\"><h2>How would you like to order?</h2>"
        f"<a class=\"action telegram\" href=\"{html.escape(telegram_url, quote=True)}\">"
        "<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"currentColor\" d=\"M21.8 3.2 18.7 20c-.2 1.2-.9 1.5-1.8.9l-5-3.7-2.4 2.3c-.3.3-.5.5-1 .5l.4-5.1 9.3-8.4c.4-.4-.1-.6-.6-.2L6.1 13.6l-5-1.6c-1.1-.3-1.1-1.1.2-1.6L20.9 2.7c.9-.3 1.6.2.9.5Z\"/></svg>"
        "Continue on Telegram</a>"
        f"<a class=\"action whatsapp\" href=\"{html.escape(whatsapp_url, quote=True)}\">"
        "<svg viewBox=\"0 0 24 24\" aria-hidden=\"true\"><path fill=\"currentColor\" d=\"M12 2a9.8 9.8 0 0 0-8.5 14.7L2 22l5.5-1.4A10 10 0 1 0 12 2Zm0 18a8 8 0 0 1-4.1-1.1l-.3-.2-3.2.8.9-3.1-.2-.3A8 8 0 1 1 12 20Zm4.4-6c-.2-.1-1.5-.8-1.8-.9-.2-.1-.4-.1-.5.1l-.8 1c-.1.2-.3.2-.5.1a6.5 6.5 0 0 1-3.2-2.8c-.2-.3.2-.3.7-1.2.1-.2 0-.3 0-.4l-.8-1.9c-.2-.5-.4-.4-.5-.4h-.5c-.2 0-.4.1-.6.3-.2.2-.8.8-.8 2s.8 2.3.9 2.4a9 9 0 0 0 3.5 3.1c1.3.6 1.8.7 2.4.6.4-.1 1.5-.6 1.7-1.2.2-.6.2-1.1.1-1.2 0-.1-.2-.2-.4-.3Z\"/></svg>"
        "Continue on WhatsApp</a>"
        f"<p class=\"muted\">or message us on {phone_label}</p></section>"
        "<section class=\"steps\"><h2>How it works</h2><ol>"
        "<li><span class=\"step\">1</span>Pick your items</li>"
        "<li><span class=\"step\">2</span>Choose how to pay</li>"
        "<li><span class=\"step\">3</span>We prepare it</li></ol></section>"
        "<footer>Powered by Chowlin</footer></main>"
        "</body></html>"
    )
    return HTMLResponse(page, headers=no_store)

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
            <p>You can close this tab and return to the chat where you placed
               your order. Your payment is being confirmed there.</p>
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

    # Load channel and restaurant details for kitchen work and customer notification.
    order_result = supabase.table("orders")\
        .select("telegram_user_id, customer_contact, order_channel, restaurant_id, restaurants(kitchen_chat_id, whatsapp_phone_number_id, whatsapp_access_token)")\
        .eq("id", order_id).execute()

    order_data = order_result.data[0] if order_result.data else {}
    restaurant_id = order_data.get("restaurant_id")
    kitchen_chat_id = (order_data.get("restaurants") or {}).get("kitchen_chat_id")

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

    # Notify and receipt-deliver through the channel that created the order.
    try:
        await notify_order_customer(
            target_bot,
            order_data,
            f"✅ Your payment for order #{order_id.replace('-', '')[:4].upper()} has been confirmed. "
            "Your order has been sent to the kitchen. 🍳",
        )
        await send_order_receipt(target_bot, order_data, order_id)
    except Exception as e:
        logger.error(f"Failed to notify/send receipt for Paystack order {order_id}: {e}", exc_info=True)

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
