import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

import aiosqlite

from db import DB_PATH as DEFAULT_DB_PATH

DB_PATH = os.getenv("DB_PATH", DEFAULT_DB_PATH)

PAYMENTS_TABLE = """
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id BIGINT,
    invoice_id TEXT UNIQUE,
    amount NUMERIC,
    currency TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP
);
"""

PAYMENTS_LOGS_TABLE = """
CREATE TABLE IF NOT EXISTS payments_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    raw_payload TEXT,
    verified BOOLEAN,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

PLAN_BY_AMOUNT: Dict[Decimal, Tuple[str, int]] = {
    Decimal("7900"): ("sub_week", 7),
    Decimal("19900"): ("sub_month", 30),
}

_schema_ready = False


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(PAYMENTS_TABLE)
        await db.execute(PAYMENTS_LOGS_TABLE)
        await db.commit()
    _schema_ready = True


# [SUBSCRIPTION-POLLING-BEGIN]
async def create_polling_payment(
    user_id: int,
    merchant_trans_id: str,
    amount: str,
    plan: str,
    currency: str = "UZS",
) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments(user_id, invoice_id, amount, currency, status, plan, created_at) "
            "VALUES(?,?,?,?, 'pending', ?, CURRENT_TIMESTAMP)",
            (user_id, merchant_trans_id, amount, currency, plan),
        )
        await db.commit()
    return await get_payment_by_invoice(merchant_trans_id)


async def get_recent_pending_payment(user_id: int) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE user_id=? AND status='pending' "
            "AND datetime(created_at) >= datetime('now', '-1 day') "
            "ORDER BY datetime(created_at) DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def mark_polling_payment_paid(
    merchant_trans_id: str,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE invoice_id=?",
            (merchant_trans_id,),
        )
        row = await cur.fetchone()
    if not row:
        await log_callback("click_polling_missing", {"merchant_trans_id": merchant_trans_id}, False)
        return None

    if row["status"] == "paid":
        await log_callback("click_polling_already_paid", {"merchant_trans_id": merchant_trans_id}, True)
        return dict(row)

    paid_at_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE invoice_id=?",
            (paid_at_iso, merchant_trans_id),
        )
        await db.commit()
    await log_callback(
        "click_polling_paid",
        {"merchant_trans_id": merchant_trans_id, "payload": payload},
        True,
    )
    updated = await get_payment_by_invoice(merchant_trans_id)
    if updated:
        updated["paid_at"] = paid_at_iso
    return updated

# [SUBSCRIPTION-POLLING-END]


async def create_invoice(user_id: int, amount: Decimal, currency: str) -> str:
    await ensure_schema()
    base = f"INV-{user_id}-{int(time.time())}"
    invoice_id = base
    attempt = 0
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO payments(user_id, invoice_id, amount, currency, status, created_at) "
                    "VALUES(?,?,?,?, 'pending', CURRENT_TIMESTAMP)",
                    (user_id, invoice_id, str(amount), currency),
                )
                await db.commit()
            break
        except aiosqlite.IntegrityError:
            attempt += 1
            suffix = uuid.uuid4().hex[:6]
            invoice_id = f"{base}-{suffix}"
            if attempt > 3:
                raise
    return invoice_id


async def log_callback(event_type: str, payload: Dict[str, Any], verified: bool=False) -> None:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO payments_logs(event_type, raw_payload, verified) VALUES(?, ?, ?)",
            (event_type, json.dumps(payload, default=str), int(verified)),
        )
        await db.commit()


async def get_payment_by_invoice(invoice_id: str) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return dict(row)


async def get_latest_payment(user_id: int) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE user_id=? AND status IN ('pending','paid') "
            "ORDER BY datetime(created_at) DESC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return dict(row)


async def mark_payment_paid(invoice_id: str) -> Optional[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM payments WHERE invoice_id=?",
            (invoice_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        if row["status"] == "paid":
            return dict(row)
        paid_at = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE payments SET status='paid', paid_at=? WHERE invoice_id=?",
            (paid_at, invoice_id),
        )
        await db.commit()
    updated = await get_payment_by_invoice(invoice_id)
    if updated:
        amount = Decimal(str(updated.get("amount", "0")))
        plan = PLAN_BY_AMOUNT.get(amount)
        if plan:
            start_iso, end_iso = await _record_subscription(
                updated["user_id"], invoice_id, plan[0], plan[1], paid_at
            )
            await update_user_subscription_fields(updated["user_id"], start_iso, end_iso)
            updated["sub_start"] = start_iso
            updated["sub_end"] = end_iso
    return updated


async def _record_subscription(user_id: int, invoice_id: str, plan_key: str, days: int, paid_at_iso: str) -> Tuple[str, str]:
    start_dt = datetime.fromisoformat(paid_at_iso)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    else:
        start_dt = start_dt.astimezone(timezone.utc)
    end_dt = start_dt + timedelta(days=days)
    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE subs SET plan=?, status='active', provider=?, start_at=?, end_at=? WHERE pay_id=?",
            (plan_key, "click", start_iso, end_iso, invoice_id),
        )
        cur = await db.execute("SELECT id FROM subs WHERE pay_id=?", (invoice_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO subs(user_id, plan, status, pay_id, provider, start_at, end_at) VALUES(?,?,?,?,?,?,?)",
                (
                    user_id,
                    plan_key,
                    "active",
                    invoice_id,
                    "click",
                    start_iso,
                    end_iso,
                ),
            )
        await db.commit()
    return start_iso, end_iso


def detect_plan(amount: Decimal) -> Optional[Tuple[str, int]]:
    return PLAN_BY_AMOUNT.get(amount)


async def update_user_subscription_fields(user_id: int, start_iso: str, end_iso: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        cur = await db.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cur.fetchall()}
        if "sub_reminder_sent" in cols:
            reminder_sql = "sub_reminder_sent=0"
        elif "sub_reminder_sent_date" in cols:
            reminder_sql = "sub_reminder_sent_date=NULL"
        else:
            reminder_sql = None
        set_parts = ["sub_started_at=?", "sub_until=?"]
        params = [start_iso, end_iso]
        if reminder_sql:
            set_parts.append(reminder_sql)
        if "activated" in cols:
            set_parts.append("activated=1")
        if "trial_used" in cols:
            set_parts.append("trial_used=1")
        sql = f"UPDATE users SET {', '.join(set_parts)} WHERE user_id=?"
        params.append(user_id)
        await db.execute(sql, params)
        await db.commit()


async def mark_user_reminder_sent(user_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET sub_reminder_sent=1 WHERE user_id=?",
            (user_id,),
        )
        await db.commit()


async def users_for_expiry_reminder(cutoff_iso: str) -> list[Dict[str, Any]]:
    await ensure_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, sub_until FROM users WHERE sub_until IS NOT NULL AND sub_reminder_sent=0",
        )
        rows = await cur.fetchall()
    items = []
    for row in rows:
        items.append(dict(row))
    return items
