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

LANG_DEFAULT = "uz"
_LANG_CACHE: dict[int, str] = {}

LANG_TEXTS: dict[str, dict[str, str]] = {
    "status_inactive": {
        "uz": "Obuna faol emas. Tarif tanlab to‘lovni amalga oshiring.",
        "ru": "Подписка не активна. Выберите тариф и оформите оплату.",
    },
    "status_active": {
        "uz": "Faol",
        "ru": "Активна",
    },
    "status_expired": {
        "uz": "Muddati tugagan",
        "ru": "Истёк",
    },
    "status_summary": {
        "uz": "Holat: {status}\nTarif: {plan}\nAmal qiladi: {until}",
        "ru": "Статус: {status}\nТариф: {plan}\nДействует до: {until}",
    },
    "price_missing": {
        "uz": "Tarif narxi sozlanmagan. Administrator bilan bog‘laning.",
        "ru": "Стоимость тарифа не настроена. Свяжитесь с администратором.",
    },
    "payment_link": {
        "uz": "To‘lov uchun havola tayyor. CLICK orqali to‘lovni amalga oshiring.",
        "ru": "Ссылка для оплаты готова. Оплатите через CLICK.",
    },
    "menu_intro": {
        "uz": (
            "⭐️ 1 oylik obunani tanlang va CLICK orqali to‘lovni amalga oshiring.\n\n"
            "To‘lov tugagach, “Obunani faollashtirish” tugmasini bosib, kartaning oxirgi 4 raqamini yuboring.\n"
            "Obuna 10 daqiqagacha faollashadi va tasdiq xabari keladi."
        ),
        "ru": (
            "⭐️ Выберите подписку на 1 месяц и оплатите её через CLICK.\n\n"
            "После оплаты нажмите «Активировать подписку» и отправьте последние 4 цифры карты.\n"
            "Подписка активируется до 10 минут, мы отправим подтверждение."
        ),
    },
    "plan_chosen": {
        "uz": "Tarif tanlandi",
        "ru": "Тариф выбран",
    },
    "invoice_missing": {
        "uz": "Invoice topilmadi.",
        "ru": "Счёт не найден.",
    },
    "manual_prompt": {
        "uz": (
            "Iltimos, to‘lov qilingan kartaning oxirgi 4 raqamini yuboring. Masalan: 1234.\n"
            "Obuna 10 daqiqagacha ichida faollashadi va tasdiq xabarini olasiz."
        ),
        "ru": (
            "Отправьте последние 4 цифры карты, с которой оплачивали. Например: 1234.\n"
            "Подписка активируется до 10 минут, мы пришлём подтверждение."
        ),
    },
    "instruction_sent": {
        "uz": "Ko‘rsatma yuborildi",
        "ru": "Инструкция отправлена",
    },
    "manual_cancelled": {
        "uz": "So‘rov bekor qilindi.",
        "ru": "Заявка отменена.",
    },
    "manual_digits_invalid": {
        "uz": "Faqat kartaning oxirgi 4 raqamini yuboring. Masalan: 1234",
        "ru": "Отправьте только последние 4 цифры карты. Например: 1234",
    },
    "manual_save_error": {
        "uz": "So‘rovni saqlashda xatolik. Administrator bilan bog‘laning.",
        "ru": "Не удалось сохранить заявку. Свяжитесь с администратором.",
    },
    "manual_ack_no_review": {
        "uz": "Ma’lumot qabul qilindi. 10 daqiqagacha ichida obunangiz faollashgani haqida xabar beramiz.",
        "ru": "Мы получили данные. Сообщим об активации подписки в течение 10 минут.",
    },
    "manual_ack": {
        "uz": "Rahmat! 10 daqiqagacha ichida obunangiz faollashgani haqida xabar beramiz.",
        "ru": "Спасибо! Сообщим об активации подписки в течение 10 минут.",
    },
    "access_denied": {
        "uz": "Ruxsat yo‘q",
        "ru": "Нет доступа",
    },
    "invalid_request_id": {
        "uz": "So‘rov ID noto‘g‘ri.",
        "ru": "Неверный ID заявки.",
    },
    "request_not_found": {
        "uz": "So‘rov topilmadi.",
        "ru": "Заявка не найдена.",
    },
    "request_processed": {
        "uz": "So‘rov allaqachon ko‘rib chiqilgan.",
        "ru": "Заявка уже обработана.",
    },
    "user_not_found": {
        "uz": "Foydalanuvchi aniqlanmadi.",
        "ru": "Пользователь не найден.",
    },
    "request_rejected_user": {
        "uz": "Obuna so‘rovi bekor qilindi. Agar xatolik bo‘lsa, administrator bilan bog‘laning.",
        "ru": "Заявка на подписку отклонена. Если это ошибка, свяжитесь с администратором.",
    },
    "request_approved_user": {
        "uz": "Obunangiz tasdiqlandi ✅\nAmal qiladi: {expires} gacha",
        "ru": "Ваша подписка подтверждена ✅\nДействует до: {expires}",
    },
    "request_rejected_admin": {
        "uz": "❌ Obuna so‘rovi bekor qilindi",
        "ru": "❌ Заявка на подписку отклонена",
    },
    "request_approved_admin": {
        "uz": "✅ Obuna tasdiqlandi",
        "ru": "✅ Подписка подтверждена",
    },
    "request_done": {
        "uz": "Tasdiqlandi",
        "ru": "Подтверждено",
    },
    "request_cancelled": {
        "uz": "Bekor qilindi",
        "ru": "Отменено",
    },
    "manual_cmd_only_admin": {
        "uz": "Faqat admin foydalana oladi.",
        "ru": "Команда доступна только администратору.",
    },
    "manual_cmd_usage": {
        "uz": "Foydalanish: /bratula <invoice_id> [weekly|monthly]",
        "ru": "Использование: /bratula <invoice_id> [weekly|monthly]",
    },
    "invoice_user_missing": {
        "uz": "Invoice foydalanuvchisi aniqlanmadi.",
        "ru": "Не удалось определить пользователя по счёту.",
    },
    "manual_cmd_success": {
        "uz": "Obuna qo‘lda faollashtirildi. Foydalanuvchi: {user_id}\nTarif: {plan}\nAmal qiladi: {until}",
        "ru": "Подписка активирована вручную. Пользователь: {user_id}\nТариф: {plan}\nДействует до: {until}",
    },
}


