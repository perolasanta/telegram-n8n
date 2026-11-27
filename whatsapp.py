"""WhatsApp catalog ordering and customer notifications.

Prices and availability always come from Chowlin's database. Meta catalog data is
used only to identify a menu item and requested quantity.
"""

from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import unicodedata

import httpx
from aiogram.types import BufferedInputFile

from whatsapp_state import WhatsAppState
from receipt_generator import generate_receipt_pdf
from bot import (
    available_payment_methods,
    bot as shared_bot,
    build_composite_cart_line,
    can_self_cancel,
    cancel_pending_order,
    delivery_bots,
    create_order_in_db,
    create_paystack_payment_link,
    create_transfer_charge,
    transfer_customer_wording,
    deduct_inventory_for_order,
    is_subscription_active,
    send_order_to_kitchen,
    send_restock_alert,
    reverse_geocode,
    format_delivery_coordinates,
    validate_cart_inventory,
    HELD_RECEIPT_METHODS,
)
from paystack_transfer import build_order_status_text

GRAPH_API = "https://graph.facebook.com/v27.0"
MAX_WHATSAPP_LIST_ROWS = 10
MAX_WHATSAPP_CATALOG_FOOTER_LENGTH = 60
MAX_LINE_QTY = 50
MAX_COMPOSITE_UNITS = 5
SESSION_TTL_MINUTES = 120
TABLE_BINDING_TTL_MINUTES = 180
WHATSAPP_COMPOSITES_ENABLED = os.getenv("WHATSAPP_COMPOSITES_ENABLED", "false").lower() == "true"


def money(value) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")


PAYMENT_OPTION_IDS = {"cash": "pay_cash", "bank": "pay_bank", "pod": "pay_pod", "paystack": "pay_paystack", "transfer": "pay_transfer_instant"}


def enabled_payment_option_ids(restaurant: dict, order_type: str) -> list[str]:
    """One eligibility source for the WhatsApp selector and reply validator."""
    methods = available_payment_methods(order_type, restaurant.get("pay_on_delivery_enabled"), restaurant.get("paystack_enabled"), bool(restaurant.get("bank_name") and restaurant.get("account_number")), bool(restaurant.get("paystack_subaccount_code")))
    return [PAYMENT_OPTION_IDS[method] for method in methods if method in PAYMENT_OPTION_IDS]


def _unix_timestamp(now=None) -> int:
    if now is None:
        return int(datetime.now(timezone.utc).timestamp())
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return int(now.timestamp())
    return int(now)


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    if value < 0:
        return "-" + _base36(-value)
    digits = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def make_table_token(public_code, secret, now, ttl_minutes) -> str:
    expiry_base36 = _base36(_unix_timestamp(now) + int(ttl_minutes * 60))
    signing_value = f"{public_code}.{expiry_base36}".encode("utf-8")
    signature = base64.urlsafe_b64encode(
        hmac.new(str(secret).encode("utf-8"), signing_value, hashlib.sha256).digest()
    ).decode("ascii")[:16]
    return f"{public_code}.{expiry_base36}.{signature}"


def verify_table_token(token, secret, now) -> tuple[str, str | None]:
    try:
        if not isinstance(token, str) or not isinstance(secret, str) or not secret:
            return "invalid", None
        public_code, expiry_base36, signature = token.rsplit(".", 2)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", public_code):
            return "invalid", None
        if not re.fullmatch(r"[0-9a-zA-Z]+", expiry_base36) or len(signature) != 16:
            return "invalid", None
        signing_value = f"{public_code}.{expiry_base36}".encode("utf-8")
        expected_signature = base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), signing_value, hashlib.sha256).digest()
        ).decode("ascii")[:16]
        if not hmac.compare_digest(signature, expected_signature):
            return "invalid", None
        expiry = int(expiry_base36, 36)
        if expiry <= _unix_timestamp(now):
            return "expired", None
        return "ok", public_code
    except Exception:
        return "invalid", None


def resolve_table_reference(reference, secret, now, allow_raw) -> tuple[str, str | None]:
    if not isinstance(reference, str):
        return "invalid", None
    if "." in reference:
        return verify_table_token(reference, secret, now)
    if allow_raw and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", reference):
        return "ok", reference
    return "invalid", None


def extract_table_ref(text: str) -> str | None:
    match = re.search(r"ref:\s*([A-Za-z0-9_.-]{1,160})(?![A-Za-z0-9_.-])", str(text or ""), re.IGNORECASE)
    return match.group(1) if match else None


def is_binding_fresh(table_bound_at, now=None, ttl_minutes=TABLE_BINDING_TTL_MINUTES) -> bool:
    if not table_bound_at:
        return False
    try:
        bound_at = table_bound_at
        if not isinstance(bound_at, datetime):
            bound_at = datetime.fromisoformat(str(bound_at).replace("Z", "+00:00"))
        if bound_at.tzinfo is None:
            bound_at = bound_at.replace(tzinfo=timezone.utc)
        current_time = now or datetime.now(timezone.utc)
        if not isinstance(current_time, datetime):
            current_time = datetime.fromisoformat(str(current_time).replace("Z", "+00:00"))
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        return current_time.astimezone(timezone.utc) - bound_at.astimezone(timezone.utc) <= timedelta(minutes=ttl_minutes)
    except (TypeError, ValueError, OverflowError):
        return False


async def clear_table_binding(state):
    data = await state.get_data()
    for key in ("table_id", "table_number", "menu_filter", "table_bound_at"):
        data.pop(key, None)
    state.supabase.table("whatsapp_sessions").update({"data": data}).eq(
        "phone_number", state.phone_number
    ).eq("restaurant_id", state.restaurant_id).execute()


async def clear_keeping_table_binding(state):
    data = await state.get_data()
    binding = {
        key: data.get(key)
        for key in ("table_id", "table_number", "menu_filter", "table_bound_at")
    }
    if not binding.get("table_id") or not is_binding_fresh(binding.get("table_bound_at")):
        binding = {}
    await state.clear()
    if binding:
        await state.update_data(**binding)


def is_whatsapp_addon_active(restaurant: dict, now=None) -> bool:
    if restaurant.get("whatsapp_addon_enabled") is not True:
        return False
    expires_at = restaurant.get("whatsapp_addon_expires_at")
    if not expires_at:
        return True
    expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    return expiry > current_time.astimezone(timezone.utc)


def parse_customer_intent(text: str) -> str | None:
    # Commands are deliberately exact.  In particular, do not turn an address
    # such as "Cancel Street" into a destructive action.
    normalized = str(text or "").strip().casefold()
    return {
        "status": "status",
        "orders": "history",
        "cancel": "cancel",
        "help": "help",
        "menu": "help",
    }.get(normalized)


