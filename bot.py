from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.utils.markdown import hbold 
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

import logging
import asyncio
import aiohttp
import os
from supabase import Client, create_client
from decimal import Decimal
from dotenv import load_dotenv
import base64

router = Router()
load_dotenv()

# Environment variables
TOKEN = os.getenv("TOKEN")
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://n8n-atad.onrender.com/webhook/new-order")
N8N_UPDATE_WEBHOOK_URL = os.getenv("N8N_UPDATE_WEBHOOK_URL", "https://n8n-atad.onrender.com/webhook/update-sheet")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(router)

# Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# FSM States
class OrderStates(StatesGroup):
    browsing_category = State()
    waiting_for_quantity = State()
    waiting_for_payment_proof = State()
    waiting_for_address = State()


# ========== HELPER FUNCTIONS ==========

async def get_categories(restaurant_id: str):
    """Get active categories for restaurant"""
    response = supabase.table("menu_categories")\
        .select("*")\
        .eq("restaurant_id", restaurant_id)\
        .eq("is_active", True)\
        .order("display_order")\
        .execute()
    return response.data


async def get_menu_items(category_id: str):
    """Get available items in category"""
    response = supabase.table("menu_items")\
        .select("*")\
        .eq("category_id", category_id)\
        .eq("is_available", True)\
        .order("name")\
        .execute()
    return response.data


async def get_cart_summary(user_id: int, restaurant_id: str):
    """Get cart items from FSM state - returns dict {menu_item_id: {name, price, qty}}"""
    # This is stored in FSM state, not DB
    # We'll manage cart in memory via state since it's temporary
    return {}



# ========== COMMAND HANDLERS ==========
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    args = message.text.split()
    
    print("="*50)
    print(f"RAW MESSAGE: '{message.text}'")
    print(f"ARGS: {args}")
    print("="*50)
    
    if len(args) < 2:
        await message.answer(
            "Please scan a QR code from the restaurant to begin ordering.\n\n"
            "🪑 Table QR = Dine-in\n"
            "🏠 Restaurant QR = Delivery/Pickup"
        )
        return
    
    public_code = args[1]
    print(f"PUBLIC CODE: '{public_code}'")
    
    try:
        # Look up in restaurant_tables (includes both regular tables and external)
        table = supabase.table("restaurant_tables")\
            .select("id, table_number, restaurant_id, restaurants(id, name, kitchen_chat_id)")\
            .eq("public_code", public_code)\
            .eq("is_active", True)\
            .execute()
        
        print(f"Query result: {table.data}")
        
        if not table.data:
            await message.answer("Invalid QR code. Please contact staff.")
            return
        
        table_data = table.data[0]
        restaurant_data = table_data["restaurants"]
        
        table_id = table_data["id"]
        table_number = table_data["table_number"]
        restaurant_id = table_data["restaurant_id"]
        restaurant_name = restaurant_data["name"]
        kitchen_chat_id = restaurant_data["kitchen_chat_id"]
        
        # Check if this is external order (table_number is NULL or 'EXTERNAL')
        if table_number is None or table_number == 'EXTERNAL':
            # EXTERNAL ORDER (delivery/pickup)
            print(f"✅ EXTERNAL ORDER: {restaurant_name}")
            await handle_external_order(message, state, restaurant_id, restaurant_name, kitchen_chat_id, table_id)
        else:
            # DINE-IN ORDER (has table number)
            print(f"✅ DINE-IN: Table {table_number} at {restaurant_name}")
            await handle_dine_in_order(message, state, restaurant_id, restaurant_name, kitchen_chat_id, table_id, table_number)
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        await message.answer("Invalid QR code. Please try again.")


