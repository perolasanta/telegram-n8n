"""WhatsApp catalog ordering and customer notifications.

Prices and availability always come from Chowlin's database. Meta catalog data is
used only to identify a menu item and requested quantity.
"""

from decimal import Decimal, InvalidOperation
import html
import logging
import os

import httpx
from aiogram.types import BufferedInputFile

from whatsapp_state import WhatsAppState
from receipt_generator import generate_receipt_pdf
from bot import (
    build_composite_cart_line,
    delivery_bots,
    create_order_in_db,
    create_paystack_payment_link,
    deduct_inventory_for_order,
    is_subscription_active,
    send_order_to_kitchen,
    send_order_receipt,
    send_restock_alert,
    reverse_geocode,
    format_delivery_coordinates,
    validate_cart_inventory,
)

GRAPH_API = "https://graph.facebook.com/v25.0"
MAX_WHATSAPP_LIST_ROWS = 10
MAX_LINE_QTY = 50
MAX_COMPOSITE_UNITS = 5
WHATSAPP_COMPOSITES_ENABLED = True


def money(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def truncate(text: str, max_length: int) -> str:
    text = str(text or "")
    if len(text) <= max_length:
        return text
    if max_length <= 1:
        return text[:max_length]
    return text[:max_length - 1] + "…"


def build_modifier_rows(group_id: str, group: dict, options: list[dict], page: int = 0) -> tuple[list[dict], int, int]:
    available_options = [option for option in options if option.get("is_available")]
    controls = int(group.get("selection_mode") == "multi") + int(int(group.get("min_select") or 0) == 0)
    base_page_size = max(1, MAX_WHATSAPP_LIST_ROWS - controls)
    page_size = base_page_size - 1 if len(available_options) > base_page_size else base_page_size
    page_size = max(1, page_size)
    page_count = max(1, (len(available_options) + page_size - 1) // page_size)
    page = min(max(int(page or 0), 0), page_count - 1)
    start = page * page_size
    rows = []
    for option in available_options[start:start + page_size]:
        description_parts = []
        price_delta = money(option.get("price_delta"))
        if price_delta > 0:
            description_parts.append(f"+₦{float(price_delta):,.0f}")
        if option.get("unit_label"):
            description_parts.append(str(option.get("unit_label")))
        row = {
            "id": f"mod:{group_id}:{option.get('id', '')}",
            "title": truncate(option.get("name", "Option"), 24),
        }
        description = " · ".join(description_parts)
        if description:
            row["description"] = truncate(description, 72)
        rows.append(row)
    if group.get("selection_mode") == "multi":
        rows.append({"id": "mod_done", "title": truncate("Done", 24)})
    if int(group.get("min_select") or 0) == 0:
        rows.append({"id": "mod_skip", "title": truncate("Skip", 24)})
    if page < page_count - 1:
        rows.append({"id": "mod_more", "title": truncate("More options", 24)})
    return rows, page, page_count


def validate_group_selection(group: dict, picks: list[dict]) -> str | None:
    count = len(picks)
    minimum = int(group.get("min_select") or 0)
    maximum = group.get("max_select")
    if count < minimum:
        return f"Please choose at least {minimum} option(s)."
    if maximum is not None and count > int(maximum):
        return f"Please choose no more than {maximum} option(s)."
    return None


def can_skip_group(group: dict) -> bool:
    return int(group.get("min_select") or 0) == 0


def parse_modifier_quantity(text: str) -> int | None:
    if not isinstance(text, str) or not text.isascii() or not text.isdigit():
        return None
    quantity = int(text)
    return quantity if 1 <= quantity <= 20 else None


def expand_composite_queue(composites: list[dict], max_units: int = MAX_COMPOSITE_UNITS) -> tuple[list[dict], bool]:
    total_units = sum(max(0, int(item.get("qty") or 0)) for item in composites)
    if total_units > max_units:
        return [], True
    queue = []
    for item in composites:
        quantity = max(0, int(item.get("qty") or 0))
        for unit_no in range(1, quantity + 1):
            queue.append({
                "menu_item_id": item.get("menu_item_id"),
                "name": item.get("name", "Item"),
                "unit_no": unit_no,
                "units_total": quantity,
            })
    return queue, False


def filter_same_composite_units(queue: list[dict], menu_item_id: str) -> tuple[list[dict], list[dict]]:
    matching = []
    remaining = []
    for unit in queue:
        (matching if unit.get("menu_item_id") == menu_item_id else remaining).append(unit)
    return matching, remaining


def build_stock_conflict_caption(data: dict, from_number: str, shortages: list[dict]) -> str:
    """Build an HTML-safe kitchen note for a paid cart with stock shortages."""
    esc = lambda value: html.escape(str(value if value is not None else ""))
    lines = ["STOCK CONFLICT, needs manual resolution"]
    lines.append(f"Customer: {esc(data.get('customer_name', 'Customer'))}")
    lines.append(f"WhatsApp: {esc(from_number)}")
    lines.append("Cart:")
    for item in (data.get("cart") or {}).values():
        lines.append(f"• {esc(item.get('name', 'Item'))} × {esc(item.get('qty', 0))}")
    total = f"{float(money(data.get('total_price'))):,.0f}"
    lines.append(f"Total: ₦{esc(total)}")
    lines.append("Shortages:")
    for shortage in shortages:
        lines.append(
            f"• {esc(shortage.get('name', 'Item'))}: asked {esc(shortage.get('requested', 0))}, "
            f"has {esc(shortage.get('available', 0))}"
        )
    return "\n".join(lines)


async def send_stock_conflict_to_kitchen(bot, restaurant, data, from_number, image_bytes, shortages):
    kitchen_chat_id = restaurant.get("kitchen_chat_id")
    if not kitchen_chat_id:
        return False
    try:
        kitchen_bot = delivery_bots.get(restaurant["id"], bot)
        await kitchen_bot.send_photo(
            chat_id=kitchen_chat_id,
            photo=BufferedInputFile(image_bytes, filename="payment_proof.jpg"),
            caption=build_stock_conflict_caption(data, from_number, shortages),
        )
        return True
    except Exception:
        logging.exception("Failed to notify kitchen about WhatsApp stock conflict for restaurant %s", restaurant.get("id"))
        return False


async def send_whatsapp_message(phone_number_id: str, token: str, to: str, payload: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GRAPH_API}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "to": to, **payload},
        )
        response.raise_for_status()


async def send_text(phone_number_id, token, to, text):
    await send_whatsapp_message(phone_number_id, token, to, {"type": "text", "text": {"body": text}})


async def send_list(phone_number_id: str, token: str, to: str, body: str, button_label: str, rows: list[dict]):
    """Send a Meta interactive list; callers must explicitly handle over-limit data."""
    if not rows or len(rows) > MAX_WHATSAPP_LIST_ROWS:
        raise ValueError("WhatsApp interactive lists must contain 1–10 rows")
    await send_whatsapp_message(phone_number_id, token, to, {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {"button": button_label, "sections": [{"title": "Options", "rows": rows}]},
        },
    })


