import os, sqlite3
from aiogram import Router, F
from aiogram.types import Message
from bot.keyboards import get_main_menu
from bot.services.http_client import ping_return

pay_debug_router = Router()

def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))

@pay_debug_router.message(F.text == "/pay_status")
async def pay_status(message: Message):
    conn=_db(); c=conn.cursor()
    rows = c.execute("""
        SELECT invoice_id, status, amount, plan, created_at
        FROM payments WHERE user_id=?
        ORDER BY created_at DESC LIMIT 5
    """, (message.from_user.id,)).fetchall()
    conn.close()
    if not rows:
        await message.answer("To‘lov topilmadi.", reply_markup=get_main_menu()); return
    text = "Oxirgi to‘lovlar:\n" + "\n".join(
        f"- {inv} | {st} | {amt} | {pl} | {created}"
        for (inv, st, amt, pl, created) in rows
    )
    await message.answer(text, reply_markup=get_main_menu())

@pay_debug_router.message(F.text == "/last_invoice")
async def last_invoice(message: Message):
    conn=_db(); c=conn.cursor()
    row = c.execute("""
        SELECT invoice_id, status FROM payments
        WHERE user_id=? ORDER BY created_at DESC LIMIT 1
    """, (message.from_user.id,)).fetchone()
    conn.close()
    if not row:
        await message.answer("Hech qanday invoice yo‘q. Obuna tugmasidan yarating.", reply_markup=get_main_menu()); return
    inv, st = row
    wb = os.getenv("WEB_BASE", "").rstrip("/")
    ret = f"{wb}/payments/return?invoice_id={inv}" if wb else "(WEB_BASE yo‘q)"
    await message.answer(f"Oxirgi invoice: {inv}\nStatus: {st}\nReturn URL: {ret}", reply_markup=get_main_menu())

@pay_debug_router.message(F.text == "/force_return")
async def force_return(message: Message):
    conn=_db(); c=conn.cursor()
    row = c.execute("""
        SELECT invoice_id FROM payments
        WHERE user_id=? ORDER BY created_at DESC LIMIT 1
    """, (message.from_user.id,)).fetchone()
    if not row:
        conn.close(); await message.answer("Invoice yo‘q. Obuna tugmasidan yarating.", reply_markup=get_main_menu()); return
    inv = row[0]
    ok, text, code = await ping_return(inv)
    # re-check after ping
    row2 = c.execute("SELECT status FROM payments WHERE invoice_id=?", (inv,)).fetchone()
    conn.close()
    status = row2[0] if row2 else "missing"
    await message.answer(f"Return ping: {code} — {'OK' if ok else 'FAIL'}\nInvoice: {inv}\nDB status: {status}\nMsg: {text[:200]}", reply_markup=get_main_menu())

# MAIN bot file: add
# from bot.routers.pay_debug import pay_debug_router
# dp.include_router(pay_debug_router)