def _normalize_lang(value: str | None) -> str:
    if not value:
        return LANG_DEFAULT
    value = value.lower()
    if value.startswith("ru"):
        return "ru"
    return "uz"


async def _get_user_lang(user_id: int, fallback: str | None = None) -> str:
    cached = _LANG_CACHE.get(user_id)
    if cached:
        return cached
    lang_value: str | None = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
        if row:
            if isinstance(row, (list, tuple)):
                lang_value = _normalize_lang(row[0])
            else:
                lang_value = _normalize_lang(row["lang"])
    except Exception as exc:  # pragma: no cover
        logger.warning("user-lang-fetch-failed", extra={"uid": user_id, "error": str(exc)})
    if not lang_value and fallback:
        lang_value = _normalize_lang(fallback)
    if not lang_value:
        lang_value = LANG_DEFAULT
    _LANG_CACHE[user_id] = lang_value
    return lang_value


def _t(key: str, lang: str, **kwargs) -> str:
    template = LANG_TEXTS.get(key, {}).get(lang) or LANG_TEXTS.get(key, {}).get(LANG_DEFAULT) or ""
    return template.format(**kwargs)


async def _is_authorized_admin(callback: types.CallbackQuery) -> bool:
    user_id = callback.from_user.id if callback.from_user else 0
    if user_id in ADMIN_IDS:
        return True
    chat = callback.message.chat if callback.message else None
    if chat and chat.id == REVIEW_CHAT_ID:
        try:
            member = await callback.bot.get_chat_member(chat.id, user_id)
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "manual-approve-admin-check-failed",
                extra={"error": str(exc), "user_id": user_id},
            )
            return False
        status = getattr(member, "status", "")
        if status in {"administrator", "creator"}:
            return True
    return False


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
def _plan_label(plan_key: str, lang: str = LANG_DEFAULT) -> str:
    amount_display = _format_display_amount(get_plan_amount(plan_key))
    if plan_key == PLAN_WEEK_KEY:
        return (
            f"1 haftalik ({amount_display})" if lang == "uz" else f"1 неделя ({amount_display})"
        )
    if plan_key == PLAN_MONTH_KEY:
        return (
            f"1 oylik ({amount_display})" if lang == "uz" else f"1 месяц ({amount_display})"
        )
    return plan_key or ("Noma'lum" if lang == "uz" else "Неизвестно")


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
    lang = await _get_user_lang(user_id, getattr(message.from_user, "language_code", None))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT sub_started_at, sub_until FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row or not row["sub_until"]:
        await message.answer(_t("status_inactive", lang))
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
    plan_label = _plan_label(plan_key, lang)
    status_key = "status_active" if until > datetime.now(timezone.utc) else "status_expired"
    status_label = _t(status_key, lang)
    await message.answer(
        _t(
            "status_summary",
            lang,
            status=status_label,
            plan=plan_label,
            until=until_local.strftime("%d.%m.%Y"),
        )
    )