async def send_buttons(phone_number_id: str, token: str, to: str, body: str, buttons: list[dict]):
    """Send a Meta interactive quick-reply message. Meta allows at most 3 buttons."""
    if not buttons or len(buttons) > 3:
        raise ValueError("WhatsApp interactive buttons must contain 1-3 options")
    await send_whatsapp_message(phone_number_id, token, to, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": b} for b in buttons]},
        },
    })


async def send_payment_options(phone_number_id, token, to, order_type: str, paystack_enabled: bool):
    rows = [
        {"id": "pay_cash", "title": "Cash"},
        {"id": "pay_bank", "title": "Bank transfer"},
    ]
    if order_type == "delivery":
        rows.append({"id": "pay_pod", "title": "Pay on delivery"})
    if paystack_enabled:
        rows.append({"id": "pay_paystack", "title": "Pay with card"})
    await send_list(phone_number_id, token, to, "How would you like to pay?", "Choose payment", rows)


async def send_whatsapp_document(phone_number_id: str, token: str, to: str, content: bytes, filename: str, caption: str):
    """Upload a document to Meta, then send it to a WhatsApp customer."""
    async with httpx.AsyncClient() as client:
        upload = await client.post(
            f"{GRAPH_API}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {token}"},
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (filename, content, "application/pdf")},
        )
        upload.raise_for_status()
        media_id = upload.json()["id"]
    await send_whatsapp_message(phone_number_id, token, to, {
        "type": "document",
        "document": {"id": media_id, "filename": filename, "caption": caption},
    })


async def sync_catalog_item_availability(menu_item_id: str, is_available: bool, supabase) -> None:
    """Best-effort push of an item's availability to the Meta catalog.

    Called whenever a kitchen toggles a menu item's availability (from either
    channel). This keeps the WhatsApp catalog from showing items customers can't
    actually order, so carts are rejected less often at submission time.

    This is deliberately best-effort: catalog sync can lag or fail without
    blocking the kitchen's own availability toggle, and server-side re-validation
    in build_authoritative_cart remains the actual safety net regardless of
    whether this sync succeeded.
    """
    mapping = supabase.table("menu_item_catalog_map").select(
        "catalog_retailer_id, restaurant_id, restaurants(whatsapp_access_token, whatsapp_catalog_id)"
    ).eq("menu_item_id", menu_item_id).execute()
    if not mapping.data:
        return  # item isn't mapped to a WhatsApp catalog product; nothing to sync

    row = mapping.data[0]
    restaurant = row.get("restaurants") or {}
    token = restaurant.get("whatsapp_access_token")
    catalog_id = restaurant.get("whatsapp_catalog_id")
    retailer_id = row.get("catalog_retailer_id")
    if not (token and catalog_id and retailer_id):
        return  # catalog not configured for this restaurant yet

    availability = "in stock" if is_available else "out of stock"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GRAPH_API}/{catalog_id}/items_batch",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "item_type": "PRODUCT_ITEM",
                    "requests": [{
                        "method": "UPDATE",
                        "data": {"id": retailer_id, "availability": availability},
                    }],
                },
            )
            response.raise_for_status()
    except Exception:
        logging.exception("Failed to sync catalog availability for retailer_id=%s (menu_item_id=%s)", retailer_id, menu_item_id)


async def send_whatsapp_receipt(supabase, order: dict, order_id: str) -> bool:
    """Generate and deliver a PDF receipt for a WhatsApp-originated order."""
    restaurant = order.get("restaurants") or {}
    contact = order.get("customer_contact")
    if not (contact and restaurant.get("whatsapp_phone_number_id") and restaurant.get("whatsapp_access_token")):
        return False

    try:
        result = supabase.table("orders").select(
            "*, order_items(*, menu_items(name, price)), restaurant_tables(table_number), restaurants(name, phone)"
        ).eq("id", order_id).execute()
        if not result.data:
            return False
        data = result.data[0]
        items = [{
            "name": (item.get("menu_items") or {}).get("name", "Item"),
            "qty": item["quantity"],
            "price": float(item["unit_price"]),
            "total": float(item["subtotal"]),
        } for item in data.get("order_items") or []]
        restaurant_data = data.get("restaurants") or {}
        total = money(data.get("total_amount"))
        fee = money(data.get("delivery_fee"))
        receipt_path = await generate_receipt_pdf({
            "order_id": order_id,
            "restaurant_name": restaurant_data.get("name", "Restaurant"),
            "restaurant_phone": restaurant_data.get("phone", ""),
            "table_number": (data.get("restaurant_tables") or {}).get("table_number") or data.get("order_type", "delivery").title(),
            "customer_name": data.get("customer_name", "Customer"),
            "created_at": __import__("datetime").datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")),
            "items": items,
            "subtotal": total - fee,
            "delivery_fee": fee,
            "tax": 0,
            "total": total,
            "payment_method": data.get("payment_method", "Unknown"),
            "payment_status": data.get("payment_status", "unknown"),
        })
        try:
            with open(receipt_path, "rb") as receipt_file:
                await send_whatsapp_document(
                    restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], contact,
                    receipt_file.read(), f"receipt_{order_id[:8]}.pdf", f"Receipt for order #{order_id[:8]}",
                )
        finally:
            if os.path.exists(receipt_path):
                os.remove(receipt_path)
        return True
    except Exception:
        logging.exception("Failed to send WhatsApp receipt for order %s", order_id)
        return False


def interactive_reply_id(msg: dict) -> str | None:
    interactive = msg.get("interactive") or {}
    if not isinstance(interactive, dict):
        return None
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    if not isinstance(reply, dict):
        return None
    reply_id = reply.get("id")
    return reply_id if isinstance(reply_id, str) else None


async def already_processed(supabase, message_id: str) -> bool:
    """Best-effort idempotency check against whatsapp_processed_messages.

    Meta redelivers webhooks on timeout/error, and a manual customer resend looks
    identical to a fresh message. This uses an insert-or-skip on a unique message_id
    so a genuine retry of the same Meta event is not reprocessed. If the table is
    missing or the check itself errors, we fail open (process the message) rather
    than risk silently dropping a legitimate order.
    """
    if not message_id:
        return False
    try:
        supabase.table("whatsapp_processed_messages").insert({"message_id": message_id}).execute()
        return False
    except Exception as e:
        # Unique violation means we've already handled this exact Meta message id.
        if "duplicate key" in str(e).lower() or "23505" in str(e):
            return True
        logging.warning("whatsapp_processed_messages check failed, processing anyway: %s", e)
        return False


