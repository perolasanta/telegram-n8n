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
| `restaurants` | Tenant, manager and kitchen Telegram ids, subscription fields, bank details, kitchen-board state, optional delivery bot token, delivery fee/pickup configuration, Paystack configuration, WhatsApp credentials and entitlement (`whatsapp_addon_enabled`, `whatsapp_addon_expires_at`, `whatsapp_display_number`), `dispatch_group_id` (rider dispatch Telegram group), and `whatsapp_catalog_id` (Meta catalog for availability sync). |
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

### WhatsApp dine-in

- `GET /t/{public_code}` in `main.py` is the table landing route. It offers Telegram and, when the WhatsApp number and add-on entitlement are valid, WhatsApp. QR codes can encode this route with `generate_qr_codes.py --landing`.
- A WhatsApp landing link includes a `ref:` token. When a customer sends it while idle, the bot looks up an active table belonging to the receiving restaurant and stores `table_id`, `table_number`, `menu_filter`, and the original UTC `table_bound_at` in `whatsapp_sessions` data.
- The binding expires after 3 hours. A fresh binding makes checkout dine-in and is preserved across successful order rounds without refreshing its timestamp.
- When `menu_filter` is set, cart and reorder lines are accepted only when their category name starts with `{menu_filter} —` (case-insensitive). A mismatched line is rejected while other valid lines can continue.

### WhatsApp add-on entitlement

- `restaurants.whatsapp_addon_enabled` controls entitlement; `whatsapp_addon_expires_at` is an optional UTC expiry, with `NULL` meaning no separate expiry. Migration 012 defaults new tenants to disabled and grandfathers tenants already configured with both `whatsapp_phone_number_id` and `whatsapp_access_token`. `whatsapp_display_number` stores the dialable international number (digits only) used for `wa.me` links; `whatsapp_phone_number_id` is Meta's internal id.
- An admin can grant or revoke the add-on with `/set_whatsapp_addon <restaurant_id> on|off [days]`. `on` defaults to 30 days; `off` clears the expiry.
- Inbound WhatsApp webhook handling checks the add-on before creating or loading session state. The `/t/{public_code}` page only displays its WhatsApp choice when the display number is valid, the Meta phone-number id exists, and the add-on is active; otherwise it redirects to Telegram.
- Outbound order-status notifications, receipts, the Paystack webhook, and catalog-availability synchronization are not gated by the add-on, so existing orders continue to receive updates after expiry.

## Telegram-to-WhatsApp parity backlog

Use this as the implementation order. It reflects the feature delta observed in the code, not a promise that every Telegram interaction can be copied literally to WhatsApp.

1. ~~**Fix channel-aware status notifications.**~~ ✅ Done. `notify_order_customer()` is channel-aware: WhatsApp orders receive status updates via `send_text`; Telegram orders use `bot.send_message`.
2. ~~**Send WhatsApp receipts.**~~ ✅ Done. `send_whatsapp_receipt()` in `whatsapp.py` uploads and sends a PDF receipt document via the Meta Graph API. `send_order_receipt()` in `bot.py` dispatches to the correct channel.
3. ~~**Add WhatsApp Paystack.**~~ ✅ Done. `start_paystack_payment()` in `whatsapp.py` creates a pending order, generates a Paystack link using a deterministic `<digits>@chowlin.ng` email, and sends the checkout URL. The Paystack webhook in `main.py` handles confirmation for both channels.
4. ~~**Support pickup and delivery configuration.**~~ ✅ Done. WhatsApp presents delivery/pickup choice when `pickup_enabled` is true. Flat fee and zone-based delivery fees are applied before payment. `handle_unusable_zone_config()` falls back to flat fee or pickup.
5. ~~**Support delivery zones and totals.**~~ ✅ Done. Zone selection uses a WhatsApp interactive list (capped at `MAX_WHATSAPP_LIST_ROWS`). Zone fee is saved to state and added to `total_price` before order creation.
6. ~~**Add catalog validation before order creation.**~~ ✅ Done. `build_authoritative_cart()` validates mapped items and inventory in batched queries, aggregates duplicate catalog lines, and reports invalid lines. Valid lines can proceed after a partial-cart confirmation in state `confirming_partial_cart`. Before bank-transfer order creation, stock conflicts are sent to the kitchen with the payment proof; the customer is told to keep the receipt while the restaurant resolves the issue.
7. ~~**Add customer commands/menu actions that fit WhatsApp.**~~ ✅ Done. Whole-message keywords are `status`, `track`, `my order`; `orders`, `history`, `my orders`; `cancel`; `restart`, `reset`, `start over`; and `help`, `menu`, `hi`, `hello`, `hey`, `start`. The interactive menu offers Order status, Recent orders, Cancel an order, and How to order. For non-order incoming messages, a session state older than 120 minutes (`SESSION_TTL_MINUTES`) is cleared; expired interactive replies are told to send the cart again. History and reorder are customer-owned WhatsApp orders; reorder fetches current menu items, validates stock, and goes through the shared cart submission pipeline, including partial-cart and composite flows. Customers may self-cancel only pending Cash Payment or Pay on Delivery orders; other orders require contacting the restaurant.
8. ~~**Handle composite items.**~~ ✅ Done when `WHATSAPP_COMPOSITES_ENABLED=true`. Composite catalog items run through a modifier state machine using `configuring_modifiers` and `entering_modifier_qty`, and save the same `cart[item].modifiers` shape used by Telegram. The flow re-fetches current groups/options and applies `MAX_COMPOSITE_UNITS = 5` across a cart. With the flag unset or false, composite catalog items are rejected with an explanation.
9. **Add a WhatsApp operations layer only if required.** Telegram kitchen controls, reports, inventory, subscription alerts, and kitchen board are deliberately Telegram-oriented. If staff must use WhatsApp, build explicit staff authorization and commands rather than exposing those controls to every customer number.

