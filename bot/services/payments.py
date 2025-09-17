import os, time, sqlite3
from urllib.parse import urlencode, quote


def _db():
    return sqlite3.connect(os.getenv("DB_PATH", "moliya.db"))


def create_invoice(user_id: int, amount: int, plan: str) -> tuple[str, str]:
    inv = f"INV-{user_id}-{int(time.time())}"
    conn=_db(); c=conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS payments(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id BIGINT,
      invoice_id TEXT UNIQUE,
      amount INTEGER,
      plan TEXT,
      status TEXT DEFAULT 'pending',
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      paid_at TIMESTAMP
    )""")
    c.execute("INSERT OR IGNORE INTO payments(user_id,invoice_id,amount,plan,status) VALUES(?,?,?,?, 'pending')",
              (user_id, inv, int(amount), plan))
    conn.commit(); conn.close()

    ret = os.getenv('RETURN_URL', '')
    if ret and 'invoice_id=' not in ret:
        ret = f"{ret}?invoice_id={quote(inv)}"

    params = {
      "service_id": os.getenv("CLICK_SERVICE_ID",""),
      "merchant_id": os.getenv("CLICK_MERCHANT_ID",""),
      "amount": str(int(amount)),
      "transaction_param": inv,
      "return_url": ret
    }
    mu = os.getenv("CLICK_MERCHANT_USER_ID")
    if mu: params["merchant_user_id"] = mu

    click_url = "https://my.click.uz/services/pay?" + urlencode(params, safe="")
    return inv, click_url
