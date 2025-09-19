import datetime
import os
import re
import sqlite3
import sys
from typing import Optional

from aiogram import F, Router
from aiogram.types import Message, ReplyKeyboardRemove

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


def _resolve_bot_context() -> dict:
    bot_module = sys.modules.get("bot") or sys.modules.get("__main__")
    if not bot_module:
        return {}
    return {
        "STEP": getattr(bot_module, "STEP", None),
        "get_lang": getattr(bot_module, "get_lang", None),
        "L": getattr(bot_module, "L", None),
        "menu": getattr(bot_module, "get_main_menu", None),
    }


async def _finish_ok(message: Message, state: Optional["FSMContext"]) -> None: # type: ignore
    if state and hasattr(state, "clear"):
        try:
            await state.clear()
        except Exception:  # pragma: no cover
            pass

    ctx = _resolve_bot_context()
    step_store = ctx.get("STEP")
    get_lang = ctx.get("get_lang")
    lang = get_lang(message.from_user.id) if callable(get_lang) else "uz"
    keyboard_factory = ctx.get("menu")
    menu_markup = keyboard_factory(lang) if callable(keyboard_factory) else get_main_menu()

    if isinstance(step_store, dict):
        step_store[message.from_user.id] = "main"

    translator_factory = ctx.get("L")
    if callable(translator_factory):
        translator = translator_factory(lang)
        menu_text = translator("menu")
    else:
        menu_text = "Asosiy menyu:"

    await message.answer(
        "📱 Raqamingiz qabul qilindi. Menyudan davom eting.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.answer(menu_text, reply_markup=menu_markup)


@contact_router.message(F.contact)
async def on_contact(msg: Message, state: Optional["FSMContext"] = None): # type: ignore
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
async def on_phone_text(msg: Message, state: Optional["FSMContext"] = None): # type: ignore
    phone = _clean_phone_text(msg.text)
    if not phone:
        return  # Guard, though predicate should filter mismatches
    _store_phone(msg.from_user.id, phone)
    await _finish_ok(msg, state)
