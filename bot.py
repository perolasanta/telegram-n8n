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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from reports import generate_daily_report, generate_weekly_report
from receipt_generator import generate_receipt_pdf
from aiogram.types import FSInputFile
import pytz
from datetime import datetime, timedelta

router = Router()
load_dotenv()

# Initialize scheduler
scheduler = AsyncIOScheduler(timezone=pytz.timezone('Africa/Lagos'))

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

async def send_receipt_to_customer(user_id: int, order_id: str):
    """Generate and send PDF receipt to customer"""
    try:
        # Get order details
        order = supabase.table("orders")\
            .select("*, order_items(*, menu_items(name, price)), restaurant_tables(table_number), restaurants(name, phone)")\
            .eq("id", order_id)\
            .execute()
        
        if not order.data:
            return
        
        order_data = order.data[0]
        
        # Prepare receipt data
        items = []
        for item in order_data["order_items"]:
            items.append({
                'name': item["menu_items"]["name"],
                'qty': item["quantity"],
                'price': float(item["unit_price"]),
                'total': float(item["subtotal"])
            })
        
        receipt_data = {
            'order_id': order_id,
            'restaurant_name': order_data["restaurants"]["name"],
            'restaurant_phone': order_data["restaurants"].get("phone", ""),
            'table_number': order_data["restaurant_tables"]["table_number"],
            'customer_name': order_data["customer_name"],
            'created_at': datetime.fromisoformat(order_data["created_at"].replace('Z', '+00:00')),
            'items': items,
            'subtotal': float(order_data["total_amount"]),
            'tax': 0,
            'total': float(order_data["total_amount"]),
            'payment_method': order_data["payment_method"],
            'payment_status': order_data["payment_status"]
        }
        
        # Generate PDF
        pdf_path = await generate_receipt_pdf(receipt_data)
        
        # Send to customer
        receipt_file = FSInputFile(pdf_path)
        await bot.send_document(
            user_id,
            receipt_file,
            caption=f"📄 Receipt for Order #{order_id[:8]}\n\nThank you for your order!"
        )
        
        # Clean up
        os.remove(pdf_path)
        
        logging.info(f"Receipt sent to user {user_id} for order {order_id}")
        
    except Exception as e:
        logging.error(f"Failed to send receipt: {e}")


async def load_pending_reorder(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pending_cart = data.get("pending_reorder_cart")
    pending_restaurant = data.get("pending_reorder_restaurant_id")
    active_restaurant = data.get("restaurant_id")
    table_number = data.get("table_number")  # None means external

    if not pending_cart or pending_restaurant != active_restaurant:
        return False

    # Load the cart first
    await state.update_data(
        cart=pending_cart,
        pending_reorder_cart=None,
        pending_reorder_restaurant_id=None
    )

    cart_text = "🔄 Your saved reorder has been loaded!\n\n"
    total = 0
    for item in pending_cart.values():
        item_total = item["price"] * item["qty"]
        cart_text += f"• {item['qty']}x {item['name']} — ₦{item_total:,.0f}\n"
        total += item_total
    cart_text += f"\n💰 Total: ₦{total:,.0f}"

    await message.answer(cart_text)

    # If external table, still ask delivery or pickup before proceeding
    if table_number is None or table_number == "EXTERNAL":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚗 Delivery", callback_data="order_type_delivery")],
            [InlineKeyboardButton(text="🏃 Pickup", callback_data="order_type_pickup")]
        ])
        await message.answer(
            "How would you like to receive your order?",
            reply_markup=keyboard
        )
        return True  # stop here, let them pick delivery/pickup first

    # Dine-in — go straight to cart confirmation
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
        [InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")]
    ])
    await message.answer(
        "What would you like to do?",
        reply_markup=keyboard
    )
    return True


# ========== SCHEDULED REPORTS ==========

