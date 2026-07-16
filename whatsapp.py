import httpx
import logging
from aiogram.types import BufferedInputFile
from whatsapp_state import WhatsAppState
from bot import (
    create_order_in_db,
    deduct_inventory_for_order,
    send_order_to_kitchen,
    send_restock_alert,
)

GRAPH_API = "https://graph.facebook.com/v25.0"

async def send_whatsapp_message(phone_number_id: str, token: str, to: str, payload: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{GRAPH_API}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"messaging_product": "whatsapp", "to": to, **payload}
        )
        response.raise_for_status()

async def send_text(phone_number_id, token, to, text):
    await send_whatsapp_message(phone_number_id, token, to,
        {"type": "text", "text": {"body": text}})

async def send_payment_buttons(phone_number_id, token, to):
    await send_whatsapp_message(phone_number_id, token, to, {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "How would you like to pay?"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "pay_cash", "title": "💰 Cash"}},
                {"type": "reply", "reply": {"id": "pay_bank", "title": "🏦 Bank Transfer"}},
                {"type": "reply", "reply": {"id": "pay_pod", "title": "🚚 Pay on Delivery"}},
            ]}
        }
    })


async def handle_whatsapp_webhook(payload: dict, supabase, bot):
    """Main router — dispatches by message type."""
    entry = payload["entry"][0]["changes"][0]["value"]
    phone_number_id = entry["metadata"]["phone_number_id"]
    if "messages" not in entry:
        return  # status update, not a message

    msg = entry["messages"][0]
    from_number = msg["from"]
    contact_name = entry.get("contacts", [{}])[0].get("profile", {}).get("name", "Customer")

    restaurant = supabase.table("restaurants")\
        .select("id, name, kitchen_chat_id, whatsapp_phone_number_id, whatsapp_access_token")\
        .eq("whatsapp_phone_number_id", phone_number_id).execute()
    if not restaurant.data:
        return
    r = restaurant.data[0]
    state = WhatsAppState(supabase, from_number, r["id"])
    session = supabase.table("whatsapp_sessions")\
        .select("state").eq("phone_number", from_number)\
        .eq("restaurant_id", r["id"]).execute()
    current_state = session.data[0]["state"] if session.data else None

    if msg["type"] == "order":
        await handle_cart_submission(msg, state, r, from_number, contact_name, supabase)
    elif msg["type"] == "interactive":
        await handle_payment_selection(msg, state, r, from_number, supabase, bot)
    elif msg["type"] == "location":
        await handle_location(msg, state, r, from_number)
    elif msg["type"] == "text":
        await handle_text(msg, state, r, from_number, current_state)
    elif msg["type"] == "image":
        await handle_payment_proof(msg, state, r, from_number, supabase, bot)


async def handle_cart_submission(msg, state, restaurant, from_number, contact_name, supabase):
    """WhatsApp 'order' message = the cart the customer just sent."""
    order_payload = msg["order"]  # {catalog_id, product_items: [{product_retailer_id, quantity, item_price}]}
    cart = {}
    for item in order_payload["product_items"]:
        mapped = supabase.table("menu_item_catalog_map")\
            .select("menu_item_id, menu_items(name, price)")\
            .eq("catalog_retailer_id", item["product_retailer_id"])\
            .eq("restaurant_id", restaurant["id"]).execute()
        if not mapped.data:
            continue
        menu_item = mapped.data[0]["menu_items"]
        cart[mapped.data[0]["menu_item_id"]] = {
            "name": menu_item["name"], "price": float(menu_item["price"]), "qty": item["quantity"]
        }

    total_price = sum(i["price"] * i["qty"] for i in cart.values())
    await state.update_data(
        cart=cart, total_price=total_price, order_type="delivery",
        restaurant_id=restaurant["id"], restaurant_name=restaurant["name"],
        kitchen_chat_id=restaurant["kitchen_chat_id"], customer_name=contact_name
    )
    await state.set_state("waiting_for_address")
    if not cart:
        await state.clear()
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "⚠️ We couldn't find available items in that cart. Please try again."
        )
        return

    await send_text(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
        "📍 Please share your delivery address or location pin."
    )


async def request_payment_method(state: WhatsAppState, restaurant: dict, from_number: str, address: str):
    await state.update_data(delivery_address=address)
    await state.set_state("waiting_for_payment_method")
    await send_payment_buttons(
        restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number
    )


async def handle_location(msg: dict, state: WhatsAppState, restaurant: dict, from_number: str):
    location = msg["location"]
    address = location.get("address") or f"Shared location: {location['latitude']:.6f}, {location['longitude']:.6f}"
    await state.update_data(delivery_lat=location["latitude"], delivery_lon=location["longitude"])
    await request_payment_method(state, restaurant, from_number, address)


async def handle_text(msg: dict, state: WhatsAppState, restaurant: dict, from_number: str, current_state: str | None):
    if current_state != "waiting_for_address":
        await send_text(
            restaurant["whatsapp_phone_number_id"], restaurant["whatsapp_access_token"], from_number,
            "Please add items from the catalog first, then send your delivery address."
        )
        return
    address = msg.get("text", {}).get("body", "").strip()
    if not address:
        return
    await request_payment_method(state, restaurant, from_number, address)


