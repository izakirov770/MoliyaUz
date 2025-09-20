import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import aiosqlite
from aiogram import Bot
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, quote

from payments import (
    ensure_schema as ensure_payment_schema,
    get_payment_by_invoice,
    log_callback,
    mark_payment_paid,
)
from db import DB_PATH

load_dotenv()

CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
WEB_BASE = os.getenv("WEB_BASE", "")

print("MINI_APP_URL_FOR_BOTFATHER:", f"{WEB_BASE}/clickpay/pay", file=sys.stderr)

try:
    LOCAL_TZ = ZoneInfo(TZ_NAME)
except Exception:
    LOCAL_TZ = ZoneInfo("Asia/Tashkent")

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None

SUCCESS_PAGE = """<!doctype html><html lang=\"uz\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>To'lov tasdiqlandi</title><style>body{font-family:'Segoe UI',Arial,sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:16px;}main{max-width:360px;text-align:center;background:#111c34;border-radius:18px;padding:32px 28px;box-shadow:0 20px 45px rgba(15,23,42,.45);}h1{font-size:24px;margin-bottom:12px;}p{margin:0 0 18px;line-height:1.5;color:#cbd5f5;}a.button{display:inline-flex;align-items:center;justify-content:center;padding:12px 20px;border-radius:999px;background:#38bdf8;color:#0f172a;text-decoration:none;font-weight:600;}a.button:hover{background:#0ea5e9;}small{display:block;margin-top:18px;font-size:12px;color:#64748b;}svg{width:60px;height:60px;fill:none;stroke:#38bdf8;stroke-width:1.8;margin-bottom:16px;}</style></head><body><main><svg viewBox=\"0 0 24 24\"><circle cx=\"12\" cy=\"12\" r=\"9\" stroke=\"rgba(56,189,248,0.35)\" stroke-width=\"2\"/><path d=\"M8.5 12.5l2.3 2.4 4.7-5.4\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/></svg><h1>To'lov tasdiqlandi ✅</h1><p>Telegram botga qayting va ilovadagi oynani yoping. Obuna holatini bot avtomatik yangiladi.</p>__BUTTON__<small>Agar Telegram o'zi yopilmasa, ushbu oynani yopib botga qayting.</small></main><script>const target='__REDIRECT__';try{if(window.Telegram&&window.Telegram.WebApp){window.Telegram.WebApp.close();}}catch(e){}if(target){setTimeout(()=>{window.location.href=target;},1200);}</script></body></html>"""

ERROR_PAGE = """<!doctype html><html lang=\"uz\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>To'lov topilmadi</title><style>body{font-family:'Segoe UI',Arial,sans-serif;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:16px;}main{max-width:360px;text-align:center;background:#111c34;border-radius:18px;padding:32px 28px;box-shadow:0 20px 45px rgba(15,23,42,.45);}h1{font-size:24px;margin-bottom:12px;}p{margin:0;line-height:1.5;color:#cbd5f5;}</style></head><body><main><h1>To'lov topilmadi 😕</h1><p>Invoice ID noto'g'ri yoki allaqachon tasdiqlangan. Telegramga qaytib, botdan so'rov yuboring.</p></main></body></html>"""

app = FastAPI()

# clickpay papkani ulaymiz
app.mount("/clickpay", StaticFiles(directory="clickpay"), name="clickpay")


async def _ensure_user_columns_async() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cur.fetchall()}
        statements = []
        if "sub_started_at" not in cols:
            statements.append("ALTER TABLE users ADD COLUMN sub_started_at TIMESTAMP")
        if "sub_until" not in cols:
            statements.append("ALTER TABLE users ADD COLUMN sub_until TIMESTAMP")
        if "sub_reminder_sent" not in cols and "sub_reminder_sent_date" not in cols:
            statements.append("ALTER TABLE users ADD COLUMN sub_reminder_sent INTEGER DEFAULT 0")
        for stmt in statements:
            try:
                await db.execute(stmt)
            except Exception:
                pass
        if statements:
            await db.commit()


@app.get("/clickpay/pay")
def clickpay_pay(
    amount: int = Query(..., ge=1),
    invoice_id: str = Query(..., min_length=3),
    card_type: Optional[str] = Query(None)
):
    base = os.getenv("WEB_BASE", "") or ""
    base_clean = base.rstrip("/")
    return_target = f"{base_clean}/payments/return?invoice_id={quote(invoice_id, safe='')}"
    params = {
        "service_id": os.getenv("CLICK_SERVICE_ID", ""),
        "merchant_id": os.getenv("CLICK_MERCHANT_ID", ""),
        "amount": str(int(amount)),
        "transaction_param": invoice_id,
        "return_url": return_target,
    }
    merchant_user = os.getenv("CLICK_MERCHANT_USER_ID")
    if merchant_user:
        params["merchant_user_id"] = merchant_user
    if card_type in ("uzcard", "humo"):
        params["card_type"] = card_type
    url = "https://my.click.uz/services/pay?" + urlencode(params, safe="")
    return RedirectResponse(url, status_code=302)