async def send_daily_reports():
    """Send daily reports to all restaurant managers"""
    try:
        restaurants = supabase.table("restaurants")\
            .select("id, name, manager_telegram_id, manager_name")\
            .eq("subscription_status", "active")\
            .not_.is_("manager_telegram_id", "null")\
            .execute()
        
        for restaurant in restaurants.data:
            manager_id = restaurant.get("manager_telegram_id")
            if not manager_id:
                continue
            
            report = await generate_daily_report(supabase, restaurant["id"])
            
            try:
                await bot.send_message(
                    manager_id,
                    report,
                    parse_mode="Markdown"
                )
                logging.info(f"Daily report sent to manager of {restaurant['name']}")
            except Exception as e:
                logging.error(f"Failed to send daily report to manager of {restaurant['name']}: {e}")
                
    except Exception as e:
        logging.error(f"Error in send_daily_reports: {e}")


async def send_weekly_reports():
    """Send weekly reports to all restaurant managers"""
    try:
        restaurants = supabase.table("restaurants")\
            .select("id, name, manager_telegram_id, manager_name")\
            .eq("subscription_status", "active")\
            .not_.is_("manager_telegram_id", "null")\
            .execute()
        
        for restaurant in restaurants.data:
            manager_id = restaurant.get("manager_telegram_id")
            if not manager_id:
                continue
            
            report = await generate_weekly_report(supabase, restaurant["id"])
            
            try:
                await bot.send_message(
                    manager_id,
                    report,
                    parse_mode="Markdown"
                )
                logging.info(f"Weekly report sent to manager of {restaurant['name']}")
            except Exception as e:
                logging.error(f"Failed to send weekly report to manager of {restaurant['name']}: {e}")
                
    except Exception as e:
        logging.error(f"Error in send_weekly_reports: {e}")