def truncate(text, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    return text[:limit - 1].rstrip() + "…"


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
        for _ in range(quantity):
            queue.append({
                "menu_item_id": item.get("menu_item_id"),
                "name": item.get("name", "Item"),
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
            json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload},
        )
        response.raise_for_status()
        return response.json()


async def send_text(phone_number_id, token, to, text):
    await send_whatsapp_message(
        phone_number_id, token, to,
        {"type": "text", "text": {"body": truncate(text, 4096)}},
    )


def build_catalog_welcome_text(restaurant_name: str, pickup_enabled: bool) -> tuple[str, str]:
    name = truncate(restaurant_name or "the restaurant", 250)
    closing = (
        "Next I'll ask if you want *delivery* or *pickup*, then payment."
        if pickup_enabled else
        "Next I'll ask for your delivery address, then payment."
    )
    body = (
        f"Welcome to *{name}*! 🍽️\n\n"
        "To order:\n"
        "1. Tap the *shop icon* 🏪 at the top right of this chat (or the *View catalog* button below)\n"
        "2. Add the items you want\n"
        "3. Tap *View cart*, then *Send*\n\n"
        f"{closing}"
        "\n\nReply *MENU* anytime for help, order status or to cancel."
    )
    footer = "Can't see it? Tap the shop icon at the top right."
    return truncate(body, 1024), footer


def build_catalog_payload(body: str, footer: str) -> dict:
    interactive = {
        "type": "catalog_message",
        "body": {"text": truncate(body, 1024).strip()},
        "action": {"name": "catalog_message"},
    }
    if footer and len(str(footer)) <= MAX_WHATSAPP_CATALOG_FOOTER_LENGTH:
        interactive["footer"] = {"text": str(footer).strip()}
    return {
        "type": "interactive",
        "interactive": interactive,
    }


async def send_catalog_message(restaurant, to: str, supabase):
    body, footer = build_catalog_welcome_text(
        restaurant.get("name") or "the restaurant", restaurant.get("pickup_enabled") is True
    )
    recipient = re.sub(r"\D", "", str(to))
    if not recipient:
        raise ValueError("WhatsApp recipient must contain digits")
    fallback_body = f"{body}\n\nCan't see it? Tap {restaurant.get('name') or 'the restaurant'} at the top of this chat, then View catalog."
    try:
        result = await send_whatsapp_message(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], recipient,
            build_catalog_payload(body, footer),
        )
        if isinstance(result, dict) and result.get("error"):
            meta_error = result["error"]
            error_data = meta_error.get("error_data") or result.get("error_data") or {}
            logging.warning(
                "WhatsApp welcome path=fallback message_id=%s http_status=%s meta_error_code=%s meta_error_message=%s error_data_details=%s fbtrace_id=%s body_length=%s footer_length=%s",
                result.get("messages", [{}])[0].get("id") if result.get("messages") else None,
                result.get("http_status"), meta_error.get("code"), meta_error.get("message"),
                error_data.get("details"), meta_error.get("fbtrace_id") or result.get("fbtrace_id"), len(body), len(footer),
            )
            await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], recipient, fallback_body)
            return
        logging.info(
            "WhatsApp welcome path=interactive message_id=%s",
            result.get("messages", [{}])[0].get("id") if isinstance(result, dict) and result.get("messages") else None,
        )
        return result
    except Exception as exc:
        response = getattr(exc, "response", None)
        error_body = {}
        if response is not None:
            try:
                error_body = response.json()
            except Exception:
                error_body = {}
        meta_error = error_body.get("error") or {}
        error_data = meta_error.get("error_data") or error_body.get("error_data") or {}
        result_id = (result.get("messages", [{}])[0].get("id")
                     if isinstance(locals().get("result"), dict) else
                     meta_error.get("fbtrace_id"))
        logging.warning(
            "WhatsApp welcome path=fallback message_id=%s http_status=%s meta_error_code=%s meta_error_message=%s error_data_details=%s fbtrace_id=%s body_length=%s footer_length=%s",
            result_id, getattr(response, "status_code", None), meta_error.get("code"), meta_error.get("message"),
            error_data.get("details"), meta_error.get("fbtrace_id") or error_body.get("fbtrace_id"), len(body), len(footer),
        )
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], recipient, fallback_body)
        return


def build_list_payload(body: str, button_label: str, rows: list[dict]) -> dict:
    if not rows or len(rows) > MAX_WHATSAPP_LIST_ROWS:
        raise ValueError("WhatsApp interactive lists must contain 1–10 rows")
    safe_rows = []
    for row in rows:
        row_id = row.get("id")
        if len(str(row_id or "")) > 200:
            raise ValueError("WhatsApp list row ids must be at most 200 characters")
        title = truncate(row.get("title"), 24)
        if not title:
            raise ValueError("WhatsApp list row titles cannot be empty")
        safe_row = {**row, "title": title}
        description = truncate(row.get("description"), 72)
        if description:
            safe_row["description"] = description
        else:
            safe_row.pop("description", None)
        safe_rows.append(safe_row)
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": truncate(body, 1024)},
            "action": {
                "button": truncate(button_label, 20),
                "sections": [{"title": "Options", "rows": safe_rows}],
            },
        },
    }


def build_buttons_payload(body: str, buttons: list[dict]) -> dict:
    if not buttons or len(buttons) > 3:
        raise ValueError("WhatsApp interactive buttons must contain 1-3 options")
    safe_buttons = []
    for button in buttons:
        button_id = button.get("id")
        if len(str(button_id or "")) > 256:
            raise ValueError("WhatsApp button ids must be at most 256 characters")
        title = truncate(button.get("title"), 20)
        if not title:
            raise ValueError("WhatsApp button titles cannot be empty")
        safe_buttons.append({**button, "title": title})
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": truncate(body, 1024)},
            "action": {"buttons": [{"type": "reply", "reply": button} for button in safe_buttons]},
        },
    }


async def send_list(phone_number_id: str, token: str, to: str, body: str, button_label: str, rows: list[dict]):
    await send_whatsapp_message(phone_number_id, token, to, build_list_payload(body, button_label, rows))


async def send_buttons(phone_number_id: str, token: str, to: str, body: str, buttons: list[dict]):
    await send_whatsapp_message(phone_number_id, token, to, build_buttons_payload(body, buttons))


async def send_payment_options(phone_number_id, token, to, restaurant: dict, state, order_type):
    offered_ids = enabled_payment_option_ids(restaurant, order_type)
    if not offered_ids:
        await send_text(
            phone_number_id, token, to,
            "This order type isn't available right now. Please contact the restaurant.",
        )
        await state.clear()
        return
    logging.info("WhatsApp payment selector restaurant_id=%s option_ids=%s", restaurant.get("id"), offered_ids)
    titles = {
        "cash": ("pay_cash", "Cash"),
        "bank": ("pay_bank", "Transfer + screenshot"),
        "pod": ("pay_pod", "Pay on delivery"),
        "paystack": ("pay_paystack", "Card payment"),
        "transfer": ("pay_transfer_instant", "Instant transfer"),
    }
    if order_type == "pickup":
        titles["cash"] = ("pay_cash", "Cash at pickup")
    method_by_id = {value: key for key, value in PAYMENT_OPTION_IDS.items()}
    rows = [{"id": reply_id, "title": titles[method_by_id[reply_id]][1], **({"description": "Needs screenshot proof; restaurant confirms manually"} if reply_id == "pay_bank" else {})} for reply_id in offered_ids]
    if not rows:
        await send_text(
            phone_number_id, token, to,
            "This order type isn't available right now. Please contact the restaurant.",
        )
        await state.clear()
        return
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


