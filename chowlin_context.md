# Chowlin — AI Engineering Context

## Purpose

Use this file as the working context for an AI making changes to this repository. It describes the code that is present in the repository, not an aspirational product specification. Verify behavior in the referenced files before making a non-trivial change.

## Product and boundaries

Chowlin is a multi-tenant Nigerian restaurant-ordering service. Customers order through:

- Telegram: QR/table ordering and optional restaurant-specific delivery bots.
- WhatsApp: Meta catalog cart submission, followed by delivery address and payment selection.

Restaurant data is isolated with `restaurant_id`. Supabase/PostgreSQL is the persistent system of record. Telegram kitchen groups receive operational order notifications for orders from either channel.

The application is an async Python service. It is deployed behind FastAPI webhooks rather than Telegram polling.

## Technology

| Area | Implementation |
|---|---|
| HTTP application | FastAPI in `main.py` |
| Telegram | aiogram 3 dispatcher and FSM in `bot.py` |
| WhatsApp | Meta Graph API, `httpx`, and Supabase-backed state in `whatsapp.py` / `whatsapp_state.py` |
| Database | Supabase Python client against PostgreSQL |
| Payments | Manual bank-transfer proof, cash/pay-on-delivery, and Telegram-only Paystack |
| Jobs | APScheduler using the `Africa/Lagos` timezone |
| Reporting | Text sales/inventory reports in `reports.py` |
| Receipts | ReportLab PDF generation in `receipt_generator.py` |
| Deployment | Docker + Compose; Nginx example config is in `nginx/` |

## Entry points and request flow

`main.py` owns the FastAPI app and routes:

| Route | Function |
|---|---|
| `POST /webhook` | Feeds updates from the shared Telegram bot into `dp`. |
| `GET /webhook/whatsapp` | Meta webhook verification using `WHATSAPP_VERIFY_TOKEN`. |
| `POST /webhook/whatsapp` | Passes Meta events to `handle_whatsapp_webhook`. Status-only events are ignored. |
| `POST /webhook/paystack` | Verifies Paystack HMAC, confirms an order idempotently, notifies the kitchen, deducts stock, and sends a Telegram receipt. |
| `GET /paystack/callback` | Simple browser confirmation page; it does not confirm a payment. |
| `POST /webhook/delivery/{restaurant_id}` | Routes Telegram updates to a restaurant-specific delivery bot. |
| `POST /admin/paystack/subaccount/{restaurant_id}` | Creates a Paystack subaccount; requires `X-Admin-Key`. |
| `POST /admin/reload-delivery-bots` | Loads/removes dedicated delivery bots; requires `X-Admin-Key`. |
| `GET /` | Health response. |

On startup the service sets webhooks, loads delivery bots, starts the scheduler, and starts an n8n heartbeat task. Scheduled work sends daily reports at 21:00 WAT, weekly reports Monday 08:00 WAT, expires subscriptions shortly after midnight, and sends expiry warnings at 09:00 WAT.

## Repository map

| File | Responsibility |
|---|---|
| `bot.py` | Shared Supabase client, aiogram dispatcher, Telegram customer flow, kitchen actions, inventory, payment, Paystack helpers, reports, and delivery-bot support. This is the largest and most coupled module. |
| `main.py` | FastAPI lifecycle, webhook endpoints, scheduler, Paystack webhook handling. |
| `whatsapp.py` | WhatsApp webhook parsing, catalog-cart conversion, address collection, payments, proof download, and kitchen handoff. |
| `whatsapp_state.py` | `WhatsAppState`, an async-shaped FSM adapter backed by `whatsapp_sessions`. |
| `reports.py` | Daily/weekly reports and inventory summary. |
| `receipt_generator.py` | Builds a PDF file under `/tmp`; callers are responsible for sending/deleting it. |
| `migrations/` | Additive database migrations. Migration 010 creates WhatsApp-specific tables/columns. |
| `generate_qr_codes.py` | Creates QR code images for table `public_code` values. |
| `catalog_csv_generator.py` | Helper for generating catalog CSV data. |
| `docker-compose.yml`, `Dockerfile` | Container deployment configuration. |

Other Python files in the repository may be experiments or utilities. Do not assume they are runtime imports without checking the deployment files and imports.

## Data model

The base tables predate the migration folder. The following is the application-level model used in code; migrations are authoritative for later additions.