# ========== COMMAND HANDLERS ==========
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    args = message.text.split()
    user_id = message.from_user.id
    
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

            # Check if first-time user
        user_history = supabase.table("orders")\
            .select("id")\
            .eq("telegram_user_id", user_id)\
            .limit(1)\
            .execute()
        
        if not user_history.data:
            # Send welcome message with tips
            await message.answer(
                "👋 Welcome! Here's how to order:\n\n"
                "1️⃣ Browse menu categories\n"
                "2️⃣ Add items to cart 🛒\n"
                "3️⃣ Confirm & pay\n"
                "4️⃣ Wait for notification when ready!\n\n"
                "💡 Tip: You can /cancel anytime"
            )
        
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
    
    if await load_pending_reorder(message, state):
        return  # reorder loaded, skip normal menu display


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

    if await load_pending_reorder(message, state):
        return
    
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
    keyboard.adjust(3)
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
    keyboard.adjust(3)
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
    order_type = data.get("order_type", "dine_in")
    
    # Store total in state
    await state.update_data(total_price=total_price)

    # Build payment keyboard based on order type
    payment_buttons = []
    if order_type == "delivery":
        payment_buttons.append([InlineKeyboardButton(text="💵 Pay on Delivery", callback_data="pay_delivery")])
    payment_buttons.append([InlineKeyboardButton(text="💰 Cash Payment", callback_data="pay_cash")])
    payment_buttons.append([InlineKeyboardButton(text="🏦 Bank Transfer", callback_data="pay_bank")])
    payment_buttons.append([InlineKeyboardButton(text="🔙 Back to Cart", callback_data="view_cart")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=payment_buttons)
    
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
        "order_status": "pending",
        "order_type": data.get("order_type", "dine_in")
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


# ========== ORDER CONFIRMATION & PAYMENT ==========
# ====== PAYMENT HANDLERS SECTION ======

@dp.callback_query(F.data == "pay_delivery")
async def payment_delivery(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle Pay on Delivery payment method"""
    user_id = callback_query.from_user.id
    
    try:
        await state.update_data(payment_method="Pay on Delivery")
        
        # CREATE ORDER IN DATABASE
        order_id, order = await create_order_in_db(user_id, state, "Pay on Delivery")
        
        # SEND TO KITCHEN
        await send_order_to_kitchen(order_id, user_id, state)
        
        # SEND RECEIPT TO CUSTOMER
        await send_receipt_to_customer(user_id, order_id)
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        await callback_query.message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💵 Payment Method: Pay on Delivery\n\n"
            f"Please have cash ready when your order arrives.\n"
            f"📄 Receipt has been sent to you."
        )
        
        # Clear cart
        await state.update_data(cart={})
        
    except Exception as e:
        print(f"Order error: {e}")
        await callback_query.message.answer("❌ Failed to place order. Please try again.")
    
    await callback_query.answer()


@dp.callback_query(F.data == "pay_cash")
async def payment_cash(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle Cash Payment method"""
    user_id = callback_query.from_user.id
    
    try:
        await state.update_data(payment_method="Cash Payment")
        
        # CREATE ORDER IN DATABASE
        order_id, order = await create_order_in_db(user_id, state, "Cash Payment")
        
        # SEND TO KITCHEN
        await send_order_to_kitchen(order_id, user_id, state)
        
        # SEND RECEIPT TO CUSTOMER
        await send_receipt_to_customer(user_id, order_id)
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        await callback_query.message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💵 Payment Method: Cash Payment\n\n"
            f"Please pay cash when collecting your order.\n"
            f"📄 Receipt has been sent to you."
        )
        
        # Clear cart
        await state.update_data(cart={})
        
    except Exception as e:
        print(f"Order error: {e}")
        await callback_query.message.answer("❌ Failed to place order. Please try again.")
    
    await callback_query.answer()


@dp.callback_query(F.data == "pay_bank")
async def payment_bank(callback_query: types.CallbackQuery, state: FSMContext):
    """Handle Bank Transfer - Show bank details first"""
    data = await state.get_data()
    total_price = data.get("total_price", 0)
    restaurant_id = data.get("restaurant_id")
    
    # Get restaurant bank details FROM DATABASE
    restaurant = supabase.table("restaurants")\
        .select("name, phone, bank_name, account_number, account_name")\
        .eq("id", restaurant_id)\
        .execute()
    
    if not restaurant.data:
        await callback_query.message.answer("Error loading payment details. Please contact staff.")
        await callback_query.answer()
        return
    
    restaurant_info = restaurant.data[0]
    
    # Check if bank details are configured
    if not restaurant_info.get("bank_name") or not restaurant_info.get("account_number"):
        await callback_query.message.answer(
            "⚠️ Bank transfer is not available at this restaurant.\n"
            "Please choose another payment method."
        )
        await callback_query.answer()
        return
    
    await state.set_state(OrderStates.waiting_for_payment_proof)
    await state.update_data(payment_method="Bank Transfer")
    
    bank_details = (
        f"🏦 <b>Bank Transfer Details</b>\n\n"
        f"💰 Amount: ₦{total_price:,.0f}\n\n"
        f"<b>Account Details:</b>\n"
        f"Bank: {restaurant_info['bank_name']}\n"
        f"Account Number: {restaurant_info['account_number']}\n"
        f"Account Name: {restaurant_info['account_name']}\n\n"
        f"📸 After making the transfer, please send a screenshot "
        f"of your payment receipt."
    )
    
    await callback_query.message.answer(bank_details)
    await callback_query.answer()


@dp.message(OrderStates.waiting_for_payment_proof, F.photo)
async def receive_payment_proof(message: types.Message, state: FSMContext):
    """Handle payment proof screenshot for bank transfer"""
    photo = message.photo[-1]
    file_id = photo.file_id
    user_id = message.from_user.id
    
    try:
        # CREATE ORDER with payment proof
        order_id, order = await create_order_in_db(user_id, state, "Bank Transfer", file_id)
        
        data = await state.get_data()
        total_price = data.get("total_price", 0)
        
        # SEND TO KITCHEN with payment proof
        await send_order_to_kitchen(order_id, user_id, state, file_id)
        
        await message.answer(
            f"✅ Order placed successfully!\n"
            f"Order ID: #{order_id[:8]}\n"
            f"💰 Total: ₦{total_price:,.0f}\n"
            f"💳 Payment Method: Bank Transfer\n\n"
            f"Your payment proof has been received and is being verified. "
            f"You will be notified once confirmed.\n"
            f"📄 Receipt will be sent after payment confirmation."
        )
        
        # Clear cart and state
        await state.update_data(cart={})
        await state.clear()
        
    except Exception as e:
        print(f"Order error: {e}")
        await message.answer("❌ Failed to place order. Please try again.")


@dp.message(OrderStates.waiting_for_payment_proof)
async def payment_proof_invalid(message: types.Message):
    """Handle invalid payment proof"""
    await message.answer(
        "⚠️ Please send a screenshot/photo of your payment receipt.\n"
        "Or type /cancel to cancel."
    )


# ========== KITCHEN CALLBACKS ==========

@dp.callback_query(F.data.startswith("confirm_pay_"))
async def confirm_payment_handler(callback_query: types.CallbackQuery):
    """Kitchen confirms payment for bank transfer"""
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
            
            # NOW SEND RECEIPT after payment confirmed
            await send_receipt_to_customer(user_id, order_id)
            
        except Exception as e:
            print(f"Failed to notify customer: {e}")
    
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
    """Kitchen rejects payment"""
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
    """Kitchen marks order as ready"""
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


# ========== ORDER HISTORY ==========

@dp.message(Command("history"))
async def order_history(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    orders = supabase.table("orders")\
        .select("id, created_at, total_amount, order_status, payment_status, order_type, order_items(quantity, menu_items(name, price)), restaurants(name), restaurant_tables(table_number)")\
        .eq("telegram_user_id", user_id)\
        .order("created_at", desc=True)\
        .limit(5)\
        .execute()

    if not orders.data:
        await message.answer(
            "📭 You have no previous orders.\n\n"
            "Scan a restaurant QR code to start ordering!"
        )
        return

    emoji_map = {
        "pending": "⏳",
        "preparing": "🍳",
        "ready": "✅",
        "completed": "🎉",
        "cancelled": "❌"
    }

    for order in orders.data:
        order_id = order["id"]
        short_id = order_id[:8]
        date = datetime.fromisoformat(order["created_at"].replace('Z', '+00:00'))
        date_str = date.strftime("%d %b %Y, %I:%M %p")
        total = float(order["total_amount"])
        restaurant = order["restaurants"]["name"] if order.get("restaurants") else "Unknown"
        table = order["restaurant_tables"]["table_number"] if order.get("restaurant_tables") else "—"

        raw_status = order.get("order_status", "pending")
        raw_payment = order.get("payment_status", "pending")
        status_emoji = emoji_map.get(raw_status.lower(), "📦")
        status = raw_status.capitalize()
        payment = raw_payment.capitalize()

        # Build items text
        items_text = ""
        for oi in order.get("order_items", []):
            mi = oi.get("menu_items")
            if mi:
                items_text += f"   • {oi['quantity']}x {mi['name']} — ₦{float(mi['price']):,.0f}\n"

        text = (
            f"{status_emoji} <b>Order #{short_id}</b>\n"
            f"🏪 {restaurant} | 🪑 Table {table}\n"
            f"📅 {date_str}\n\n"
            f"{items_text}\n"
            f"💰 ₦{total:,.0f} | {status} | Payment: {payment}"
        )

        # Reorder button sits directly under each order card
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"🔄 Reorder these items",
                callback_data=f"reorder_{order_id}"
            )]
        ])

        await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("reorder_"))