async def sync_catalog_item_availability(
    menu_item_id: str, is_available: bool, supabase, restaurant_id=None
) -> None:
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
    ).eq("menu_item_id", menu_item_id)
    if restaurant_id is not None:
        mapping = mapping.eq("restaurant_id", restaurant_id)
    mapping = mapping.execute()
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
    visibility = "published" if is_available else "staging"
    request = {"method": "UPDATE", "data": {
        "id": retailer_id, "availability": availability, "visibility": visibility,
    }}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GRAPH_API}/{catalog_id}/items_batch",
                headers={"Authorization": f"Bearer {token}"},
                data={"item_type": "PRODUCT_ITEM", "allow_upsert": "false", "requests": json.dumps([request])},
            )
            response.raise_for_status()
            handles = response.json().get("handles")
            handle = handles[0] if isinstance(handles, list) and handles and isinstance(handles[0], str) else None
            if handle:
                await asyncio.sleep(0.1)
                status = await client.get(
                    f"{GRAPH_API}/{catalog_id}/check_batch_request_status",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"handle": handle, "load_ids_of_invalid_requests": "true"},
                )
                # Batch status is informational; a lagging/invalid handle must
                # not turn an accepted catalog update into a failed submission.
                if status.is_error:
                    logging.warning("Catalog status check unavailable: http_status=%s", status.status_code)
    except Exception as exc:
        response = getattr(exc, "response", None)
        body = {}
        try:
            body = response.json() if response is not None else {}
        except Exception:
            pass
        meta = body.get("error") or {}
        if meta.get("code") != 100:
            logging.warning("Catalog availability sync failed without visibility retry: http_status=%s meta_error_code=%s meta_error_message=%s", getattr(response, "status_code", None), meta.get("code"), meta.get("message"))
            return
        logging.warning("Catalog visibility update rejected; retrying availability only: http_status=%s meta_error_code=%s meta_error_message=%s",
                        getattr(response, "status_code", None), meta.get("code"), meta.get("message"))
        try:
            async with httpx.AsyncClient() as client:
                retry = await client.post(
                    f"{GRAPH_API}/{catalog_id}/items_batch", headers={"Authorization": f"Bearer {token}"},
                    data={"item_type": "PRODUCT_ITEM", "allow_upsert": "false", "requests": json.dumps([{"method": "UPDATE", "data": {
                        "id": retailer_id, "availability": availability,
                    }}])},
                )
                retry.raise_for_status()
                return
        except Exception:
            pass
        logging.exception("Failed to sync catalog availability for retailer_id=%s (menu_item_id=%s)", retailer_id, menu_item_id)


def catalog_error(exc, retailer_id=None) -> dict:
    response = getattr(exc, "response", None)
    body = {}
    try:
        body = response.json() if response is not None else {}
    except Exception:
        pass
    meta = body.get("error") or body
    message = str(meta.get("message") or str(exc))
    if meta.get("code") in {10, 190, 200} or "permission" in message.lower():
        message = "Access token lacks catalog_management permission or catalog not accessible"
    return {"http_status": getattr(response, "status_code", None), "meta_error_code": meta.get("code"),
            "meta_error_message": message, "type": meta.get("type"), "error_subcode": meta.get("error_subcode"),
            "error_user_title": meta.get("error_user_title"), "error_user_msg": meta.get("error_user_msg"),
            "error_data": meta.get("error_data"), "fbtrace_id": meta.get("fbtrace_id"), "retailer_id": retailer_id}


def catalog_request_shape(catalog_id, requests, allow_upsert=False):
    return {"method": "POST", "url_path": f"/{catalog_id}/items_batch",
            "content_type": "application/x-www-form-urlencoded",
            "top_level_parameters": ["item_type", "requests", "allow_upsert"],
            "item_type": "PRODUCT_ITEM", "allow_upsert": allow_upsert,
            "request_count": len(requests),
            "first_request": {"method": requests[0]["method"], "data": requests[0]["data"]} if requests else None}


async def resync_catalog_availability(restaurant_id: str, supabase) -> dict:
    """Best-effort tenant-scoped full catalog resync, in batches of at most 100."""
    mappings = supabase.table("menu_item_catalog_map").select(
        "menu_item_id, catalog_retailer_id, menu_items(is_available), restaurants(whatsapp_access_token, whatsapp_catalog_id)"
    ).eq("restaurant_id", restaurant_id).execute().data or []
    submitted = synced = failed = invalid_count = 0
    errors, catalog_ids, attempted, attempts, handle_info = [], [], 0, [], []
    visibility_supported = None
    groups = {}
    for row in mappings:
        restaurant = row.get("restaurants") or {}
        key = (restaurant.get("whatsapp_access_token"), restaurant.get("whatsapp_catalog_id"))
        if key[0] and key[1] and row.get("catalog_retailer_id"):
            groups.setdefault(key, []).append(row)
        else:
            failed += 1
            errors.append({"http_status": None, "meta_error_code": None, "meta_error_message": "Catalog credentials or retailer mapping missing", "error_subcode": None, "fbtrace_id": None, "retailer_id": row.get("catalog_retailer_id")})
    for (token, catalog_id), rows in groups.items():
        catalog_ids.append(catalog_id)
        for start in range(0, len(rows), 100):
            chunk = rows[start:start + 100]
            attempted += len(chunk)
            requests = [{"method": "UPDATE", "data": {
                "id": str(row["catalog_retailer_id"]).strip(),
                "availability": "in stock" if (row.get("menu_items") or {}).get("is_available") else "out of stock",
                "visibility": "published" if (row.get("menu_items") or {}).get("is_available") else "staging",
            }} for row in chunk]
            submitted_this_chunk = False
            for attempt_number, attempt_requests in ((1, requests), (2, [{"method": r["method"], "data": {k: v for k, v in r["data"].items() if k != "visibility"}} for r in requests])):
                shape = catalog_request_shape(catalog_id, attempt_requests, False)
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.post(f"{GRAPH_API}/{catalog_id}/items_batch", headers={"Authorization": f"Bearer {token}"}, data={"item_type": "PRODUCT_ITEM", "allow_upsert": "false", "requests": json.dumps(attempt_requests)})
                        response.raise_for_status()
                        attempts.append({"attempt": attempt_number, "fields_sent": list(attempt_requests[0]["data"]), "http_status": response.status_code, "ok": True, "meta_error_code": None, "error_subcode": None, "error_user_msg": None})
                        handles = response.json().get("handles")
                        handle = handles[0] if isinstance(handles, list) and handles and isinstance(handles[0], str) else None
                        info = {"type": type(handle).__name__ if handle is not None else None, "empty": not bool(handle), "length": len(handle) if isinstance(handle, str) else 0, "status_check": "skipped"}
                        submitted += len(chunk); submitted_this_chunk = True
                        if attempt_number == 2:
                            visibility_supported = False
                        if handle:
                            # The batch processor can lag. Retry only this GET once.
                            for status_attempt in range(2):
                                await asyncio.sleep(1 if status_attempt == 0 else 4)
                                status = await client.get(f"{GRAPH_API}/{catalog_id}/check_batch_request_status", headers={"Authorization": f"Bearer {token}"}, params={"handle": handle, "load_ids_of_invalid_requests": "true"})
                                if not status.is_error:
                                    listed = status.json().get("ids_of_invalid_requests") or status.json().get("invalid_requests") or []
                                    invalid_count += len(listed)
                                    info["status_check"] = "ok"
                                    if attempt_number == 1 and not listed:
                                        visibility_supported = True
                                    for item in listed:
                                        errors.append({"http_status": None, "meta_error_code": None, "meta_error_message": str(item), "type": None, "error_subcode": None, "error_user_title": None, "error_user_msg": None, "error_data": None, "fbtrace_id": None, "retailer_id": str(item)})
                                    break
                                status_error = catalog_error(httpx.HTTPStatusError("status check failed", request=status.request, response=status))
                                # only retry a handle that has not propagated yet
                                if status_attempt == 0 and "handle" in str(status_error["meta_error_message"]).lower():
                                    continue
                                info.update({"status_check": "unavailable", "http_status": status_error["http_status"], "meta_error_code": status_error["meta_error_code"], "error_user_msg": status_error["error_user_msg"]})
                                errors.append({**status_error, "retailer_id": None})
                                break
                        handle_info.append(info)
                        logging.info("Catalog batch handle info: %s", info)
                    break
                except Exception as exc:
                    error = catalog_error(exc)
                    attempts.append({"attempt": attempt_number, "fields_sent": list(attempt_requests[0]["data"]), "http_status": error["http_status"], "ok": False, "meta_error_code": error["meta_error_code"], "error_subcode": error["error_subcode"], "error_user_msg": error["error_user_msg"]})
                    logging.warning("Catalog resync failed: error=%s request_shape=%s", error, shape)
                    if attempt_number == 1 and error["meta_error_code"] == 100:
                        visibility_supported = False
                        continue
                    failed += len(chunk)
                    errors.extend([{**error, "retailer_id": row.get("catalog_retailer_id")} for row in chunk])
                    break
    deduped = []
    for error in errors:
        if error not in deduped:
            deduped.append(error)
    failed += invalid_count
    synced = submitted - invalid_count
    return {"submitted": submitted, "invalid": invalid_count, "synced": synced, "failed": failed, "errors": deduped[:5], "catalog_id_used": catalog_ids[0] if len(catalog_ids) == 1 else catalog_ids, "items_attempted": attempted, "request_shape": catalog_request_shape(catalog_ids[0], requests, False) if catalog_ids and attempted else None, "attempts": attempts, "batch_handle_info": handle_info, "visibility_supported": visibility_supported}


