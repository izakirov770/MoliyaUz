from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from texts import UZ

# [SUBSCRIPTION-POLLING-BEGIN]
import importlib.util
import sys
from pathlib import Path


def _ensure_inline_module() -> None:
    pkg_name = __name__ + ".inline"
    if pkg_name in sys.modules:
        return
    inline_path = Path(__file__).with_name("inline.py")
    if not inline_path.exists():
        return
    spec = importlib.util.spec_from_file_location(pkg_name, inline_path)
    if not spec or not spec.loader:  # pragma: no cover
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[pkg_name] = module


_ensure_inline_module()
# [SUBSCRIPTION-POLLING-END]

def lang_kb():
    # hozircha UZga yo'naltiramiz, lekin tugmalar ko'rinishda turadi
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇺🇿 O‘zbek", callback_data="lang:uz")]
    ])

def phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=UZ["share_phone"], request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def currency_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=UZ["btn_uzs"], callback_data="cur:UZS")],
        [InlineKeyboardButton(text=UZ["btn_usd"], callback_data="cur:USD")],
    ])

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=UZ["btn_hisobla"], callback_data="go:hisobla")],
        [InlineKeyboardButton(text=UZ["btn_balans"], callback_data="go:balans")],
        [InlineKeyboardButton(text=UZ["btn_hisobot"], callback_data="go:hisobot")],
        [InlineKeyboardButton(text=UZ["btn_obuna"],  callback_data="go:subs")],
    ])

def confirm_kb(tx_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=UZ["btn_ok"], callback_data=f"tx:{tx_id}:ok"),
            InlineKeyboardButton(text=UZ["btn_no"], callback_data=f"tx:{tx_id}:no"),
        ],
        [InlineKeyboardButton(text=UZ["btn_edit"], callback_data=f"tx:{tx_id}:edit")],
    ])

def balance_post_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=UZ["btn_show_balance"], callback_data="go:balans")]
    ])

def report_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=UZ["btn_rep_day"], callback_data="rep:day"),
            InlineKeyboardButton(text=UZ["btn_rep_week"], callback_data="rep:week"),
            InlineKeyboardButton(text=UZ["btn_rep_month"], callback_data="rep:month"),
        ]
    ])

def subs_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=UZ["btn_month"], callback_data="sub:month")],
    ])
