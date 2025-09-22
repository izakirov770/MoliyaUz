# [SUBSCRIPTION-POLLING-BEGIN]
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def subscription_plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1 HAFTALIK", callback_data="subpoll:weekly")],
            [InlineKeyboardButton(text="1 OYLIK", callback_data="subpoll:monthly")],
        ]
    )


def subscription_payment_kb(pay_url: str, merchant_trans_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="To‘lov", url=pay_url)],
            [InlineKeyboardButton(text="Davom etish (to‘lovni tekshirish)", callback_data=f"subpoll:check:{merchant_trans_id}")],
        ]
    )


def subscription_check_only_kb(merchant_trans_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Davom etish (to‘lovni tekshirish)", callback_data=f"subpoll:check:{merchant_trans_id}")],
        ]
    )


__all__ = [
    "subscription_plans_kb",
    "subscription_payment_kb",
    "subscription_check_only_kb",
]

# [SUBSCRIPTION-POLLING-END]
