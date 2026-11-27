# Performance audit

Read-only source audit, 2026-11-27. `scripts/load_test_webhooks.py` must only
target a TEST restaurant with a dummy WhatsApp token; never use it on production,
because synthetic `Hi` messages may trigger outbound WhatsApp calls.

## Summary

- `bot.py:61` creates the synchronous Supabase `Client`, yet 174 `.execute()` calls run directly in async code.
- All four webhook routes complete database and outbound work before returning 200; this invites Meta/Paystack retries under load.
- The single process owns APScheduler, Telegram in-memory FSM, and delivery-bot globals; scaling replicas is unsafe.
- WhatsApp session read-modify-write and kitchen-board refreshes repeat hot work.
- Reports and ReportLab receipt generation execute on the event loop.

## Findings ranked High

### H1 — synchronous Supabase I/O blocks the event loop

**Locations:** `bot.py:17,61`; hot calls include `whatsapp.py:823,853,883`,
`bot.py:2160,2269-2328`, and `main.py:579-645`. Static count: 108 in `bot.py`,
27 in `main.py`, 29 in `whatsapp.py`, 4 in `whatsapp_state.py`, and 6 in
`reports.py` (complete inventory below).

**Why under load:** each network-bound `.execute()` prevents the sole event loop
from acknowledging unrelated webhooks. **Proposed fix:** async repository/client,
or bounded `to_thread` as an interim measure. **Effort:** L. **Risk:** M.

### H2 — webhook acknowledgement is coupled to slow business work

**Locations:** `main.py:467-476,486-493,510-552,818-833`; Paystack release
continues at `main.py:555-684`.

**Why under load:** no `BackgroundTasks`/queue is used. `/webhook` and delivery
wait for `dp.feed_update`; WhatsApp waits for all state/DB/Meta work; Paystack
waits for kitchen notification, inventory, customer notification and receipt.
Worst-case time is unbounded for synchronous DB and Telegram/Meta work; explicit
upstream maxima include 15 s Paystack verification (`main.py:565`) and 20 s
refund/charge calls (`main.py:740`, `paystack_transfer.py:177`), plus a 15 s
charge reconciliation. **Proposed fix:** durable inbox/outbox then immediate 200.
**Effort:** L. **Risk:** H.

### H3 — replica scaling duplicates jobs and loses/diverges process state

**Locations:** one bare Uvicorn worker in `Dockerfile:14`; one compose service
`docker-compose.yml:1-19`; every process starts scheduler `main.py:874-916`;
global delivery-bot map `bot.py:136-151`; default in-memory aiogram FSM
`bot.py:55-59`.

**Why under load:** replicas duplicate daily/weekly/expiry jobs and webhook
lifecycle, have divergent bot maps, and lose Telegram cart/FSM state on restart.
`max_instances=1` is process-local. **Proposed fix:** one scheduler worker with
distributed lock, durable queue/outbox, Redis/database FSM, shared bot config.
**Effort:** L. **Risk:** H.

### H4 — several hot lookup predicates lack migration-proven indexes

**Locations:** restaurant phone lookup `whatsapp.py:853-858`; WhatsApp recent
orders `whatsapp.py:2014-2032`; Paystack references `main.py:531,579,719,775`;
board queues `bot.py:1107-1113,1200`; active table `main.py:112-114`.

**Why under load:** scans rise with all tenants/orders/payments. Migrations only
explicitly create modifier and session indexes (`migrations/009:65-67`,
`010:45-46`); sessions and processed messages also have primary keys. **Proposed
fix:** apply the reviewed SQL below after checking production schema/plans.
**Effort:** S/M. **Risk:** L.

### H5 — kitchen board rebuilds and Telegram edits are unthrottled

**Locations:** `bot.py:1098,1199-1260`; callers `bot.py:2476,3203,3252,3289,3377,4500`.

**Why under load:** each order event rebuilds the board and edits one message;
bursts produce competing edits/429s. Any edit failure attempts a new board.
No `RetryAfter` handling exists. **Proposed fix:** per-restaurant debounced,
serialized refresh with explicit 429 retry. **Effort:** M. **Risk:** M.

## Findings ranked Medium

### M1 — WhatsApp sessions repeatedly read then rewrite state

**Locations:** ingress session read `whatsapp.py:883-885`; `get_data`
`whatsapp_state.py:8-11`; `update_data` read/upsert `:14-21`; transfer flow
`whatsapp.py:2441-2466`.