async def handle_dine_in_order(message: types.Message, state: FSMContext, restaurant_id: str, restaurant_name: str, kitchen_chat_id: int, table_id: str, table_number: str):
    """Handle dine-in order"""
    
    # Initialize cart for DINE-IN
    await state.update_data(
        restaurant_id=restaurant_id,
        table_id=table_id,
        restaurant_name=restaurant_name,
        table_number=table_number,
        kitchen_chat_id=kitchen_chat_id,
        order_type="dine_in",
        cart={}
    )
    
    # Get categories
    categories = await get_categories(restaurant_id)
    
    if not categories:
        await message.answer("No menu available. Please contact staff.")
        return
    
    # Build keyboard
    keyboard = InlineKeyboardBuilder()
    for category in categories:
        keyboard.add(InlineKeyboardButton(
            text=category["name"], 
            callback_data=f"cat_{category['id']}"
        ))
    keyboard.adjust(3)
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await message.answer(
        f"Welcome to {hbold(restaurant_name)}!\n"
        f"🪑 Dine-in - Table {hbold(table_number)}\n\n"
        f"Choose a category:",
        reply_markup=keyboard.as_markup()
    )


async def handle_external_order(message: types.Message, state: FSMContext, restaurant_id: str, restaurant_name: str, kitchen_chat_id: int, table_id: str):
    """Handle external order (delivery/pickup)"""
    
    # Store restaurant info in state
    await state.update_data(
        restaurant_id=restaurant_id,
        restaurant_name=restaurant_name,
        kitchen_chat_id=kitchen_chat_id,
        table_id=table_id,  # Still store the "EXTERNAL" table_id
        table_number=None,
        cart={}
    )
    
    # Ask order type
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Delivery", callback_data="order_type_delivery")],
        [InlineKeyboardButton(text="🏃 Pickup", callback_data="order_type_pickup")]
    ])
    
    await message.answer(
        f"Welcome to {hbold(restaurant_name)}!\n\n"
        f"How would you like to receive your order?",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "order_type_delivery")