async def handle_whatsapp_webhook(payload: dict, supabase, bot):
    """Dispatch a Meta webhook event for a configured restaurant number."""
    entries = payload.get("entry") or []
    changes = entries[0].get("changes") if entries else []
    entry = (changes or [{}])[0].get("value") or {}
    phone_number_id = (entry.get("metadata") or {}).get("phone_number_id")
    messages = entry.get("messages") or []
    if not phone_number_id or not messages:
        return

    msg = messages[0]
    message_id = msg.get("id")
    from_number = msg.get("from")
    if not from_number:
        return
    if await already_processed(supabase, message_id):
        logging.info("Skipping already-processed WhatsApp message %s from %s", message_id, from_number)
        return

    contact_name = ((entry.get("contacts") or [{}])[0].get("profile") or {}).get("name", "Customer")
    response = supabase.table("restaurants").select(
        "id, name, kitchen_chat_id, whatsapp_phone_number_id, whatsapp_access_token, "
        "pickup_enabled, delivery_fee_type, delivery_fee_flat, paystack_enabled, paystack_subaccount_code"
    ).eq("whatsapp_phone_number_id", phone_number_id).execute()
    if not response.data:
        logging.error("No restaurant configured for whatsapp_phone_number_id=%s (message_id=%s)", phone_number_id, message_id)
        return
    restaurant = response.data[0]

    try:
        state = WhatsAppState(supabase, from_number, restaurant["id"])
        session = supabase.table("whatsapp_sessions").select("state").eq("phone_number", from_number).eq("restaurant_id", restaurant["id"]).execute()
        current_state = session.data[0].get("state") if session.data else None

        if msg.get("type") == "order":
            await handle_cart_submission(msg, state, restaurant, from_number, contact_name, supabase)
        elif msg.get("type") == "interactive":
            await handle_interactive(msg, state, restaurant, from_number, supabase, bot, current_state)
        elif msg.get("type") == "location":
            await handle_location(msg, state, restaurant, from_number, current_state)
        elif msg.get("type") == "text":
            await handle_text(msg, state, restaurant, from_number, current_state)
        elif msg.get("type") == "image":
            await handle_payment_proof(msg, state, restaurant, from_number, supabase, bot, current_state)
    except Exception:
        # Never let the customer see total silence. Log with enough context to trace
        # the specific failed order, then send a plain apology so they know to retry
        # rather than assuming the bot never got their message.
        logging.exception(
            "WhatsApp webhook dispatch failed: restaurant_id=%s from_number=%s message_id=%s type=%s",
            restaurant.get("id"), from_number, message_id, msg.get("type"),
        )
        try:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "⚠️ Something went wrong on our end processing that. Please try again in a moment, or resend your message.",
            )
        except Exception:
            logging.exception("Also failed to send the fallback error message to %s", from_number)


def parse_requested_items(order_payload: dict) -> tuple[dict[str, int], list[str]]:
    """Pure: aggregate quantities per retailer_id and surface structural problems.

    Returns (requested, problems) where *requested* maps catalog_retailer_id to
    the total integer quantity requested, and *problems* is a deduplicated list
    of human-readable issue strings.
    """
    requested: dict[str, int] = {}
    problems: list[str] = []
    seen_problems: set[str] = set()

    def _add_problem(msg: str) -> None:
        if msg not in seen_problems:
            seen_problems.add(msg)
            problems.append(msg)

    for item in order_payload.get("product_items") or []:
        retailer_id = item.get("product_retailer_id")
        raw_qty = item.get("quantity")

        # Validate retailer id
        if not retailer_id or not isinstance(retailer_id, str) or not retailer_id.strip():
            _add_problem("an item has an invalid quantity")
            continue

        # Validate quantity: must be a positive integer
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            _add_problem("an item has an invalid quantity")
            continue
        if qty <= 0:
            _add_problem("an item has an invalid quantity")
            continue

        requested[retailer_id] = requested.get(retailer_id, 0) + qty

    # Drop lines whose aggregated quantity exceeds MAX_LINE_QTY
    oversized = [rid for rid, qty in requested.items() if qty > MAX_LINE_QTY]
    for rid in oversized:
        problems.append(f"{rid} quantity is too large")
        del requested[rid]

    return requested, problems


def classify_cart_lines(
    requested: dict[str, int],
    rows_by_rid: dict,
) -> tuple[dict, list[dict], list[str]]:
    """Pure: classify catalog rows into simple cart lines, composites, and problems.

    *rows_by_rid* maps catalog_retailer_id -> a menu_item_catalog_map row that
    must include an embedded ``menu_items`` dict with keys:
        name, price, is_available, item_type, menu_categories (optional embed).

    Returns (cart, composites, problems):
        cart        – keyed by menu_item_id, merged when two retailer ids share one
        composites  – list of {"menu_item_id", "name", "base_price": float, "qty"}
        problems    – human-readable issue strings
    """
    cart: dict = {}
    composites: list[dict] = []
    problems: list[str] = []

    for rid, qty in requested.items():
        row = rows_by_rid.get(rid)
        if row is None:
            problems.append("an item is no longer in this restaurant's catalog")
            continue

        menu_item = row.get("menu_items") or {}
        name = menu_item.get("name") or "an item"

        # Availability check
        if not menu_item.get("is_available"):
            problems.append(f"{name} is unavailable")
            continue

        # Category active check — missing embed is treated as active
        category = menu_item.get("menu_categories")
        if category is not None and not category.get("is_active", True):
            problems.append(f"{name} is unavailable")
            continue

        menu_item_id = row.get("menu_item_id")
        item_type = menu_item.get("item_type") or "simple"

        if item_type == "composite":
            composites.append({
                "menu_item_id": menu_item_id,
                "name": name,
                "base_price": float(money(menu_item.get("price"))),
                "qty": qty,
            })
            continue

        # Simple item — merge quantities if two retailer ids map to the same menu_item_id
        if menu_item_id in cart:
            cart[menu_item_id]["qty"] += qty
        else:
            cart[menu_item_id] = {
                "name": name,
                "price": float(money(menu_item.get("price"))),
                "qty": qty,
                "menu_item_id": menu_item_id,
            }

    return cart, composites, problems


async def build_authoritative_cart(
    order_payload: dict, restaurant_id: str, supabase
) -> tuple[dict, list[dict], list[str]]:
    """Build a validated cart from a Meta order payload.

    Returns (cart, composites, problems):
        cart        – simple items keyed by menu_item_id, ready for checkout
        composites  – composite items that need modifier selection
        problems    – human-readable strings describing items that were rejected
    """
    requested, problems = parse_requested_items(order_payload)

    if not requested:
        return {}, [], problems

    # ONE batched query for all retailer ids in this order
    result = supabase.table("menu_item_catalog_map").select(
        "catalog_retailer_id, menu_item_id, "
        "menu_items(name, price, is_available, item_type, category_id, menu_categories(is_active))"
    ).in_("catalog_retailer_id", list(requested)).eq("restaurant_id", restaurant_id).execute()

    rows_by_rid: dict = {row["catalog_retailer_id"]: row for row in result.data or []}

    cart, composites, classify_problems = classify_cart_lines(requested, rows_by_rid)
    problems.extend(classify_problems)

    # Inventory validation — remove short lines and report shortages
    shortages = await validate_cart_inventory(cart, restaurant_id)
    for shortage in shortages:
        mid = shortage.get("menu_item_id") or next(
            (k for k, v in cart.items() if v.get("name") == shortage["name"]), None
        )
        available = int(shortage.get("available") or 0)
        name = shortage["name"]
        requested_qty = int(shortage.get("requested") or 0)
        if available <= 0:
            problems.append(f"{name} is sold out")
        else:
            problems.append(f"Only {available} {name} left (you asked for {requested_qty})")
        # Remove the short line from the cart regardless of key form
        if mid and mid in cart:
            del cart[mid]
        else:
            cart = {k: v for k, v in cart.items() if v.get("name") != name}

    return cart, composites, problems


