import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

import aiosqlite
from aiogram import Bot
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Query
from fastapi.responses import RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from zoneinfo import ZoneInfo
from urllib.parse import urlencode, quote
import sqlite3

from payments import (
    ensure_schema as ensure_payment_schema,
    get_payment_by_invoice,
    log_callback,
    mark_payment_paid,
)
from bot.services.activate import activate_invoice
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

app = FastAPI()

# clickpay papkani ulaymiz
app.mount("/clickpay", StaticFiles(directory="clickpay"), name="clickpay")


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


def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


@app.get("/payments/return")
def payments_return(invoice_id: str):
    conn = _db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id BIGINT,
            invoice_id TEXT UNIQUE,
            amount NUMERIC,
            currency TEXT DEFAULT 'UZS',
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP
        )
        """
    )
    _ensure_column(conn, "users", "sub_started_at", "TIMESTAMP")
    _ensure_column(conn, "users", "sub_until", "TIMESTAMP")
    _ensure_column(conn, "users", "sub_reminder_sent_date", "DATE")
    conn.close()

    ok = activate_invoice(invoice_id)
    if not ok:
        return PlainTextResponse("Invoice not found", status_code=404)
    return PlainTextResponse("OK", status_code=200)


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
from fastapi import Query
from fastapi.responses import PlainTextResponse
import sqlite3, datetime, os

def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))

def _ensure_user_columns(c):
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "sub_started_at" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_started_at TIMESTAMP")
    if "sub_until" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_until TIMESTAMP")
    if "sub_reminder_sent_date" not in cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_reminder_sent_date DATE")

@app.get("/payments/return")
def payments_return(invoice_id: str = Query(...)):
    ok = activate_invoice(invoice_id)
    if not ok:
        return PlainTextResponse("Invoice not found", status_code=404)
    return PlainTextResponse("OK", status_code=200)