async def order_type_delivery(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle delivery order selection"""
    
    await state.update_data(order_type="delivery")
    
    await callback_query.message.answer(
        "📍 Please enter your delivery address:\n\n"
        "Example: 123 Main Street, Minna, Niger State"
    )
    
    await state.set_state(OrderStates.waiting_for_address)
    await callback_query.answer()


@dp.callback_query(F.data == "order_type_pickup")
async def order_type_pickup(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle pickup order selection"""
    
    await state.update_data(order_type="pickup")
    
    await callback_query.message.answer(
        "✅ You selected Pickup!\n\n"
        "You'll collect your order from the restaurant."
    )
    
    await show_menu_categories(callback_query.message, state)
    await callback_query.answer()




@dp.message(OrderStates.waiting_for_address)
async def receive_address(message: types.Message, state: FSMContext):
    """Receive delivery address"""
    
    address = message.text.strip()
    
    if len(address) < 10:
        await message.answer("⚠️ Please enter a complete address.")
        return
    
    await state.update_data(delivery_address=address)
    
    await message.answer(
        f"✅ Delivery address saved:\n{address}\n\n"
        f"Now let's see the menu!"
    )
    
    await state.set_state(None)
    await show_menu_categories(message, state)


async def show_menu_categories(message: types.Message, state: FSMContext):
    """Show menu categories"""
    
    data = await state.get_data()
    restaurant_id = data.get("restaurant_id")
    restaurant_name = data.get("restaurant_name", "Restaurant")
    
    categories = await get_categories(restaurant_id)
    
    if not categories:
        await message.answer("No menu available. Please contact staff.")
        return
    
    keyboard = InlineKeyboardBuilder()
    for category in categories:
        keyboard.add(InlineKeyboardButton(
            text=category["name"], 
            callback_data=f"cat_{category['id']}"
        ))
    keyboard.adjust(3)
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await message.answer(
        f"{restaurant_name}\nChoose a category:",
        reply_markup=keyboard.as_markup()
    )   
    # ========== MENU NAVIGATION ==========

@dp.callback_query(F.data == "main_menu")
async def go_to_main_menu(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    restaurant_id = data.get("restaurant_id")
    restaurant_name = data.get("restaurant_name", "Restaurant")
    
    if not restaurant_id:
        await callback_query.message.answer("Session expired. Please /start again.")
        await callback_query.answer()
        return
    
    categories = await get_categories(restaurant_id)
    
    keyboard = InlineKeyboardBuilder()
    for category in categories:
        keyboard.add(InlineKeyboardButton(
            text=category["name"],
            callback_data=f"cat_{category['id']}"
        ))
    keyboard.adjust(2)
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await callback_query.message.answer(
        f"{restaurant_name}\nChoose a category:",
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("cat_"))
async def show_menu(callback_query: types.CallbackQuery, state: FSMContext):
    category_id = callback_query.data.replace("cat_", "")
    
    # Get category details
    category = supabase.table("menu_categories")\
        .select("name")\
        .eq("id", category_id)\
        .execute()
    
    if not category.data:
        await callback_query.answer("Category not found!")
        return
    
    # Get menu items
    items = await get_menu_items(category_id)
    
    if not items:
        await callback_query.message.answer("No items available in this category.")
        await callback_query.answer()
        return
    
    keyboard = InlineKeyboardBuilder()
    for item in items:
        keyboard.add(InlineKeyboardButton(
            text=f"{item['name']} - ₦{float(item['price']):,.0f}",
            callback_data=f"item_{item['id']}"
        ))
    keyboard.adjust(2)
    keyboard.row(
        InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")
    )
    
    await callback_query.message.answer(
        f"{category.data[0]['name']}:\n\nSelect an item:",
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


# ========== ITEM SELECTION ==========

@dp.callback_query(F.data.startswith("item_"))
async def select_quantity(callback_query: types.CallbackQuery, state: FSMContext):
    menu_item_id = callback_query.data.replace("item_", "")
    
    # Get item details
    item = supabase.table("menu_items")\
        .select("name, price")\
        .eq("id", menu_item_id)\
        .execute()
    
    if not item.data:
        await callback_query.answer("Item not found!")
        return
    
    # Store current item in state
    await state.update_data(current_item_id=menu_item_id)
    
    keyboard = InlineKeyboardBuilder()
    for i in range(1, 6):
        keyboard.add(InlineKeyboardButton(
            text=str(i),
            callback_data=f"qty_{menu_item_id}_{i}"
        ))
    keyboard.add(InlineKeyboardButton(
        text="Custom",
        callback_data=f"custom_{menu_item_id}"
    ))
    keyboard.adjust(3)
    keyboard.row(
        InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")
    )
    
    await callback_query.message.answer(
        f"Select quantity for {item.data[0]['name']}:",
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("qty_"))
async def add_to_cart(callback_query: types.CallbackQuery, state: FSMContext):
    parts = callback_query.data.split("_")
    menu_item_id = parts[1]
    quantity = int(parts[2])
    
    # Get item details from DB
    item = supabase.table("menu_items")\
        .select("name, price")\
        .eq("id", menu_item_id)\
        .execute()
    
    if not item.data:
        await callback_query.answer("Item not found!")
        return
    
    item_data = item.data[0]
    
    # Update cart in state
    data = await state.get_data()
    cart = data.get("cart", {})
    
    if menu_item_id in cart:
        cart[menu_item_id]["qty"] += quantity
    else:
        cart[menu_item_id] = {
            "name": item_data["name"],
            "price": float(item_data["price"]),
            "qty": quantity
        }
    
    await state.update_data(cart=cart)
    
    await callback_query.message.answer(
        f"✅ Added {quantity}x {item_data['name']} to cart!"
    )
    
    # Show main menu
    await go_to_main_menu(callback_query, state)
    await callback_query.answer()


@dp.callback_query(F.data.startswith("custom_"))
async def ask_custom_quantity(callback_query: types.CallbackQuery, state: FSMContext):
    menu_item_id = callback_query.data.replace("custom_", "")
    
    # Get item name
    item = supabase.table("menu_items")\
        .select("name")\
        .eq("id", menu_item_id)\
        .execute()
    
    await state.update_data(ordering_item_id=menu_item_id)
    await state.set_state(OrderStates.waiting_for_quantity)
    
    await callback_query.message.answer(
        f"How many {item.data[0]['name']} would you like? 🔢\n"
        "Please enter a number:"
    )
    await callback_query.answer()


@dp.message(OrderStates.waiting_for_quantity)
async def handle_custom_quantity(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("⚠️ Please enter a valid number.")
        return
    
    quantity = int(message.text)
    
    if quantity <= 0:
        await message.answer("⚠️ Quantity must be greater than zero.")
        return
    
    data = await state.get_data()
    menu_item_id = data.get("ordering_item_id")
    
    # Get item details
    item = supabase.table("menu_items")\
        .select("name, price")\
        .eq("id", menu_item_id)\
        .execute()
    
    if not item.data:
        await message.answer("Item not found!")
        return
    
    item_data = item.data[0]
    
    # Update cart
    cart = data.get("cart", {})
    
    if menu_item_id in cart:
        cart[menu_item_id]["qty"] += quantity
    else:
        cart[menu_item_id] = {
            "name": item_data["name"],
            "price": float(item_data["price"]),
            "qty": quantity
        }
    
    await state.update_data(cart=cart)
    
    await message.answer(f"✅ Added {quantity}x {item_data['name']} to cart!")
    
    # Clear waiting state
    await state.set_state(None)
    
    # Show categories
    restaurant_id = data.get("restaurant_id")
    categories = await get_categories(restaurant_id)
    
    keyboard = InlineKeyboardBuilder()
    for category in categories:
        keyboard.add(InlineKeyboardButton(
            text=category["name"],
            callback_data=f"cat_{category['id']}"
        ))
    keyboard.adjust(2)
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await message.answer(
        "Choose a category:",
        reply_markup=keyboard.as_markup()
    )


# ========== CART MANAGEMENT ==========

@dp.callback_query(F.data == "view_cart")
async def view_cart(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    
    if not cart:
        await callback_query.message.answer("Your cart is empty. 🛒")
        await callback_query.answer()
        return
    
    # Build cart display
    cart_text = "🛒 Your Cart:\n\n"
    total_price = 0
    total_items = 0
    
    for menu_item_id, item in cart.items():
        qty = item["qty"]
        price = item["price"]
        item_total = price * qty
        
        cart_text += f"• {qty}x {item['name']} - ₦{price:,.0f} = ₦{item_total:,.0f}\n"
        total_price += item_total
        total_items += qty
    
    cart_text += f"\n📊 Total Items: {total_items}"
    cart_text += f"\n💰 Total Price: ₦{total_price:,.0f}"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
        [InlineKeyboardButton(text="🗑 Clear Cart", callback_data="clear_cart")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")]
    ])
    
    await callback_query.message.answer(cart_text, reply_markup=keyboard)
    await callback_query.answer()


@dp.callback_query(F.data == "clear_cart")
async def handle_clear_cart(callback_query: types.CallbackQuery, state: FSMContext):
    await state.update_data(cart={})
    await callback_query.message.answer("🗑 Your cart has been cleared.")
    await callback_query.answer()


# ========== ORDER CONFIRMATION ==========

@dp.callback_query(F.data == "confirm_order")
async def confirm_order(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = data.get("cart", {})
    
    if not cart:
        await callback_query.message.answer("Your cart is empty. 🛒")
        await callback_query.answer()
        return
    
    # Calculate total
    total_price = sum(item["price"] * item["qty"] for item in cart.values())
    
    # Store total in state
    await state.update_data(total_price=total_price)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Pay on Delivery", callback_data="pay_delivery")],
        [InlineKeyboardButton(text="💰 Cash Payment", callback_data="pay_cash")],
        [InlineKeyboardButton(text="🏦 Bank Transfer", callback_data="pay_bank")],
        [InlineKeyboardButton(text="🔙 Back to Cart", callback_data="view_cart")]
    ])
    
    await callback_query.message.answer(
        f"💰 Total Amount: ₦{total_price:,.0f}\n\n"
        "Please select your payment method:",
        reply_markup=keyboard
    )
    await callback_query.answer()


async def create_order_in_db(user_id: int, state: FSMContext, payment_method: str, payment_proof_file_id: str = None):
    """Create order in database"""
    data = await state.get_data()
    cart = data.get("cart", {})
    restaurant_id = data.get("restaurant_id")
    table_id = data.get("table_id")
    total_price = data.get("total_price", 0)
    
    # Get customer name
    user = await bot.get_chat(user_id)
    customer_name = user.username or user.first_name or "Unknown"
    
    # Create order
    order_response = supabase.table("orders").insert({
        "restaurant_id": restaurant_id,
        "table_id": table_id,
        "telegram_user_id": user_id,
        "customer_name": customer_name,
        "total_amount": str(total_price),
        "payment_method": payment_method,
        "payment_status": "pending" if payment_method == "Bank Transfer" else "confirmed",
        "order_status": "pending"
    }).execute()
    
    if not order_response.data:
        raise Exception("Failed to create order")
    
    order_id = order_response.data[0]["id"]
    
    # Create order items
    order_items = []
    for menu_item_id, item in cart.items():
        unit_price = item["price"]
        quantity = item["qty"]
        subtotal = unit_price * quantity
        
        order_items.append({
            "order_id": order_id,
            "menu_item_id": menu_item_id,
            "quantity": quantity,
            "unit_price": str(unit_price),
            "subtotal": str(subtotal)
        })
    
    supabase.table("order_items").insert(order_items).execute()
    
    # If bank transfer, create payment record
    if payment_method == "Bank Transfer" and payment_proof_file_id:
        supabase.table("payments").insert({
            "order_id": order_id,
            "restaurant_id": restaurant_id,
            "amount": str(total_price),
            "provider": "Bank Transfer",
            "status": "pending",
            "provider_reference": payment_proof_file_id
        }).execute()
    
    return order_id, order_response.data[0]


async def send_order_to_kitchen(order_id: str, user_id: int, state: FSMContext, payment_proof_file_id: str = None):
    """Send order notification to kitchen"""
    data = await state.get_data()
    cart = data.get("cart", {})
    total_price = data.get("total_price", 0)
    table_number = data.get("table_number", "Unknown")
    restaurant_name = data.get("restaurant_name", "Restaurant")
    kitchen_chat_id = data.get("kitchen_chat_id")
    
    if not kitchen_chat_id:
        print("⚠️ No kitchen_chat_id configured for this restaurant")
        return
    
    # Get customer info
    user = await bot.get_chat(user_id)
    customer_name = user.username or user.first_name or "Unknown"
    
    # Build order message
    order_text = f"🍽 New Order #{order_id[:8]}\n"
    order_text += f"🏪 {restaurant_name}\n"
    order_text += f"📍 Table {table_number}\n\n"
    
    for item in cart.values():
        qty = item["qty"]
        name = item["name"]
        price = item["price"]
        order_text += f"• {qty}x {name} - ₦{price:,.0f}\n"
    
    order_text += f"\n💰 Total: ₦{total_price:,.0f}"
    
    payment_method = data.get("payment_method", "Unknown")
    order_text += f"\n💳 Payment: {payment_method}"
    
    if payment_method == "Bank Transfer":
        order_text += "\n⏳ Status: Pending Verification"
    
    order_text += f"\n👤 Customer: @{customer_name}" if user.username else f"\n👤 Customer: {customer_name}"
    
    # Send to kitchen
    if payment_proof_file_id:
        # Bank transfer - with payment proof
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Payment", callback_data=f"confirm_pay_{order_id}"),
                InlineKeyboardButton(text="❌ Reject Payment", callback_data=f"reject_pay_{order_id}")
            ]
        ])
        
        await bot.send_photo(
            kitchen_chat_id,
            photo=payment_proof_file_id,
            caption=order_text,
            reply_markup=keyboard
        )
    else:
        # Other payment methods
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍽️ Mark as Ready", callback_data=f"ready_{order_id}")]
        ])
        
        await bot.send_message(
            kitchen_chat_id,
            text=order_text,
            reply_markup=keyboard
        )