async def send_whatsapp_receipt(supabase, order: dict, order_id: str, *, release: bool = False) -> bool:
    """Generate and deliver a PDF receipt for a WhatsApp-originated order."""
    restaurant = order.get("restaurants") or {}
    contact = order.get("customer_contact")
    if not (contact and restaurant.get("whatsapp_phone_number_id") and restaurant.get("whatsapp_access_token")):
        return False

    try:
        result = supabase.table("orders").select(
            "*, order_items(*, menu_items(name, price)), restaurant_tables(table_number), restaurants(name, phone)"
        ).eq("id", order_id).eq("restaurant_id", order.get("restaurant_id")).execute()
        if not result.data:
            return False
        data = result.data[0]
        if data.get("payment_method") in HELD_RECEIPT_METHODS and not release:
            logging.info("WhatsApp receipt held until order is ready for order %s", order_id)
            return False
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
        "whatsapp_addon_enabled, whatsapp_addon_expires_at, "
        "pickup_enabled, delivery_fee_type, delivery_fee_flat, paystack_enabled, paystack_subaccount_code, "
        "pay_on_delivery_enabled, bank_name, account_number, account_name"
    ).eq("whatsapp_phone_number_id", phone_number_id).execute()
    if not response.data:
        logging.error("No restaurant configured for whatsapp_phone_number_id=%s (message_id=%s)", phone_number_id, message_id)
        return
    restaurant = response.data[0]

    if not is_whatsapp_addon_active(restaurant):
        logging.warning(
            "WhatsApp add-on is inactive: restaurant_id=%s message_id=%s",
            restaurant.get("id"), message_id,
        )
        try:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "Ordering on WhatsApp isn't available right now. Please contact the restaurant directly.",
            )
        except Exception:
            logging.exception(
                "Failed to notify customer that WhatsApp ordering is unavailable: restaurant_id=%s message_id=%s",
                restaurant.get("id"), message_id,
            )
        return

    try:
        state = WhatsAppState(supabase, from_number, restaurant["id"])
        session = supabase.table("whatsapp_sessions").select("state, updated_at").eq(
            "phone_number", from_number
        ).eq("restaurant_id", restaurant["id"]).execute()
        session_row = session.data[0] if session.data else {}
        current_state = session_row.get("state")

        if current_state is not None and msg.get("type") != "order":
            updated_at = session_row.get("updated_at")
            if updated_at:
                try:
                    session_updated_at = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                    if session_updated_at.tzinfo is None:
                        session_updated_at = session_updated_at.replace(tzinfo=timezone.utc)
                    session_is_stale = session_updated_at < (
                        datetime.now(timezone.utc) - timedelta(minutes=SESSION_TTL_MINUTES)
                    )
                except (TypeError, ValueError):
                    logging.warning(
                        "Invalid WhatsApp session timestamp: restaurant_id=%s from_number=%s",
                        restaurant.get("id"), from_number,
                    )
                    session_is_stale = False
                if session_is_stale:
                    await state.clear()
                    current_state = None
                    if msg.get("type") == "interactive":
                        await send_text(
                            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                            "That order session expired. Send your cart again to start over.",
                        )
                        return

        if msg.get("type") == "order":
            await handle_cart_submission(msg, state, restaurant, from_number, contact_name, supabase)
        elif msg.get("type") == "interactive":
            await handle_interactive(msg, state, restaurant, from_number, supabase, bot, current_state)
        elif msg.get("type") == "location":
            await handle_location(msg, state, restaurant, from_number, current_state)
        elif msg.get("type") == "text":
            await handle_text(msg, state, restaurant, from_number, current_state, supabase, bot)
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
    menu_filter: str | None = None,
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
        if menu_filter:
            category_name = str((category or {}).get("name") or "")
            required_prefix = f"{menu_filter} —".casefold()
            if not category_name.casefold().startswith(required_prefix):
                problems.append(f"{name} isn't on this table's menu")
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


def build_reorder_catalog_rows(order_items: list[dict], menu_items_by_id: dict) -> tuple[dict, dict]:
    requested = {}
    for item in order_items:
        menu_item_id = item.get("menu_item_id")
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if menu_item_id and quantity > 0:
            requested[menu_item_id] = requested.get(menu_item_id, 0) + quantity
    rows_by_id = {
        menu_item_id: {"menu_item_id": menu_item_id, "menu_items": menu_items_by_id[menu_item_id]}
        for menu_item_id in requested
        if menu_item_id in menu_items_by_id
    }
    return requested, rows_by_id


def format_order_status_line(order: dict) -> str:
    order_id = str(order.get("id") or "")[:8]
    order_status = str(order.get("order_status") or "pending").lower()
    if order.get("payment_method") == "Paystack" and order.get("payment_status") == "pending":
        status_label = "Awaiting payment"
    elif order.get("payment_method") == "Bank Transfer" and order.get("payment_status") == "pending":
        status_label = "Payment being verified"
    else:
        status_label = {
            "pending": "Received",
            "preparing": "Being prepared",
            "ready": "Ready",
            "delivered": "Delivered",
            "cancelled": "Cancelled",
        }.get(order_status, "Received")
    total = f"{money(order.get('total_amount')):,.0f}"
    return f"#{order_id} — {status_label} — ₦{total}"


def order_status_words(order) -> str:
    """Customer wording for the statuses actually persisted on orders."""
    status = str((order if isinstance(order, str) else order.get("order_status")) or "pending").lower()
    if status in {"pending", "received"}:
        return "We've received your order and the restaurant will start soon."
    if status == "preparing":
        return "Your order is being prepared."
    if status == "ready":
        if isinstance(order, dict) and order.get("order_type") == "delivery" and order.get("dispatch_sent_at"):
            return "Your order is on its way."
        if isinstance(order, dict) and order.get("order_type") == "pickup":
            return "Your order is ready for pickup."
        return "Your order is ready and will be sent out shortly."
    return {"delivered": "Your order was delivered.", "cancelled": "This order was cancelled."}.get(status, "We've received your order and the restaurant will start soon.")


def payment_status_words(order: dict) -> str:
    if order.get("payment_status") == "confirmed":
        return "Confirmed"
    return "Awaiting confirmation" if order.get("payment_method") == "Bank Transfer" else "Awaiting payment"


async def validate_cart_inventory_and_drop_shortages(cart: dict, problems: list[str], restaurant_id: str):
    shortages = await validate_cart_inventory(cart, restaurant_id)
    for shortage in shortages:
        mid = shortage.get("menu_item_id") or next(
            (key for key, line in cart.items() if line.get("name") == shortage["name"]), None
        )
        available = int(shortage.get("available") or 0)
        name = shortage["name"]
        requested_qty = int(shortage.get("requested") or 0)
        if available <= 0:
            problems.append(f"{name} is sold out")
        else:
            problems.append(f"Only {available} {name} left (you asked for {requested_qty})")
        if mid and mid in cart:
            del cart[mid]
        else:
            cart = {key: line for key, line in cart.items() if line.get("name") != name}
    return cart, problems