def price_composite_line(base_price, modifiers: dict, fresh_options_by_id: dict) -> tuple[Decimal, dict]:
    unit_price = money(base_price)
    refreshed_modifiers = {}
    for group_id, picks in modifiers.items():
        refreshed_picks = []
        for pick in picks:
            option = fresh_options_by_id.get(pick.get("option_id"))
            if not option or not option.get("is_available"):
                name = (option or {}).get("name") or pick.get("name") or "An option"
                raise ValueError(f"{name} is no longer available")
            quantity = int(pick.get("quantity") or 0)
            price_delta = money(option.get("price_delta"))
            unit_price += price_delta * quantity
            refreshed_picks.append({
                **pick,
                "price_delta": float(price_delta),
            })
        refreshed_modifiers[group_id] = refreshed_picks
    return unit_price, refreshed_modifiers


async def recalculate_total(state: WhatsAppState, restaurant: dict) -> Decimal:
    """Refresh item prices from menu_items and calculate the final total with Decimal."""
    data = await state.get_data()
    cart = data.get("cart") or {}
    refreshed_cart = {}
    subtotal = Decimal("0")
    option_ids = list(dict.fromkeys(
        pick.get("option_id")
        for line in cart.values()
        for picks in (line.get("modifiers") or {}).values()
        for pick in picks
        if pick.get("option_id")
    ))
    fresh_options_by_id = {}
    if option_ids:
        options = state.supabase.table("modifier_options").select(
            "id, name, price_delta, is_available, modifier_groups!inner(menu_item_id, menu_items!inner(restaurant_id))"
        ).in_("id", option_ids).eq(
            "modifier_groups.menu_items.restaurant_id", restaurant["id"]
        ).execute()
        fresh_options_by_id = {
            option.get("id"): option for option in options.data or []
        }

    for key, line in cart.items():
        menu_item_id = line.get("menu_item_id", key)
        row = state.supabase.table("menu_items").select("id, name, price, is_available").eq("id", menu_item_id).eq("restaurant_id", restaurant["id"]).execute()
        if not row.data or not row.data[0].get("is_available"):
            raise ValueError(f"{line.get('name', 'An item')} is no longer available")
        item = row.data[0]
        quantity = int(line.get("qty") or 0)
        if quantity <= 0:
            raise ValueError("Cart contains an invalid quantity")
        modifiers = line.get("modifiers") or {}
        if modifiers:
            unit_price, refreshed_modifiers = price_composite_line(
                item["price"], modifiers, fresh_options_by_id
            )
        else:
            unit_price = money(item["price"])
        refreshed_line = {
            **line,
            "menu_item_id": item["id"],
            "name": item["name"],
            "price": float(unit_price),
            "qty": quantity,
        }
        if modifiers:
            refreshed_line["modifiers"] = refreshed_modifiers
        refreshed_cart[key] = refreshed_line
        subtotal += unit_price * quantity

    delivery_fee = money(data.get("delivery_fee")) if data.get("order_type") == "delivery" else Decimal("0")
    total = subtotal + delivery_fee
    await state.update_data(cart=refreshed_cart, total_price=float(total), delivery_fee=float(delivery_fee))
    return total


def format_partial_cart_body(problems: list[str], max_length: int = 1000) -> str:
    prefix = "Some items couldn't be added:\n"
    suffix = "\n\nContinue with the rest?"
    available = max(0, max_length - len(prefix) - len(suffix))
    details = ""
    for problem in problems:
        bullet = f"• {problem}"
        addition = ("\n" if details else "") + bullet
        if len(details) + len(addition) > available:
            remaining = available - len(details) - (1 if details else 0)
            if remaining > 0:
                details += ("\n" if details else "") + bullet[:remaining]
            break
        details += addition
    return prefix + details + suffix


async def begin_delivery_or_pickup(state, restaurant, from_number):
    if restaurant.get("pickup_enabled"):
        await state.set_state("waiting_for_order_type")
        await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "How would you like to receive your order?", "Choose order type", [
                            {"id": "order_type_delivery", "title": "Delivery"},
                            {"id": "order_type_pickup", "title": "Pickup"},
                        ])
    else:
        await start_delivery_address_step(state, restaurant, from_number)


async def begin_checkout(state, restaurant, from_number):
    data = await state.get_data()
    if data.get("pending_composites"):
        await begin_composite_configuration(state, restaurant, from_number)
        return
    await begin_delivery_or_pickup(state, restaurant, from_number)


async def fetch_composite_groups(state, menu_item_id, restaurant):
    result = state.supabase.table("modifier_groups").select(
        "id, menu_item_id, name, selection_mode, min_select, max_select, allow_quantity, display_order, "
        "modifier_options(id, name, price_delta, unit_label, is_available, display_order), "
        "menu_items!inner(restaurant_id)"
    ).eq("menu_item_id", menu_item_id).eq("is_active", True).eq(
        "menu_items.restaurant_id", restaurant["id"]
    ).order("display_order").execute()
    groups = result.data or []
    for group in groups:
        group["modifier_options"] = sorted(
            group.get("modifier_options") or [],
            key=lambda option: option.get("display_order") or 0,
        )
    return groups


async def begin_composite_configuration(state, restaurant, from_number):
    data = await state.get_data()
    queue, exceeded = expand_composite_queue(data.get("pending_composites") or [])
    if exceeded:
        await state.update_data(pending_composites=[], composite_queue=[], active_unit=None)
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            f"Please order fewer composite items per cart (maximum {MAX_COMPOSITE_UNITS} units). "
            "We'll continue with the other items.",
        )
        if not (data.get("cart") or {}):
            await state.clear()
            return
        await begin_delivery_or_pickup(state, restaurant, from_number)
        return
    await state.update_data(composite_queue=queue, active_unit=None)
    await next_composite_unit(state, restaurant, from_number)


async def next_composite_unit(state, restaurant, from_number):
    data = await state.get_data()
    queue = data.get("composite_queue") or []
    if not queue:
        await state.update_data(pending_composites=[], composite_queue=[], active_unit=None)
        await begin_delivery_or_pickup(state, restaurant, from_number)
        return

    active_unit = queue[0]
    await state.update_data(composite_queue=queue[1:], active_unit=None)
    groups = await fetch_composite_groups(state, active_unit.get("menu_item_id"), restaurant)
    active_unit = {
        **active_unit,
        "group_index": 0,
        "selections": {},
        "option_page": 0,
        "pending_option_id": None,
    }
    if not groups:
        menu_item = state.supabase.table("menu_items").select(
            "id, name, price, is_available"
        ).eq("id", active_unit["menu_item_id"]).eq("restaurant_id", restaurant["id"]).execute()
        if menu_item.data and menu_item.data[0].get("is_available"):
            cart_key, line, _ = build_composite_cart_line(menu_item.data[0], {})
            cart = (await state.get_data()).get("cart") or {}
            cart[cart_key] = line
            await state.update_data(cart=cart)
        else:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                f"{active_unit.get('name', 'This item')} is temporarily unavailable.",
            )
        await next_composite_unit(state, restaurant, from_number)
        return

    unavailable_required = any(
        int(group.get("min_select") or 0) > 0
        and not any(option.get("is_available") for option in group.get("modifier_options") or [])
        for group in groups
    )
    if unavailable_required:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            f"{active_unit.get('name', 'This item')} is temporarily unavailable.",
        )
        await next_composite_unit(state, restaurant, from_number)
        return

    await state.update_data(active_unit=active_unit, modifier_note=None)
    await state.set_state("configuring_modifiers")
    await render_modifier_group(state, restaurant, from_number)


