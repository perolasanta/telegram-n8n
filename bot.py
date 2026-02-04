from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.filters import  CommandStart, Command
from aiogram.utils.markdown import hbold 

from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


router = Router()


import sqlite3
import logging
import asyncio
import aiohttp


# Set up bot
TOKEN = "7667383218:AAEqvEgBoj6J6eFMtDIbPu3uxTF7GOzH5Q4"
KITCHEN_CHAT_ID = -4732576905 # Replace with your kitchen chat ID
N8N_WEBHOOK_URL = "https://n8n-atad.onrender.com/webhook-test/new-order"  # Replace with your actual webhook URL


bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(router)

# User data storage
user_orders = {}  # {user_id: {item_name: quantity}}
user_table_map = {}  # {user_id: table_number}

# Database setup
conn = sqlite3.connect("orders.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        table_number TEXT,
        item TEXT,
        quantity INTEGER
    )
""")
conn.commit()



# Menu items with prices
MENU = {
    "Alcohol": {
        "Beer": 500,
        "Wine": 2000,
        "Whiskey": 3500,
        "Bitters": 300
    },
    "Pepper Soup": {
        "Fish": 1500,
        "Chicken": 1200,
        "Goat": 2000,
        "Bokoto": 1800
    },
    "Rice": {
        "Jollof Rice": 1000,
        "Fried Rice": 1200,
        "Ofada Rice": 900
    },
    "Noodles": {
        "Indomie": 500,
        "Spaghetti": 600
    },
    "Soft Drinks": {
        "Coke": 200,
        "Fanta": 200,
        "Sprite": 200,
        "Water": 100,
        "Fearless": 300
    },
}

class OrderStates(StatesGroup):
    browsing_category = State()
    waiting_for_quantity = State()
    waiting_for_payment_proof = State() 


@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    # Parse table number from command arguments
    args = message.text.split()
    table_number = args[1].replace("table_", "") if len(args) > 1 and args[1].startswith("table_") else "Unknown"
    
    # Store table number and initialize orders
    user_table_map[message.from_user.id] = table_number
    user_orders[message.from_user.id] = {}
    
    # Optional: Store table number in FSM state as well
    await state.update_data(table_number=table_number)
    
    # Clear any previous state (fresh start)
    await state.clear()
    
    print(user_table_map)
    print(user_orders)
    
    # Build category selection keyboard
    keyboard = InlineKeyboardBuilder()
    for category in MENU.keys():
        keyboard.add(InlineKeyboardButton(text=category, callback_data=f"menu_{category}"))
    keyboard.adjust(3)
    
    # Add View Cart button
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await message.answer(
        f"Welcome! Table {hbold(table_number)}.\nChoose a category:", 
        reply_markup=keyboard.as_markup()
    )

    # Handle main menu button
@dp.callback_query(F.data == "main_menu")
async def go_to_main_menu(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    
    # Build category selection keyboard
    keyboard = InlineKeyboardBuilder()
    for category in MENU.keys():
        keyboard.add(InlineKeyboardButton(text=category, callback_data=f"menu_{category}"))
    keyboard.adjust(3)
    
    # Add View Cart button
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await callback_query.message.answer(
        "Choose a category:", 
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


# Handle view cart button
@dp.callback_query(F.data == "view_cart")
async def view_cart_callback(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    cart = user_orders.get(user_id, {})
    
    if not cart:
        await callback_query.message.answer("Your cart is empty. 🛒")
        await callback_query.answer()
        return
    
    # Build cart display with prices
    cart_text = "🛒 Your Cart:\n\n"
    total_items = 0
    total_price = 0
    
    for item, qty in cart.items():
        price = get_item_price(item)
        item_total = price * qty
        total_price += item_total
        cart_text += f"• {qty}x {item} - ₦{price:,} = ₦{item_total:,}\n"
        total_items += qty
    
    cart_text += f"\n📊 Total Items: {total_items}"
    cart_text += f"\n💰 Total Price: ₦{total_price:,}"
    
    # Create keyboard with Confirm, Clear Cart, and Back options
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
            [InlineKeyboardButton(text="🗑 Clear Cart", callback_data="clear_cart")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")]
        ]
    )
    await callback_query.message.answer(cart_text, reply_markup=keyboard)
    await callback_query.answer()    

# Show menu items
async def show_menu(message: types.Message, category: str, state: FSMContext):
    # Store category in state
    await state.set_state(OrderStates.browsing_category)
    await state.update_data(current_category=category)
    
    keyboard = InlineKeyboardBuilder()
    for item, price in MENU[category].items():
        # Show item with price
        keyboard.add(InlineKeyboardButton(
            text=f"{item} - ₦{price:,}", 
            callback_data=f"item_{item}"
        ))
    keyboard.adjust(2)
    
    # Add navigation buttons
    keyboard.row(
        InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")
    )
    
    await message.answer(
        f"Choose from {category}:", 
        reply_markup=keyboard.as_markup()
    )

# Helper function to get item price
def get_item_price(item_name):
    for category_items in MENU.values():
        if item_name in category_items:
            return category_items[item_name]
    return 0  # Return 0 if item not found

# Show menu items handler
@dp.callback_query(F.data.startswith("menu_"))
async def show_menu_handler(callback_query: types.CallbackQuery, state: FSMContext):
    category = callback_query.data.split("_")[1]
    await show_menu(callback_query.message, category, state)
    await callback_query.answer()


# Select item and show quantity options
@dp.callback_query(F.data.startswith("item_"))
async def select_quantity(callback_query: types.CallbackQuery, state: FSMContext):
    item = callback_query.data.split("_")[1]
    
    # Store item in state (we already have category stored)
    await state.update_data(current_item=item)
    
    keyboard = InlineKeyboardBuilder()
    for i in range(1, 6):
        keyboard.add(InlineKeyboardButton(
            text=str(i), 
            callback_data=f"quantity_{item}_{i}"
        ))
    keyboard.add(InlineKeyboardButton(
        text="Other", 
        callback_data=f"custom_{item}"
    ))
    keyboard.adjust(3)
    
    # Add navigation buttons
    keyboard.row(
        InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton(text="🏠 Main Menu", callback_data="main_menu")
    )
    
    await callback_query.message.answer(
        f"Select quantity for {item}:", 
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()


# Handle predefined quantities
@dp.callback_query(F.data.startswith("quantity_"))
async def add_to_cart(callback_query: types.CallbackQuery, state: FSMContext):
    _, item, quantity = callback_query.data.split("_")
    user_id = callback_query.from_user.id
    
    # Initialize user orders if needed
    if user_id not in user_orders:
        user_orders[user_id] = {}
    
    # Add or update quantity
    if item in user_orders[user_id]:
        user_orders[user_id][item] += int(quantity)
    else:
        user_orders[user_id][item] = int(quantity)
    
    await callback_query.message.answer(f"✅ Added {quantity}x {item} to your cart.")
    
    # Clear state and go to main menu
    await state.clear()
    
    # Build main menu keyboard
    keyboard = InlineKeyboardBuilder()
    for category in MENU.keys():
        keyboard.add(InlineKeyboardButton(text=category, callback_data=f"menu_{category}"))
    keyboard.adjust(3)
    
    # Add View Cart button
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await callback_query.message.answer(
        "Choose a category:", 
        reply_markup=keyboard.as_markup()
    )
    await callback_query.answer()

# Handle custom quantity request
@dp.callback_query(F.data.startswith("custom_"))
async def ask_custom_quantity(callback_query: types.CallbackQuery, state: FSMContext):
    item = callback_query.data.replace("custom_", "")
    
    # Store item and set state
    await state.update_data(ordering_item=item)
    await state.set_state(OrderStates.waiting_for_quantity)
    
    await callback_query.message.answer(
        f"How many **{item}** would you like? 🔢\n"
        "Please enter a number below:"
    )
    await callback_query.answer()

# Handle custom quantity input
@dp.message(OrderStates.waiting_for_quantity)
async def handle_custom_quantity(message: types.Message, state: FSMContext):
    # Validation
    if not message.text.isdigit():
        await message.answer("⚠️ Please enter a valid number (e.g., 1, 2, 5).")
        return
    
    # Retrieve data from state
    data = await state.get_data()
    item = data.get("ordering_item")
    quantity = int(message.text)
    
    # Check positive quantity
    if quantity <= 0:
        await message.answer("⚠️ Quantity must be greater than zero.")
        return
    
    # Update orders
    user_id = message.from_user.id
    if user_id not in user_orders:
        user_orders[user_id] = {}
    
    # Add to existing quantity instead of replacing
    if item in user_orders[user_id]:
        user_orders[user_id][item] += quantity
    else:
        user_orders[user_id][item] = quantity
    
    await message.answer(f"✅ Added {quantity}x {item} to your cart!")
    
    # Clear state and go to main menu
    await state.clear()
    
    # Build main menu keyboard
    keyboard = InlineKeyboardBuilder()
    for category in MENU.keys():
        keyboard.add(InlineKeyboardButton(text=category, callback_data=f"menu_{category}"))
    keyboard.adjust(3)
    
    # Add View Cart button
    keyboard.row(InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"))
    
    await message.answer(
        "Choose a category:", 
        reply_markup=keyboard.as_markup()
    )

# Show cart
@dp.message(F.text.lower() == "cart")
async def show_cart(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cart = user_orders.get(user_id, {})
    
    if not cart:
        await message.answer("Your cart is empty. 🛒")
        return
    
    # Build cart display with prices
    cart_text = "🛒 Your Cart:\n\n"
    total_items = 0
    total_price = 0
    
    for item, qty in cart.items():
        price = get_item_price(item)
        item_total = price * qty
        total_price += item_total
        cart_text += f"• {qty}x {item} - ₦{price:,} = ₦{item_total:,}\n"
        total_items += qty
    
    cart_text += f"\n📊 Total Items: {total_items}"
    cart_text += f"\n💰 Total Price: ₦{total_price:,}"
    
    # Create keyboard with Confirm and Clear Cart options
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
            [InlineKeyboardButton(text="🗑 Clear Cart", callback_data="clear_cart")]
        ]
    )
    await message.answer(cart_text, reply_markup=keyboard)


# Confirm order - Show payment methods
@dp.callback_query(F.data == "confirm_order")
async def confirm_order(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    table_number = user_table_map.get(user_id, "Unknown")
    cart = user_orders.get(user_id, {})
    
    if not cart:
        await callback_query.message.answer("Your cart is empty. 🛒")
        await callback_query.answer()
        return
    
    # Calculate total
    total_price = 0
    for item, qty in cart.items():
        price = get_item_price(item)
        total_price += price * qty
    
    # Store order details in state for later use
    await state.update_data(
        total_price=total_price,
        cart=cart,
        table_number=table_number
    )
    
    # Show payment method selection
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💵 Pay on Delivery", callback_data="payment_delivery")],
            [InlineKeyboardButton(text="💰 Cash Payment", callback_data="payment_cash")],
            [InlineKeyboardButton(text="🏦 Bank Transfer", callback_data="payment_bank")],
            [InlineKeyboardButton(text="🔙 Back to Cart", callback_data="view_cart")]
        ]
    )
    
    await callback_query.message.answer(
        f"💰 Total Amount: ₦{total_price:,}\n\n"
        "Please select your payment method:",
        reply_markup=keyboard
    )
    await callback_query.answer()

# Handle Pay on Delivery
@dp.callback_query(F.data == "payment_delivery")
async def payment_delivery(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.answer(
        "Please have cash or transfer ready on delivery."
    )
    await process_order(callback_query, state, "Pay on Delivery")

# Handle Cash Payment
@dp.callback_query(F.data == "payment_cash")
async def payment_cash(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.message.answer(
        "Please pay cash when collecting"
    )
    await process_order(callback_query, state, "Cash Payment")

# Handle Bank Transfer - Show bank details
@dp.callback_query(F.data == "payment_bank")
async def payment_bank(callback_query: types.CallbackQuery, state: FSMContext):
    # Get order details from state
    data = await state.get_data()
    total_price = data.get('total_price', 0)
    
    # Set state to wait for payment proof
    await state.set_state(OrderStates.waiting_for_payment_proof)
    
    # Bank details message
    bank_details = (
        f"🏦 <b>Bank Transfer Details</b>\n\n"
        f"💰 Amount: ₦{total_price:,}\n\n"
        f"<b>Account Details:</b>\n"
        f"Bank: GTBank\n"
        f"Account Number: 0123456789\n"
        f"Account Name: City Bites Minna\n\n"
        f"📸 After making the transfer, please send a screenshot "
        f"of your payment receipt."
    )
    
    await callback_query.message.answer(bank_details)
    await callback_query.answer()

# Handle payment proof (screenshot)
# Handle payment proof (screenshot)
@dp.message(OrderStates.waiting_for_payment_proof, F.photo)
async def receive_payment_proof(message: types.Message, state: FSMContext):
    # Get the highest resolution photo
    photo = message.photo[-1]
    file_id = photo.file_id
    
    # Get order details from state
    data = await state.get_data()
    cart = data.get('cart', {})
    table_number = data.get('table_number', 'Unknown')
    total_price = data.get('total_price', 0)
    
    user_id = message.from_user.id
    user = message.from_user
    telegram_username = user.username or user.first_name or "Unknown User"
    
    # Build order text with payment proof
    order_text = f"🍽 New Order for Table {hbold(table_number)}:\n\n"
    items_list = []
    
    for item, qty in cart.items():
        price = get_item_price(item)
        item_total = price * qty
        
        items_list.append({
            "item": item,
            "qty": qty,
            "price": price
        })
        
        order_text += f"• {qty}x {item} - ₦{price:,} = ₦{item_total:,}\n"
        
        # Insert into database
        cursor.execute(
            "INSERT INTO orders (table_number, item, quantity) VALUES (?, ?, ?)", 
            (table_number, item, qty)
        )
    
    order_text += f"\n💰 {hbold(f'Total: ₦{total_price:,}')}"
    order_text += f"\n💳 Payment Method: Bank Transfer"
    order_text += f"\n⏳ Payment Status: Pending Verification"
    order_text += f"\n👤 Customer: @{telegram_username}" if user.username else f"\n👤 Customer: {telegram_username}"
    
    conn.commit()
    
    # Prepare webhook payload
    webhook_payload = {
        "restaurant": "Demo Restaurant Minna",
        "customer_name": telegram_username,
        "chat_id": str(user_id),
        "table_number": table_number,
        "items": items_list,
        "total": total_price,
        "payment_method": "Bank Transfer",
        "payment_status": "pending",
        "payment_proof_file_id": file_id
    }
    
    # Send to n8n webhook
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                N8N_WEBHOOK_URL, 
                json=webhook_payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    print("✅ Webhook sent successfully")
    except Exception as e:
        print(f"❌ Error sending webhook: {e}")
    
    # CREATE KEYBOARD WITH CONFIRM/REJECT BUTTONS FOR KITCHEN
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm Payment", 
                    callback_data=f"confirm_payment_{user_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Reject Payment", 
                    callback_data=f"reject_payment_{user_id}"
                )
            ]
        ]
    )
    
    # SEND TO KITCHEN WITH PAYMENT PROOF AND BUTTONS
    await bot.send_photo(
        KITCHEN_CHAT_ID,
        photo=file_id,
        caption=order_text,
        reply_markup=keyboard  # Add buttons for kitchen staff to confirm/reject
    )
    
    # Clear cart and state
    user_orders[user_id] = {}
    await state.clear()
    
    # Confirmation to customer
    await message.answer(
        f"✅ Your order has been placed successfully!\n"
        f"💰 Total: ₦{total_price:,}\n"
        f"💳 Payment Method: Bank Transfer\n\n"
        f"Your payment proof has been received and is being verified. "
        f"You will be notified once confirmed."
    )

    
# Handle if user sends text instead of photo
@dp.message(OrderStates.waiting_for_payment_proof)
async def payment_proof_invalid(message: types.Message):
    await message.answer(
        "⚠️ Please send a screenshot/photo of your payment receipt.\n"
        "Or type /cancel to cancel the order."
    )

# Process order for non-bank payment methods
async def process_order(callback_query: types.CallbackQuery, state: FSMContext, payment_method: str):
    # Get order details from state
    data = await state.get_data()
    cart = data.get('cart', {})
    table_number = data.get('table_number', 'Unknown')
    total_price = data.get('total_price', 0)
    
    user_id = callback_query.from_user.id
    user = callback_query.from_user
    telegram_username = user.username or user.first_name or "Unknown User"
    
    # Determine payment status based on method
    if payment_method == "Bank Transfer":
        payment_status = "pending"  # Needs proof verification
    elif payment_method == "Pay on Delivery":
        payment_status = "pending"  # Will pay when food arrives
    elif payment_method == "Cash Payment":
        payment_status = "confirmed"  # Assuming they paid at counter
    else:
        payment_status = "pending"
    
    # Build order text
    order_text = f"🍽 New Order for Table {hbold(table_number)}:\n\n"
    items_list = []
    
    for item, qty in cart.items():
        price = get_item_price(item)
        item_total = price * qty
        
        items_list.append({
            "item": item,
            "qty": qty,
            "price": price
        })
        
        order_text += f"• {qty}x {item} - ₦{price:,} = ₦{item_total:,}\n"
        
        # Insert into database
        cursor.execute(
            "INSERT INTO orders (table_number, item, quantity) VALUES (?, ?, ?)", 
            (table_number, item, qty)
        )
    
    order_text += f"\n💰 {hbold(f'Total: ₦{total_price:,}')}"
    order_text += f"\n💳 Payment Method: {payment_method}"
    
    # Add payment status indicator
    if payment_status == "pending":
        order_text += f"\n⏳ Payment Status: Pending"
    else:
        order_text += f"\n✅ Payment Status: Confirmed"
    
    conn.commit()
    
    # Prepare webhook payload
    webhook_payload = {
        "restaurant": "Demo Restaurant Minna",
        "customer_name": telegram_username,
        "chat_id": str(user_id),
        "table_number": table_number,
        "items": items_list,
        "total": total_price,
        "payment_method": payment_method,
        "payment_status": payment_status  # Add status here
    }
    
    # Send to n8n webhook
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                N8N_WEBHOOK_URL, 
                json=webhook_payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    print("✅ Webhook sent successfully")
    except Exception as e:
        print(f"❌ Error sending webhook: {e}")
    
    # Send to kitchen
    await bot.send_message(KITCHEN_CHAT_ID, order_text)
    
    # Clear cart and state
    user_orders[user_id] = {}
    await state.clear()
    
    # Confirmation message
    await callback_query.message.answer(
        f"✅ Your order has been placed successfully!\n"
        f"💰 Total: ₦{total_price:,}\n"
        f"💳 Payment Method: {payment_method}"
    )
    await callback_query.answer("Order sent to kitchen! 🍳")

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment_handler(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split("_")[2])
    
    # Notify customer that payment is confirmed
    try:
        await bot.send_message(
            user_id,
            "✅ Your payment has been verified!\n"
            "Your order is now being prepared. 🍳"
        )
    except Exception as e:
        print(f"Failed to notify customer {user_id}: {e}")
    
    # Update the kitchen message to show it's confirmed
    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + "\n\n✅ PAYMENT CONFIRMED ✅",
        reply_markup=None  # Remove the buttons
    )
    
    await callback_query.answer("✅ Payment confirmed and customer notified!")


@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment_handler(callback_query: types.CallbackQuery):
    user_id = int(callback_query.data.split("_")[2])
    
    # Notify customer that payment was rejected
    try:
        await bot.send_message(
            user_id,
            "❌ Your payment could not be verified.\n"
            "Please contact us or submit a new payment proof."
        )
    except Exception as e:
        print(f"Failed to notify customer {user_id}: {e}")
    
    # Update the kitchen message
    await callback_query.message.edit_caption(
        caption=callback_query.message.caption + "\n\n❌ PAYMENT REJECTED ❌",
        reply_markup=None
    )
    
    await callback_query.answer("❌ Payment rejected and customer notified!")


@dp.message(Command("cancel"))
async def cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Order cancelled. Use /start to begin a new order.")

# Optional: Clear cart handler
@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    user_orders[user_id] = {}
    await state.clear()
    
    await callback_query.message.answer("🗑 Your cart has been cleared.")
    await callback_query.answer()



@router.callback_query(F.data.startswith("ready|"))
async def handle_ready(callback_query: CallbackQuery):
    # Answer immediately so the button doesn't hang
    await callback_query.answer("Done!")

    # Parse the callback_data
    action, customer, client_chat_id = callback_query.data.split("|")

    # Notify the client
    await callback_query.bot.send_message(
        chat_id=int(client_chat_id),
        text=f"✅ Hi *{customer}*, your order is ready! Please come pick it up.",
        parse_mode="Markdown"
    )

    # Confirm to the admin (update the button message so they know it's done)
    await callback_query.message.edit_text(
        f"✅ *{customer}* — marked as ready. Client has been notified.",
        parse_mode="Markdown"
    )

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