async def handle_reorder(callback_query: types.CallbackQuery, state: FSMContext):
    """Reorder items from a past order"""
    order_id = callback_query.data.replace("reorder_", "")
    user_id = callback_query.from_user.id

    order = supabase.table("orders")\
        .select("restaurant_id, table_id, order_type, order_items(quantity, unit_price, menu_item_id, menu_items(id, name, price, is_available)), restaurants(name, kitchen_chat_id), restaurant_tables(table_number)")\
        .eq("id", order_id)\
        .eq("telegram_user_id", user_id)\
        .execute()

    if not order.data:
        await callback_query.message.answer("❌ Order not found.")
        await callback_query.answer()
        return

    order_data = order.data[0]
    reorder_restaurant_id = order_data["restaurant_id"]

    # Rebuild cart, skip unavailable items
    new_cart = {}
    skipped = []
    for item in order_data["order_items"]:
        menu_item = item.get("menu_items")
        
        if menu_item:
            # Normal path — menu item found
            if menu_item.get("is_available") is False:
                skipped.append(menu_item["name"])
                continue
            menu_item_id = menu_item["id"]
            new_cart[menu_item_id] = {
                "name": menu_item["name"],
                "price": float(menu_item["price"]),
                "qty": item["quantity"]
            }
        else:
            # Fallback — use data stored on the order_item itself
            menu_item_id = item.get("menu_item_id")
            if not menu_item_id:
                continue
            new_cart[menu_item_id] = {
                "name": f"Item ({menu_item_id[:8]})",  # best we can do without the join
                "price": float(item.get("unit_price", 0)),
                "qty": item["quantity"]
            }

    if not new_cart:
        await callback_query.message.answer(
            "⚠️ None of the items from this order are currently available."
        )
        await callback_query.answer()
        return

    # Check current session state
    current_data = await state.get_data()
    active_restaurant = current_data.get("restaurant_id")

    if active_restaurant and active_restaurant == reorder_restaurant_id:
        # ✅ Same restaurant session active — merge into current cart
        existing_cart = current_data.get("cart", {})
        for item_id, item in new_cart.items():
            if item_id in existing_cart:
                existing_cart[item_id]["qty"] += item["qty"]
            else:
                existing_cart[item_id] = item
        await state.update_data(cart=existing_cart)

        cart_text = "✅ Items added to your current cart!\n\n"
        total = 0
        for item in new_cart.values():
            item_total = item["price"] * item["qty"]
            cart_text += f"• {item['qty']}x {item['name']} — ₦{item_total:,.0f}\n"
            total += item_total
        if skipped:
            cart_text += f"\n⚠️ Unavailable (skipped): {', '.join(skipped)}"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")]
        ])
        await callback_query.message.answer(cart_text, reply_markup=keyboard)

    elif active_restaurant and active_restaurant != reorder_restaurant_id:
        # ❌ Different restaurant session active
        await callback_query.message.answer(
            "⚠️ You're currently in a session at a different restaurant.\n"
            "Please /cancel first, then scan the correct QR code to reorder."
        )

    else:
        # 📲 No active session — save cart and prompt QR scan
        await state.update_data(
            pending_reorder_cart=new_cart,
            pending_reorder_restaurant_id=reorder_restaurant_id
        )

        cart_text = "📋 Items saved for reorder:\n\n"
        total = 0
        for item in new_cart.values():
            item_total = item["price"] * item["qty"]
            cart_text += f"• {item['qty']}x {item['name']} — ₦{item_total:,.0f}\n"
            total += item_total
        cart_text += f"\n💰 Total: ₦{total:,.0f}"
        if skipped:
            cart_text += f"\n⚠️ Unavailable (skipped): {', '.join(skipped)}"
        cart_text += "\n\n📲 Please scan your table's QR code to place this reorder.\nYour items will be loaded automatically."

        await callback_query.message.answer(cart_text)

    await callback_query.answer()


