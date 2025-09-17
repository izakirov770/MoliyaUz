from aiogram import Router, F
from aiogram.types import Message
from bot.keyboards import get_main_menu

cards_router = Router()


@cards_router.message(F.text.in_({"💳 Kartalarim", "Kartalarim"}))
async def cards_menu(message: Message):
    await message.answer("💳 Karta ro‘yxati hozircha mavjud emas.", reply_markup=get_main_menu())
