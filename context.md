# Chowlin Context — Complete AI Guide

## 🎯 Project Overview

**Chowlin** is a **multi-tenant Telegram-based food ordering SaaS** for Nigerian restaurants. It enables customers to scan QR codes and order food directly via Telegram, with restaurant owners managing orders, inventory, and subscriptions. The platform supports multiple order types (dine-in, delivery, pickup) and payment methods (cash, bank transfer, pay-on-delivery).

- **Owner**: Peter Bello (Petbell Integrated Services)
- **Current Status**: Live on Render (free tier), migrating to Docker VPS in Frankfurt
- **Domain**: chowlin.com.ng (planned, not yet registered as of June 2026)
- **Multi-tenant**: Each restaurant has isolated menus, orders, and kitchen groups
- **Integration**: Linked to n8n for notifications (Render free tier), Supabase for persistence

---

## 🏗️ Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| **Bot Framework** | aiogram 3.x (async) | Handles Telegram interactions asynchronously |
| **Web Server** | FastAPI | Receives Telegram webhook updates, serves API endpoints |
| **Database** | Supabase (PostgreSQL) | Same account as Bursara, separate schema/tables |
| **Async Tasks** | APScheduler (AsyncIOScheduler) | Scheduled reports, subscription checks (Lagos timezone) |
| **PDF Generation** | ReportLab | Generates receipts for orders |
| **QR Codes** | qrcode + Pillow | Generate public_code QR images for tables |
| **Notifications** | n8n | Sends order updates to Google Sheets (Render-specific) |
| **Containerization** | Docker + docker-compose | For VPS deployment |
| **Reverse Proxy** | Nginx | Routes chowlin.com.ng → localhost:8001 on VPS |

---

## 📊 Architecture Overview

```
┌─────────────────┐
│   Telegram      │
│   Customer      │
└────────┬────────┘
         │
         ▼ (POST /webhook)
    ┌─────────────────────────┐
    │   FastAPI (main.py)     │
    │   - Webhook endpoint    │
    │   - Scheduler startup   │
    └─────────────┬───────────┘
                  │
         ▼ (updates)
    ┌──────────────────────┐
    │  aiogram Dispatcher  │
    │  - Routes updates    │
    │  - Manages handlers  │
    │  - FSM states        │
    └──────────┬───────────┘
               │
         ▼ (queries)
    ┌──────────────────────┐
    │  Supabase Client     │
    │  - Restaurants       │
    │  - Orders            │
    │  - Menu items        │
    │  - Inventory         │
    └──────────┬───────────┘
               │
         ▼ (PostgreSQL)
    ┌──────────────────────┐
    │  Supabase Database   │
    │  - All persistent    │
    │    data              │
    └──────────────────────┘

APScheduler:
  ├─ Daily reports (9 PM WAT)
  ├─ Weekly reports (Monday 9 AM WAT)
  ├─ Subscription expiry checks
  └─ Subscription warnings (3 days before)
```

---

## 📁 Key Files & Their Responsibilities

| File | Purpose | Key Functions |
|------|---------|---|
| **main.py** | FastAPI app entry point | - Webhook endpoint at `/webhook`<br>- Scheduler initialization<br>- Startup/shutdown lifecycle<br>- n8n heartbeat (Render-only) |
| **bot.py** | ALL Telegram handlers | - `/start` handler (table lookup)<br>- Menu browsing (categories → items)<br>- Cart management<br>- Payment flows<br>- Kitchen order board<br>- FSM states for user flows |
| **reports.py** | Sales & analytics | - `generate_daily_report()` → revenue, payment methods, top items, inventory<br>- `generate_weekly_report()` → same, weekly timeframe |
| **receipt_generator.py** | PDF generation | - `generate_receipt_pdf()` → creates receipts for orders<br>- Formats order details, items, totals, payment info |
| **generate_qr_codes.py** | QR code images | - Creates public_code QR images for restaurant tables<br>- Used during setup |
| **generate_short_codes.py** | Short codes | - Helper for referral/promo codes |
| **docker-compose.yml** | Container orchestration | - Defines chowlin service (port 8001, health checks) |
| **Dockerfile** | Container image | - Python 3.11, installs requirements, runs FastAPI |

