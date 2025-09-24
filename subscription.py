from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
import re

import aiosqlite
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo

from keyboards.inline import subscription_payment_kb, subscription_plans_kb
from payments import (
    attach_manual_request_message,
    create_manual_activation_request,
    create_polling_payment,
    get_manual_activation_request,
    get_payment_by_invoice,
    mark_polling_payment_paid,
    update_manual_request_status,
    update_user_subscription_fields,
)
from payments.click_polling import (
    PLAN_MONTH_KEY,
    PLAN_WEEK_KEY,
    build_click_pay_url,
    get_plan_amount,
)
from db import DB_PATH


subscription_router = Router()
logger = logging.getLogger(__name__)

ADMIN_IDS: set[int] = set()
admin_ids_raw = os.getenv("ADMIN_IDS", "")
for part in admin_ids_raw.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        ADMIN_IDS.add(int(part))
    except Exception:
        continue
admin_id_main = os.getenv("ADMIN_ID")
if admin_id_main:
    try:
        ADMIN_IDS.add(int(admin_id_main))
    except Exception:
        pass

REVIEW_CHAT_ID_RAW = os.getenv("SUBSCRIPTION_REVIEW_CHAT_ID", "0").strip()
try:
    REVIEW_CHAT_ID = int(REVIEW_CHAT_ID_RAW or "0")
except Exception:
    REVIEW_CHAT_ID = 0

PENDING_MANUAL_DIGITS: dict[int, dict[str, str]] = {}


def _has_pending_manual_request(message: types.Message) -> bool:
    if not isinstance(message, types.Message) or not message.from_user:
        return False
    ctx = PENDING_MANUAL_DIGITS.get(message.from_user.id)
    if not ctx:
        return False
    text = (message.text or "").strip()
    if text.lower() in {"/cancel", "cancel", "bekor"}:
        return True
    digits = re.sub(r"\D", "", text)
    return len(digits) == 4


# [SUBSCRIPTION-POLLING-BEGIN]
def _plan_label(plan_key: str) -> str:
    if plan_key == PLAN_WEEK_KEY:
        return f"1 haftalik ({_format_display_amount(get_plan_amount(plan_key))})"
    if plan_key == PLAN_MONTH_KEY:
        return f"1 oylik ({_format_display_amount(get_plan_amount(plan_key))})"
    return plan_key or "Noma'lum"


def _plan_delta(plan_key: str) -> timedelta:
    if plan_key == PLAN_WEEK_KEY:
        return timedelta(days=7)
    if plan_key == PLAN_MONTH_KEY:
        return timedelta(days=30)
    return timedelta(days=30)


def _format_display_amount(amount: str) -> str:
    try:
        value = int(round(float(amount)))
        return f"{value:,}".replace(",", " ")
    except Exception:
        return amount


async def _send_status(message: types.Message) -> None:
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sub_started_at, sub_until FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row or not row["sub_until"]:
        await message.answer("Obuna faol emas. Tarif tanlab to‘lovni amalga oshiring.")
        return
    until = datetime.fromisoformat(row["sub_until"])
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))
    until_local = until.astimezone(tz)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT plan FROM payments WHERE user_id=? AND status='paid' ORDER BY datetime(paid_at) DESC LIMIT 1",
            (user_id,),
        )
        plan_row = await cur.fetchone()
    plan_key = plan_row["plan"] if plan_row else ""
    plan_label = _plan_label(plan_key)
    status = "Faol" if until > datetime.now(timezone.utc) else "Muddati tugagan"
    await message.answer(
        f"Holat: {status}\nTarif: {plan_label}\nAmal qiladi: {until_local.strftime('%d.%m.%Y %H:%M')}"
    )


async def _create_invoice_for_plan(message: types.Message, plan_key: str) -> None:
    amount = get_plan_amount(plan_key)
    if not amount or amount in {"0", "0.00"}:
        await message.answer("Tarif narxi sozlanmagan. Administrator bilan bog‘laning.")
        return
    merchant_trans_id = uuid.uuid4().hex
    for attempt in range(3):
        try:
            await create_polling_payment(
                message.from_user.id,
                merchant_trans_id,
                amount,
                plan_key,
            )
            break
        except Exception as exc:  # pragma: no cover
            logger.warning("create-polling-payment-failed", extra={"error": str(exc)})
            merchant_trans_id = uuid.uuid4().hex
    pay_url = build_click_pay_url(merchant_trans_id, amount)
    await message.answer(
        "To‘lov uchun havola tayyor. CLICK orqali to‘lovni amalga oshiring.",
        reply_markup=subscription_payment_kb(pay_url, merchant_trans_id),
    )


