# Telegram Restaurant Bot

A Telegram bot for restaurant ordering with payment integration.

## Features
- Menu browsing
- Cart management
- Multiple payment methods (Cash, Bank Transfer, Pay on Delivery, Paystack)
- Kitchen notifications
- n8n integration

## Paystack
- Paystack is added as a Telegram-only payment option.
- Add `PAYSTACK_SECRET_KEY` to your environment.
- One-time subaccount creation is available via `/admin/paystack/subaccount/{restaurant_id}`.
- The webhook endpoint is `/webhook/paystack` and verifies `x-paystack-signature`.

## Deployment
Deployed on Render with FastAPI webhook.