---

## 🗄️ Database Schema (Supabase PostgreSQL)

### Core Tables

#### `restaurants`
```sql
id (uuid, PK)
name (text)
manager_telegram_id (bigint) — manager's Telegram user ID
manager_name (text)
kitchen_chat_id (bigint) — Telegram group ID for kitchen notifications
phone (text)
bank_name, account_number, account_name (text) — for bank transfers
subscription_status (text: trialing | active | expired)
subscription_expires_at (timestamptz)
plan (text) — plan type (e.g., "starter", "professional")
onboarding_fee_paid (boolean)

-- Added in Migration 004 (Kitchen Board)
kitchen_board_message_id (bigint) — pinned order board message
kitchen_board_message_date (date) — when board was last updated
kitchen_board_pinned (boolean) — is board currently pinned?
kitchen_rush_alert_date (date) — when last rush alert was sent
```

#### `restaurant_tables`
```sql
id (uuid, PK)
restaurant_id (uuid, FK) → restaurants.id
table_number (integer | NULL) — NULL or 'EXTERNAL' = delivery/pickup
public_code (text, unique) — QR code identifier
is_active (boolean)

-- Added in Migration 002
menu_filter (text) — filter for menu items (optional)
```

#### `menu_categories`
```sql
id (uuid, PK)
restaurant_id (uuid, FK) → restaurants.id
name (text)
is_active (boolean)
display_order (integer)
```

#### `menu_items`
```sql
id (uuid, PK)
restaurant_id (uuid, FK) → restaurants.id
category_id (uuid, FK) → menu_categories.id
name (text)
description (text)
price (numeric)
is_available (boolean) — kitchen can toggle availability

-- Added in Migration 003 (Inventory)
inventory_count (integer) — qty on hand
restock_threshold (integer) — alert qty
track_inventory (boolean) — enable tracking for this item?
```

#### `orders`
```sql
id (uuid, PK)
restaurant_id (uuid, FK) → restaurants.id
table_id (uuid, FK) → restaurant_tables.id (NULL for delivery/pickup)
telegram_user_id (bigint) — customer's Telegram user ID
customer_name (text)
total_amount (numeric)
payment_method (text: cash | bank_transfer | pay_on_delivery)
payment_status (text: pending | confirmed | rejected)
order_status (text: pending | preparing | ready | collected | cancelled)
order_type (text: dine_in | delivery | pickup)
created_at (timestamptz)

-- Added in Migration 001 (Delivery)
delivery_address (text)
delivery_lat (numeric)
delivery_lon (numeric)

-- Added in Migration 003
inventory_deducted (boolean) — has inventory been deducted for this order?
```

#### `order_items`
```sql
id (uuid, PK)
order_id (uuid, FK) → orders.id
menu_item_id (uuid, FK) → menu_items.id
quantity (integer)
unit_price (numeric) — price at order time
subtotal (numeric) — qty × unit_price
```

#### `payments`
```sql
id (uuid, PK)
order_id (uuid, FK) → orders.id
restaurant_id (uuid, FK) → restaurants.id
amount (numeric)
provider (text) — payment gateway identifier
status (text)
provider_reference (text) — bank transfer proof file_id or reference
```

### Database Functions (PostgreSQL)

#### `deduct_order_inventory(p_order_id uuid)`
- **When**: Called after payment confirmed for orders with tracked items
- **What**: Deducts item quantities from `inventory_count`, marks `inventory_deducted = true`
- **Returns**: Low-stock items (below restock_threshold) for alerts
- **Migration**: 003_Add_Menu_Item_Inventory.sql

---

## 🔄 Data Flow & User Journeys

### 1. **Customer Order Flow**

