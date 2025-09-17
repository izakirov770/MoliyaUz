from aiogram import Router, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

subscription_router = Router()

@subscription_router.message(Command("subscription"))
async def subscription_menu(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(
        text="💳 Оплатить через CLICK",
        url="http://127.0.0.1:8000/clickpay/click_form.html"  # Lokalda test uchun
    )
    await message.answer("Obuna uchun to‘lov sahifasini tanlang 👇", reply_markup=kb.as_markup())
