from aiogram import Bot, Dispatcher, F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties 
import asyncio

TOKEN = "7667383218:AAEqvEgBoj6J6eFMtDIbPu3uxTF7GOzH5Q4"
KITCHEN_CHAT_ID = -4732576905 # Replace with your kitchen chat ID

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()



async def get_keyboard():
    return InlineKeyboardMarkup (inline_keyboard=[
        [InlineKeyboardButton(text="option 1", callback_data="opt_1")],
        [InlineKeyboardButton(text="option 2", callback_data="opt_2")]
    ])
async def mark_markup():
    return InlineKeyboardMarkup (inline_keyboard=[
        [InlineKeyboardButton(text="Marko", callback_data="mark")],
        
    ])

@dp.message(F.text == "/start")
async def cmd_start (message:Message):
    await message.answer("Choose your option: ", reply_markup=await get_keyboard())


@dp.callback_query(F.data == "opt_1")
async def process_1 (callbackquery:CallbackQuery):
    await callbackquery.answer("Option 1 was selected")
    await callbackquery.message.edit_text("Another markup",reply_markup=await mark_markup())

@dp.callback_query(F.data == "opt_2")
async def process_1 (callbackquery:CallbackQuery):
    await callbackquery.answer()
    await callbackquery.message.edit_text("We saw you chose question biyu")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())