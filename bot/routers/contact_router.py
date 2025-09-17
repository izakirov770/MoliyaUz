import datetime
import os
import re
import sqlite3
from typing import Optional

from aiogram import F, Router
from aiogram.types import Message

from bot.keyboards import get_main_menu

try:  # Optional FSM import; safe on projects without FSM enabled
    from aiogram.fsm.context import FSMContext
except Exception:  # pragma: no cover
    FSMContext = None  # type: ignore


contact_router = Router(name="contact_router")


def _db():
    """Return shared SQLite connection honoring DB_PATH if provided."""
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))


UZ_PHONE_RE = re.compile(r"^\+?998\d{9}$")


def _ensure_user_columns(cur: sqlite3.Cursor) -> None:
    cols = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
    if "phone" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN phone TEXT")
    if "contact_verified_at" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN contact_verified_at TIMESTAMP")


def _store_phone(user_id: int, phone: str) -> None:
    conn = _db(); cur = conn.cursor()
    _ensure_user_columns(cur)
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    cur.execute(
        "UPDATE users SET phone=?, contact_verified_at=? WHERE user_id=?",
        (phone, datetime.datetime.utcnow(), user_id),
    )
    conn.commit(); conn.close()


def _clean_phone_text(text: str) -> Optional[str]:
    if not isinstance(text, str):
        return None
    stripped = re.sub(r"[\s-]", "", text)
    if UZ_PHONE_RE.match(stripped):
        if not stripped.startswith("+"):
            stripped = "+" + stripped
        return stripped
    return None


def _is_phone_text(text: str) -> bool:
    return _clean_phone_text(text) is not None


async def _finish_ok(message: Message, state: Optional["FSMContext"]) -> None:
    if state and hasattr(state, "clear"):
        try:
            await state.clear()
        except Exception:
            pass
    await message.answer(
        "📱 Raqamingiz qabul qilindi. Menyudan davom eting.",
        reply_markup=get_main_menu(),
    )


@contact_router.message(F.contact)
async def on_contact(msg: Message, state: Optional["FSMContext"] = None):
    contact = msg.contact
    if not contact or not getattr(contact, "phone_number", None):
        await msg.answer(
            "Raqamni yuborishda xatolik. '📱 Raqamni yuborish' tugmasidan foydalaning.",
            reply_markup=get_main_menu(),
        )
        return

    if getattr(contact, "user_id", None) and contact.user_id != msg.from_user.id:
        await msg.answer(
            "Faqat o‘zingizning raqamingizni yuboring.",
            reply_markup=get_main_menu(),
        )
        return

    phone = _clean_phone_text(contact.phone_number) or contact.phone_number.strip()
    _store_phone(msg.from_user.id, phone)
    await _finish_ok(msg, state)


@contact_router.message(F.text.func(_is_phone_text))
async def on_phone_text(msg: Message, state: Optional["FSMContext"] = None):
    phone = _clean_phone_text(msg.text)
    if not phone:
        return  # Guard, though predicate should filter mismatches
    _store_phone(msg.from_user.id, phone)
    await _finish_ok(msg, state)