async def build_authoritative_cart(
    order_payload: dict, restaurant_id: str, supabase, menu_filter: str | None = None
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
        "menu_items(name, price, is_available, item_type, category_id, menu_categories(name, is_active))"
    ).in_("catalog_retailer_id", list(requested)).eq("restaurant_id", restaurant_id).execute()

    rows_by_rid: dict = {row["catalog_retailer_id"]: row for row in result.data or []}

    cart, composites, classify_problems = classify_cart_lines(requested, rows_by_rid, menu_filter)
    problems.extend(classify_problems)

    cart, problems = await validate_cart_inventory_and_drop_shortages(
        cart, problems, restaurant_id
    )
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
                "price_delta": price_delta,
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
            session_modifiers = {
                group_id: [
                    {**pick, "price_delta": float(money(pick.get("price_delta")))}
                    for pick in picks
                ]
                for group_id, picks in refreshed_modifiers.items()
            }
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
            refreshed_line["modifiers"] = session_modifiers
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
    data = await state.get_data()
    if data.get("table_id") and is_binding_fresh(data.get("table_bound_at")):
        await state.update_data(
            order_type="dine_in", delivery_fee=0, delivery_zone_id=None,
            delivery_zone_name=None, delivery_address=None,
            delivery_lat=None, delivery_lon=None,
        )
        await request_payment_method(state, restaurant, from_number)
        return
    if data.get("table_id"):
        await clear_table_binding(state)
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
        await state.update_data(
            pending_composites=[], composite_queue=[], active_unit=None,
            same_for_all_template=None,
        )
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
        await state.update_data(
            pending_composites=[], composite_queue=[], active_unit=None,
            same_for_all_template=None,
        )
        await begin_delivery_or_pickup(state, restaurant, from_number)
        return

    active_unit = queue[0]
    await state.update_data(composite_queue=queue[1:], active_unit=None)
    pending_composites = data.get("pending_composites") or []
    units_total = sum(
        int(item.get("qty") or 0)
        for item in pending_composites
        if item.get("menu_item_id") == active_unit.get("menu_item_id")
    )
    remaining_same_units = sum(
        1 for unit in queue[1:]
        if unit.get("menu_item_id") == active_unit.get("menu_item_id")
    )
    groups = await fetch_composite_groups(state, active_unit.get("menu_item_id"), restaurant)
    active_unit = {
        **active_unit,
        "unit_no": max(1, units_total - remaining_same_units),
        "units_total": max(1, units_total),
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
                f"{truncate(active_unit.get('name', 'This item'), 100)} is temporarily unavailable.",
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
            f"{truncate(active_unit.get('name', 'This item'), 100)} is temporarily unavailable.",
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
            f"{truncate(active_unit.get('name', 'This item'), 100)} is temporarily unavailable.",
        )
        await next_composite_unit(state, restaurant, from_number)
        return
    rows, page, _ = build_modifier_rows(group["id"], group, options, active_unit.get("option_page", 0))
    active_unit["option_page"] = page
    await state.update_data(active_unit=active_unit)
    prompt = "Choose one" if group.get("selection_mode") == "single" else "Choose one or more"
    body_lines = [
        f"{truncate(active_unit.get('name', 'Item'), 100)} (unit {active_unit.get('unit_no', 1)} of {active_unit.get('units_total', 1)})",
        truncate(group.get("name", "Choose an option"), 100),
        prompt,
    ]
    selected = (active_unit.get("selections") or {}).get(group["id"], [])
    if group.get("selection_mode") == "multi" and selected:
        selected_names = [
            f"{truncate(pick.get('name', 'Option'), 80)} x{pick.get('quantity', 1)}" if int(pick.get("quantity") or 1) > 1 else truncate(pick.get("name", "Option"), 100)
            for pick in selected
        ]
        body_lines.append("Selected: " + ", ".join(selected_names))
    note = data.get("modifier_note")
    if note:
        body_lines.insert(0, truncate(note, 200))
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
            f"{truncate(active_unit.get('name', 'This item'), 100)} is temporarily unavailable.",
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
                    f"{truncate((option or {}).get('name') or pick.get('name') or 'An option', 100)} is no longer available. "
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
    session_data = await state.get_data()
    if session_data.get("table_id") and not is_binding_fresh(session_data.get("table_bound_at")):
        await clear_table_binding(state)
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Your table link has expired. Please scan the QR code on your table again to order for dine-in.",
        )
        return
    if not await is_subscription_active(restaurant["id"]):
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "We're not accepting orders right now. Please try again later.")
        return
    menu_filter = session_data.get("menu_filter") if session_data.get("table_id") else None
    cart, composites, problems = await build_authoritative_cart(
        msg.get("order") or {}, restaurant["id"], supabase, menu_filter
    )
    await submit_authoritative_cart(
        cart, composites, problems, state, restaurant, from_number, contact_name, supabase
    )


async def submit_authoritative_cart(cart, composites, problems, state, restaurant, from_number, contact_name, supabase):
    # Composite items cannot be completed through the catalog flow yet
    if composites and not WHATSAPP_COMPOSITES_ENABLED:
        for composite in composites:
            problems.append(
                f"{truncate(composite.get('name'), 100)} needs choices we can't take through the catalog yet. "
                "Please order it on Telegram or call the restaurant."
            )
        composites = []

    if not cart and not composites:
        await state.clear()
        detail = "\n• ".join(problems) if problems else "no available items were found"
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ We couldn't accept this cart:\n• {truncate(detail, 900)}\n\nPlease update it and send it again.")
        return
    await state.update_data(
        cart=cart, restaurant_id=restaurant["id"], restaurant_name=restaurant["name"],
        kitchen_chat_id=restaurant.get("kitchen_chat_id"), customer_name=contact_name, delivery_fee=0,
        pending_composites=[
            {"menu_item_id": item.get("menu_item_id"), "name": item.get("name", "Item"), "qty": int(item.get("qty") or 0)}
            for item in composites
        ] if WHATSAPP_COMPOSITES_ENABLED else [],
        composite_queue=[], active_unit=None, same_for_all_template=None,
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
    await send_payment_options(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
        from_number, restaurant, state, "pickup",
    )


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
                        {"id": f"zone:{zone['id']}", "title": truncate(zone.get("zone_name"), 24), "description": f"₦{float(money(zone.get('fee'))):,.0f}"}
                        for zone in zones
                    ])


async def handle_unusable_zone_config(state, restaurant, from_number, reason: str):
    # A configured flat amount is the only safe fallback for a zone-priced delivery flow.
    flat_fee = money(restaurant.get("delivery_fee_flat"))
    if flat_fee > 0:
        await state.update_data(delivery_fee=float(flat_fee), delivery_zone_id=None, delivery_zone_name=None)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(reason, 250)}; using the restaurant's flat delivery fee of ₦{float(flat_fee):,.0f}.")
        await request_payment_method(state, restaurant, from_number)
    elif restaurant.get("pickup_enabled"):
        await state.set_state("waiting_for_order_type")
        await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(reason, 250)}. Delivery is unavailable; please choose pickup.", "Choose order type", [{"id": "order_type_pickup", "title": "Pickup"}])
    else:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(reason, 250)}. Delivery is temporarily unavailable; please contact the restaurant.")
        await state.clear()