```
1. Customer scans QR code (table public_code in TG message)
   ↓
2. /start handler receives public_code param
   ↓
3. Bot looks up restaurant_tables by public_code
   ├─ If not found → "Invalid QR code"
   ├─ If table_number is NULL/EXTERNAL → go to step 4a
   └─ If table_number exists → go to step 5
   ↓
4a. [DELIVERY/PICKUP] Bot asks: "Delivery or Pickup?"
   ├─ Delivery → Bot asks for delivery address (text or location pin)
   ├─ Pickup → Bot records order_type = "pickup"
   └─ → Go to step 5
   ↓
5. [BROWSE MENU] Bot shows restaurant menu categories
   ├─ Customer selects category
   ├─ Bot shows menu_items for that category
   ├─ Customer selects item → FSM state: waiting_for_quantity
   ├─ Customer enters qty → added to cart
   └─ Repeat until satisfied → go to step 6
   ↓
6. [REVIEW CART] Bot displays cart: items × qty, subtotal
   ├─ Customer can remove items, adjust qty, or proceed
   ├─ Proceed → FSM state: waiting_for_payment_method
   ├─ → Bot shows payment buttons:
   │  ├─ 💵 Cash Payment
   │  ├─ 🏦 Bank Transfer
   │  └─ 🚚 Pay on Delivery (delivery orders only)
   └─ → Go to step 7
   ↓
7. [PAYMENT SELECTION]
   ├─ CASH → Order created, payment_status = "confirmed" → step 8
   ├─ PAY_ON_DELIVERY → Order created, payment_status = "confirmed" → step 8
   └─ BANK_TRANSFER → FSM state: waiting_for_payment_proof
      └─ Customer uploads screenshot/proof → step 7a
      ↓
7a. [BANK TRANSFER PROOF]
    ├─ Bot saves file_id as payments.provider_reference
    ├─ Order created with payment_status = "pending"
    ├─ Kitchen sees order with photo + "Approve/Reject" buttons
    └─ On Approve → payment_status = "confirmed" → step 8
    └─ On Reject → payment_status = "rejected" → customer notified
   ↓
8. [ORDER CREATED & INVENTORY DEDUCTED]
    ├─ Order inserted with all items
    ├─ deduct_order_inventory() called
    ├─ Items marked unavailable if inventory_count → 0
    ├─ Low-stock alerts sent to kitchen & manager
    └─ → Step 9
   ↓
9. [KITCHEN NOTIFICATION]
    ├─ Order formatted and sent to kitchen_chat_id (Telegram group)
    ├─ Buttons: "Mark as Ready" (cash/delivery) or "Approve/Reject" (bank transfer)
    ├─ Kitchen staff marks ready when food is done
    └─ Customer notified: "Your order is ready!"
   ↓
10. [CUSTOMER PICKUP/DELIVERY]
    ├─ Dine-in: Customer arrives at table, eats
    ├─ Pickup: Customer collects at restaurant counter
    └─ Delivery: Driver delivers to address (future: delivery tracking)
    └─ Receipt sent to customer (PDF or formatted message)
```

### 2. **Kitchen/Manager Flow**

```
[KITCHEN GROUP (kitchen_chat_id)]
├─ Receives new orders with items, location, payment proof (if bank transfer)
├─ Buttons: "Mark as Ready", "Approve", "Reject"
├─ /menu command → shows all items with availability toggle
├─ /restock command → update inventory counts and thresholds
└─ Sees live order board (pinned message, updated in real-time)

[MANAGER DASHBOARD (via DM with bot)]
├─ /daily_report → yesterday's sales breakdown
├─ /weekly_report → last 7 days
├─ /monthly_report → last 30 days
├─ /register_manager → shows their Telegram ID for admin setup
├─ Subscription expiry warnings (auto-sent 3 days before expiry)
└─ Low-stock alerts from kitchen
```

---

## 🎯 Order Types & Payment Methods

### Order Types
| Type | Triggered By | Table Logic | Location |
|------|-------------|------------|----------|
| **Dine-in** | Scanning QR on physical table | table_number > 0 | Restaurant |
| **Delivery** | Scanning EXTERNAL QR + selecting Delivery | table_number = NULL | Customer address |
| **Pickup** | Scanning EXTERNAL QR + selecting Pickup | table_number = NULL | Restaurant |