async def render_modifier_group(state, restaurant, from_number):
    data = await state.get_data()
    active_unit = data.get("active_unit") or {}
    groups = await fetch_composite_groups(state, active_unit.get("menu_item_id"), restaurant)
    group_index = int(active_unit.get("group_index") or 0)
    if group_index >= len(groups):
        await finish_composite_unit(state, restaurant, from_number)
        return
    group = groups[group_index]
    options = [option for option in group.get("modifier_options") or [] if option.get("is_available")]
    if int(group.get("min_select") or 0) > 0 and not options:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            f"{active_unit.get('name', 'This item')} is temporarily unavailable.",
        )
        await next_composite_unit(state, restaurant, from_number)
        return
    rows, page, _ = build_modifier_rows(group["id"], group, options, active_unit.get("option_page", 0))
    active_unit["option_page"] = page
    await state.update_data(active_unit=active_unit)
    prompt = "Choose one" if group.get("selection_mode") == "single" else "Choose one or more"
    body_lines = [
        f"{active_unit.get('name', 'Item')} (unit {active_unit.get('unit_no', 1)} of {active_unit.get('units_total', 1)})",
        group.get("name", "Choose an option"),
        prompt,
    ]
    selected = (active_unit.get("selections") or {}).get(group["id"], [])
    if group.get("selection_mode") == "multi" and selected:
        selected_names = [
            f"{pick.get('name', 'Option')} x{pick.get('quantity', 1)}" if int(pick.get("quantity") or 1) > 1 else pick.get("name", "Option")
            for pick in selected
        ]
        body_lines.append("Selected: " + ", ".join(selected_names))
    note = data.get("modifier_note")
    if note:
        body_lines.insert(0, note)
        await state.update_data(modifier_note=None)
    body = truncate("\n".join(body_lines), 1000)
    await send_list(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        body, "Choose", rows,
    )


async def finish_composite_unit(state, restaurant, from_number):
    data = await state.get_data()
    active_unit = data.get("active_unit") or {}
    menu_item_result = state.supabase.table("menu_items").select(
        "id, name, price, is_available"
    ).eq("id", active_unit.get("menu_item_id")).eq("restaurant_id", restaurant["id"]).execute()
    if not menu_item_result.data or not menu_item_result.data[0].get("is_available"):
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            f"{active_unit.get('name', 'This item')} is temporarily unavailable.",
        )
        await next_composite_unit(state, restaurant, from_number)
        return
    menu_item = menu_item_result.data[0]
    selections = active_unit.get("selections") or {}
    option_ids = list(dict.fromkeys(
        pick.get("option_id") for picks in selections.values() for pick in picks if pick.get("option_id")
    ))
    options_by_id = {}
    if option_ids:
        option_result = state.supabase.table("modifier_options").select(
            "id, name, price_delta, is_available, modifier_groups!inner(menu_item_id, menu_items!inner(restaurant_id))"
        ).in_("id", option_ids).eq(
            "modifier_groups.menu_items.restaurant_id", restaurant["id"]
        ).eq("modifier_groups.menu_item_id", menu_item["id"]).execute()
        options_by_id = {option.get("id"): option for option in option_result.data or []}
    refreshed_selections = {}
    for group_id, picks in selections.items():
        refreshed_picks = []
        for pick in picks:
            option = options_by_id.get(pick.get("option_id"))
            if not option or not option.get("is_available"):
                await send_text(
                    restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                    f"{(option or {}).get('name') or pick.get('name') or 'An option'} is no longer available. "
                    "Please choose another option.",
                )
                selections[group_id] = [
                    selected for selected in picks
                    if selected.get("option_id") != pick.get("option_id")
                ]
                active_unit["selections"] = selections
                current_groups = await fetch_composite_groups(state, menu_item["id"], restaurant)
                active_unit["group_index"] = next(
                    (index for index, current_group in enumerate(current_groups)
                     if current_group.get("id") == group_id),
                    0,
                )
                active_unit["option_page"] = 0
                await state.update_data(active_unit=active_unit, modifier_note=None)
                await state.set_state("configuring_modifiers")
                await render_modifier_group(state, restaurant, from_number)
                return
            refreshed_picks.append({
                "option_id": option["id"],
                "name": option["name"],
                "price_delta": float(money(option.get("price_delta"))),
                "quantity": int(pick.get("quantity") or 1),
            })
        refreshed_selections[group_id] = refreshed_picks
    cart_key, line, _ = build_composite_cart_line(menu_item, refreshed_selections)
    cart = data.get("cart") or {}
    cart[cart_key] = line
    same_units, _ = filter_same_composite_units(
        data.get("composite_queue") or [], menu_item["id"]
    )
    if same_units:
        same_for_all_template = {
            "menu_item": {
                "id": menu_item["id"],
                "name": menu_item["name"],
                "price": float(money(menu_item.get("price"))),
            },
            "selections": refreshed_selections,
        }
        await state.update_data(
            cart=cart, active_unit=None, same_for_all_template=same_for_all_template
        )
        await state.set_state("confirming_same_for_all")
        await send_buttons(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Use these choices for the remaining units of this item?",
            [
                {"id": "same_all", "title": "Same for all"},
                {"id": "customize_each", "title": "Customize each"},
            ],
        )
        return
    await state.update_data(cart=cart, active_unit=None, same_for_all_template=None)
    await next_composite_unit(state, restaurant, from_number)


async def apply_same_composite_to_queue(state, restaurant, from_number):
    data = await state.get_data()
    template = data.get("same_for_all_template") or {}
    menu_item = template.get("menu_item") or {}
    selections = template.get("selections") or {}
    matching_units, remaining_queue = filter_same_composite_units(
        data.get("composite_queue") or [], menu_item.get("id")
    )
    cart = data.get("cart") or {}
    for _unit in matching_units:
        cart_key, line, _summary = build_composite_cart_line(menu_item, selections)
        cart[cart_key] = line
    await state.update_data(
        cart=cart,
        composite_queue=remaining_queue,
        same_for_all_template=None,
    )
    await next_composite_unit(state, restaurant, from_number)