async def request_payment_method(state, restaurant, from_number):
    total = await recalculate_total(state, restaurant)
    await state.set_state("waiting_for_payment_method")
    await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"💰 Total: ₦{float(total):,.0f}")
    data = await state.get_data()
    await send_payment_options(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"],
        from_number, restaurant, state, data.get("order_type", "delivery"),
    )


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
            f"How many {truncate(option.get('name', 'items'), 100)} ({truncate(option.get('unit_label') or 'units', 40)})? Reply with a number from 1 to 20.",
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
                        f"📍 We found this address:\n\n{truncate(address, 600)}\n\nIs this correct?", [
                            {"id": "address_confirmed", "title": "Yes, correct"},
                            {"id": "address_retype", "title": "No, retype"},
                        ])


async def handle_text(msg, state, restaurant, from_number, current_state, supabase, bot):
    text_data = msg.get("text") or {}
    body = text_data.get("body") or "" if isinstance(text_data, dict) else ""
    table_ref = extract_table_ref(body)
    if table_ref:
        secret = os.getenv("TABLE_LINK_SECRET")
        allow_raw = os.getenv("ALLOW_RAW_TABLE_REF", "false").lower() == "true"
        status, verified_public_code = resolve_table_reference(
            table_ref, secret, datetime.now(timezone.utc), allow_raw
        )
        if status == "expired":
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "That link has expired. Please scan the QR code on your table again.",
            )
            return
        if status != "ok" or not verified_public_code:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "That table link isn't valid. Please scan the QR code on your table.",
            )
            return
        table_ref = verified_public_code
        if current_state is not None:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "Please finish or cancel your current order first (reply CANCEL), then scan the table QR code again.",
            )
            return
        result = supabase.table("restaurant_tables").select(
            "id, table_number, restaurant_id, menu_filter"
        ).eq("public_code", table_ref).eq("is_active", True).eq(
            "restaurant_id", restaurant["id"]
        ).limit(1).execute()
        if not result.data:
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "That table code isn't valid for this restaurant.",
            )
            return
        if not await is_subscription_active(restaurant["id"]):
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "This restaurant isn't taking orders right now.",
            )
            return
        row = result.data[0]
        table_number = row.get("table_number")
        if table_number is None or table_number == "EXTERNAL":
            await clear_table_binding(state)
            await send_catalog_message(
                restaurant, from_number, supabase,
            )
            return
        await state.update_data(
            table_id=row.get("id"), table_number=table_number,
            menu_filter=row.get("menu_filter"),
            table_bound_at=datetime.now(timezone.utc).isoformat(),
        )
        await send_catalog_message(
            restaurant, from_number, supabase,
        )
        return
    intent = parse_customer_intent(body)
    free_text_state = current_state in {"waiting_for_address", "entering_modifier_qty"}
    exact_word = body.strip().casefold()
    if exact_word == "menu":
        await state.clear()
        await send_whatsapp_command_menu(restaurant, from_number)
        return
    if exact_word == "help" and not free_text_state:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "To order: tap the *shop icon*, add items, tap *View cart*, then *Send*.\n\nCommands: *MENU*, *STATUS*, *CANCEL*, *ORDERS*.")
        await send_whatsapp_command_menu(restaurant, from_number)
        return
    if intent == "cancel" and current_state is not None and current_state not in (
        "browsing_orders", "confirming_cancel"
    ):
        await state.clear()
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Your current order was cancelled. Send a new cart anytime.",
        )
        return
    if intent and not free_text_state:
        reply_id = {
            "status": "cmd_status",
            "history": "cmd_history",
            "cancel": "cmd_cancel",
            "help": "cmd_help",
        }.get(intent)
        if reply_id:
            await handle_command_reply(reply_id, state, restaurant, from_number, supabase, bot, current_state)
            return

    if current_state == "entering_modifier_qty":
        await handle_modifier_quantity(msg, state, restaurant, from_number)
        return
    if current_state not in ("waiting_for_address", "confirming_address"):
        await handle_freeform_message(msg, state, restaurant, from_number, current_state)
        return
    address = ((msg.get("text") or {}).get("body") or "").strip()
    if len(address) > 300:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "Please send a shorter address (under 300 characters) or share a location pin.")
        return
    if len(address) < 10:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please send a complete delivery address or a location pin.")
        return
    await state.update_data(delivery_address=address, delivery_lat=None, delivery_lon=None)
    await state.set_state("waiting_for_address")
    await continue_after_delivery_address(state, restaurant, from_number)


async def handle_freeform_message(msg, state, restaurant, from_number, current_state):
    await send_whatsapp_main_menu(restaurant, from_number, state.supabase)


async def send_whatsapp_main_menu(restaurant, from_number, supabase):
    await send_catalog_message(restaurant, from_number, supabase)


async def send_whatsapp_command_menu(restaurant, from_number):
    await send_list(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "Choose an option.", "Options", [
            {"id": "cmd_status", "title": "Order status"},
            {"id": "cmd_cancel", "title": "Cancel order"},
            {"id": "cmd_history", "title": "Recent orders"},
            {"id": "cmd_help", "title": "Help"},
        ])


async def fetch_customer_order(supabase, order_id, restaurant_id, from_number, select):
    result = supabase.table("orders").select(select).eq(
        "id", order_id
    ).eq("restaurant_id", restaurant_id).eq(
        "order_channel", "whatsapp"
    ).eq("customer_contact", from_number).execute()
    return result.data[0] if result.data else None


async def send_customer_order_status(restaurant, from_number, supabase):
    result = supabase.table("orders").select(
        "id, order_status, order_type, dispatch_sent_at, payment_method, payment_status, total_amount"
    ).eq("restaurant_id", restaurant["id"]).eq(
        "order_channel", "whatsapp"
    ).eq("customer_contact", from_number).order("created_at", desc=True).limit(1).execute()
    orders = result.data or []
    if not orders:
        body = "You have no orders yet. Tap the shop icon to place one."
    else:
        order = orders[0]
        if order.get("payment_method") == "Paystack":
            payment_result = supabase.table("payments").select("paystack_reference, status, account_name, account_number, bank_name, amount, expires_at").eq("order_id", order["id"]).eq("restaurant_id", restaurant["id"]).eq("provider", "Paystack").eq("channel", "transfer").limit(1).execute()
            payment = (payment_result.data or [None])[0]
            if payment and payment.get("status") == "pending" and payment.get("paystack_reference"):
                from main import confirm_paystack_payment
                await confirm_paystack_payment(payment["paystack_reference"], "whatsapp_status")
                payment_result = supabase.table("payments").select("paystack_reference, status, account_name, account_number, bank_name, amount, expires_at").eq("order_id", order["id"]).eq("restaurant_id", restaurant["id"]).eq("provider", "Paystack").eq("channel", "transfer").limit(1).execute()
                payment = (payment_result.data or [payment])[0]
            if payment:
                current = await fetch_customer_order(supabase, order["id"], restaurant["id"], from_number,
                    "id, order_status, order_type, dispatch_sent_at, payment_method, payment_status")
                body = build_order_status_text(current or {}, payment, "whatsapp")
                await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, body)
                return
        detail = await fetch_customer_order(supabase, order["id"], restaurant["id"], from_number,
            "id, total_amount, order_status, order_type, dispatch_sent_at, payment_method, payment_status, order_items(quantity, menu_items(name))")
        items = ", ".join(f"{x.get('quantity') or 0} x {(x.get('menu_items') or {}).get('name', 'Item')}" for x in (detail or {}).get("order_items") or [])
        current = detail or order
        body = f"Order #{str(order.get('id'))[:8]}\n{truncate(items or 'No item details available.', 500)}\nTotal: ₦{money(order.get('total_amount')):,.0f}\n{build_order_status_text(current, None, 'whatsapp')}"
    await send_text(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        body,
    )