async def handle_payment_selection(msg: dict, state: WhatsAppState, restaurant: dict,
                                     from_number: str, supabase, bot):
    """Customer tapped a payment-method button (pay_cash / pay_bank / pay_pod)."""
    button_id = msg["interactive"]["button_reply"]["id"]
    data = await state.get_data()
    rest_id = restaurant["id"]
    phone_number_id = restaurant["whatsapp_phone_number_id"]
    token = restaurant["whatsapp_access_token"]
    customer_name = data.get("customer_name", "Customer")

    if button_id not in {"pay_cash", "pay_bank", "pay_pod"}:
        await send_text(phone_number_id, token, from_number, "Please choose one of the payment options above.")
        return

    if button_id == "pay_bank":
        bank_info = supabase.table("restaurants")\
            .select("bank_name, account_number, account_name")\
            .eq("id", rest_id).execute()
        info = bank_info.data[0] if bank_info.data else {}

        if not info.get("bank_name") or not info.get("account_number"):
            await send_text(phone_number_id, token, from_number,
                "⚠️ Bank transfer isn't available right now. Please choose Cash or Pay on Delivery.")
            return

        await state.update_data(payment_method="Bank Transfer")
        await state.set_state("waiting_for_payment_proof")

        total = data.get("total_price", 0)
        await send_text(phone_number_id, token, from_number,
            f"🏦 *Bank Transfer Details*\n\n"
            f"💰 Amount: ₦{total:,.0f}\n\n"
            f"Bank: {info['bank_name']}\n"
            f"Account Number: {info['account_number']}\n"
            f"Account Name: {info['account_name']}\n\n"
            f"📸 After paying, please send a screenshot of your payment receipt."
        )
        return

    payment_method = "Cash Payment" if button_id == "pay_cash" else "Pay on Delivery"
    await state.update_data(payment_method=payment_method)

    try:
        order_id, order = await create_order_in_db(
            user_id=None, state=state, payment_method=payment_method,
            customer_name=customer_name, order_channel="whatsapp",
            payment_proof_reference=None, customer_contact=from_number,
        )

        await send_order_to_kitchen(
            bot=bot, order_id=order_id, state=state,
            customer_name=customer_name, customer_contact=from_number,
        )

        low_stock_items = await deduct_inventory_for_order(order_id)
        await send_restock_alert(bot, rest_id, restaurant.get("kitchen_chat_id"), low_stock_items)

        total = data.get("total_price", 0)
        await send_text(phone_number_id, token, from_number,
            f"✅ Order placed!\n\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total:,.0f}\n"
            f"💵 Payment: {payment_method}\n\n"
            f"We'll notify you as soon as it's ready. 🛵"
        )
        await state.clear()

    except ValueError as e:
        await send_text(phone_number_id, token, from_number, f"⚠️ {e}")
    except Exception as e:
        logging.error(f"WhatsApp order error: {e}", exc_info=True)
        await send_text(phone_number_id, token, from_number,
            "❌ Something went wrong placing your order. Please try again or contact us directly.")
        



async def download_whatsapp_media(media_id: str, token: str) -> tuple[bytes, str]:
    """Resolve a WhatsApp media ID to (bytes, mime_type)."""
    async with httpx.AsyncClient() as client:
        meta_resp = await client.get(
            f"{GRAPH_API}/{media_id}",
            headers={"Authorization": f"Bearer {token}"}
        )
        meta_resp.raise_for_status()
        media_url = meta_resp.json()["url"]
        mime_type = meta_resp.json().get("mime_type", "image/jpeg")

        file_resp = await client.get(media_url, headers={"Authorization": f"Bearer {token}"})
        file_resp.raise_for_status()
        return file_resp.content, mime_type


async def handle_payment_proof(msg: dict, state: WhatsAppState, restaurant: dict,
                                 from_number: str, supabase, bot):
    """Customer sent a bank transfer payment screenshot."""
    phone_number_id = restaurant["whatsapp_phone_number_id"]
    token = restaurant["whatsapp_access_token"]
    media_id = msg["image"]["id"]
    rest_id = restaurant["id"]

    data = await state.get_data()
    if data.get("payment_method") != "Bank Transfer":
        await send_text(phone_number_id, token, from_number,
            "Please choose Bank Transfer and wait for the payment instructions before sending a receipt.")
        return
    customer_name = data.get("customer_name", "Customer")

    try:
        image_bytes, mime_type = await download_whatsapp_media(media_id, token)
    except Exception as e:
        logging.error(f"Failed to download WhatsApp payment proof: {e}")
        await send_text(phone_number_id, token, from_number,
            "⚠️ We couldn't read that image. Please try sending the screenshot again.")
        return

    try:
        order_id, order = await create_order_in_db(
            user_id=None, state=state, payment_method="Bank Transfer",
            customer_name=customer_name, order_channel="whatsapp",
            payment_proof_reference=f"wa_media:{media_id}", customer_contact=from_number,
        )

        proof_file = BufferedInputFile(image_bytes, filename="payment_proof.jpg")
        await send_order_to_kitchen(
            bot=bot, order_id=order_id, state=state, customer_name=customer_name,
            customer_contact=from_number, payment_proof=proof_file,
        )

        total = data.get("total_price", 0)
        await send_text(phone_number_id, token, from_number,
            f"✅ Order placed!\n\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total:,.0f}\n\n"
            f"Your payment proof has been received and is being verified. "
            f"You'll be notified once confirmed."
        )
        await state.clear()

    except ValueError as e:
        await send_text(phone_number_id, token, from_number, f"⚠️ {e}")
    except Exception as e:
        logging.error(f"WhatsApp bank transfer order error: {e}", exc_info=True)
        await send_text(phone_number_id, token, from_number,
            "❌ Something went wrong. Please try again or contact us directly.")