async def handle_cart_submission(msg, state, restaurant, from_number, contact_name, supabase):
    if not await is_subscription_active(restaurant["id"]):
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "We're not accepting orders right now. Please try again later.")
        return
    cart, composites, problems = await build_authoritative_cart(msg.get("order") or {}, restaurant["id"], supabase)

    # Composite items cannot be completed through the catalog flow yet
    if composites and not WHATSAPP_COMPOSITES_ENABLED:
        for composite in composites:
            problems.append(
                f"{composite['name']} needs choices we can't take through the catalog yet. "
                "Please order it on Telegram or call the restaurant."
            )
        composites = []

    if not cart and not composites:
        await state.clear()
        detail = "\n• ".join(problems) if problems else "no available items were found"
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ We couldn't accept this cart:\n• {detail}\n\nPlease update it and send it again.")
        return
    await state.update_data(
        cart=cart, restaurant_id=restaurant["id"], restaurant_name=restaurant["name"],
        kitchen_chat_id=restaurant.get("kitchen_chat_id"), customer_name=contact_name, delivery_fee=0,
        pending_composites=[
            {"menu_item_id": item.get("menu_item_id"), "name": item.get("name", "Item"), "qty": int(item.get("qty") or 0)}
            for item in composites
        ] if WHATSAPP_COMPOSITES_ENABLED else [],
        composite_queue=[], active_unit=None,
    )
    if problems:
        await state.set_state("confirming_partial_cart")
        await send_buttons(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            format_partial_cart_body(problems),
            [{"id": "partial_continue", "title": "Continue without"}, {"id": "partial_cancel", "title": "Cancel"}],
        )
        return
    await begin_checkout(state, restaurant, from_number)


async def start_delivery_address_step(state, restaurant, from_number):
    await state.update_data(order_type="delivery", delivery_fee=0, delivery_zone_id=None, delivery_zone_name=None)
    await state.set_state("waiting_for_address")
    await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                    "📍 Please share your delivery address or location pin.")


async def choose_pickup(state, restaurant, from_number):
    await state.update_data(order_type="pickup", delivery_fee=0, delivery_zone_id=None, delivery_zone_name=None)
    await recalculate_total(state, restaurant)
    await state.set_state("waiting_for_payment_method")
    await send_payment_options(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "pickup", bool(restaurant.get("paystack_enabled")))


async def continue_after_delivery_address(state, restaurant, from_number):
    fee_type = restaurant.get("delivery_fee_type") or "none"
    if fee_type == "flat":
        await state.update_data(delivery_fee=float(money(restaurant.get("delivery_fee_flat"))), delivery_zone_id=None, delivery_zone_name=None)
        await request_payment_method(state, restaurant, from_number)
        return
    if fee_type != "zone":
        await state.update_data(delivery_fee=0, delivery_zone_id=None, delivery_zone_name=None)
        await request_payment_method(state, restaurant, from_number)
        return

    zones = state.supabase.table("delivery_zones").select("id, zone_name, fee").eq("restaurant_id", restaurant["id"]).eq("is_active", True).order("display_order").execute().data or []
    if len(zones) > MAX_WHATSAPP_LIST_ROWS:
        logging.error("Restaurant %s has %s active delivery zones; WhatsApp supports at most %s", restaurant["id"], len(zones), MAX_WHATSAPP_LIST_ROWS)
        await handle_unusable_zone_config(state, restaurant, from_number, "The delivery-zone setup needs attention")
        return
    if not zones:
        await handle_unusable_zone_config(state, restaurant, from_number, "No active delivery zones are configured")
        return
    await state.set_state("waiting_for_delivery_zone")
    await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                    "Choose the area closest to your delivery address.", "Choose zone", [
                        {"id": f"zone:{zone['id']}", "title": zone["zone_name"], "description": f"₦{float(money(zone['fee'])):,.0f}"}
                        for zone in zones
                    ])


async def handle_unusable_zone_config(state, restaurant, from_number, reason: str):
    # A configured flat amount is the only safe fallback for a zone-priced delivery flow.
    flat_fee = money(restaurant.get("delivery_fee_flat"))
    if flat_fee > 0:
        await state.update_data(delivery_fee=float(flat_fee), delivery_zone_id=None, delivery_zone_name=None)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {reason}; using the restaurant's flat delivery fee of ₦{float(flat_fee):,.0f}.")
        await request_payment_method(state, restaurant, from_number)
    elif restaurant.get("pickup_enabled"):
        await state.set_state("waiting_for_order_type")
        await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {reason}. Delivery is unavailable; please choose pickup.", "Choose order type", [{"id": "order_type_pickup", "title": "Pickup"}])
    else:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {reason}. Delivery is temporarily unavailable; please contact the restaurant.")
        await state.clear()


async def request_payment_method(state, restaurant, from_number):
    total = await recalculate_total(state, restaurant)
    await state.set_state("waiting_for_payment_method")
    await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"💰 Total: ₦{float(total):,.0f}")
    data = await state.get_data()
    await send_payment_options(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, data.get("order_type", "delivery"), bool(restaurant.get("paystack_enabled")))


async def advance_composite_group(state, restaurant, from_number, active_unit):
    active_unit["group_index"] = int(active_unit.get("group_index") or 0) + 1
    active_unit["option_page"] = 0
    active_unit["pending_option_id"] = None
    await state.update_data(active_unit=active_unit, modifier_note=None)
    await render_modifier_group(state, restaurant, from_number)


async def fetch_modifier_option(state, option_id, group_id, restaurant):
    result = state.supabase.table("modifier_options").select(
        "id, group_id, name, price_delta, unit_label, is_available, "
        "modifier_groups!inner(menu_item_id, menu_items!inner(restaurant_id))"
    ).eq("id", option_id).eq("group_id", group_id).eq(
        "modifier_groups.menu_items.restaurant_id", restaurant["id"]
    ).execute()
    return result.data[0] if result.data else None


async def handle_modifier_reply(reply_id, state, restaurant, from_number):
    data = await state.get_data()
    active_unit = data.get("active_unit") or {}
    groups = await fetch_composite_groups(state, active_unit.get("menu_item_id"), restaurant)
    group_index = int(active_unit.get("group_index") or 0)
    if group_index >= len(groups):
        await finish_composite_unit(state, restaurant, from_number)
        return
    group = groups[group_index]
    group_id = group.get("id")
    selections = active_unit.get("selections") or {}
    picks = selections.get(group_id, [])

    if reply_id == "mod_done":
        error = validate_group_selection(group, picks)
        if group.get("selection_mode") != "multi":
            error = "Please choose an option from the current list."
        if error:
            await state.update_data(modifier_note=error)
            await render_modifier_group(state, restaurant, from_number)
            return
        await advance_composite_group(state, restaurant, from_number, active_unit)
        return
    if reply_id == "mod_skip":
        if not can_skip_group(group):
            await state.update_data(modifier_note="This option is required. Please choose one.")
            await render_modifier_group(state, restaurant, from_number)
            return
        await advance_composite_group(state, restaurant, from_number, active_unit)
        return
    if reply_id == "mod_more":
        _, _, page_count = build_modifier_rows(
            group_id, group, group.get("modifier_options") or [], active_unit.get("option_page", 0)
        )
        active_unit["option_page"] = min(int(active_unit.get("option_page") or 0) + 1, page_count - 1)
        await state.update_data(active_unit=active_unit)
        await render_modifier_group(state, restaurant, from_number)
        return

    parts = reply_id.split(":") if isinstance(reply_id, str) else []
    if len(parts) != 3 or parts[0] != "mod" or parts[1] != group_id:
        await state.update_data(modifier_note="Please choose an option from the current list.")
        await render_modifier_group(state, restaurant, from_number)
        return
    option = await fetch_modifier_option(state, parts[2], group_id, restaurant)
    if not option or not option.get("is_available"):
        note = f"{(option or {}).get('name') or 'That option'} is no longer available."
        await state.update_data(modifier_note=note)
        await render_modifier_group(state, restaurant, from_number)
        return
    maximum = group.get("max_select")
    if maximum is not None and len(picks) >= int(maximum):
        await state.update_data(modifier_note=f"You can choose no more than {maximum} option(s).")
        await render_modifier_group(state, restaurant, from_number)
        return

    if group.get("allow_quantity"):
        active_unit["pending_option_id"] = option["id"]
        await state.update_data(active_unit=active_unit)
        await state.set_state("entering_modifier_qty")
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            f"How many {option.get('name', 'items')} ({option.get('unit_label') or 'units'})? Reply with a number from 1 to 20.",
        )
        return

    picks.append({
        "option_id": option["id"],
        "name": option.get("name", "Option"),
        "price_delta": float(money(option.get("price_delta"))),
        "quantity": 1,
    })
    selections[group_id] = picks
    active_unit["selections"] = selections
    await state.update_data(active_unit=active_unit, modifier_note=None)
    if group.get("selection_mode") == "single":
        await advance_composite_group(state, restaurant, from_number, active_unit)
    else:
        await render_modifier_group(state, restaurant, from_number)


