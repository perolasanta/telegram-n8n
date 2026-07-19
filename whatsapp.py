"""WhatsApp catalog ordering and customer notifications.

Prices and availability always come from Chowlin's database. Meta catalog data is
used only to identify a menu item and requested quantity.
"""

from decimal import Decimal, InvalidOperation
import logging
import os

import httpx
from aiogram.types import BufferedInputFile

from whatsapp_state import WhatsAppState
from receipt_generator import generate_receipt_pdf
from bot import (
    create_order_in_db,
    create_paystack_payment_link,
    deduct_inventory_for_order,
    is_subscription_active,
    send_order_to_kitchen,
    send_order_receipt,
    send_restock_alert,
    reverse_geocode,
    format_delivery_coordinates,
)

GRAPH_API = "https://graph.facebook.com/v25.0"
MAX_WHATSAPP_LIST_ROWS = 10


def money(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


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
    return (interactive.get("button_reply") or interactive.get("list_reply") or {}).get("id")


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


async def build_authoritative_cart(order_payload: dict, restaurant_id: str, supabase) -> tuple[dict, list[str]]:
    cart: dict = {}
    problems: list[str] = []
    for item in order_payload.get("product_items") or []:
        retailer_id = item.get("product_retailer_id")
        try:
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        if not retailer_id or quantity <= 0:
            problems.append("an item has an invalid quantity")
            continue
        mapped = supabase.table("menu_item_catalog_map").select(
            "menu_item_id, menu_items(name, price, is_available)"
        ).eq("catalog_retailer_id", retailer_id).eq("restaurant_id", restaurant_id).execute()
        if not mapped.data:
            problems.append("an item is no longer in this restaurant's catalog")
            continue
        menu_item = mapped.data[0].get("menu_items") or {}
        if not menu_item.get("is_available"):
            problems.append(f"{menu_item.get('name', 'an item')} is unavailable")
            continue
        item_id = mapped.data[0]["menu_item_id"]
        if item_id in cart:
            cart[item_id]["qty"] += quantity
        else:
            cart[item_id] = {"name": menu_item["name"], "price": float(money(menu_item["price"])), "qty": quantity}
    return cart, problems


async def recalculate_total(state: WhatsAppState, restaurant: dict) -> Decimal:
    """Refresh item prices from menu_items and calculate the final total with Decimal."""
    data = await state.get_data()
    cart = data.get("cart") or {}
    refreshed_cart = {}
    subtotal = Decimal("0")
    for key, line in cart.items():
        menu_item_id = line.get("menu_item_id", key)
        row = state.supabase.table("menu_items").select("id, name, price, is_available").eq("id", menu_item_id).eq("restaurant_id", restaurant["id"]).execute()
        if not row.data or not row.data[0].get("is_available"):
            raise ValueError(f"{line.get('name', 'An item')} is no longer available")
        item = row.data[0]
        quantity = int(line.get("qty") or 0)
        if quantity <= 0:
            raise ValueError("Cart contains an invalid quantity")
        unit_price = money(item["price"])
        refreshed_cart[key] = {**line, "menu_item_id": item["id"], "name": item["name"], "price": float(unit_price), "qty": quantity}
        subtotal += unit_price * quantity

    delivery_fee = money(data.get("delivery_fee")) if data.get("order_type") == "delivery" else Decimal("0")
    total = subtotal + delivery_fee
    await state.update_data(cart=refreshed_cart, total_price=float(total), delivery_fee=float(delivery_fee))
    return total


async def handle_cart_submission(msg, state, restaurant, from_number, contact_name, supabase):
    if not await is_subscription_active(restaurant["id"]):
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "We're not accepting orders right now. Please try again later.")
        return
    cart, problems = await build_authoritative_cart(msg.get("order") or {}, restaurant["id"], supabase)
    if problems or not cart:
        await state.clear()
        detail = "\n• ".join(problems) if problems else "no available items were found"
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ We couldn't accept this cart:\n• {detail}\n\nPlease update it and send it again.")
        return
    await state.update_data(
        cart=cart, restaurant_id=restaurant["id"], restaurant_name=restaurant["name"],
        kitchen_chat_id=restaurant.get("kitchen_chat_id"), customer_name=contact_name, delivery_fee=0,
    )
    if restaurant.get("pickup_enabled"):
        await state.set_state("waiting_for_order_type")
        await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "How would you like to receive your order?", "Choose order type", [
                            {"id": "order_type_delivery", "title": "Delivery"},
                            {"id": "order_type_pickup", "title": "Pickup"},
                        ])
    else:
        await start_delivery_address_step(state, restaurant, from_number)


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
    if reply_id == "order_type_delivery" and current_state == "waiting_for_order_type":
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
        order_id, _ = await create_order_in_db(None, state, "Bank Transfer", data.get("customer_name", "Customer"), "whatsapp", f"wa_media:{media_id}", from_number)
        await send_order_to_kitchen(bot, order_id, state, data.get("customer_name", "Customer"), from_number, BufferedInputFile(image_bytes, filename="payment_proof.jpg"))
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order placed!\n\nOrder ID: #{order_id[:8]}\nTotal: ₦{float(total):,.0f}\n\nYour payment proof is being verified.")
        await state.clear()
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {exc}")
    except Exception:
        logging.exception("WhatsApp bank-transfer order error")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Something went wrong. Please try again or contact the restaurant.")
