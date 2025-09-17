from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def get_phone_keyboard() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.add(KeyboardButton(text="📱 Raqamni yuborish", request_contact=True))
    return kb
