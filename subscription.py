from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite
from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from zoneinfo import ZoneInfo

from keyboards.inline import (
    subscription_check_only_kb,
    subscription_payment_kb,
    subscription_plans_kb,
)
from payments import (
    create_polling_payment,
    get_payment_by_invoice,
    get_recent_pending_payment,
    mark_polling_payment_paid,
    update_user_subscription_fields,
)
from payments.click_polling import (
    CLICK_POLLING_ENABLED,
    PLAN_MONTH_KEY,
    PLAN_WEEK_KEY,
    build_click_pay_url,
    check_click_status,
    get_plan_amount,
)
from db import DB_PATH


subscription_router = Router()
logger = logging.getLogger(__name__)


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
    if not CLICK_POLLING_ENABLED:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="💳 Оплатить через CLICK",
            url="http://127.0.0.1:8000/clickpay/click_form.html",
        )
        await message.answer("Obuna uchun to‘lov sahifasini tanlang 👇", reply_markup=kb.as_markup())
        return
    await message.answer("Obuna tarifini tanlang:", reply_markup=subscription_plans_kb())
    await _send_status(message)


@subscription_router.callback_query(F.data == "subpoll:weekly")
async def on_choose_weekly(callback: types.CallbackQuery):
    if not CLICK_POLLING_ENABLED:
        await callback.answer()
        return
    await _create_invoice_for_plan(callback.message, PLAN_WEEK_KEY)
    await callback.answer("Tarif tanlandi")


@subscription_router.callback_query(F.data == "subpoll:monthly")
async def on_choose_monthly(callback: types.CallbackQuery):
    if not CLICK_POLLING_ENABLED:
        await callback.answer()
        return
    await _create_invoice_for_plan(callback.message, PLAN_MONTH_KEY)
    await callback.answer("Tarif tanlandi")


@subscription_router.callback_query(F.data.startswith("subpoll:check"))
async def on_check_subscription(callback: types.CallbackQuery):
    if not CLICK_POLLING_ENABLED:
        await callback.answer()
        return
    parts = callback.data.split(":")
    merchant_trans_id = parts[2] if len(parts) > 2 and parts[2] else None
    if not merchant_trans_id:
        record = await get_recent_pending_payment(callback.from_user.id)
        if not record:
            await callback.message.answer("Aktiv invoice topilmadi.")
            await callback.answer()
            return
        merchant_trans_id = record.get("invoice_id")
    attempts = 3
    delay_seconds = 2
    check_result = {}
    for attempt in range(attempts):
        check_result = await check_click_status(merchant_trans_id)
        if check_result.get("status") == "error" and check_result.get("error"):
            break
        if check_result.get("paid"):
            break
        if attempt < attempts - 1:
            await asyncio.sleep(delay_seconds)

    if check_result.get("status") == "error" and check_result.get("error"):
        await callback.message.answer("Hozir tekshirib bo‘lmadi, birozdan so‘ng urinib ko‘ring.")
        await callback.answer()
        return

    if not check_result.get("paid"):
        await callback.message.answer(
            "To‘lov topilmadi. Agar to‘lagan bo‘lsangiz, yana ‘Davom etish’ni bosing.",
            reply_markup=subscription_check_only_kb(merchant_trans_id),
        )
        await callback.answer()
        return

    data = check_result.get("payload") or {}
    status = check_result.get("status", "")
    record = await get_payment_by_invoice(merchant_trans_id)
    if not record:
        await callback.message.answer("To‘lov yozuvi topilmadi.")
        await callback.answer()
        return
    if (record.get("status") or "").lower() == "paid":
        paid_at = record.get("paid_at")
        try:
            paid_dt = datetime.fromisoformat(paid_at) if paid_at else datetime.now(timezone.utc)
        except Exception:
            paid_dt = datetime.now(timezone.utc)
        expires_at = paid_dt + _plan_delta(record.get("plan") or PLAN_MONTH_KEY)
        await callback.message.answer(
            "Obuna allaqachon faollashtirilgan ✅\n"
            f"Tarif: {_plan_label(record.get('plan'))}\n"
            f"Amal qiladi: {expires_at.strftime('%d.%m.%Y %H:%M')}"
        )
        await callback.answer()
        return
    stored_amount = f"{float(record.get('amount', 0)):.2f}"
    remote_amount = data.get("amount") or data.get("amount_sum")
    if remote_amount is not None:
        try:
            remote_amount = f"{float(remote_amount):.2f}"
        except Exception:
            remote_amount = str(remote_amount)
        if remote_amount != stored_amount:
            await callback.message.answer("Summalarda farq bor. Administrator bilan bog‘laning.")
            await callback.answer()
            return
    paid_at_raw = data.get("perform_time") or data.get("payment_time") or data.get("paid_time")
    try:
        paid_at = datetime.fromisoformat(paid_at_raw)
    except Exception:
        paid_at = datetime.now(timezone.utc)
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    plan_key = record.get("plan") or PLAN_MONTH_KEY
    expires_at = await _finalize_paid(
        callback.from_user.id,
        plan_key,
        paid_at,
        merchant_trans_id,
        {
            "status_code": check_result.get("status_code"),
            "status": check_result.get("status"),
            "payload": data,
        },
    )
    await callback.message.answer(
        "Obuna faollashdi ✅\n"
        f"Tarif: {_plan_label(plan_key)}\n"
        f"Amal qiladi: {expires_at.strftime('%d.%m.%Y %H:%M')}"
    )
    await callback.answer("Tasdiqlandi")

# [SUBSCRIPTION-POLLING-END]