### WhatsApp message limits

`send_list()` and `send_buttons()` bound their payloads before sending; `send_text()` caps text bodies. Keep within Meta's limits: list rows 10 total, row title 24 characters, row description 72 characters, list button label 20 characters, reply button title 20 characters, interactive body 1024 characters, and text body 4096 characters. Interactive reply IDs are validated rather than truncated.

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

Set application variables in the repository-root `.env` file for Docker Compose (`docker-compose.yml` loads it into the container). For a non-Compose deployment, set the same variables in the application service's environment configuration. Keep secret values out of this file and out of source control. `CHOWLIN_HOST_PORT` is read by Compose for the host-side port mapping only.

Runtime environment variables:

| Variable | Needed / default | Purpose |
|---|---|---|
| `TOKEN` | Required | Shared Telegram bot token. |
| `SUPABASE_URL` | Required | Supabase project URL. |
| `SUPABASE_SERVICE_KEY` | Required, secret | Supabase service-role key. |
| `WHATSAPP_VERIFY_TOKEN` | Required to verify Meta's webhook | Must match the verification token configured in Meta. |
| `FASTAPI_WEBHOOK_URL` | Optional; defaults to `https://telegram-n8n-restaurant-bot.onrender.com` | Public application base URL, used for webhook/payment callback URLs. |
| `TELEGRAM_BOT_USERNAME` | Required for the `/t/{public_code}` landing route; no `@` | Username used to build the table's Telegram deep link. |
| `PUBLIC_BASE_URL` | Optional | Base URL used by `generate_qr_codes.py --landing`; falls back to `FASTAPI_WEBHOOK_URL`. |
| `N8N_WEBHOOK_URL` | Optional; defaults to the configured Chowlin n8n new-order URL | Sends new-order events to n8n. |
| `N8N_UPDATE_WEBHOOK_URL` | Optional; defaults to the configured Chowlin n8n update URL | Sends order updates to n8n. |
| `N8N_HEARTBEAT_URL` | Optional; defaults to the configured Chowlin n8n heartbeat URL | Sends service heartbeat events. |
| `ADMIN_TELEGRAM_ID` | Optional for restricted admin commands | Telegram ID authorized for admin-only actions. |
| `ADMIN_API_KEY` | Required to use the protected admin HTTP endpoints | Secret sent in the `X-Admin-Key` header. |
| `RUSH_HOUR_PENDING_THRESHOLD` | Optional; defaults to `5` | Pending-order threshold for kitchen rush alerts. |
| `PAYSTACK_SECRET_KEY` | Required to use Paystack | Paystack API/webhook secret. |
| `PAYSTACK_COMMISSION_PERCENTAGE` | Optional; defaults to `5` | Paystack commission percentage used by payment calculations. |
| `WHATSAPP_COMPOSITES_ENABLED` | Optional; defaults to `false` | Set to `true` to enable WhatsApp composite modifier selection; false/unset rejects composite catalog items. |
| `CHOWLIN_HOST_PORT` | Optional; defaults to `8001` | Host port published by Docker Compose; container port remains `8001`. |

The old standalone `boted.py` and helper scripts mention `BOT_TOKEN` and `KITCHEN_CHAT_ID`; they are not part of the current Docker Compose runtime. Do not add them unless deploying those scripts separately.

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
