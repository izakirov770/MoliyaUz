import os, sqlite3
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from bot.keyboards import get_main_menu
from bot.services.payments import create_invoice
from bot.services.http_client import ping_return
from bot.services.activate import activate_invoice

sub_router = Router()

def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))

MONTH_PRICE = int(os.getenv("MONTH_PRICE", 19900))
WEEK_PRICE = int(os.getenv("WEEK_PRICE", 7900))

async def show_subscription_plans(message: Message):
    inv_m, url_m = create_invoice(message.from_user.id, MONTH_PRICE, "month")
    inv_w, url_w = create_invoice(message.from_user.id, WEEK_PRICE, "week")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 1 oylik — 19 900 so'm", url=url_m)],
        [InlineKeyboardButton(text="🧪 1 haftalik — 7 900 so'm", url=url_w)],
        [InlineKeyboardButton(text="🔄 To‘lovni tekshirish", callback_data="pay_check")]
    ])
    await message.answer("Obuna turini tanlang:", reply_markup=kb)

@sub_router.callback_query(F.data == "pay_check")
async def pay_check(call: CallbackQuery):
    conn=_db(); c=conn.cursor()
    row = c.execute("""
        SELECT invoice_id, status, amount, plan, created_at
        FROM payments WHERE user_id=?
        ORDER BY created_at DESC LIMIT 1
    """, (call.from_user.id,)).fetchone()

    if not row:
        conn.close()
        await call.message.answer("Avval tarif tanlang va to‘lovni boshlang.", reply_markup=get_main_menu())
        await call.answer(); return

    invoice_id, status, amount, plan, _ = row

    # Already active
    if status == "paid":
        conn.close()
        await call.message.answer("Obuna faollashgan ✅", reply_markup=get_main_menu())
        await call.answer(); return

    # Fallback: call return endpoint ourselves
    ok, _msg, code = await ping_return(invoice_id)

    # Re-check in DB
    row2 = c.execute("SELECT status FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    if row2 and row2[0] == "paid":
        conn.close()
        await call.message.answer("Obuna faollashgan ✅", reply_markup=get_main_menu())
        await call.answer(); return

    # Optional manual confirm via ENV switch (safety off by default)
    if os.getenv("ALLOW_MANUAL_CONFIRM","").lower() == "true":
        if activate_invoice(invoice_id):
            conn.close()
            await call.message.answer("Obuna faollashgan ✅", reply_markup=get_main_menu())
            await call.answer(); return

    conn.close()
    text = "To‘lov hali tasdiqlanmagan."
    if code == 404:
        text += " (Invoice topilmadi — Obuna tugmasidan qayta to‘lov yarating.)"
    elif code == 0:
        text += " (Serverga ulanishda xatolik. WEB_BASE/RETURN_URL va domenni tekshiring.)"
    await call.message.answer(text, reply_markup=get_main_menu())
    await call.answer()