@dp.callback_query(F.data == "pay_delivery")
async def payment_delivery(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    
    try:
        await state.update_data(payment_method="Pay on Delivery")
        order_id, order = await create_order_in_db(user_id, state, "Pay on Delivery")
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        # Send to kitchen
        await send_order_to_kitchen(order_id, user_id, state)
        
        await callback_query.message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💵 Payment Method: Pay on Delivery\n\n"
            f"Please have cash ready when your order arrives."
        )
        
        # Clear cart
        await state.update_data(cart={})
        
    except Exception as e:
        print(f"Order error: {e}")
        await callback_query.message.answer("❌ Failed to place order. Please try again.")
    
    await callback_query.answer()


@dp.callback_query(F.data == "pay_cash")
async def payment_cash(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    
    try:
        await state.update_data(payment_method="Cash Payment")
        order_id, order = await create_order_in_db(user_id, state, "Cash Payment")
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        # Send to kitchen
        await send_order_to_kitchen(order_id, user_id, state)
        
        await callback_query.message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💵 Payment Method: Cash Payment\n\n"
            f"Please pay cash when collecting your order."
        )
        
        # Clear cart
        await state.update_data(cart={})
        
    except Exception as e:
        print(f"Order error: {e}")
        await callback_query.message.answer("❌ Failed to place order. Please try again.")
    
    await callback_query.answer()