| Table | Important fields / role |
|---|---|
| `restaurants` | Tenant, manager and kitchen Telegram ids, subscription fields, bank details, kitchen-board state, optional delivery bot token, delivery fee/pickup configuration, Paystack configuration, WhatsApp credentials, `dispatch_group_id` (rider dispatch Telegram group), and `whatsapp_catalog_id` (Meta catalog for availability sync). |
| `restaurant_tables` | Tenant table/Qr mapping: `public_code`, `table_number`, active status, optional `menu_filter`. `NULL` or `EXTERNAL` represents external delivery/pickup ordering. |
| `menu_categories` | Tenant categories with active flag and display order. |
| `menu_items` | Tenant category items: price, availability, inventory fields, image URL, and `item_type` (`simple` or `composite`). |
| `orders` | Restaurant/table/customer/order totals, payment and fulfilment states, delivery data, inventory flag, source channel, customer contact, and dispatch tracking fields (`dispatch_sent_at`, `dispatch_sent_by`). |
| `order_items` | Snapshotted menu item quantity, unit price, and subtotal. |
| `payments` | Bank-transfer/Paystack payment records and provider references. |
| `delivery_zones` | Per-restaurant active named delivery fees. |
| `modifier_groups`, `modifier_options` | Choices for composite menu items. |
| `order_item_modifiers` | Snapshots selected modifier quantities and prices for an order item. |
| `whatsapp_sessions` | Composite primary key `(phone_number, restaurant_id)`, current state, JSONB session data, timestamp. |
| `menu_item_catalog_map` | Maps Meta catalog retailer ids to Chowlin menu items and restaurants. |
| `whatsapp_processed_messages` | Idempotency table: stores processed Meta `message_id` values to prevent duplicate order creation on webhook retries. |

### Important persistence rules

- `create_order_in_db()` in `bot.py` is intentionally channel-agnostic. Both Telegram and WhatsApp call it. Preserve that interface when adding a channel feature.
- It validates tracked inventory before insertion and creates `order_items` plus modifier snapshots when present.
- Cash orders are stored as `payment_status = confirmed`; bank transfer, pay-on-delivery, and Paystack begin pending.
- `deduct_order_inventory(order_id)` calls the PostgreSQL function from migration 003. That function only deducts confirmed orders and is idempotent through `orders.inventory_deducted`.
- In the current implementation, cash and pay-on-delivery flows call inventory deduction immediately. For pay-on-delivery, the database function does not deduct because that payment remains pending.
- All customer-visible money is Nigerian naira (`₦`). Use `Decimal` or database numeric values for new money calculations when practical; do not introduce float rounding errors.

## Telegram functionality (implemented)

### Customer ordering

1. `/start <public_code>` resolves an active table QR code, verifies the restaurant subscription, and initializes an aiogram FSM session.
2. Table QR codes start a dine-in order. External QR codes start delivery/pickup selection. Dedicated delivery bots start a delivery session without a QR code and only offer pickup when `pickup_enabled` is true.
3. Delivery accepts typed addresses or Telegram locations. Telegram attempts reverse geocoding with Nominatim and asks the customer to confirm a shared location.
4. Customers browse active categories and available items, choose quantities, manage/clear a cart, and confirm the order.
5. Composite items walk customers through modifier groups, selection limits, and optional modifier quantities. Selections are persisted in `order_item_modifiers`.
6. Payment choices are cash, bank transfer with photo proof, pay-on-delivery, and Paystack when the restaurant has a configured subaccount.
7. Customers can use `/cancel`, `/history`, `/status`, and reorder from history after availability checks. Telegram sends PDF receipts after applicable orders and after confirmed Paystack webhooks.

### Kitchen and manager operations

- Every submitted order is sent to the restaurant kitchen Telegram group. Bank-transfer orders contain proof plus confirm/reject controls; other orders contain preparing/ready/dispatch controls.
- Delivery orders show a "Send to Rider" button (`dispatch_`). Tapping it sends a dispatch message with customer name, contact, address, Google Maps link, and total to `restaurants.dispatch_group_id`. The dispatch message has a "Delivered" button (`delivered_`) for the rider to close the order loop. Delivery status and customer notification are updated on confirmation.
- Kitchen status actions update the order and notify customers (Telegram or WhatsApp via `notify_order_customer()`). A pinned live board tracks pending, preparing, and recent ready orders. A manager rush alert is sent once per day above `RUSH_HOUR_PENDING_THRESHOLD`.
- Kitchen commands include `/pending`, `/board`, `/menu`, and `/restock`; menu controls toggle item availability and composite-option availability.
- Manager/reporting commands include `/daily_report`, `/weekly_report`, `/monthly_report`, `/register_manager`, and `/set_manager`. `/activate` is restricted by `ADMIN_TELEGRAM_ID`.
- Delivery staff configuration commands include `/set_delivery_fee` and `/add_zone` in the kitchen group.

