import os, sqlite3, datetime
from aiogram import Router, F
from aiogram.types import Message
from bot.keyboards import get_main_menu

# FSM bo'lsa holatni tozalash uchun (yo'q bo'lsa ham xato bermaydi)
try:
    from aiogram.fsm.context import FSMContext
except Exception:  # pragma: no cover - older aiogram
    FSMContext = None  # fallback

contact_router = Router(name="contact_router")

def _db():
    # Barcha joyda bir xil DB bo'lsin (Volume bo'lsa DB_PATH dan)
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))

@contact_router.message(F.contact)
async def on_contact(msg: Message, state: "FSMContext|None" = None):
    # Foydalanuvchi o'z raqamini yuborganini tekshiramiz
    c = msg.contact
    if not c or not c.phone_number:
        await msg.answer(
            "Raqamni yuborishda xatolik. Iltimos, '📱 Raqamni yuborish' tugmasidan foydalaning.",
            reply_markup=get_main_menu(),
        )
        return

    # Begona kontakt emasligini tekshiramiz
    if getattr(c, "user_id", None) and c.user_id != msg.from_user.id:
        await msg.answer(
            "Iltimos, faqat o‘zingizning raqamingizni '📱 Raqamni yuborish' tugmasi bilan yuboring.",
            reply_markup=get_main_menu(),
        )
        return

    phone = c.phone_number.strip()
    now = datetime.datetime.utcnow()

    conn = _db(); cur = conn.cursor()
    # Idempotent ustunlar
    cur.execute("PRAGMA journal_mode=WAL")
    cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "phone" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "contact_verified_at" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN contact_verified_at TIMESTAMP")

    # Foydalanuvchi qatori bo'lmasa, yaratib qo'yamiz
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (msg.from_user.id,))
    # Ma'lumotni yangilaymiz
    cur.execute(
        "UPDATE users SET phone=?, contact_verified_at=? WHERE user_id=?",
        (phone, now, msg.from_user.id),
    )
    conn.commit(); conn.close()

    # FSM bo'lsa holatni tozalaymiz (keyingi handlerlar ishlashi uchun)
    if state and hasattr(state, "clear"):
        try:
            await state.clear()
        except Exception:
            pass

    # Asosiy menyuga qaytaramiz (📊 Analiz | 💳 Kartalarim qatorda turadi)
    await msg.answer(
        "📱 Raqamingiz qabul qilindi. Endi menyudan davom etishingiz mumkin.",
        reply_markup=get_main_menu(),
    )
