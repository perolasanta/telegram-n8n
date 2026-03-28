# Chowlin Context

## Overview
Chowlin is a multi-tenant Telegram bot food ordering platform for Nigerian restaurants.
Currently hosted on Render (free tier). Moving to a VPS (same Frankfurt server as Bursara).
Domain: chowlin.com.ng (not yet registered as of March 2026).
Owner: Peter Bello (Petbell Integrated Services).

## Tech Stack
- **Bot framework**: aiogram 3.x (async)
- **Web server**: FastAPI (webhook mode — NOT polling)
- **Database**: Supabase (Postgres) — same Supabase account as Bursara but different schema/tables
- **Scheduler**: APScheduler (AsyncIOScheduler)
- **PDF receipts**: Custom receipt_generator.py
- **Reports**: reports.py (daily + weekly)
- **Deployment**: Render free tier → migrating to Docker on VPS

## Architecture
- Bot runs in webhook mode via FastAPI
- Telegram sends updates to POST /webhook
- FastAPI feeds update to aiogram dispatcher
- n8n was used for notifications (on Render free tier, being kept alive with ping)
- Supabase handles all persistent data

## Entry Points
- `main.py` — FastAPI app, webhook endpoint, scheduler startup
- `bot.py` — aiogram Bot, Dispatcher, all handlers, Supabase client

## Key Files
- `main.py` — FastAPI app setup, webhook, scheduler, startup/shutdown
- `bot.py` — ALL bot handlers (commands, callbacks, FSM states)
- `reports.py` — generate_daily_report(), generate_weekly_report()
- `receipt_generator.py` — generate_receipt_pdf() → returns file path
- `.env` — environment variables (see below)

## Environment Variables
```
TOKEN=<telegram bot token>
SUPABASE_URL=<supabase project url>
SUPABASE_SERVICE_KEY=<supabase service role key>
FASTAPI_WEBHOOK_URL=https://chowlin.com.ng  # will change on VPS
N8N_WEBHOOK_URL=<n8n new order webhook>
N8N_UPDATE_WEBHOOK_URL=<n8n update sheet webhook>
N8N_HEARTBEAT_URL=<n8n heartbeat — only needed on Render free tier>
```

## Database Tables (Supabase)
- **restaurants** — id, name, kitchen_chat_id, manager_telegram_id, manager_name, phone, bank_name, account_number, account_name, subscription_status, subscription_expires_at, plan, onboarding_fee_paid
- **restaurant_tables** — id, restaurant_id, table_number, public_code, is_active (table_number = NULL or 'EXTERNAL' means delivery/pickup)
- **menu_categories** — id, restaurant_id, name, is_active, display_order
- **menu_items** — id, category_id, restaurant_id, name, price, is_available
- **orders** — id, restaurant_id, table_id, telegram_user_id, customer_name, total_amount, payment_method, payment_status, order_status, order_type (dine_in/delivery/pickup), delivery_address, created_at
- **order_items** — id, order_id, menu_item_id, quantity, unit_price, subtotal
- **payments** — id, order_id, restaurant_id, amount, provider, status, provider_reference (file_id for bank transfer proof)

## Order Types
1. **Dine-in** — QR code on table has table_number → customer scans → orders for that table
2. **Delivery** — QR code is EXTERNAL type → customer chooses delivery → enters address
3. **Pickup** — QR code is EXTERNAL type → customer chooses pickup → collects at restaurant

## Payment Methods
1. **Cash Payment** — order confirmed immediately, kitchen notified
2. **Pay on Delivery** — only for delivery orders, kitchen notified
3. **Bank Transfer** — customer uploads payment screenshot → kitchen sees photo + approve/reject buttons → on approval, customer notified + receipt sent

## Order Flow
1. Customer scans QR code → /start with public_code param
2. Bot looks up restaurant_tables by public_code
3. Checks restaurant subscription is active
4. Dine-in: shows menu categories directly
5. External: asks Delivery or Pickup first
6. Customer browses categories → items → quantity → cart
7. Confirm order → select payment method
8. Order created in DB → sent to kitchen_chat_id (Telegram group)
9. Kitchen marks ready → customer notified
10. Bank transfer: kitchen approves/rejects payment proof