@app.get("/payments/return")
async def payments_return(invoice_id: str):
    try:
        await ensure_payment_schema()
        await _ensure_user_columns_async()
        record = await mark_payment_paid(invoice_id)
    except Exception:
        record = None

    ok = bool(record and record.get("status") == "paid")
    if not ok:
        return HTMLResponse(ERROR_PAGE, status_code=404)

    bot_username = (os.getenv("BOT_USERNAME", "") or "").lstrip("@")
    button_html = ""
    redirect_url = ""
    if bot_username:
        bot_link = f"https://t.me/{bot_username}"
        button_html = (
            f"<a class=\"button\" href=\"{bot_link}\" target=\"_blank\">Botga qaytish</a>"
        )
        redirect_url = bot_link

    html = SUCCESS_PAGE.replace("__BUTTON__", button_html)
    html = html.replace("__REDIRECT__", redirect_url)
    return HTMLResponse(html)


async def _read_payload(request: Request) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if request.method == "POST":
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            data = await request.json()
            if isinstance(data, dict):
                payload = data
        else:
            form = await request.form()
            payload = {k: v for k, v in form.multi_items()}
    if not payload:
        payload = dict(request.query_params)
    return payload


def _parse_iso(dt_str: str) -> datetime:
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    else:
        dt = dt.astimezone(LOCAL_TZ)
    return dt


async def _get_user_lang(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return "uz"
        return (row["lang"] or "uz")


def _render_sub_ok(lang: str, start: str, end: str) -> str:
    if lang == "ru":
        return f"Подписка на 1 месяц активирована: {start} → {end}"
    return f"1 oylik obuna faollashdi: {start} → {end}"


async def _notify_subscription(user_id: int, start_iso: str, end_iso: str) -> None:
    if not bot:
        return
    lang = await _get_user_lang(user_id)
    start_dt = _parse_iso(start_iso)
    end_dt = _parse_iso(end_iso)
    text = _render_sub_ok(lang, start_dt.strftime("%d.%m.%Y"), end_dt.strftime("%d.%m.%Y"))
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass


@app.api_route("/payments/callback", methods=["GET", "POST"])
async def payments_callback(request: Request) -> Response:
    await ensure_payment_schema()
    payload = await _read_payload(request)
    invoice_id = payload.get("transaction_param") or payload.get("invoice_id")
    await log_callback("callback", payload, False)
    if not invoice_id:
        return Response("ERROR", media_type="text/plain", status_code=400)
    record = await get_payment_by_invoice(invoice_id)
    if not record:
        return Response("NOTFOUND", media_type="text/plain")
    if CLICK_SERVICE_ID and payload.get("service_id") and str(payload.get("service_id")) != CLICK_SERVICE_ID:
        return Response("SERVICE_ID_MISMATCH", media_type="text/plain", status_code=400)
    if CLICK_MERCHANT_ID and payload.get("merchant_id") and str(payload.get("merchant_id")) != CLICK_MERCHANT_ID:
        return Response("MERCHANT_ID_MISMATCH", media_type="text/plain", status_code=400)
    amount_payload = payload.get("amount") or payload.get("amount_sum")
    if amount_payload is not None:
        try:
            payload_amount = Decimal(str(amount_payload))
            stored_amount = Decimal(str(record.get("amount", "0")))
            if payload_amount != stored_amount:
                return Response("AMOUNT_MISMATCH", media_type="text/plain", status_code=400)
        except Exception:
            pass
    result = await mark_payment_paid(invoice_id)
    await log_callback("callback_verified", payload, True)
    if result and result.get("status") == "paid":
        start_iso = result.get("sub_start") or result.get("paid_at") or datetime.now(LOCAL_TZ).isoformat()
        end_iso = result.get("sub_end")
        if not end_iso:
            end_dt = _parse_iso(start_iso) + timedelta(days=SUBSCRIPTION_DAYS)
            end_iso = end_dt.isoformat()
        await _notify_subscription(result.get("user_id"), start_iso, end_iso)
    return Response("SUCCESS", media_type="text/plain")