@dp.callback_query(F.data == "pay_bank")
async def payment_bank(callback_query: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    total_price = data.get("total_price", 0)
    restaurant_id = data.get("restaurant_id")
    
    # Get restaurant bank details (you'll need to add these columns to restaurants table)
    restaurant = supabase.table("restaurants")\
        .select("name, phone")\
        .eq("id", restaurant_id)\
        .execute()
    
    await state.set_state(OrderStates.waiting_for_payment_proof)
    await state.update_data(payment_method="Bank Transfer")
    
    bank_details = (
        f"🏦 <b>Bank Transfer Details</b>\n\n"
        f"💰 Amount: ₦{total_price:,.0f}\n\n"
        f"<b>Account Details:</b>\n"
        f"Bank: GTBank\n"
        f"Account Number: 0123456789\n"
        f"Account Name: City Bites Minna\n\n"
        f"📸 After making the transfer, please send a screenshot "
        f"of your payment receipt."
    )
    
    await callback_query.message.answer(bank_details)
    await callback_query.answer()


@dp.message(OrderStates.waiting_for_payment_proof, F.photo)
async def receive_payment_proof(message: types.Message, state: FSMContext):
    photo = message.photo[-1]
    file_id = photo.file_id
    user_id = message.from_user.id
    
    try:
        # Create order with payment proof
        order_id, order = await create_order_in_db(user_id, state, "Bank Transfer", file_id)
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        # Send to kitchen with payment proof
        await send_order_to_kitchen(order_id, user_id, state, file_id)
        
        await message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💳 Payment Method: Bank Transfer\n\n"
            f"Your payment proof has been received and is being verified. "
            f"You will be notified once confirmed."
        )
        
        # Clear cart and state
        await state.update_data(cart={})
        await state.clear()
        
    except Exception as e:
        print(f"Order error: {e}")
        await message.answer("❌ Failed to place order. Please try again.")