async def send_customer_order_history(state, restaurant, from_number, supabase):
    result = supabase.table("orders").select(
        "id, created_at, order_status, payment_method, payment_status, total_amount"
    ).eq("restaurant_id", restaurant["id"]).eq(
        "order_channel", "whatsapp"
    ).eq("customer_contact", from_number).order(
        "created_at", desc=True
    ).limit(3).execute()
    orders = result.data or []
    await state.set_state("browsing_orders")
    if not orders:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "You have no previous orders yet.",
        )
        return
    rows = []
    for order in orders:
        order_id = str(order.get("id") or "")
        status = format_order_status_line(order).split(" — ", 2)[1]
        created_at = str(order.get("created_at") or "Date unknown").split("T", 1)[0]
        rows.append({
            "id": f"hist:{order_id}",
            "title": f"#{order_id[:8]} ₦{money(order.get('total_amount')):,.0f}",
            "description": f"{created_at}, {status}",
        })
    await send_list(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "Your 3 most recent orders.", "Orders", rows,
    )


async def send_cancellable_orders(state, restaurant, from_number, supabase):
    result = supabase.table("orders").select(
        "id, created_at, order_status, payment_method, payment_status, total_amount"
    ).eq("restaurant_id", restaurant["id"]).eq(
        "order_channel", "whatsapp"
    ).eq("customer_contact", from_number).eq(
        "order_status", "pending"
    ).in_("payment_method", ["Cash Payment", "Pay on Delivery"]).order(
        "created_at", desc=True
    ).limit(5).execute()
    orders = result.data or []
    if not orders:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "You have no orders that can be cancelled. If your order is already being prepared, please contact the restaurant.",
        )
        return
    rows = []
    for order in orders:
        order_id = str(order.get("id") or "")
        created_at = str(order.get("created_at") or "Date unknown").split("T", 1)[0]
        rows.append({
            "id": f"cancel:{order_id}",
            "title": format_order_status_line(order),
            "description": f"{created_at}, {order.get('payment_method') or 'Payment'}",
        })
    await state.set_state("browsing_orders")
    await send_list(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "Choose a pending order to cancel.", "Orders", rows,
    )


async def begin_order_cancellation(order_id, state, restaurant, from_number, supabase):
    order = await fetch_customer_order(
        supabase, order_id, restaurant["id"], from_number,
        "id, order_status, payment_method, payment_status",
    )
    if not order:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "That order could not be found.",
        )
        return
    if order.get("order_status") != "pending":
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "It is too late to cancel; the restaurant is already working on this order.")
        return
    await state.update_data(cancel_order_id=str(order.get("id") or ""))
    await state.set_state("confirming_cancel")
    await send_buttons(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "Are you sure you want to cancel this order?",
        [
            {"id": f"cancel_yes:{order_id}", "title": "Yes, cancel"},
            {"id": "cancel_no", "title": "No, keep it"},
        ],
    )


async def confirm_order_cancellation(order_id, state, restaurant, from_number, supabase, bot):
    data = await state.get_data()
    if str(data.get("cancel_order_id") or "") != order_id:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Please use the buttons for the current order.",
        )
        return
    order = await fetch_customer_order(supabase, order_id, restaurant["id"], from_number,
                                      "id, order_status, payment_method, payment_status")
    confirmed_online = order and order.get("payment_method") in {"Paystack", "Bank Transfer"} and order.get("payment_status") == "confirmed"
    if confirmed_online:
        kitchen = restaurant.get("kitchen_chat_id")
        if kitchen:
            await (delivery_bots.get(restaurant["id"], bot)).send_message(kitchen, f"⚠️ Cancellation requested for order #{order_id[:8]} (payment confirmed).")
        await state.clear()
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                        "Your payment is confirmed, so we've asked the restaurant to review your cancellation. They'll get back to you here.")
        return
    outcome = await cancel_pending_order(bot, order_id, customer_contact=from_number,
                                         restaurant_id=restaurant["id"], allow_unconfirmed_online=True)
    if outcome["result"] != "cancelled":
        message = {
            "not_found": "We couldn't find that order. Please contact the restaurant.",
            "not_allowed": "This order can't be cancelled. Please contact the restaurant.",
            "already_started": "Sorry, the kitchen has already started this order. Please contact the restaurant.",
        }.get(outcome["result"], "We couldn't cancel that order. Please contact the restaurant.")
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            message,
        )
        await state.clear()
        return
    await send_text(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        f"Your order #{order_id[:8]} has been cancelled.",
    )
    await state.clear()


async def show_customer_order(order_id, restaurant, from_number, supabase):
    order = await fetch_customer_order(
        supabase, order_id, restaurant["id"], from_number,
        "id, total_amount, order_status, payment_method, payment_status, order_items(quantity, menu_items(name))",
    )
    if not order:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "That order could not be found.",
        )
        return
    item_lines = []
    for item in order.get("order_items") or []:
        menu_item = item.get("menu_items") or {}
        item_lines.append(f"{item.get('quantity') or 0} x {truncate(menu_item.get('name') or 'Item', 100)}")
    item_text = truncate("\n".join(item_lines) or "No item details available.", 3500)
    await send_text(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        f"{item_text}\n\n{format_order_status_line(order)}",
    )
    await send_buttons(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "Choose an action for this order.",
        [
            {"id": f"reorder:{order_id}", "title": "Reorder"},
            {"id": "cmd_history", "title": "Back"},
        ],
    )


async def reorder_customer_order(order_id, state, restaurant, from_number, supabase):
    order = await fetch_customer_order(
        supabase, order_id, restaurant["id"], from_number,
        "id, customer_name",
    )
    if not order:
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "That order could not be found.",
        )
        return
    old_items = supabase.table("order_items").select(
        "menu_item_id, quantity"
    ).eq("order_id", order["id"]).execute().data or []
    menu_item_ids = list(dict.fromkeys(
        item.get("menu_item_id") for item in old_items if item.get("menu_item_id")
    ))
    current_items = []
    if menu_item_ids:
        current_items = supabase.table("menu_items").select(
            "id, name, price, is_available, item_type, category_id, menu_categories(name, is_active)"
        ).in_("id", menu_item_ids).eq("restaurant_id", restaurant["id"]).execute().data or []
    menu_items_by_id = {item.get("id"): item for item in current_items if item.get("id")}
    requested, pseudo_rows = build_reorder_catalog_rows(old_items, menu_items_by_id)
    session_data = await state.get_data()
    menu_filter = session_data.get("menu_filter") if (
        session_data.get("table_id") and is_binding_fresh(session_data.get("table_bound_at"))
    ) else None
    cart, composites, problems = classify_cart_lines(requested, pseudo_rows, menu_filter)
    cart, problems = await validate_cart_inventory_and_drop_shortages(
        cart, problems, restaurant["id"]
    )
    if not await is_subscription_active(restaurant["id"]):
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "We're not accepting orders right now. Please try again later.",
        )
        return
    await submit_authoritative_cart(
        cart, composites, problems, state, restaurant, from_number,
        order.get("customer_name") or "Customer", supabase,
    )


async def handle_command_reply(reply_id, state, restaurant, from_number, supabase, bot, current_state):
    if reply_id == "cmd_status":
        await send_customer_order_status(restaurant, from_number, supabase)
    elif reply_id == "cmd_history":
        await send_customer_order_history(state, restaurant, from_number, supabase)
    elif reply_id == "cmd_help":
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "To order: tap the *shop icon*, add items, tap *View cart*, then *Send*.\n\nCommands: *MENU*, *STATUS*, *CANCEL*, *ORDERS*.")
        await send_whatsapp_command_menu(restaurant, from_number)
    elif reply_id == "cmd_cancel":
        await send_cancellable_orders(state, restaurant, from_number, supabase)


