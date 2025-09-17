# One shared activator used by both FastAPI and bot fallback.
import datetime
import os
import sqlite3


def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))


def _ensure_payment_columns(c: sqlite3.Cursor) -> set[str]:
    cols = {row[1] for row in c.execute("PRAGMA table_info(payments)").fetchall()}
    if "plan" not in cols:
        c.execute("ALTER TABLE payments ADD COLUMN plan TEXT")
        cols.add("plan")
    if "status" not in cols:
        c.execute("ALTER TABLE payments ADD COLUMN status TEXT DEFAULT 'pending'")
        cols.add("status")
    if "paid_at" not in cols:
        c.execute("ALTER TABLE payments ADD COLUMN paid_at TIMESTAMP")
        cols.add("paid_at")
    return cols


def activate_invoice(invoice_id: str) -> bool:
    conn = _db(); c = conn.cursor()

    pay_cols = _ensure_payment_columns(c)
    select_cols = "user_id, amount, plan" if "plan" in pay_cols else "user_id, amount"
    row = c.execute(f"SELECT {select_cols} FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not row:
        conn.close()
        return False

    if "plan" in pay_cols:
        user_id, amount, plan = row
    else:
        user_id, amount = row
        plan = None

    now = datetime.datetime.utcnow()
    now_iso = now.isoformat()
    c.execute("UPDATE payments SET status='paid', paid_at=? WHERE invoice_id=?", (now_iso, invoice_id))

    # ensure user columns
    user_cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    if "sub_started_at" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_started_at TIMESTAMP")
    if "sub_until" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_until TIMESTAMP")
    if "sub_reminder_sent_date" not in user_cols:
        c.execute("ALTER TABLE users ADD COLUMN sub_reminder_sent_date DATE")

    month_price = int(os.getenv("MONTH_PRICE", 19900))
    try:
        amount_int = int(amount)
    except Exception:
        amount_int = month_price

    plan_key = (plan or "").strip().lower()
    if not plan_key:
        plan_key = "month" if amount_int == month_price else "week"

    days = 30 if plan_key == "month" or amount_int == month_price else 7

    c.execute(
        """UPDATE users
                 SET sub_started_at=?, sub_until=date(?, '+'||?||' days'), sub_reminder_sent_date=NULL
                 WHERE user_id=?""",
        (now_iso, now_iso, days, user_id),
    )
    conn.commit()
    conn.close()
    return True