@dp.message(OrderStates.waiting_for_payment_proof)
async def payment_proof_invalid(message: types.Message):
    await message.answer(
        "⚠️ Please send a screenshot/photo of your payment receipt.\n"
        "Or type /cancel to cancel."
    )


# ========== KITCHEN CALLBACKS ==========

@dp.callback_query(F.data.startswith("confirm_pay_"))
async def confirm_payment_handler(callback_query: types.CallbackQuery):
    order_id = callback_query.data.replace("confirm_pay_", "")
    
    # Update payment status in payments table
    supabase.table("payments")\
        .update({"status": "confirmed"})\
        .eq("order_id", order_id)\
        .execute()
    
    # Update order payment status
    supabase.table("orders")\
        .update({"payment_status": "confirmed"})\
        .eq("id", order_id)\
        .execute()
    
    # Get order details
    order = supabase.table("orders")\
        .select("telegram_user_id, customer_name")\
        .eq("id", order_id)\
        .execute()
    
    if order.data:
        user_id = order.data[0]["telegram_user_id"]
        customer_name = order.data[0]["customer_name"]
        
        try:
            await bot.send_message(
                user_id,
                f"✅ Your payment has been verified!\n"
                f"Order #{order_id[:8]} is now being prepared. 🍳"
            )
        except Exception as e:
            print(f"Failed to notify customer: {e}")
    
        # Send webhook notification
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    N8N_UPDATE_WEBHOOK_URL,
                    json={
                        "event": "payment_confirmed",
                        "chat_id": str(user_id),
                        "customer": customer_name,
                        "payment_status": "confirmed",
                        "order_id": str(order_id)
                    }
                )
        except Exception as e:
            print(f"Webhook error: {e}")
    
    # Update kitchen message
    ready_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽️ Mark as Ready", callback_data=f"ready_{order_id}")]
    ])
    
    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + "\n\n✅ PAYMENT CONFIRMED ✅",
        reply_markup=ready_keyboard
    )
    
    await callback_query.answer("✅ Payment confirmed!")