**Why under load:** `Hi` is three DB calls (dedup, restaurant, session); checkout
adds repeated session reads/writes and has a lost-update race. **Proposed fix:**
atomic state/data RPC/upsert, loaded once per message. **Effort:** M. **Risk:** M.

### M2 — ReportLab, PDF file I/O, and reports run on event loop

**Locations:** `receipt_generator.py:28-142`; synchronous `open` at
`whatsapp.py:786`; receipt paths `bot.py:1303-1365`, `main.py:674-681`; reports
`reports.py:85-383` and scheduler `main.py:291-400`.

**Why under load:** CPU/files block acknowledgements. **Proposed fix:** bounded
worker process/thread pool and aggregate SQL reports. **Effort:** M. **Risk:** M.

### M3 — new outbound clients and missing explicit timeouts

**Locations:** fresh Meta `httpx.AsyncClient()` at `whatsapp.py:338,534,586,620,691,2470`; two media requests use defaults `:2470-2475`; n8n creates fresh
unbounded aiohttp sessions `bot.py:927,967,1007` and `main.py:446-453`.

**Why under load:** connection setup/unlimited waits exhaust capacity. **Proposed
fix:** lifespan-shared clients, connection limits and explicit connect/read/write
timeouts. **Effort:** M. **Risk:** M.

### M4 — scheduled work is sequential and only daily isolates each tenant failure

**Locations:** daily `main.py:291-371`, weekly `:373-400`, subscription jobs
`:403-440`, transfer expiry `:772-792`.

**Why under load:** at 100 restaurants daily is one listing plus ~4 DB calls and
one Telegram send per restaurant, serially; weekly is ~3 DB calls plus send.
With 5,000 open orders, expiry/confirmation and nested report payloads can exceed
the five-minute cadence. Daily catches per restaurant; a weekly generation error
escapes to its broad outer catch and ends the rest of that run. **Proposed fix:**
paginate, aggregate, bounded concurrency, worker ownership. **Effort:** M/L.
**Risk:** M.

### M5 — broad/nested selects amplify hot payloads

**Locations:** `bot.py:522,540,1276`; `reports.py:105,204,292` use `*` and nested
items; board loads related rows `bot.py:1107-1121`.

**Why under load:** PostgREST serialization and Python memory rise with catalogue
and order history size. **Proposed fix:** select rendered columns and aggregate
reports server-side. **Effort:** S/M. **Risk:** L/M.

## Findings ranked Low

### L1 — processed-message retention and global bot-map lifecycle

**Locations:** dedup insert `whatsapp.py:823`; no cleanup noted at
`migrations/011:12-14`; global map reload `main.py:837-867`.

**Why under load:** dedup table grows indefinitely; bot map is stale until admin
reload. **Proposed fix:** retention/pruning plus controlled refresh. **Effort:** S.
**Risk:** L.

### L2 — outbound failures are broadly swallowed; no Telegram 429 policy

**Locations:** `bot.py:1223-1260`, `main.py:653-681`, `whatsapp.py:907-920`.

**Why under load:** most failures log and continue; no `TelegramRetryAfter` use
was found. **Proposed fix:** central retry/backoff/dead-letter policy. **Effort:**
M. **Risk:** M.

## Proposed index SQL

