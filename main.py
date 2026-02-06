from fastapi import FastAPI, Request
from aiogram.types import Update
from bot import bot, dp, router
import os
import logging
import asyncio
import aiohttp

FASTAPI_WEBHOOK_URL = os.getenv("FASTAPI_WEBHOOK_URL","https://telegram-n8n-restaurant-bot.onrender.com")  # Replace with your actual webhook URL

# URL of  n8n Heartbeat Webhook
N8N_HEARTBEAT_URL=os.getenv("N8N_HEARTBEAT_URL", "https://n8n-atad.onrender.com/webhook/heartbeat")


app = FastAPI(title="Telegram Bot webservice", version= "1.0.0",
              description="Fastapi webservice to service Telegram bot through webhook")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)




async def ping_n8n_periodically():
    """Background task to keep n8n awake on Render's free tier."""
    async with aiohttp.ClientSession() as session:
        while True:

            try:
                    
                    async with session.get(N8N_HEARTBEAT_URL) as response:
                        print(f"Pinged n8n: {response.status}")
            except Exception as e:
                print(f"Ping failed: {e}")
            
            # Ping every 10 minutes to stay within Render's 15-min window
            await asyncio.sleep(600)


@app.post("/webhook")
async def webhook(request:Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update (bot=bot, update=update)
    return {"ok": True}

@app.get("/")
async def health():
    """health check for Render"""
    return {"status":"ok"}

@app.get("/")
async def root():
    """Health check endpoint"""
    return {"status": f"Bot is running", "webhook": f"{FASTAPI_WEBHOOK_URL}/webhook"}

@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(f"{FASTAPI_WEBHOOK_URL}/webhook")
    print (f"Webhook set to {FASTAPI_WEBHOOK_URL}/webhook")
    asyncio.create_task(ping_n8n_periodically())

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    await bot.session.close()
