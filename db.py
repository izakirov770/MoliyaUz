import os

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "moliya.db")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users(
  user_id INTEGER PRIMARY KEY,
  name TEXT,
  phone TEXT,
  currency TEXT DEFAULT 'UZS',
  lang TEXT DEFAULT 'uz',
  seed INTEGER DEFAULT 0,
  reminder_on INTEGER DEFAULT 1,
  remind_time TEXT DEFAULT '20:00',
  seen_example INTEGER DEFAULT 0,
  trial_used INTEGER DEFAULT 0,
  activated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  kind TEXT CHECK(kind IN ('income','expense')) NOT NULL,
  amount INTEGER NOT NULL,
  category TEXT,
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS debts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  direction TEXT CHECK(direction IN ('given','taken')) NOT NULL,
  amount INTEGER NOT NULL,
  counterparty TEXT,
  due_date DATE,
  due_morning_ping INTEGER DEFAULT 0,
  due_evening_ping INTEGER DEFAULT 0,
  done INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  plan TEXT,
  status TEXT,
  pay_id TEXT,
  provider TEXT,
  start_at TIMESTAMP,
  end_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT,
  pan_masked TEXT,
  owner TEXT,
  is_default INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS debts_archive(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debt_id INTEGER,
  user_id INTEGER,
  direction TEXT,
  amount INTEGER,
  currency TEXT,
  counterparty TEXT,
  due_date DATE,
  status TEXT,
  archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS config(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""

async def connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await _migrate_add_columns(db)
    await db.commit()
    return db

async def _migrate_add_columns(db):
    async def ensure_col(table, col, ddl):
        # PRAGMA bilan mavjud ustunlarni tekshiramiz
        cur = await db.execute(f"PRAGMA table_info({table})")
        cols = [r["name"] for r in await cur.fetchall()]
        if col not in cols:
            # ✅ MUHIM TUZATISH: ustun nomini ham qo'shamiz
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # users
    await ensure_col("users", "seen_example", "INTEGER DEFAULT 0")
    await ensure_col("users", "trial_used", "INTEGER DEFAULT 0")
    await ensure_col("users", "remind_time", "TEXT DEFAULT '20:00'")
    await ensure_col("users", "activated", "INTEGER DEFAULT 0")
    await ensure_col("users", "sub_started_at", "TIMESTAMP")
    await ensure_col("users", "sub_until", "TIMESTAMP")
    await ensure_col("users", "sub_reminder_sent", "INTEGER DEFAULT 0")

    # debts
    await ensure_col("debts", "due_morning_ping", "INTEGER DEFAULT 0")
    await ensure_col("debts", "due_evening_ping", "INTEGER DEFAULT 0")
    await ensure_col("debts", "created_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

# USERS
async def upsert_user(db, user_id:int, **kw):
    await db.execute("INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING", (user_id,))
    if kw:
        fields = ", ".join([f"{k}=?" for k in kw.keys()])
        await db.execute(f"UPDATE users SET {fields} WHERE user_id=?", [*kw.values(), user_id])
    await db.commit()

async def get_user(db, user_id:int):
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    return await cur.fetchone()

# TX
async def add_tx(db, user_id:int, kind:str, amount:int, category:str, note:str):
    await db.execute(
        "INSERT INTO transactions(user_id, kind, amount, category, note) VALUES(?,?,?,?,?)",
        (user_id, kind, amount, category, note)
    )
    await db.commit()

async def stats(db, user_id:int, since=None):
    q = "SELECT kind, SUM(amount) s FROM transactions WHERE user_id=?"
    params = [user_id]
    if since:
        q += " AND datetime(created_at) >= datetime(?)"
        params.append(since)
    q += " GROUP BY kind"
    cur = await db.execute(q, params)
    data = {"income":0, "expense":0}
    for r in await cur.fetchall():
        data[r["kind"]] = r["s"] or 0
    return data

async def list_report(db, user_id:int, delta_days:int):
    cur = await db.execute(
        "SELECT date(created_at) d, kind, category, amount FROM transactions "
        "WHERE user_id=? AND datetime(created_at) >= datetime('now', ? || ' days') "
        "ORDER BY created_at DESC",
        (user_id, -delta_days)
    )
    return await cur.fetchall()

# DEBTS
async def add_debt(db, user_id:int, direction:str, amount:int, due_date:str, counterparty:str=None):
    await db.execute(
        "INSERT INTO debts(user_id, direction, amount, due_date, counterparty) VALUES(?,?,?,?,?)",
        (user_id, direction, amount, due_date, counterparty)
    )
    await db.commit()

async def debts_due_today_morning(db):
    cur = await db.execute(
        "SELECT * FROM debts WHERE done=0 AND date(due_date)=date('now') AND due_morning_ping=0"
    )
    return await cur.fetchall()

async def debts_due_today_evening(db):
    cur = await db.execute(
        "SELECT * FROM debts WHERE done=0 AND date(due_date)=date('now') AND due_evening_ping=0"
    )
    return await cur.fetchall()

async def mark_debt_ping(db, debt_id:int, which:str):
    col = "due_morning_ping" if which=="morning" else "due_evening_ping"
    await db.execute(f"UPDATE debts SET {col}=1 WHERE id=?", (debt_id,))
    await db.commit()

# SUBS
async def create_sub(db, user_id:int, plan:str, pay_id:str, provider:str, start_at:str, end_at:str):
    await db.execute(
        "INSERT INTO subs(user_id, plan, status, pay_id, provider, start_at, end_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, plan, "pending", pay_id, provider, start_at, end_at)
    )
    await db.commit()

async def activate_sub(db, pay_id:str):
    await db.execute("UPDATE subs SET status='active' WHERE pay_id=?", (pay_id,))
    await db.commit()

async def current_sub(db, user_id:int):
    cur = await db.execute(
        "SELECT * FROM subs WHERE user_id=? AND status='active' AND datetime(end_at) >= datetime('now') ORDER BY end_at DESC LIMIT 1",
        (user_id,)
    )
    return await cur.fetchone()