async def handle_modifier_quantity(msg, state, restaurant, from_number):
    text_data = msg.get("text") or {}
    body = text_data.get("body") or "" if isinstance(text_data, dict) else ""
    quantity = parse_modifier_quantity(body)
    if quantity is None:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Please reply with a number from 1 to 20.",
        )
        return
    data = await state.get_data()
    active_unit = data.get("active_unit") or {}
    groups = await fetch_composite_groups(state, active_unit.get("menu_item_id"), restaurant)
    group_index = int(active_unit.get("group_index") or 0)
    if group_index >= len(groups):
        await state.set_state("configuring_modifiers")
        await finish_composite_unit(state, restaurant, from_number)
        return
    group = groups[group_index]
    option_id = active_unit.get("pending_option_id")
    option = await fetch_modifier_option(state, option_id, group.get("id"), restaurant)
    if not option or not option.get("is_available"):
        active_unit["pending_option_id"] = None
        await state.update_data(active_unit=active_unit, modifier_note="That option is no longer available.")
        await state.set_state("configuring_modifiers")
        await render_modifier_group(state, restaurant, from_number)
        return
    selections = active_unit.get("selections") or {}
    picks = selections.get(group["id"], [])
    maximum = group.get("max_select")
    if maximum is not None and len(picks) >= int(maximum):
        active_unit["pending_option_id"] = None
        await state.update_data(active_unit=active_unit, modifier_note=f"You can choose no more than {maximum} option(s).")
        await state.set_state("configuring_modifiers")
        await render_modifier_group(state, restaurant, from_number)
        return
    picks.append({
        "option_id": option["id"],
        "name": option.get("name", "Option"),
        "price_delta": float(money(option.get("price_delta"))),
        "quantity": quantity,
    })
    selections[group["id"]] = picks
    active_unit["selections"] = selections
    active_unit["pending_option_id"] = None
    await state.update_data(active_unit=active_unit, modifier_note=None)
    await state.set_state("configuring_modifiers")
    if group.get("selection_mode") == "single":
        await advance_composite_group(state, restaurant, from_number, active_unit)
    else:
        await render_modifier_group(state, restaurant, from_number)


async def handle_location(msg, state, restaurant, from_number, current_state):
    if current_state != "waiting_for_address":
        return
    location = msg.get("location") or {}
    if "latitude" not in location or "longitude" not in location:
        return
    lat = float(location["latitude"])
    lon = float(location["longitude"])

    # Save coordinates immediately so we never lose them, even if reverse geocoding fails
    # or the customer never responds to the confirm prompt below.
    fallback_address = location.get("address") or format_delivery_coordinates(lat, lon)
    await state.update_data(delivery_address=fallback_address, delivery_lat=lat, delivery_lon=lon)

    resolved = await reverse_geocode(lat, lon)
    address = resolved or fallback_address
    await state.update_data(delivery_address=address)

    # Nominatim results can be rough for Nigerian addresses, so confirm before
    # proceeding — mirrors the Telegram flow's confirm/retype step.
    await state.set_state("confirming_address")
    await send_buttons(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        f"📍 We found this address:\n\n{address}\n\nIs this correct?", [
                            {"id": "address_confirmed", "title": "Yes, correct"},
                            {"id": "address_retype", "title": "No, retype"},
                        ])


async def handle_text(msg, state, restaurant, from_number, current_state):
    if current_state == "entering_modifier_qty":
        await handle_modifier_quantity(msg, state, restaurant, from_number)
        return
    if current_state not in ("waiting_for_address", "confirming_address"):
        await handle_freeform_message(msg, state, restaurant, from_number, current_state)
        return
    address = ((msg.get("text") or {}).get("body") or "").strip()
    if len(address) < 10:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please send a complete delivery address or a location pin.")
        return
    await state.update_data(delivery_address=address, delivery_lat=None, delivery_lon=None)
    await state.set_state("waiting_for_address")
    await continue_after_delivery_address(state, restaurant, from_number)


async def handle_freeform_message(msg, state, restaurant, from_number, current_state):
    """Handle idle-session text: no active checkout in progress.

    This is the seam for a future AI/RAG assistant (menu questions, hours, FAQs).
    It must stay read-only: never create orders, touch payment state, or write to
    whatsapp_sessions here. If a future agent wants to nudge someone toward
    ordering, have it reply with a prompt rather than attempting to build a cart.
    Kept as a simple, safe fallback until that agent is wired in.
    """
    await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                    f"Hi! To order from {restaurant['name']}, please browse our WhatsApp catalog and send your cart. "
                    "Need help? Just ask and we'll get back to you.")


async def handle_interactive(msg, state, restaurant, from_number, supabase, bot, current_state):
    reply_id = interactive_reply_id(msg)
    if isinstance(reply_id, str) and (reply_id.startswith("mod:") or reply_id in {"mod_done", "mod_skip", "mod_more"}):
        if current_state == "configuring_modifiers":
            await handle_modifier_reply(reply_id, state, restaurant, from_number)
        return
    if current_state == "confirming_same_for_all":
        if reply_id == "same_all":
            await apply_same_composite_to_queue(state, restaurant, from_number)
        elif reply_id == "customize_each":
            await state.update_data(same_for_all_template=None)
            await next_composite_unit(state, restaurant, from_number)
        else:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "Please tap Same for all or Customize each.",
            )
    elif current_state == "confirming_partial_cart":
        if reply_id == "partial_continue":
            await begin_checkout(state, restaurant, from_number)
        elif reply_id == "partial_cancel":
            await state.clear()
            await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                            "Cancelled. Send a new cart whenever you're ready.")
        else:
            await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                            "Please tap Continue or Cancel.")
    elif reply_id == "order_type_delivery" and current_state == "waiting_for_order_type":
        await start_delivery_address_step(state, restaurant, from_number)
    elif reply_id == "order_type_pickup" and current_state == "waiting_for_order_type":
        await choose_pickup(state, restaurant, from_number)
    elif reply_id == "address_confirmed" and current_state == "confirming_address":
        await continue_after_delivery_address(state, restaurant, from_number)
    elif reply_id == "address_retype" and current_state == "confirming_address":
        await state.set_state("waiting_for_address")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "✍️ Please type your delivery address, or share a location pin again.")
    elif reply_id and reply_id.startswith("zone:") and current_state == "waiting_for_delivery_zone":
        zone_id = reply_id.split(":", 1)[1]
        zone = supabase.table("delivery_zones").select("id, zone_name, fee").eq("id", zone_id).eq("restaurant_id", restaurant["id"]).eq("is_active", True).execute()
        if not zone.data:
            await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "That delivery zone is no longer available. Please choose another one.")
            return
        selected = zone.data[0]
        await state.update_data(delivery_zone_id=selected["id"], delivery_zone_name=selected["zone_name"], delivery_fee=float(money(selected["fee"])))
        await request_payment_method(state, restaurant, from_number)
    else:
        await handle_payment_selection(reply_id, state, restaurant, from_number, supabase, bot, current_state)