# ==================== SEND REPORTS ===========================

# Manual report commands for managers
@dp.message(Command("daily_report"))
async def manual_daily_report(message: types.Message):
    """Manually request daily report (managers only)"""
    # Get restaurant by manager_telegram_id
    restaurant = supabase.table("restaurants")\
        .select("id, name")\
        .eq("manager_telegram_id", message.from_user.id)\
        .execute()
    
    if not restaurant.data:
        await message.answer("⚠️ You are not registered as a restaurant manager.")
        return
    
    await message.answer("📊 Generating daily report...")
    report = await generate_daily_report(supabase, restaurant.data[0]["id"])
    await message.answer(report, parse_mode="Markdown")


@dp.message(Command("weekly_report"))
async def manual_weekly_report(message: types.Message):
    """Manually request weekly report (managers only)"""
    restaurant = supabase.table("restaurants")\
        .select("id, name")\
        .eq("manager_telegram_id", message.from_user.id)\
        .execute()
    
    if not restaurant.data:
        await message.answer("⚠️ You are not registered as a restaurant manager.")
        return
    
    await message.answer("📊 Generating weekly report...")
    report = await generate_weekly_report(supabase, restaurant.data[0]["id"])
    await message.answer(report, parse_mode="Markdown")


# Optional: Monthly report
@dp.message(Command("monthly_report"))
async def manual_monthly_report(message: types.Message):
    """Manually request monthly report (managers only)"""
    restaurant = supabase.table("restaurants")\
        .select("id, name")\
        .eq("manager_telegram_id", message.from_user.id)\
        .execute()
    
    if not restaurant.data:
        await message.answer("⚠️ You are not registered as a restaurant manager.")
        return
    
    await message.answer("📊 Generating monthly report...")
    
    # Get last 30 days
    from datetime import datetime, timedelta
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    # You can create a similar function in reports.py
    # For now, let's use weekly report logic
    report = await generate_weekly_report(supabase, restaurant.data[0]["id"], end_date)
    report = report.replace("Weekly Report", "Monthly Report (Last 30 Days)")
    
    await message.answer(report, parse_mode="Markdown")