## WhatsApp functionality (implemented)

WhatsApp is not a Telegram UI clone. It begins when Meta sends an `order` message after a customer adds catalog items and sends the catalog cart.

1. `handle_whatsapp_webhook()` identifies the restaurant by the receiving `whatsapp_phone_number_id`. Incoming messages are deduplicated via `whatsapp_processed_messages` (`already_processed()`).
2. Catalog `product_retailer_id` values are resolved through `menu_item_catalog_map`; mapped items form the cart.
3. When `pickup_enabled` is true, the customer is offered delivery or pickup via an interactive list. Delivery proceeds to address collection; pickup skips directly to payment.
4. Delivery fee is applied based on `delivery_fee_type`: flat fee is applied automatically; zone-based fee presents an interactive zone picker (capped at `MAX_WHATSAPP_LIST_ROWS`). `handle_unusable_zone_config()` falls back to flat fee or pickup when zone config is broken.
5. The customer provides a typed delivery address or WhatsApp location pin. Coordinates are stored; no reverse geocoding or confirmation screen is implemented.
6. Interactive buttons offer cash, bank transfer, pay-on-delivery, and Paystack (when `paystack_enabled` is true for the restaurant).
7. Cash/pay-on-delivery create `order_channel = whatsapp`, carry the phone number in `customer_contact`, send a Telegram kitchen notification, attempt inventory deduction, and send a WhatsApp PDF receipt.
8. Bank transfer sends bank details, accepts an image proof, downloads it from Meta, creates the order/payment record, and forwards the proof to the Telegram kitchen for manual confirmation.
9. Paystack: `start_paystack_payment()` creates a pending order, generates a checkout link using a deterministic `<digits>@chowlin.ng` email, and sends the URL to the customer. The Paystack webhook in `main.py` confirms the order and sends a WhatsApp receipt.
10. Status notifications (payment confirmed/rejected, preparing, ready, delivered) are sent to the customer's WhatsApp number via `notify_order_customer()`.
11. State lives in Supabase, so it survives service restarts. `WhatsAppState` offers `get_data`, `update_data`, `set_state`, and `clear` so it can be passed to shared persistence/kitchen functions.
12. `sync_catalog_item_availability()` in `whatsapp.py` pushes item availability changes to the Meta Catalog Batch API using `restaurants.whatsapp_catalog_id`.

## Telegram-to-WhatsApp parity backlog

Use this as the implementation order. It reflects the feature delta observed in the code, not a promise that every Telegram interaction can be copied literally to WhatsApp.

1. ~~**Fix channel-aware status notifications.**~~ ✅ Done. `notify_order_customer()` is channel-aware: WhatsApp orders receive status updates via `send_text`; Telegram orders use `bot.send_message`.
2. ~~**Send WhatsApp receipts.**~~ ✅ Done. `send_whatsapp_receipt()` in `whatsapp.py` uploads and sends a PDF receipt document via the Meta Graph API. `send_order_receipt()` in `bot.py` dispatches to the correct channel.
3. ~~**Add WhatsApp Paystack.**~~ ✅ Done. `start_paystack_payment()` in `whatsapp.py` creates a pending order, generates a Paystack link using a deterministic `<digits>@chowlin.ng` email, and sends the checkout URL. The Paystack webhook in `main.py` handles confirmation for both channels.
4. ~~**Support pickup and delivery configuration.**~~ ✅ Done. WhatsApp presents delivery/pickup choice when `pickup_enabled` is true. Flat fee and zone-based delivery fees are applied before payment. `handle_unusable_zone_config()` falls back to flat fee or pickup.
5. ~~**Support delivery zones and totals.**~~ ✅ Done. Zone selection uses a WhatsApp interactive list (capped at `MAX_WHATSAPP_LIST_ROWS`). Zone fee is saved to state and added to `total_price` before order creation.
6. **Add catalog validation before order creation.** The WhatsApp cart mapping currently does not reject inactive/unavailable items and does not support composite modifiers. Query current `is_available`, aggregate duplicate retailer ids safely, validate inventory, and give a clear resubmit-cart response.
7. **Add customer commands/menu actions that fit WhatsApp.** At minimum: start/help, current-order status, recent orders/reorder, cancel before preparation, and a way to recover an abandoned session. Use WhatsApp list/button constraints and conversational text rather than Telegram callbacks.
8. **Handle composite items.** Meta catalog orders cannot represent modifier choices in the current mapping. A practical approach is detecting `item_type = composite` after catalog submission, then running a WhatsApp interactive-list/text state machine for `modifier_groups`, persisting the same `cart[item].modifiers` shape that Telegram uses.
9. **Add a WhatsApp operations layer only if required.** Telegram kitchen controls, reports, inventory, subscription alerts, and kitchen board are deliberately Telegram-oriented. If staff must use WhatsApp, build explicit staff authorization and commands rather than exposing those controls to every customer number.

