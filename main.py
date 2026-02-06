from fastapi import FastAPI, Request
from aiogram.types import Update
from bot import bot, dp, router
import os
import logging

FASTAPI_WEBHOOK_URL = os.getenv("FASTAPI_WEBHOOK_URL","https://n8n-atad.onrender.com/webhook/new-order")  # Replace with your actual webhook URL


app = FastAPI(title="Telegram Bot webservice", version= "1.0.0",
              description="Fastapi webservice to service Telegram bot through webhook")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    await bot.session.close()