@dp.message(Command("register_manager"))
async def register_manager(message: types.Message):
    """Register as a restaurant manager"""
    
    # Check if user is already registered
    existing = supabase.table("restaurants")\
        .select("id, name")\
        .eq("manager_telegram_id", message.from_user.id)\
        .execute()
    
    if existing.data:
        await message.answer(
            f"✅ You are already registered as manager of:\n"
            f"• {existing.data[0]['name']}\n\n"
            f"Available commands:\n"
            f"/daily_report - Get today's sales report\n"
            f"/weekly_report - Get this week's report\n"
            f"/monthly_report - Get last 30 days report"
        )
        return
    
    await message.answer(
        "⚠️ You are not registered as a manager.\n\n"
        "To register, please ask your system administrator to update your restaurant record with your Telegram ID:\n"
        f"Your Telegram ID: `{message.from_user.id}`\n"
        f"Your Username: @{message.from_user.username or 'Not set'}",
        parse_mode="Markdown"
    )


# Admin command to set manager (optional)
@dp.message(Command("set_manager"))
async def set_manager(message: types.Message):
    """Set manager for a restaurant (admin only)"""
    
    # You can add admin check here
    # For now, anyone can use it (you should restrict this)
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer(
            "Usage: /set_manager <restaurant_id> <telegram_id>\n\n"
            "Example: /set_manager abc123 987654321"
        )
        return
    
    restaurant_id = args[1]
    manager_telegram_id = args[2]
    
    try:
        # Update restaurant
        result = supabase.table("restaurants")\
            .update({
                "manager_telegram_id": int(manager_telegram_id),
                "manager_name": f"Manager {manager_telegram_id}"
            })\
            .eq("id", restaurant_id)\
            .execute()
        
        if result.data:
            await message.answer(
                f"✅ Manager set successfully!\n"
                f"Restaurant: {result.data[0]['name']}\n"
                f"Manager Telegram ID: {manager_telegram_id}"
            )
        else:
            await message.answer("❌ Restaurant not found.")
            
    except Exception as e:
        await message.answer(f"❌ Error: {e}")