### Payment Methods
| Method | Flow | Payment Status | Notes |
|--------|------|---|---|
| **Cash** | Customer pays at restaurant | confirmed immediately | Kitchen notified right away |
| **Bank Transfer** | Customer uploads proof → kitchen approves | pending → confirmed | High-touch, prevents fraud |
| **Pay on Delivery** | Driver collects payment | confirmed immediately | Only for delivery orders |

---

## 📅 Scheduled Jobs (APScheduler)

| Job | Trigger | Timezone | What It Does |
|-----|---------|----------|------------|
| **send_daily_reports** | Daily 9:00 PM | Lagos (WAT) | Emails daily sales to all managers |
| **send_weekly_reports** | Monday 9:00 AM | Lagos (WAT) | Emails weekly sales to all managers |
| **expire_subscriptions** | Daily 12:05 AM | Lagos (WAT) | Marks trialing/active → expired if past expiry_date |
| **notify_expiring_subscriptions** | Daily 9:00 AM | Lagos (WAT) | Warns managers 3 days before expiry |

---

## 🔐 Subscription System

- **Status**: `trialing` → `active` → `expired`
- **Check**: On every `/start`, verify restaurant.subscription_status == "active" and subscription_expires_at > now()
- **Expired Access**: Customers see "This restaurant is not currently accepting orders"
- **Manager Activation**: `/activate <restaurant_id> <days>` extends subscription_expires_at
- **Auto-Warnings**: Sent 3 days before expiry via scheduled job

---

## 🛒 Advanced Features

### Reorder / Order History
- `/history` command → shows customer's last 5 orders
- Each order has "Reorder" button
- Reorder loads previous cart items (skips if now unavailable)
- Merges into active session if same restaurant, or saves as pending

### Inventory Management
- Items can be marked for tracking: `track_inventory = true`
- Kitchen uses `/restock` to update `inventory_count` and `restock_threshold`
- Low-stock alerts sent to kitchen & manager when count ≤ threshold
- After payment confirmed, `deduct_order_inventory()` auto-deducts quantities

### Kitchen Order Board
- Pinned message in kitchen_chat_id showing live order summary
- Sections: 🔴 PENDING, 🟡 PREPARING, ✅ READY
- Auto-updates every time order status changes
- Rush hour alerts (🔥 if pending > threshold) sent to manager

### Menu Filtering
- `restaurant_tables.menu_filter` allows per-table menu restrictions
- E.g., table_number = 5 might filter out alcohol items for minors
- Applied at menu browse stage

---

## 🚀 Deployment Architecture

### Current (Render Free Tier)
- Single dyno running FastAPI + aiogram
- n8n pinged every 10 min to stay alive (Render kills idle services)
- Domain: `telegram-n8n-restaurant-bot.onrender.com`
- Custom domain: chowlin.com.ng (planned)

### Target (VPS + Docker)
- Docker container on Frankfurt VPS (same as Bursara)
- Port 8001 (internal) exposed on 127.0.0.1
- Nginx reverse proxy: `chowlin.com.ng` → `http://127.0.0.1:8001`
- SSL via Let's Encrypt
- Health checks every 30s
- Restart policy: unless-stopped

### Docker Setup
```yaml
chowlin:
  build: ./chowlin
  container_name: chowlin_bot
  restart: unless-stopped
  env_file: .env
  ports:
    - "127.0.0.1:8001:8001"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8001/"]
    interval: 30s
    retries: 3
```

### Nginx Configuration
```nginx
server {
    server_name chowlin.com.ng www.chowlin.com.ng;
    listen 443 ssl;
    ssl_certificate /etc/letsencrypt/live/chowlin.com.ng/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chowlin.com.ng/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

---

## 🔧 Environment Variables

```bash
# Telegram
TOKEN=<bot_token>

# Supabase
SUPABASE_URL=<project_url>
SUPABASE_SERVICE_KEY=<service_role_key>

# FastAPI
FASTAPI_WEBHOOK_URL=https://chowlin.com.ng