## Safe implementation guidance

- Check subscription activity for WhatsApp before accepting a cart, matching Telegram behavior.
- Treat all Meta webhooks as untrusted input: avoid direct indexing of optional arrays/objects, handle duplicate delivery, and log a safe event id/message id for idempotency.
- Validate that any interactive reply belongs to the expected `whatsapp_sessions.state`; do not create an order merely because a reply id matches a payment option.
- Keep state and order writes tenant-scoped by restaurant id and avoid selecting all tenants when a scoped query is possible.
- Use one notification abstraction for customer messages (`telegram_user_id` versus `customer_contact` / WhatsApp) before adding more cross-channel states.
- Preserve kitchen notifications as Telegram unless a new explicit channel is requested. `send_order_to_kitchen()` already accepts channel-neutral order data and an optional proof.
- Do not store, print, commit, or place secrets in `context.md`. Credentials belong in `.env` and are accessed through environment variables.
- Add or update tests for any changed payment/order state transition. The repository currently has only lightweight test scripts, so prioritize pure helper tests and webhook-payload fixtures when extending it.

## Environment configuration

Names observed in runtime code include:

`TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `FASTAPI_WEBHOOK_URL`, `N8N_WEBHOOK_URL`, `N8N_UPDATE_WEBHOOK_URL`, `N8N_HEARTBEAT_URL`, `ADMIN_TELEGRAM_ID`, `ADMIN_API_KEY`, `RUSH_HOUR_PENDING_THRESHOLD`, `PAYSTACK_SECRET_KEY`, `PAYSTACK_COMMISSION_PERCENTAGE`, and `WHATSAPP_VERIFY_TOKEN`.

WhatsApp restaurant credentials are stored per tenant in `restaurants`: `whatsapp_phone_number_id`, `whatsapp_business_account_id`, and `whatsapp_access_token`.

Paystack requires an email address. For WhatsApp orders, use the documented deterministic placeholder `<digits-only WhatsApp number>@chowlin.ng`; do not derive an email from a customer name or invent a new pattern per call site.

## Known implementation notes for future work

- `Dockerfile` copies only the runtime modules it currently imports. If a new runtime module/asset is added, include it in the image build.
- Migration 010 has no `.sql` extension but contains SQL and is the WhatsApp schema migration. Keep migration execution/documentation aware of that filename.
- The existing report implementation totals all orders in its time range; it does not filter to confirmed payments. Clarify the desired revenue definition before changing financial reporting.
- Current WhatsApp text responses use Markdown-like asterisks but the Graph API text payload does not specify formatting. Prefer plain text or supported WhatsApp formatting intentionally.
- `whatsapp_sessions` state updates and data updates are separate upserts. If extending concurrent webhook behavior, consider a single atomic state/data update or optimistic versioning.
- `orders.order_status` may have a CHECK constraint that does not include `'delivered'`. Verify and update the constraint before deploying the rider delivery flow to production (see migration 011 note).
- `whatsapp_processed_messages` rows are safe to prune after ~30 days. No automatic cleanup job is implemented; add one if message volume warrants it.
- `restaurants.dispatch_group_id` must be set manually per tenant to enable the rider dispatch flow. There is no admin command to set it; update the column directly in Supabase.

## Change checklist

1. Read the handlers and migration that own the affected flow.
2. Keep tenant, channel, payment, order-status, and inventory semantics intact.
3. Apply additive migrations for schema changes; do not edit already-applied migrations to retrofit production state.
4. Run at least syntax/import checks and focused tests or payload simulations.
5. Document any new endpoint, environment variable, state value, and operational setup step in the README/context as appropriate.

# Product & Pricing Update — August 2026

## Current commercial plans

### Starter
- Setup: ₦50,000.
- Monthly: ₦20,000.
- First monthly subscription under current introductory offer: ₦10,000.
- Positioning: small restaurants and cafés.
- Includes: QR ordering up to 10 tables, kitchen Telegram notifications, cash/bank-transfer payments, PDF receipts, daily sales reports.

### Delivery Bot
- Setup: ₦60,000.
- Monthly: ₦25,000.
- First monthly subscription under current introductory offer: ₦12,500.
- Positioning: home chefs, cloud kitchens and online food vendors.
- Includes: own branded Telegram bot, customers find vendor by name without QR, delivery address by text/location, delivery-zone fee configuration, kitchen Telegram notifications, cash/bank-transfer/pay-on-delivery, PDF receipts, daily sales reports, pickup on request.

### Growth
- Setup: ₦100,000.
- Monthly: ₦35,000.
- First monthly subscription under current introductory offer: ₦17,500.
- Positioning: busy restaurants with dine-in and delivery.
- Includes everything in Starter plus unlimited tables, delivery/pickup, delivery zones and fees, weekly reports, kitchen menu management, customer reorder, inventory tracking/low-stock alerts and priority support.

### Enterprise
- Setup: from ₦200,000.
- Monthly: from ₦80,000.
- First monthly subscription under current introductory offer: from ₦40,000.
- Positioning: multi-venue, multi-section and high-volume operations.
- Includes everything in Growth plus multiple branches under one account, custom menus per section/event type, club/event-night menu switching, dedicated support, monthly business review and custom feature development.

## Current introductory offer

Public pricing currently advertises 50% off the first monthly subscription on any plan.
- Setup fee is paid in full at onboarding.
- First month subscription is half price.
- Full rate starts from month two.

This is a commercial rule. Do not invent billing implementation from the marketing copy; inspect subscription/billing code before changing billing behavior.

## Annual pricing advertised

- Starter: ₦200,000/year.
- Delivery Bot: ₦250,000/year.
- Growth: ₦350,000/year.
- Enterprise: custom quote.

Marketing says annual upfront payment saves two months free. Verify whether annual billing/entitlements are technically implemented before coding against this claim.

## WhatsApp Ordering add-on

Available commercially on Delivery Bot, Growth and Enterprise.

- Setup add-on: ₦30,000.
- Monthly add-on: ₦12,000/month.
- Requires Delivery Bot tier or above.
- Customers order through the vendor's WhatsApp catalog without a separate app or QR.
- Delivery address can be typed or supplied as a location pin.
- Delivery zones/fees share the same delivery configuration concept as Telegram.
- Payment options include cash, bank transfer, pay on delivery and Paystack checkout where enabled.
- Customers can receive order status updates, cancel/order-history/reorder functionality where implemented, and receipts.
- Kitchen continues receiving orders on Telegram.
- Add-on is transactional only; it is not intended for automated marketing blasts.

Important: the repository already contains WhatsApp ordering, including catalog cart handling, WhatsApp state persistence, bank-transfer proof, Paystack, receipts, status notifications and catalog availability synchronization. Verify `whatsapp.py`, `whatsapp_state.py` and `main.py` before treating any specific behavior as complete.

# Verified bot.py capability map

The current `bot.py` is a large coupled aiogram module. Its major handlers/helpers include:

## Delivery and location
- `start_delivery_session()` initializes a restaurant-specific delivery bot session, checks subscription, loads the EXTERNAL table context and optionally offers pickup.
- `load_delivery_bots()` creates one aiogram `Bot` instance per restaurant with `delivery_bot_token`.
- `reverse_geocode()` uses Nominatim for Telegram location handling and returns a human-readable address or falls back safely.
- `format_maps_link()` creates a Google Maps link from coordinates.
- `get_delivery_fee()` handles none/flat/zone fee types.
- `show_delivery_zones()` presents active restaurant delivery zones.
- `proceed_after_address()` routes to zone selection, flat fee application or the menu.

## Composite menu items
- `start_composite_item()` loads active modifier groups/options.
- `build_group_prompt()` respects available options, selection mode, minimum selections and optional groups.
- Modifier options can have price deltas and quantities.
- `finalize_composite_selection()` calculates base price plus modifier price deltas and stores a unique composite cart line.

## Inventory
- `get_tracked_inventory_items()` retrieves inventory-tracked menu items.
- `validate_cart_inventory()` checks requested quantities against current stock.
- `send_restock_alert()` notifies kitchen and manager about low stock.
- `deduct_inventory_for_order()` delegates deduction to the PostgreSQL `deduct_order_inventory` RPC/function.

## Paystack
- `resolve_bank_code()` resolves a bank name through Paystack.
- `create_paystack_subaccount()` creates and saves a restaurant Paystack subaccount.
- `create_paystack_payment_link()` initializes NGN checkout using the order id as reference and the restaurant subaccount.
- Paystack payment confirmation is handled through the FastAPI webhook in `main.py`; browser callback must not be treated as payment proof.

## Kitchen operations
- `send_order_to_kitchen_from_db()` sends a submitted order to the restaurant kitchen Telegram group.
- `build_kitchen_order_board()` groups current-day orders into pending, preparing and recent-ready sections.
- `refresh_kitchen_order_board()` edits/recreates the pinned board and can trigger the daily rush alert.
- `send_rush_hour_alert_if_needed()` alerts the manager when pending orders exceed the configured threshold.

## Customer ordering
- `/start` handles table QR, external order and delivery-bot entry points.
- Delivery/pickup selection is implemented.
- Address collection accepts text and location.
- Category/item browsing and quantity selection are implemented.
- Cart review/clear/confirmation are implemented.
- Composite items are implemented on Telegram.
- Payment handlers exist for pay-on-delivery, cash, Paystack and bank transfer.
- `/cancel`, `/history`, `/status` and reorder handlers exist.

## Reports and administration
- `/daily_report`, `/weekly_report`, `/monthly_report` are available.
- `/register_manager` and `/set_manager` manage manager configuration.
- `/restock` provides kitchen stock management.
- `/menu` provides kitchen menu management and availability controls.
- `/pending` and `/board` expose/refresh kitchen order information.
- `/set_delivery_fee` and `/add_zone` configure delivery pricing.
- `/activate` is admin-restricted through `ADMIN_TELEGRAM_ID`.

# Product-to-code guardrails

1. Do not implement a plan entitlement merely because it appears on the pricing page. First locate the subscription/entitlement code and database fields.
2. Do not implement annual billing merely from the annual pricing copy. Verify subscription billing support.
3. Do not assume Enterprise multi-branch behavior is already present. The pricing page is a commercial requirement, not proof of current schema support.
4. WhatsApp Ordering is a paid add-on commercially, but existing WhatsApp code may not yet enforce the add-on entitlement. Check subscription/add-on fields before adding enforcement.
5. Starter is intentionally limited to 10 tables commercially; verify whether current code enforces a table limit before changing onboarding or table creation.
6. Delivery Bot is a distinct product tier centered on dedicated branded Telegram delivery bots. Do not confuse it with ordinary QR-based external ordering.
7. Growth includes inventory, kitchen menu management, delivery/pickup and reorder as commercial positioning. Verify actual plan gates before changing those features.
8. Enterprise custom development is a service offering. Do not hard-code custom requirements into the core product without a specific customer requirement.
9. WhatsApp ordering should remain transactional. Do not add marketing automation or unsolicited broadcast behavior as part of ordinary ordering features.

# Pricing/UI copy reference

When editing the public pricing page, preserve these current customer-facing facts unless the user supplies a newer price:
- Starter ₦50k setup / ₦20k monthly.
- Delivery Bot ₦60k setup / ₦25k monthly.
- Growth ₦100k setup / ₦35k monthly.
- Enterprise from ₦200k setup / from ₦80k monthly.
- First monthly subscription is currently 50% off on all plans.
- Annual: Starter ₦200k, Delivery Bot ₦250k, Growth ₦350k, Enterprise custom.
- WhatsApp add-on: ₦30k setup + ₦12k/month, Delivery Bot and above.

# Engineering source-of-truth rule

This file is context, not a replacement for source code or database schema.

For engineering behavior, current repository code and applied database schema win.
For public commercial pricing, the pricing section above is the current supplied pricing unless the user provides newer instructions.
For ambiguous requirements, inspect the owning code and explain any mismatch rather than silently assuming one side is correct.