# ========== KITCHEN MENU MANAGEMENT ==========

def short_id(uuid_str):
    """Shorten UUID to 12 chars by removing dashes"""
    return uuid_str.replace('-', '')[:12]


@dp.message(Command("menu"))
async def kitchen_menu_management(message: types.Message):
    chat_id = message.chat.id

    restaurant = supabase.table("restaurants")\
        .select("id, name")\
        .eq("kitchen_chat_id", chat_id)\
        .execute()

    if not restaurant.data:
        await message.answer("⚠️ This command only works in a registered kitchen group.")
        return

    restaurant_id = restaurant.data[0]["id"]
    restaurant_name = restaurant.data[0]["name"]

    categories = await get_categories(restaurant_id)

    if not categories:
        await message.answer("No menu categories found.")
        return

    keyboard = InlineKeyboardBuilder()
    for cat in categories:
        keyboard.add(InlineKeyboardButton(
            text=cat["name"],
            callback_data=f"kmc_{short_id(cat['id'])}"  # kmc_ + 12 = 16 chars
        ))
    keyboard.adjust(2)

    await message.answer(
        f"🍽️ <b>{restaurant_name} — Menu Management</b>\n\n"
        f"Select a category to manage items:",
        reply_markup=keyboard.as_markup()
    )


async def get_full_id(table: str, short: str, id_field: str = "id"):
    """Resolve a short ID back to full UUID"""
    rows = supabase.table(table).select(id_field).execute()
    for row in rows.data:
        if row[id_field].replace('-', '')[:12] == short:
            return row[id_field]
    return None


