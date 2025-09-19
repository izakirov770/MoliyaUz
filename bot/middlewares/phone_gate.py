import os
import sqlite3
import sys
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware
from aiogram.types import Message

from bot.keyboards_phone import get_phone_keyboard

WHITELIST_TEXTS = {
    "/start",
    "/help",
    "📱 Raqamni yuborish",
    "⭐ Obuna",
    "🔄 To‘lovni tekshirish",
    "📊 Analiz",
    "💳 Kartalarim",
}


def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))


def _user_has_phone(user_id: int) -> bool:
    conn = _db(); cur = conn.cursor()
    try:
        cur.execute("SELECT phone FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    return bool(row and row[0])


def _current_step(user_id: int) -> Optional[str]:
    bot_module = sys.modules.get("bot")
    if not bot_module:
        return None
    step_store = getattr(bot_module, "STEP", None)
    if isinstance(step_store, dict):
        return step_store.get(user_id)
    return None


class PhoneGateMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user = event.from_user
        if not user:
            return await handler(event, data)

        user_id = user.id

        if event.contact is not None:
            return await handler(event, data)

        text = (event.text or "").strip()

        if _user_has_phone(user_id):
            state = data.get("state")
            if state:
                try:
                    await state.clear()
                except Exception:
                    pass
            return await handler(event, data)

        step = _current_step(user_id)
        if step and step != "need_phone":
            return await handler(event, data)

        if text in WHITELIST_TEXTS or text.lower().startswith("/start"):
            return await handler(event, data)

        try:
            await event.answer(
                "Iltimos, avval raqamingizni yuboring:",
                reply_markup=get_phone_keyboard(),
            )
        except Exception:
            pass
        return