async def handle_interactive(msg, state, restaurant, from_number, supabase, bot, current_state):
    reply_id = interactive_reply_id(msg)
    if isinstance(reply_id, str) and (reply_id.startswith("mod:") or reply_id in {"mod_done", "mod_skip", "mod_more"}):
        if current_state == "configuring_modifiers":
            await handle_modifier_reply(reply_id, state, restaurant, from_number)
        return
    if isinstance(reply_id, str) and reply_id.startswith("cmd_"):
        if current_state in (None, "browsing_orders"):
            await handle_command_reply(reply_id, state, restaurant, from_number, supabase, bot, current_state)
        return
    if isinstance(reply_id, str) and reply_id.startswith("cancel_yes:"):
        if current_state == "confirming_cancel":
            await confirm_order_cancellation(
                reply_id.partition(":")[2], state, restaurant, from_number, supabase, bot
            )
        return
    if reply_id == "cancel_no":
        if current_state == "confirming_cancel":
            await state.clear()
            await send_text(
                restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
                "No problem, your order is unchanged.",
            )
        return
    if isinstance(reply_id, str) and reply_id.startswith("cancel:"):
        if current_state in (None, "browsing_orders"):
            await begin_order_cancellation(
                reply_id.partition(":")[2], state, restaurant, from_number, supabase
            )
        return
    if isinstance(reply_id, str) and reply_id.startswith("hist:"):
        if current_state in (None, "browsing_orders"):
            await show_customer_order(reply_id.partition(":")[2], restaurant, from_number, supabase)
        return
    if isinstance(reply_id, str) and reply_id.startswith("reorder:"):
        if current_state in (None, "browsing_orders"):
            await reorder_customer_order(
                reply_id.partition(":")[2], state, restaurant, from_number, supabase
            )
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
    if current_state != "waiting_for_payment_method" or button_id not in {"pay_cash", "pay_bank", "pay_pod", "pay_paystack", "pay_transfer_instant"}:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please choose a payment option from the current order.")
        return
    data = await state.get_data()
    allowed_ids = enabled_payment_option_ids(restaurant, data.get("order_type", "delivery"))
    if button_id not in allowed_ids:
        logging.warning("Rejected WhatsApp payment reply id=%s state=%s allowed_ids=%s", button_id, current_state, allowed_ids)
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "That payment method isn't available for this order.",
        )
        return
    try:
        total = await recalculate_total(state, restaurant)
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(exc, 1000)}. Please send a new catalog cart.")
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
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"Bank Transfer Details\n\nAmount: ₦{float(total):,.0f}\nBank: {truncate(info.get('bank_name'), 100)}\nAccount Number: {truncate(info.get('account_number'), 64)}\nAccount Name: {truncate(info.get('account_name'), 100)}\n\nAfter paying, send a screenshot of your transfer receipt as an image in this chat. The restaurant will confirm it manually, which can take a few minutes.")
        return
    if button_id == "pay_paystack":
        await start_paystack_payment(state, restaurant, from_number, customer_name, total)
        return
    if button_id == "pay_transfer_instant":
        await start_instant_transfer(state, restaurant, from_number, customer_name)
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
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order placed!\n\nOrder ID: #{order_id[:8]}\nTotal: ₦{float(total):,.0f}\nPayment: {truncate(payment_method, 100)}\n\nWe'll notify you when it is ready.\nYour receipt will be sent when your order is ready.\n\nReply MENU for options.")
        await clear_keeping_table_binding(state)
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(exc, 1000)}")
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
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order #{order_id[:8]} is pending payment.\nTotal: ₦{float(total):,.0f}\n\nComplete payment here:\n{truncate(payment_url, 2500)}\n\nWe'll confirm payment and send your order to the kitchen.\n\nReply MENU for options.")
        await clear_keeping_table_binding(state)
    except Exception:
        logging.exception("WhatsApp Paystack setup failed")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Could not create a Paystack payment link. Please choose another payment method.")


async def start_instant_transfer(state, restaurant, from_number, customer_name):
    """Create the pending Paystack order only after the normal stock re-check."""
    order_id = None
    details = None
    channel = "whatsapp"
    rid = restaurant.get("id")
    try:
        await state.update_data(payment_method="Paystack")
        order_id, order = await create_order_in_db(None, state, "Paystack", customer_name, "whatsapp", None, from_number)
    except Exception:
        logging.exception("Instant transfer setup failed stage=stock_recheck_failed channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, rid, order_id, None)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Please check your cart and try again.")
        return
    email = f"{''.join(ch for ch in from_number if ch.isdigit())}@chowlin.ng"
    order = {**(order or {}), "id": order_id, "total_amount": (order or {}).get("total_amount") or (await state.get_data()).get("total_price"), "restaurant_id": rid, "order_channel": channel}
    try:
        details = await create_transfer_charge(order, restaurant, email)
    except Exception:
        logging.exception("Instant transfer setup failed stage=charge_creation_failed channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, rid, order_id, None)
    if not details:
        logging.error("Instant transfer setup failed stage=charge_creation_failed channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, rid, order_id, None)
        await cancel_pending_order(shared_bot, order_id, customer_contact=from_number, restaurant_id=rid, allow_unconfirmed_online=True)
        await state.set_state("waiting_for_payment_method")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Instant transfer isn't available right now, please choose another payment method")
        return
    total = money(order.get("total_amount"))
    try:
        state.supabase.table("payments").insert({"order_id": order_id, "restaurant_id": rid, "amount": str(total), "provider": "Paystack", "status": "pending", "provider_reference": details["reference"], "paystack_reference": details["reference"], "paystack_subaccount_code": restaurant["paystack_subaccount_code"], "account_number": details["account_number"], "account_name": details["account_name"], "bank_name": details["bank_name"], "expires_at": details["expires_at"], "channel": "transfer"}).execute()
    except Exception:
        logging.exception("Instant transfer setup failed stage=persist_payment_failed channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, rid, order_id, details.get("reference"))
        await cancel_pending_order(shared_bot, order_id, customer_contact=from_number, restaurant_id=rid, allow_unconfirmed_online=True)
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "Something went wrong while setting up your transfer. Please try again or choose another payment method.")
        return
    expires = datetime.fromisoformat(details["expires_at"].replace("Z", "+00:00")).astimezone(__import__('pytz').timezone("Africa/Lagos")).strftime("%I:%M %p")
    await state.set_state("awaiting_transfer")
    await state.update_data(active_transfer_order_id=order_id)
    try:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"Bank: {details['bank_name']}\nAccount name: {details['account_name']}\nAccount number:\n{details['account_number']}\nAmount: ₦{total:,.0f}\nExpires: {expires} WAT\n\nTransfer the exact amount. Your order is confirmed automatically once payment arrives. {transfer_customer_wording('whatsapp')}")
    except Exception:
        logging.exception("Instant transfer setup failed stage=send_details_failed channel=%s restaurant_id=%s order_id=%s paystack_reference=%s", channel, rid, order_id, details.get("reference"))


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
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"✅ Order placed!\n\nOrder ID: #{order_id[:8]}\nTotal: ₦{float(total):,.0f}\n\nYour payment proof is being verified.\n\nReply MENU for options.")
        await clear_keeping_table_binding(state)
    except ValueError as exc:
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, f"⚠️ {truncate(exc, 1000)}")
    except Exception:
        logging.exception("WhatsApp bank-transfer order error")
        await send_text(restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number, "❌ Something went wrong. Please try again or contact the restaurant.")