@dp.callback_query(F.data.startswith("reject_pay_"))
async def reject_payment_handler(callback_query: types.CallbackQuery):
    order_id = callback_query.data.replace("reject_pay_", "")
    
    # Update payment status
    supabase.table("payments")\
        .update({"status": "rejected"})\
        .eq("order_id", order_id)\
        .execute()
    
    # Update order payment status
    supabase.table("orders")\
        .update({"payment_status": "rejected"})\
        .eq("id", order_id)\
        .execute()
    
    # Get order details
    order = supabase.table("orders")\
        .select("telegram_user_id")\
        .eq("id", order_id)\
        .execute()
    
    if order.data:
        user_id = order.data[0]["telegram_user_id"]
        
        try:
            await bot.send_message(
                user_id,
                f"❌ Your payment for Order #{order_id[:8]} could not be verified.\n"
                f"Please contact us or submit a new payment proof."
            )
        except Exception as e:
            print(f"Failed to notify customer: {e}")
    
    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + "\n\n❌ PAYMENT REJECTED ❌",
        reply_markup=None
    )
    
    await callback_query.answer("❌ Payment rejected!")


@router.callback_query(F.data.startswith("ready_"))
async def handle_ready(callback_query: CallbackQuery):
    order_id = callback_query.data.replace("ready_", "")
    
    # Update order status
    supabase.table("orders")\
        .update({"order_status": "ready"})\
        .eq("id", order_id)\
        .execute()
    
    # Get order details
    order = supabase.table("orders")\
        .select("telegram_user_id, customer_name")\
        .eq("id", order_id)\
        .execute()
    
    if order.data:
        user_id = order.data[0]["telegram_user_id"]
        customer_name = order.data[0]["customer_name"]
        
        await bot.send_message(
            user_id,
            f"✅ Hi {customer_name}, your order #{order_id[:8]} is ready! Please come pick it up."
        )
    
    # Update kitchen message
    if callback_query.message.photo:
        await callback_query.message.edit_caption(
            caption=f"{callback_query.message.caption}\n\n🍽️ Order marked as ready. Customer notified.",
            reply_markup=None
        )
    else:
        await callback_query.message.edit_text(
            text=f"{callback_query.message.text}\n\n🍽️ Order marked as ready. Customer notified.",
            reply_markup=None
        )
    
    await callback_query.answer("Done!")


@dp.message(Command("cancel"))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Session cancelled. Scan QR code to start a new order.")


# For production with FastAPI/webhook
# Don't use polling
async def main():
    logging.basicConfig(level=logging.INFO)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