async def _create_invoice_for_plan(message: types.Message, plan_key: str) -> None:
    amount = get_plan_amount(plan_key)
    lang = await _get_user_lang(message.from_user.id, getattr(message.from_user, "language_code", None))
    if not amount or amount in {"0", "0.00"}:
        await message.answer(_t("price_missing", lang))
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
        _t("payment_link", lang),
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
    lang = await _get_user_lang(message.from_user.id, getattr(message.from_user, "language_code", None))
    await message.answer(_t("menu_intro", lang), reply_markup=subscription_plans_kb())
    await _send_status(message)


@subscription_router.callback_query(F.data == "subpoll:monthly")
async def on_choose_monthly(callback: types.CallbackQuery):
    await _create_invoice_for_plan(callback.message, PLAN_MONTH_KEY)
    lang = await _get_user_lang(callback.from_user.id, getattr(callback.from_user, "language_code", None))
    await callback.answer(_t("plan_chosen", lang))


@subscription_router.callback_query(F.data.startswith("subpoll:manual"))
async def on_manual_activation_request(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    invoice_id = parts[2] if len(parts) > 2 and parts[2] else None
    if not invoice_id:
        lang = await _get_user_lang(callback.from_user.id, getattr(callback.from_user, "language_code", None))
        await callback.answer(_t("invoice_missing", lang), show_alert=True)
        return

    if callback.from_user.id not in PENDING_MANUAL_DIGITS:
        PENDING_MANUAL_DIGITS[callback.from_user.id] = {}
    PENDING_MANUAL_DIGITS[callback.from_user.id]["invoice_id"] = invoice_id

    lang = await _get_user_lang(callback.from_user.id, getattr(callback.from_user, "language_code", None))
    await callback.message.answer(_t("manual_prompt", lang))
    await callback.answer(_t("instruction_sent", lang))


@subscription_router.message(F.text, _has_pending_manual_request)
async def on_manual_last_four(message: types.Message):
    pending = PENDING_MANUAL_DIGITS.get(message.from_user.id)
    if not pending:
        return

    text = (message.text or "").strip()
    lang = await _get_user_lang(message.from_user.id, getattr(message.from_user, "language_code", None))
    lowered = text.casefold()
    if lowered in {"ortga", "назад", "nazad", "back"}:
        PENDING_MANUAL_DIGITS.pop(message.from_user.id, None)
        await subscription_menu(message)
        return
    if lowered in {"/cancel", "bekor", "cancel"}:
        PENDING_MANUAL_DIGITS.pop(message.from_user.id, None)
        await message.answer(_t("manual_cancelled", lang))
        return

    digits = re.sub(r"\D", "", text)
    if len(digits) != 4:
        await message.answer(_t("manual_digits_invalid", lang))
        return

    invoice_id = pending.get("invoice_id")
    PENDING_MANUAL_DIGITS.pop(message.from_user.id, None)

    record = await create_manual_activation_request(message.from_user.id, invoice_id, digits)
    if not record:
        await message.answer(_t("manual_save_error", lang))
        return

    if not REVIEW_CHAT_ID:
        await message.answer(_t("manual_ack_no_review", lang))
        return

    builder = InlineKeyboardBuilder()
    builder.button(
        text="Obunani faollashtirish ✅",
        callback_data=f"subpoll:approve:{record['id']}",
    )
    builder.button(
        text="Bekor qilish ❌",
        callback_data=f"subpoll:reject:{record['id']}",
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

    await message.answer(_t("manual_ack", lang))


@subscription_router.callback_query(F.data.startswith("subpoll:approve"))
async def on_manual_approve(callback: types.CallbackQuery):
    admin_lang = await _get_user_lang(callback.from_user.id, getattr(callback.from_user, "language_code", None))
    if not await _is_authorized_admin(callback):
        await callback.answer(_t("access_denied", admin_lang), show_alert=True)
        return

    parts = callback.data.split(":")
    try:
        request_id = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        request_id = 0
    if not request_id:
        await callback.answer(_t("invalid_request_id", admin_lang), show_alert=True)
        return

    request = await get_manual_activation_request(request_id)
    if not request:
        await callback.answer(_t("request_not_found", admin_lang), show_alert=True)
        return
    if (request.get("status") or "").lower() != "pending":
        await callback.answer(_t("request_processed", admin_lang))
        return

    user_id = request.get("user_id")
    if not user_id:
        await callback.answer(_t("user_not_found", admin_lang), show_alert=True)
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

    expires_str = expires_local.strftime('%d.%m.%Y')

    user_lang = await _get_user_lang(user_id)
    try:
        await callback.bot.send_message(
            user_id,
            _t("request_approved_user", user_lang, expires=expires_str),
        )
    except Exception as exc:
        logger.warning(
            "manual-approve-notify-failed",
            extra={"error": str(exc), "user_id": user_id},
        )

    admin_username = (
        f" @{callback.from_user.username}" if callback.from_user.username else ""
    )
    user_display = ""
    user_username = ""
    try:
        chat = await callback.bot.get_chat(user_id)
        user_display = chat.full_name
        if getattr(chat, "username", None):
            user_username = f" @{chat.username}"
    except Exception:
        user_display = ""
    user_display = user_display or str(user_id)
    card_last_four = request.get("last_four") or "—"
    invoice_line = f"Invoice: {invoice_id or '—'}\n"
    admin_summary = (
        f"{_t('request_approved_admin', 'uz')}\n"
        f"So‘rov ID: {request_id}\n"
        f"Foydalanuvchi: {user_display}{user_username} (ID: {user_id})\n"
        f"Karta oxirgi 4 raqami: {card_last_four}\n"
        f"{invoice_line}"
        f"Tarif: {plan_label}\n"
        f"Amal qiladi: {expires_str}\n"
        f"Tasdiqladi: {callback.from_user.full_name}{admin_username} (ID: {callback.from_user.id})"
    )

    try:
        await callback.message.edit_text(admin_summary)
    except Exception:
        pass

    await callback.answer(_t("request_done", admin_lang))


@subscription_router.callback_query(F.data.startswith("subpoll:reject"))
async def on_manual_reject(callback: types.CallbackQuery):
    admin_lang = await _get_user_lang(callback.from_user.id, getattr(callback.from_user, "language_code", None))
    if not await _is_authorized_admin(callback):
        await callback.answer(_t("access_denied", admin_lang), show_alert=True)
        return

    parts = callback.data.split(":")
    try:
        request_id = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        request_id = 0
    if not request_id:
        await callback.answer(_t("invalid_request_id", admin_lang), show_alert=True)
        return

    request = await get_manual_activation_request(request_id)
    if not request:
        await callback.answer(_t("request_not_found", admin_lang), show_alert=True)
        return
    if (request.get("status") or "").lower() != "pending":
        await callback.answer(_t("request_processed", admin_lang))
        return

    user_id = request.get("user_id")
    if not user_id:
        await callback.answer(_t("user_not_found", admin_lang), show_alert=True)
        return

    rejected_at = datetime.now(timezone.utc)
    updated_request = await update_manual_request_status(
        request_id,
        "rejected",
        approved_by=callback.from_user.id,
        approved_at_iso=rejected_at.isoformat(),
    )

    try:
        await callback.bot.send_message(
            user_id,
            _t("request_rejected_user", await _get_user_lang(user_id)),
        )
    except Exception as exc:
        logger.warning(
            "manual-reject-notify-failed",
            extra={"error": str(exc), "user_id": user_id},
        )

    admin_username = (
        f" @{callback.from_user.username}" if callback.from_user.username else ""
    )
    user_display = ""
    user_username = ""
    try:
        chat = await callback.bot.get_chat(user_id)
        user_display = chat.full_name
        if getattr(chat, "username", None):
            user_username = f" @{chat.username}"
    except Exception:
        user_display = ""
    user_display = user_display or str(user_id)
    card_last_four = request.get("last_four") or "—"
    invoice_id = request.get("invoice_id")
    invoice_line = f"Invoice: {invoice_id or '—'}\n"
    admin_summary = (
        f"{_t('request_rejected_admin', 'uz')}\n"
        f"So‘rov ID: {request_id}\n"
        f"Foydalanuvchi: {user_display}{user_username} (ID: {user_id})\n"
        f"Karta oxirgi 4 raqami: {card_last_four}\n"
        f"{invoice_line}"
        f"Bekor qildi: {callback.from_user.full_name}{admin_username} (ID: {callback.from_user.id})"
    )

    try:
        await callback.message.edit_text(admin_summary)
    except Exception:
        pass

    await callback.answer(_t("request_cancelled", admin_lang))

# [SUBSCRIPTION-POLLING-END]
@subscription_router.message(Command("bratula"))
async def manual_activate(message: types.Message):
    lang = await _get_user_lang(message.from_user.id, getattr(message.from_user, "language_code", None))
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(_t("manual_cmd_only_admin", lang))
        return

    args = (message.text or "").split(maxsplit=2)
    if len(args) < 2:
        await message.answer(_t("manual_cmd_usage", lang))
        return

    invoice_id = args[1].strip()
    plan_arg = args[2].strip().lower() if len(args) > 2 else ""

    record = await get_payment_by_invoice(invoice_id)
    if not record:
        await message.answer(_t("invoice_missing", lang))
        return

    user_id = record.get("user_id")
    if not user_id:
        await message.answer(_t("invoice_user_missing", lang))
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
        _t(
            "manual_cmd_success",
            lang,
            user_id=user_id,
            plan=_plan_label(plan_key, lang),
            until=expires_local.strftime("%d.%m.%Y"),
        )
    )

    try:
        await message.bot.send_message(
            user_id,
            _t("request_approved_user", await _get_user_lang(user_id), expires=expires_local.strftime("%d.%m.%Y")),
        )
    except Exception:
        pass
