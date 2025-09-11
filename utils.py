import re
from datetime import datetime, timedelta
import pytz

PLUS_WORDS = [
    "keldi","daromad","tushdi","olindi","topdim","kiritdim","maosh","bonus","premiya","kirim","+",
    "salary","income"
]
MINUS_WORDS = [
    "sarfladim","chiqdim","to'ladim","oldim","xarajat","harajat","tashladim","to'lov","minus","chiqim","-",
    "spent","expense"
]

def parse_amount(text:str) -> int | None:
    t = text.lower().replace("’","").replace("‘","").replace("'","").replace("`","")
    m = re.search(r"(\d+(?:[., ]\d{3})*|\d+)\s*([km])?\b", t)
    if not m: return None
    raw, suf = m.group(1), (m.group(2) or "")
    n = int(re.sub(r"[^\d]", "", raw))
    if suf == "k": n *= 1000
    elif suf == "m": n *= 1_000_000
    return n

def guess_kind(text:str) -> str:
    tl = text.lower()
    if any(w in tl for w in PLUS_WORDS) or tl.strip().startswith("+"):
        return "income"
    if any(w in tl for w in MINUS_WORDS) or tl.strip().startswith("-"):
        return "expense"
    return "expense"

def guess_category(text:str) -> str:
    tl = text.lower()
    if "kofe" in tl or "coffee" in tl: return "Kofe va tamaddi"
    if "tushlik" in tl or "restoran" in tl or "ovqat" in tl or "lunch" in tl: return "Ovqat"
    if "benzin" in tl or "yoqil" in tl or "ai-95" in tl or "gas" in tl: return "Transport"
    if "maosh" in tl or "oylik" in tl or "salary" in tl: return "Maosh"
    return "Umumiy"

def now_tashkent():
    return datetime.now(pytz.timezone("Asia/Tashkent"))

# ---- Qarzni matndan aniqlash ----
def parse_debt(text:str):
    """
    'qarz berdim Temurga 150k' -> ('given', 150000, 'Temur')
    'qarz oldim Akadan 200000' -> ('taken', 200000, 'Aka')
    """
    tl = text.lower()
    direction = None
    if "qarz berdim" in tl or "qarz ber" in tl:
        direction = "given"
    elif "qarz oldim" in tl or "qarz ol" in tl:
        direction = "taken"
    else:
        return None

    amount = parse_amount(text)
    if not amount: return None

    # oddiy ism ajratish: "ga", "dan" atrofidagi so'zlarni olishga urinamiz
    # misol: "temurga", "umidga", "akadan"
    m = re.search(r"(?:qarz berdim|qarz oldim)\s+([a-zA-Z\u0400-\u04FF\u0410-\u044F\u0492\u04B3\u04CF\u04E9\u04AF\u04A3]+)\w*", tl)
    counterparty = None
    if m:
        cp = m.group(1)
        counterparty = cp.capitalize()
    return (direction, amount, counterparty)

# ---- Sanani parse qilish ----
def parse_due_date(text:str):
    """
    'ertaga' -> tomorrow
    'bugun' -> today
    'indin' -> +2 days
    '15.09.2025' or '2025-09-15'
    """
    tl = text.lower().strip()
    tz = pytz.timezone("Asia/Tashkent")
    today = now_tashkent().date()

    if tl in ("bugun", "bugunlik", "today"):
        return today.isoformat()
    if tl in ("ertaga", "ertalikka", "tomorrow"):
        return (today + timedelta(days=1)).isoformat()
    if tl in ("indin", "ertasiga", "posangi"):
        return (today + timedelta(days=2)).isoformat()

    m1 = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", tl)  # 15.09.2025
    if m1:
        d = f"{m1.group(3)}-{m1.group(2)}-{m1.group(1)}"
        return d
    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", tl)    # 2025-09-15
    if m2:
        return tl
    return None