async def handle_payment_selection(button_id, state, restaurant, from_number, supabase, bot, current_state):
    if current_state != "waiting_for_payment_method" or button_id not in {"pay_cash", "pay_bank", "pay_pod", "pay_paystack"}:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please choose a payment option from the current order.")
        return
    data = await state.get_data()
    if button_id == "pay_pod" and data.get("order_type") != "delivery":
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Pay on delivery is only available for delivery orders.")
        return
    try:
        total = await recalculate_total(state, restaurant)
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {exc}. Please send a new catalog cart.")
        return
    customer_name = data.get("customer_name", "Customer")
    if button_id == "pay_bank":
        info = supabase.table("restaurants").select("bank_name, account_number, account_name").eq("id", restaurant["id"]).execute().data or [{}]
        info = info[0]
        if not info.get("bank_name") or not info.get("account_number"):
            await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Bank transfer isn't available right now. Please choose another payment method.")
            return
        await state.update_data(payment_method="Bank Transfer")
        await state.set_state("waiting_for_payment_proof")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"Bank Transfer Details\n\nAmount: ₦{float(total):,.0f}\nBank: {info['bank_name']}\nAccount Number: {info['account_number']}\nAccount Name: {info['account_name']}\n\nSend a screenshot of your payment receipt.")
        return
    if button_id == "pay_paystack":
        await start_paystack_payment(state, restaurant, from_number, customer_name, total)
        return
    payment_method = "Cash Payment" if button_id == "pay_cash" else "Pay on Delivery"
    await state.update_data(payment_method=payment_method)
    await create_and_send_order(state, restaurant, from_number, customer_name, payment_method, bot, total)


async def create_and_send_order(state, restaurant, from_number, customer_name, payment_method, bot, total):
    try:
        order_id, _ = await create_order_in_db(None, state, payment_method, customer_name, "whatsapp", None, from_number)
        await send_order_to_kitchen(bot, order_id, state, customer_name, from_number)
        low_stock_items = await deduct_inventory_for_order(order_id)
        await send_restock_alert(bot, restaurant["id"], restaurant.get("kitchen_chat_id"), low_stock_items)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order placed!\n\nOrder ID: #{order_id[:8]}\nTotal: ₦{float(total):,.0f}\nPayment: {payment_method}\n\nWe'll notify you when it is ready.")
        await send_order_receipt(bot, {
            "order_channel": "whatsapp",
            "customer_contact": from_number,
            "restaurants": restaurant,
        }, order_id)
        await state.clear()
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {exc}")
    except Exception:
        logging.exception("WhatsApp order error")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Something went wrong placing your order. Please try again or contact the restaurant.")


async def start_paystack_payment(state, restaurant, from_number, customer_name, total):
    if not restaurant.get("paystack_enabled") or not restaurant.get("paystack_subaccount_code"):
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Paystack is not available for this restaurant. Please choose another payment method.")
        return
    try:
        await state.update_data(payment_method="Paystack")
        order_id, _ = await create_order_in_db(None, state, "Paystack", customer_name, "whatsapp", None, from_number)
        # WhatsApp has no verified email field. This documented deterministic placeholder meets Paystack's required email input.
        email = f"{''.join(character for character in from_number if character.isdigit())}@chowlin.ng"
        payment_url = await create_paystack_payment_link(order_id, float(total), email, restaurant["paystack_subaccount_code"])
        state.supabase.table("payments").insert({"order_id": order_id, "restaurant_id": restaurant["id"], "amount": str(total), "provider": "Paystack", "status": "pending", "provider_reference": order_id, "paystack_reference": order_id, "paystack_subaccount_code": restaurant["paystack_subaccount_code"]}).execute()
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order #{order_id[:8]} is pending payment.\nTotal: ₦{float(total):,.0f}\n\nComplete payment here:\n{payment_url}\n\nWe'll confirm payment and send your order to the kitchen.")
        await state.clear()
    except Exception:
        logging.exception("WhatsApp Paystack setup failed")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Could not create a Paystack payment link. Please choose another payment method.")


async def download_whatsapp_media(media_id: str, token: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient() as client:
        meta = await client.get(f"{GRAPH_API}/{media_id}", headers={"Authorization": f"Bearer {token}"})
        meta.raise_for_status()
        media = await client.get(meta.json()["url"], headers={"Authorization": f"Bearer {token}"})
        media.raise_for_status()
        return media.content, meta.json().get("mime_type", "image/jpeg")


async def handle_payment_proof(msg, state, restaurant, from_number, supabase, bot, current_state):
    if current_state != "waiting_for_payment_proof" or (await state.get_data()).get("payment_method") != "Bank Transfer":
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please choose Bank Transfer before sending a payment receipt.")
        return
    media_id = (msg.get("image") or {}).get("id")
    if not media_id:
        return
    try:
        image_bytes, _ = await download_whatsapp_media(media_id, restaurant["whatsapp_access_token"])
        data = await state.get_data()
        total = await recalculate_total(state, restaurant)
        shortages = await validate_cart_inventory(data.get("cart") or {}, restaurant["id"])
        if shortages:
            await send_stock_conflict_to_kitchen(bot, restaurant, data, from_number, image_bytes, shortages)
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "We received your payment proof, but some items ran out while you were paying. "
                "The restaurant has been notified and will contact you shortly. Please keep your payment receipt.",
            )
            await state.clear()
            return
        order_id, _ = await create_order_in_db(None, state, "Bank Transfer", data.get("customer_name", "Customer"), "whatsapp", f"wa_media:{media_id}", from_number)
        await send_order_to_kitchen(bot, order_id, state, data.get("customer_name", "Customer"), from_number, BufferedInputFile(image_bytes, filename="payment_proof.jpg"))
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order placed!\n\nOrder ID: #{order_id[:8]}\nTotal: ₦{float(total):,.0f}\n\nYour payment proof is being verified.")
        await state.clear()
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {exc}")
    except Exception:
        logging.exception("WhatsApp bank-transfer order error")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Something went wrong. Please try again or contact the restaurant.")