async def _finalize_paid(
    user_id: int,
    plan_key: str,
    paid_at: datetime,
    merchant_trans_id: str,
    payload: dict,
) -> datetime:
    expires_at = paid_at + _plan_delta(plan_key)
    await update_user_subscription_fields(user_id, paid_at.isoformat(), expires_at.isoformat())
    await mark_polling_payment_paid(merchant_trans_id, payload, expires_at.isoformat())
    return expires_at


@subscription_router.message(Command("subscription"))
async def subscription_menu(message: types.Message):
    await message.answer(
        "⭐️ 1 oylik obunani tanlang va CLICK orqali to‘lovni amalga oshiring.\n\n"
        "To‘lov tugagach, “Obunani faollashtirish” tugmasini bosib, kartaning oxirgi 4 raqamini yuboring.\n"
        "Obuna 10 daqiqagacha faollashadi va tasdiq xabari keladi.",
        reply_markup=subscription_plans_kb(),
    )
    await _send_status(message)


@subscription_router.callback_query(F.data == "subpoll:monthly")
async def on_choose_monthly(callback: types.CallbackQuery):
    await _create_invoice_for_plan(callback.message, PLAN_MONTH_KEY)
    await callback.answer("Tarif tanlandi")


@subscription_router.callback_query(F.data.startswith("subpoll:manual"))
async def on_manual_activation_request(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    invoice_id = parts[2] if len(parts) > 2 and parts[2] else None
    if not invoice_id:
        await callback.answer("Invoice topilmadi.", show_alert=True)
        return

    if callback.from_user.id not in PENDING_MANUAL_DIGITS:
        PENDING_MANUAL_DIGITS[callback.from_user.id] = {}
    PENDING_MANUAL_DIGITS[callback.from_user.id]["invoice_id"] = invoice_id

    await callback.message.answer(
        "Iltimos, to‘lov qilingan kartaning oxirgi 4 raqamini yuboring. Masalan: 1234."
        "\nObuna 10 daqiqagacha ichida faollashadi va tasdiq xabarini olasiz."
    )
    await callback.answer("Ko‘rsatma yuborildi")


@subscription_router.message(F.text, _has_pending_manual_request)
async def on_manual_last_four(message: types.Message):
    pending = PENDING_MANUAL_DIGITS.get(message.from_user.id)
    if not pending:
        return

    text = (message.text or "").strip()
    if text.lower() in {"/cancel", "bekor", "cancel"}:
        PENDING_MANUAL_DIGITS.pop(message.from_user.id, None)
        await message.answer("So‘rov bekor qilindi.")
        return

    digits = re.sub(r"\D", "", text)
    if len(digits) != 4:
        await message.answer("Faqat kartaning oxirgi 4 raqamini yuboring. Masalan: 1234")
        return

    invoice_id = pending.get("invoice_id")
    PENDING_MANUAL_DIGITS.pop(message.from_user.id, None)

    record = await create_manual_activation_request(message.from_user.id, invoice_id, digits)
    if not record:
        await message.answer("So‘rovni saqlashda xatolik. Administrator bilan bog‘laning.")
        return

    if not REVIEW_CHAT_ID:
        await message.answer(
            "Ma’lumot qabul qilindi. Administrator so‘rovni qo‘lda ko‘rib chiqadi."
            " 10 daqiqagacha ichida obunangiz faollashgani haqida xabar beramiz."
        )
        return

    builder = InlineKeyboardBuilder()
    builder.button(
        text="Obunani faollashtirish ✅",
        callback_data=f"subpoll:approve:{record['id']}",
    )
    builder.adjust(1)

    tz = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))
    submitted_at = datetime.now(timezone.utc).astimezone(tz)
    username = f" @{message.from_user.username}" if message.from_user.username else ""
    admin_text = (
        "🆕 Yangi obuna so‘rovi\n"
        f"So‘rov ID: {record['id']}\n"
        f"Foydalanuvchi: {message.from_user.full_name}{username} (ID: {message.from_user.id})\n"
        f"Invoice: {invoice_id or '—'}\n"
        f"Karta oxirgi 4 raqami: {digits}\n"
        f"Yuborilgan: {submitted_at.strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        admin_message = await message.bot.send_message(
            REVIEW_CHAT_ID,
            admin_text,
            reply_markup=builder.as_markup(),
        )
        await attach_manual_request_message(record["id"], REVIEW_CHAT_ID, admin_message.message_id)
    except Exception as exc:
        logger.warning(
            "manual-request-dispatch-failed",
            extra={"error": str(exc), "request_id": record["id"]},
        )
        await message.answer(
            "So‘rov qabul qilindi, ammo administratorlarga xabar yuborilmadi."
            " Iltimos, qo‘lda bog‘laning."
        )
        return

    await message.answer(
        "Rahmat! Administratorlar so‘rovni ko‘rib chiqishadi."
        " 10 daqiqagacha ichida obunangiz faollashgani haqida xabar beramiz."
    )