Existing: `whatsapp_sessions(phone_number, restaurant_id)` (`migrations/010:45`)
and primary keys on that table and `whatsapp_processed_messages.message_id`.
The base schema may contain additional indexes not visible in migrations; check
production plans before applying.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_restaurants_whatsapp_phone_number_id ON public.restaurants (whatsapp_phone_number_id) WHERE whatsapp_phone_number_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_whatsapp_customer_recent ON public.orders (restaurant_id, customer_contact, created_at DESC) WHERE order_channel = 'whatsapp';
CREATE INDEX IF NOT EXISTS idx_orders_telegram_customer_recent ON public.orders (telegram_user_id, created_at DESC) WHERE order_channel = 'telegram';
CREATE INDEX IF NOT EXISTS idx_orders_restaurant_status_created ON public.orders (restaurant_id, order_status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_paystack_reference ON public.payments (paystack_reference) WHERE paystack_reference IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_transfer_expiry ON public.payments (expires_at) WHERE provider = 'Paystack' AND channel = 'transfer' AND status = 'pending';
CREATE INDEX IF NOT EXISTS idx_restaurant_tables_public_code_active ON public.restaurant_tables (public_code) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_menu_item_catalog_map_restaurant_retailer ON public.menu_item_catalog_map (restaurant_id, catalog_retailer_id);
CREATE INDEX IF NOT EXISTS idx_delivery_zones_restaurant_active_display ON public.delivery_zones (restaurant_id, is_active, display_order);
CREATE INDEX IF NOT EXISTS idx_whatsapp_processed_messages_processed_at ON public.whatsapp_processed_messages (processed_at);
```

## Must change before running more than one container

1. Move APScheduler to a single worker with a distributed leader lock.
2. Use Redis/database FSM and shared/reloadable delivery-bot configuration.
3. Use durable inbox/outbox jobs plus per-order idempotency/locking for webhook,
   notification, receipt, board, and expiry side effects.
4. Ensure only the chosen process owns each Telegram webhook/session lifecycle.

## Security observations

- Meta POST does not verify `X-Hub-Signature-256` (`main.py:486-493`). Add a
  `WHATSAPP_APP_SECRET` environment variable and HMAC-verify the raw body.
- Telegram registration lacks a secret token and `/webhook` does not verify
  `X-Telegram-Bot-Api-Secret-Token` (`main.py:467-476,876`).
- Admin endpoints use only `X-Admin-Key`, without rate-limit/IP policy visible
  (`main.py:796-816,837-867`).
- Telegram, Meta, Paystack, and delivery POST endpoints can trigger outbound
  messages; only Paystack HMAC is verified in this code.

## Recommended order of fixes

1. Add Meta/Telegram verification and outbound/inbound timeout policy.
2. Apply reviewed hot-path indexes after `EXPLAIN` on production-like data.
3. Convert Supabase calls to asynchronous/bounded execution; consolidate session writes.
4. Debounce board refreshes and handle Telegram `RetryAfter`.
5. Put receipts, reports, and webhook side effects behind durable workers.
6. Split scheduler ownership and persist FSM before replicas.

## Complete blocking `.execute()` inventory

All are in `async def` without `to_thread`/executor: **174** total. No audited
runtime `requests` or `time.sleep` calls were found. QR generation is offline,
not imported by runtime. Synchronous file/CPU work is ReportLab at
`receipt_generator.py:28-142` and PDF `open` at `whatsapp.py:786`.

- `bot.py` (108): 82, 96, 144, 193, 211, 224, 250, 277, 311, 522, 540, 555, 586, 624, 655, 671, 698, 719, 948, 982, 1025, 1092, 1107, 1113, 1191, 1200, 1249, 1276, 1282, 1303, 1449, 1475, 1513, 1545, 1823, 1868, 1914, 1955, 1986, 2055, 2061, 2067, 2087, 2160, 2269, 2308, 2324, 2328, 2468, 2484, 2524, 2538, 2556, 2564, 2594, 2743, 2777, 2813, 2848, 2886, 2893, 2904, 2914, 2955, 3000, 3152, 3158, 3164, 3214, 3220, 3226, 3260, 3275, 3315, 3331, 3393, 3465, 3488, 3539, 3670, 3687, 3705, 3733, 3783, 3849, 3889, 3900, 3923, 3958, 3970, 3985, 4027, 4044, 4061, 4067, 4093, 4149, 4158, 4172, 4231, 4268, 4312, 4319, 4426, 4460, 4468, 4474, 4491.
- `main.py` (27): 112, 307, 376, 405, 417, 531, 536, 537, 579, 588, 597, 599, 633, 638, 639, 645, 689, 696, 700, 719, 725, 753, 763, 767, 775, 782, 843.
- `whatsapp.py` (29): 166, 568, 662, 752, 823, 853, 883, 1155, 1207, 1218, 1306, 1378, 1459, 1476, 1652, 1703, 1903, 2007, 2016, 2027, 2032, 2052, 2084, 2224, 2232, 2343, 2374, 2419, 2454.
- `whatsapp_state.py` (4): 9, 17, 25, 32. `reports.py` (6): 103, 111, 202, 210, 290, 298. `paystack_transfer.py`: 0.

## What code alone cannot determine

Production base-schema indexes/query plans, Supabase pool limits and latency,
external API limits/latency, proxy timeouts, deployed replica count, and whether
other infrastructure already owns scheduling are not knowable from this source.
