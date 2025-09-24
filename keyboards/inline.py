# [SUBSCRIPTION-POLLING-BEGIN]
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from payments.click_polling import PLAN_MONTH_KEY, get_plan_amount


def _fmt_label(plan_key: str, title: str) -> str:
    amount = get_plan_amount(plan_key)
    try:
        value = int(round(float(amount)))
        amount_display = f"{value:,}".replace(",", " ")
    except Exception:
        amount_display = amount
    return f"{title} ({amount_display})"


def subscription_plans_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_fmt_label(PLAN_MONTH_KEY, "1 OYLIK"), callback_data="subpoll:monthly")],
        ]
    )


def subscription_payment_kb(pay_url: str, merchant_trans_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="To‘lov", url=pay_url)],
            [
                InlineKeyboardButton(
                    text="Obunani faollashtirish",
                    callback_data=f"subpoll:manual:{merchant_trans_id}",
                )
            ],
        ]
    )


__all__ = [
    "subscription_plans_kb",
    "subscription_payment_kb",
]

# [SUBSCRIPTION-POLLING-END]