@subscription_router.callback_query(F.data.startswith("subpoll:approve"))
async def on_manual_approve(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return

    parts = callback.data.split(":")
    try:
        request_id = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        request_id = 0
    if not request_id:
        await callback.answer("So‘rov ID noto‘g‘ri.", show_alert=True)
        return

    request = await get_manual_activation_request(request_id)
    if not request:
        await callback.answer("So‘rov topilmadi.", show_alert=True)
        return
    if (request.get("status") or "").lower() != "pending":
        await callback.answer("So‘rov allaqachon ko‘rib chiqilgan.")
        return

    user_id = request.get("user_id")
    if not user_id:
        await callback.answer("Foydalanuvchi aniqlanmadi.", show_alert=True)
        return

    invoice_id = request.get("invoice_id")
    payment_record = await get_payment_by_invoice(invoice_id) if invoice_id else None
    plan_key = (payment_record.get("plan") if payment_record else None) or PLAN_MONTH_KEY

    approved_at = datetime.now(timezone.utc)
    expires_at = approved_at + _plan_delta(plan_key)

    await update_user_subscription_fields(
        user_id,
        approved_at.isoformat(),
        expires_at.isoformat(),
    )

    payload = {
        "manual": True,
        "approved_by": callback.from_user.id,
        "approved_at": approved_at.isoformat(),
        "request_id": request_id,
        "last_four": request.get("last_four"),
    }
    try:
        if invoice_id:
            await mark_polling_payment_paid(invoice_id, payload, expires_at.isoformat())
    except Exception as exc:
        logger.warning(
            "manual-approve-payment-update-failed",
            extra={"error": str(exc), "invoice_id": invoice_id},
        )

    updated_request = await update_manual_request_status(
        request_id,
        "approved",
        approved_by=callback.from_user.id,
        approved_at_iso=approved_at.isoformat(),
    )

    tz = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))
    expires_local = expires_at.astimezone(tz)
    plan_label = _plan_label(plan_key)

    try:
        await callback.bot.send_message(
            user_id,
            "Obunangiz qo‘lda tasdiqlandi ✅\n"
            f"Tarif: {plan_label}\n"
            f"Amal qiladi: {expires_local.strftime('%d.%m.%Y %H:%M')} gacha",
        )
    except Exception as exc:
        logger.warning(
            "manual-approve-notify-failed",
            extra={"error": str(exc), "user_id": user_id},
        )

    admin_username = (
        f" @{callback.from_user.username}" if callback.from_user.username else ""
    )
    admin_summary = (
        "✅ Obuna tasdiqlandi\n"
        f"So‘rov ID: {request_id}\n"
        f"Foydalanuvchi ID: {user_id}\n"
        f"Tarif: {plan_label}\n"
        f"Amal qilish muddati: {expires_local.strftime('%d.%m.%Y %H:%M')}\n"
        f"Tasdiqladi: {callback.from_user.full_name}{admin_username} (ID: {callback.from_user.id})"
    )

    try:
        await callback.message.edit_text(admin_summary)
    except Exception:
        pass

    await callback.answer("Tasdiqlandi")

# [SUBSCRIPTION-POLLING-END]
@subscription_router.message(Command("bratula"))
async def manual_activate(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Faqat admin foydalana oladi.")
        return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer("Foydalanish: /bratula <invoice_id> [weekly|monthly]")
        return

    invoice_id = args[1].strip()
    plan_arg = args[2].strip().lower() if len(args) > 2 else ""

    record = await get_payment_by_invoice(invoice_id)
    if not record:
        await message.answer("Invoice topilmadi.")
        return

    user_id = record.get("user_id")
    if not user_id:
        await message.answer("Invoice foydalanuvchisi aniqlanmadi.")
        return

    plan_key = PLAN_MONTH_KEY

    paid_at_utc = datetime.now(timezone.utc)
    expires_at_utc = paid_at_utc + _plan_delta(plan_key)

    await update_user_subscription_fields(
        user_id,
        paid_at_utc.isoformat(),
        expires_at_utc.isoformat(),
    )
    await mark_polling_payment_paid(
        invoice_id,
        {"manual": True, "by": message.from_user.id},
        expires_at_utc.isoformat(),
    )

    tz = ZoneInfo(os.getenv("TZ", "Asia/Tashkent"))
    paid_local = paid_at_utc.astimezone(tz)
    expires_local = expires_at_utc.astimezone(tz)
    await message.answer(
        f"Obuna qo‘lda faollashtirildi. Foydalanuvchi: {user_id}\n"
        f"Tarif: {_plan_label(plan_key)}\n"
        f"Amal qiladi: {expires_local.strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        await message.bot.send_message(
            user_id,
            "Obunangiz qo‘lda faollashtirildi. Rahmat!",
        )
    except Exception:
        pass
