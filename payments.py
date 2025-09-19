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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO subs(user_id, plan, status, pay_id, provider, start_at, end_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                user_id,
                plan_key,
                "active",
                invoice_id,
                "click",
                start_dt.isoformat(),
                end_dt.isoformat(),
            ),
        )
        await db.commit()
    return start_dt.isoformat(), end_dt.isoformat()


def detect_plan(amount: Decimal) -> Optional[Tuple[str, int]]:
    return PLAN_BY_AMOUNT.get(amount)


async def update_user_subscription_fields(user_id: int, start_iso: str, end_iso: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING",
            (user_id,),
        )
        await db.execute(
            "UPDATE users SET sub_started_at=?, sub_until=?, sub_reminder_sent=0 WHERE user_id=?",
            (start_iso, end_iso, user_id),
        )
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