# n8n (Render-specific, remove for VPS)
N8N_WEBHOOK_URL=<n8n_new_order_webhook>
N8N_UPDATE_WEBHOOK_URL=<n8n_update_sheet_webhook>
N8N_HEARTBEAT_URL=<n8n_heartbeat_url>

# Admin
ADMIN_TELEGRAM_ID=<admin_user_id>

# Thresholds
RUSH_HOUR_PENDING_THRESHOLD=5
```

---

## ⚠️ Known Issues & Technical Debt

### Issues
1. **Duplicate Scheduler**: `bot.py` defines scheduler but `main.py` also defines one. Only main.py's runs; bot.py's should be deleted.
2. **Duplicate "/" Route**: Two `@app.get("/")` in main.py. Remove first (health check), keep second (webhook info).
3. **n8n Heartbeat**: `ping_n8n_periodically()` only needed on Render. Remove for VPS.
4. **Domain Not Registered**: chowlin.com.ng still not registered as of June 2026.

### TODO
- [ ] Register chowlin.com.ng domain
- [ ] Update FASTAPI_WEBHOOK_URL to https://chowlin.com.ng post-registration
- [ ] Deploy to VPS Docker setup
- [ ] Remove Render-specific code (n8n heartbeat, ping logic)
- [ ] Implement location sharing for delivery (Telegram native + OpenStreetMap reverse geocoding)
- [ ] Add delivery tracking UI
- [ ] Implement multi-language support (English + Yoruba + Igbo)
- [ ] Add customer refund/dispute workflow
- [ ] Add promo codes / referral system
- [ ] Implement subscription plan tiers (Starter, Professional, Enterprise)
- [ ] Add analytics dashboard for managers

---

## 🎓 Understanding the Flow: AI Quick Reference

### When a customer scans a QR:
1. Telegram sends `/start public_code` to bot
2. bot.py looks up restaurant_tables by public_code
3. Verifies restaurant subscription is active
4. Loads menu or asks delivery/pickup
5. Customer adds items to cart
6. Selects payment method
7. Order created, inventory deducted
8. Kitchen notified via kitchen_chat_id group

### When kitchen marks order ready:
1. Kitchen presses "Mark as Ready" button in Telegram
2. bot.py updates orders.order_status = "ready"
3. APScheduler sends message to customer
4. Customer collects food

### When daily report runs (9 PM Lagos time):
1. APScheduler triggers send_daily_reports()
2. Queries all active restaurants with managers
3. Fetches orders from start-of-day to end-of-day
4. Calculates revenue, payment breakdown, top items
5. Sends formatted report to manager_telegram_id

---

## 🤝 Multi-Tenant Isolation

Each restaurant is isolated by:
- `restaurant_id` FK on all tables (restaurants, menu_categories, menu_items, orders)
- Each restaurant has its own `kitchen_chat_id` (separate Telegram group)
- Each restaurant has its own `manager_telegram_id` (separate manager)
- QR codes are unique by `public_code` (per table)
- Reports are per-restaurant (by restaurant_id filter)

---

## 📞 Integration Points

- **Telegram Bot API**: Webhook mode (POST /webhook with Telegram updates)
- **Supabase PostgreSQL**: REST API via Python client, RPC for stored procedures
- **n8n**: Webhooks for order notifications + Google Sheets updates (Render-only)
- **OpenStreetMap Nominatim**: Planned for delivery address reverse geocoding

---

## 🎯 For New AI Assistants

When working on Chowlin:
1. **Always check subscription_status** before allowing orders
2. **Verify restaurant exists** before any table lookup
3. **Deduct inventory immediately** after payment confirmed
4. **Send alerts to both kitchen and manager** for low stock
5. **Use Lagos timezone (Africa/Lagos)** for all timestamps
6. **Query orders with order_status filters** (pending/preparing/ready/collected)
7. **Test with FSM states** for multi-step flows (waiting_for_quantity, waiting_for_address, etc.)
8. **Remember: webhook mode, not polling** — all updates pushed by Telegram
9. **Supabase FKs must match**: restaurant_id, table_id, category_id, etc.
10. **n8n removal is planned** — remove hardcoded webhook calls before VPS deployment

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