## Kitchen Features
- Kitchen receives orders in a Telegram group (kitchen_chat_id)
- Bank transfer orders: photo + Confirm/Reject buttons
- Cash/delivery orders: "Mark as Ready" button
- /menu command in kitchen group → toggle item availability on/off
- Kitchen can mark items unavailable (e.g. sold out)

## Scheduled Jobs (APScheduler)
- Daily reports: 10:00 PM WAT → sent to manager_telegram_id
- Weekly reports: Monday 9:00 AM WAT → sent to manager_telegram_id
- Subscription expiry check: 12:05 AM WAT daily
- Expiry warnings: 9:00 AM WAT daily (3 days before expiry)

## Manager Features
- /daily_report — manual daily sales report
- /weekly_report — manual weekly report
- /monthly_report — last 30 days report
- /register_manager — shows their Telegram ID for admin to register them
- /activate <restaurant_id> <days> — extend subscription

## Subscription System
- Restaurants have subscription_status: trialing / active / expired
- subscription_expires_at controls access
- is_subscription_active() checked on every /start
- Expired restaurants → customers see "subscription inactive" message

## Reorder Feature
- /history shows last 5 orders with "Reorder" button per order
- Reorder loads previous cart items (skips unavailable items)
- If same restaurant session active → merges into current cart
- If different restaurant → warns user to /cancel first
- If no active session → saves as pending_reorder, loads when QR scanned

## Current Issues / TODO
- [ ] Move from Render to VPS (Dockerize)
- [ ] Add location sharing for delivery orders (Telegram native location → reverse geocode)
- [ ] Remove n8n heartbeat ping (not needed on VPS, n8n will be local)
- [ ] Update FASTAPI_WEBHOOK_URL to chowlin.com.ng after domain setup
- [ ] Register chowlin.com.ng domain
- [ ] Remove duplicate scheduler code (scheduler defined in both main.py and bot.py — only main.py should have it)
- [ ] Two duplicate health check routes on "/" in main.py — remove one

## Known Code Issues
1. **Duplicate scheduler** — bot.py imports and defines scheduler but main.py also defines one. Only main.py scheduler should run. bot.py scheduler should be removed.
2. **Duplicate "/" route** in main.py — two @app.get("/") decorated functions. Remove the first one (health check), keep the second (root with webhook info).
3. **n8n heartbeat** — ping_n8n_periodically() is only needed on Render free tier. Remove on VPS since n8n will run as a local Docker container.
4. **No Dockerfile yet** — needs to be created for VPS deployment.
5. **No docker-compose entry yet** — needs to be added to Bursara's docker-compose.yml.

## VPS Deployment Plan
The bot will run alongside Bursara on the same Frankfurt VPS.

### Docker service to add to docker-compose.yml:
```yaml
chowlin:
  build: ./chowlin
  container_name: chowlin_bot
  restart: unless-stopped
  env_file: ./chowlin/.env
  ports:
    - "127.0.0.1:8001:8001"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8001/"]
    interval: 30s
    timeout: 10s
    retries: 3
```

### NGINX block to add to bursara.conf:
```nginx
server {
    server_name chowlin.com.ng www.chowlin.com.ng;

    location / {
        proxy_pass         http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }

    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/chowlin.com.ng/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chowlin.com.ng/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
}

server {
    if ($host = chowlin.com.ng) { return 301 https://$host$request_uri; }
    if ($host = www.chowlin.com.ng) { return 301 https://$host$request_uri; }
    listen 80;
    server_name chowlin.com.ng www.chowlin.com.ng;
    return 404;
}
```

## Location Feature (Planned)
Telegram supports native location sharing:
- Customer taps paperclip → Location → Share current location
- Bot receives message.location with latitude and longitude
- Reverse geocode using OpenStreetMap Nominatim (free):
  GET https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json
- Returns human-readable address
- Store as delivery_address in orders table
- Add new FSM state: waiting_for_location
- Allow both location pin AND typed address

## Pitching Together with Bursara
- Bursara: school fee management SaaS
- Chowlin: restaurant ordering SaaS
- Both under Petbell Integrated Services
- Both on same VPS, same Supabase account
- Demonstrates ability to build and run multiple SaaS products
- Target: school canteens could use both products together