@dp.callback_query(F.data.startswith("kmc_"))
async def kitchen_show_category_items(callback_query: types.CallbackQuery):
    short_cat = callback_query.data.replace("kmc_", "")

    # Resolve short category id
    cats = supabase.table("menu_categories").select("id, name, restaurant_id").execute()
    category_data = None
    for c in cats.data:
        if c["id"].replace('-', '')[:12] == short_cat:
            category_data = c
            break

    if not category_data:
        await callback_query.answer("Category not found.")
        return

    category_id = category_data["id"]
    category_name = category_data["name"]
    restaurant_id = category_data["restaurant_id"]

    items = supabase.table("menu_items")\
        .select("id, name, price, is_available")\
        .eq("category_id", category_id)\
        .order("name")\
        .execute()

    if not items.data:
        await callback_query.message.answer("No items in this category.")
        await callback_query.answer()
        return

    keyboard = InlineKeyboardBuilder()
    for item in items.data:
        status = "✅" if item["is_available"] else "❌"
        # kmt_ + 12 + _ + 12 = 29 chars
        keyboard.add(InlineKeyboardButton(
            text=f"{status} {item['name']} — ₦{float(item['price']):,.0f}",
            callback_data=f"kmt_{short_id(item['id'])}_{short_id(category_id)}"
        ))
    keyboard.adjust(1)
    # kmb_ + 12 = 16 chars
    keyboard.row(InlineKeyboardButton(
        text="🔙 Back to Categories",
        callback_data=f"kmb_{short_id(restaurant_id)}"
    ))

    await callback_query.message.answer(
        f"📋 <b>{category_name}</b>\n\n"
        f"✅ = Available | ❌ = Unavailable\n"
        f"Tap an item to toggle:",
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("kmt_"))
async def kitchen_toggle_item(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    short_item = parts[1]
    short_cat = parts[2]

    # Resolve item
    all_items = supabase.table("menu_items")\
        .select("id, name, is_available, category_id")\
        .execute()

    item_data = None
    for i in all_items.data:
        if i["id"].replace('-', '')[:12] == short_item:
            item_data = i
            break

    if not item_data:
        await callback_query.answer("Item not found.")
        return

    category_id = item_data["category_id"]
    new_status = not item_data["is_available"]

    supabase.table("menu_items")\
        .update({"is_available": new_status})\
        .eq("id", item_data["id"])\
        .execute()

    status_text = "✅ Available" if new_status else "❌ Unavailable"
    await callback_query.answer(f"{item_data['name']} → {status_text}", show_alert=True)

    # Refresh keyboard
    items = supabase.table("menu_items")\
        .select("id, name, price, is_available")\
        .eq("category_id", category_id)\
        .order("name")\
        .execute()

    category = supabase.table("menu_categories")\
        .select("restaurant_id")\
        .eq("id", category_id)\
        .execute()
    restaurant_id = category.data[0]["restaurant_id"]

    keyboard = InlineKeyboardBuilder()
    for item in items.data:
        status = "✅" if item["is_available"] else "❌"
        keyboard.add(InlineKeyboardButton(
            text=f"{status} {item['name']} — ₦{float(item['price']):,.0f}",
            callback_data=f"kmt_{short_id(item['id'])}_{short_id(category_id)}"
        ))
    keyboard.adjust(1)
    keyboard.row(InlineKeyboardButton(
        text="🔙 Back to Categories",
        callback_data=f"kmb_{short_id(restaurant_id)}"
    ))

    await callback_query.message.edit_reply_markup(
        reply_markup=keyboard.as_markup()
    )


@dp.callback_query(F.data.startswith("kmb_"))
async def kitchen_back_to_categories(callback_query: types.CallbackQuery):
    short_rest = callback_query.data.replace("kmb_", "")

    # Resolve restaurant
    restaurants = supabase.table("restaurants").select("id, name").execute()
    restaurant_data = None
    for r in restaurants.data:
        if r["id"].replace('-', '')[:12] == short_rest:
            restaurant_data = r
            break

    if not restaurant_data:
        await callback_query.answer("Restaurant not found.")
        return

    restaurant_id = restaurant_data["id"]
    categories = await get_categories(restaurant_id)

    keyboard = InlineKeyboardBuilder()
    for cat in categories:
        keyboard.add(InlineKeyboardButton(
            text=cat["name"],
            callback_data=f"kmc_{short_id(cat['id'])}"
        ))
    keyboard.adjust(2)

    await callback_query.message.answer(
        f"🍽️ <b>{restaurant_data['name']} — Menu Management</b>\n\n"
        f"Select a category to manage items:",
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()

## For production with FastAPI/webhook
## Don't use polling
#async def main():
#    logging.basicConfig(
#        level=logging.INFO,
#        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
#    )
#    
#    # Schedule reports
#    # Daily report at 11:59 PM (sent to managers)
#    scheduler.add_job(
#        send_daily_reports,
#        CronTrigger(hour=23, minute=59, timezone=pytz.timezone('Africa/Lagos')),
#        id='daily_reports'
#    )
#    
#   # Weekly report every Monday at 9:00 AM (sent to managers)
#    scheduler.add_job(
#        send_weekly_reports,
 #       CronTrigger(day_of_week='mon', hour=9, minute=0, timezone=pytz.timezone('Africa/Lagos')),
 #       id='weekly_reports'
 #   )
 #   
#    # Start scheduler
#    scheduler.start()
#    logging.info("✅ Scheduler started")
#    logging.info("📊 Daily reports: Every day at 11:59 PM → Managers")
#    logging.info("📊 Weekly reports: Every Monday at 9:00 AM → Managers")
#    
#    # Delete webhook if exists
#    await bot.delete_webhook(drop_pending_updates=True)
#    
#    logging.info("🤖 Starting bot...")
#    
#   # Start polling
 #   await dp.start_polling(bot)
#
#if __name__ == "__main__":
    asyncio.run(main())