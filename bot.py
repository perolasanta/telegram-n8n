from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, message
from aiogram.filters import  CommandStart
from aiogram.utils.markdown import hbold 

from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext


import sqlite3
import logging
import asyncio


# Set up bot
TOKEN = "7667383218:AAEqvEgBoj6J6eFMtDIbPu3uxTF7GOzH5Q4"
KITCHEN_CHAT_ID = -4732576905 # Replace with your kitchen chat ID

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


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



# Menu items
MENU = {
    "Alcohol": ["Beer", "Wine", "Whiskey", "Bitters"],
    "Pepper Soup": ["Fish", "Chicken", "Goat", "Bokoto"],
    "Rice": ["Jollof Rice", "Fried Rice", "Ofada Rice"],
    "Noodles": ["Indomie", "Spaghetti"],
    "Soft Drinks": ["Coke", "Fanta", "Sprite", "Water", "Fearless"],
}

class OrderStates(StatesGroup):
    browsing_category = State()
    waiting_for_quantity = State()

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
    
    # Build cart display
    cart_text = "🛒 Your Cart:\n\n"
    total_items = 0
    for item, qty in cart.items():
        cart_text += f"• {qty}x {item}\n"
        total_items += qty
    
    cart_text += f"\n📊 Total Items: {total_items}"
    
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
    for item in MENU[category]:
        keyboard.add(InlineKeyboardButton(text=item, callback_data=f"item_{item}"))
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
    
    # Get category from state and return to category menu
    data = await state.get_data()
    category = data.get('current_category')
    
    # ✅ Call show_menu with correct parameters
    await show_menu(callback_query.message, category, state)
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
    category = data.get("current_category")
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
    
    # Return to browsing state and show category menu
    await state.set_state(OrderStates.browsing_category)
    
    # Show the category menu again with navigation buttons
    keyboard = InlineKeyboardBuilder()
    for menu_item in MENU[category]:
        keyboard.add(InlineKeyboardButton(text=menu_item, callback_data=f"item_{menu_item}"))
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

    
# Show cart
@dp.message(F.text.lower() == "cart")
async def show_cart(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    cart = user_orders.get(user_id, {})
    
    if not cart:
        await message.answer("Your cart is empty. 🛒")
        return
    
    # Build cart display
    cart_text = "🛒 Your Cart:\n\n"
    total_items = 0
    for item, qty in cart.items():
        cart_text += f"• {qty}x {item}\n"
        total_items += qty
    
    cart_text += f"\n📊 Total Items: {total_items}"
    
    # Create keyboard with Confirm and Clear Cart options
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm Order", callback_data="confirm_order")],
            [InlineKeyboardButton(text="🗑 Clear Cart", callback_data="clear_cart")]
        ]
    )
    await message.answer(cart_text, reply_markup=keyboard)


# Confirm order
@dp.callback_query(F.data == "confirm_order")
async def confirm_order(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    table_number = user_table_map.get(user_id, "Unknown")
    cart = user_orders.get(user_id, {})
    
    if not cart:
        await callback_query.message.answer("Your cart is empty. 🛒")
        await callback_query.answer()
        return
    
    # Build order text for kitchen
    order_text = f"🍽 New Order for Table {hbold(table_number)}:\n\n"
    for item, qty in cart.items():
        order_text += f"• {qty}x {item}\n"
        # Insert into database
        cursor.execute(
            "INSERT INTO orders (table_number, item, quantity) VALUES (?, ?, ?)", 
            (table_number, item, qty)
        )
    conn.commit()
    
    # Send to kitchen
    await bot.send_message(KITCHEN_CHAT_ID, order_text)
    
    # Clear cart and state
    user_orders[user_id] = {}
    await state.clear()
    
    # Confirmation message
    await callback_query.message.answer("✅ Your order has been placed successfully!")
    await callback_query.answer("Order sent to kitchen! 🍳")


# Optional: Clear cart handler
@dp.callback_query(F.data == "clear_cart")
async def clear_cart(callback_query: types.CallbackQuery, state: FSMContext):
    user_id = callback_query.from_user.id
    user_orders[user_id] = {}
    await state.clear()
    
    await callback_query.message.answer("🗑 Your cart has been cleared.")
    await callback_query.answer()



async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())