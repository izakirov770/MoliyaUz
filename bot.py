# bot.py
import asyncio, os, re, json, logging, sys, types
import aiosqlite
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus, urlencode, urlparse, urlunparse, parse_qsl
from typing import Optional, Dict, List, Tuple, Any

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, WebAppInfo,
)
from dotenv import load_dotenv

from db import DB_PATH
from payments import (
    create_invoice as payments_create_invoice,
    detect_plan as payments_detect_plan,
    ensure_schema as ensure_payment_schema,
    get_latest_payment as payments_get_latest_payment,
    mark_payment_paid as payments_mark_payment_paid,
    users_for_expiry_reminder as payments_users_for_expiry_reminder,
)
from services.payments import create_invoice_id, build_miniapp_url

# ====== ENV ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN yo'q")

CLICK_PAY_URL_BASE = os.getenv("CLICK_PAY_URL_BASE", "https://my.click.uz/services/pay")
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "")
PAYMENT_RETURN_URL = os.getenv("PAYMENT_RETURN_URL", "")
USD_UZS = float(os.getenv("USD_UZS", "12600"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUBSCRIPTION_DAYS = int(os.getenv("SUBSCRIPTION_DAYS", "30"))
TZ_NAME = os.getenv("TZ", "Asia/Tashkent")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = set()
for part in ADMIN_IDS_RAW.split(","):
    part = part.strip()
    if not part:
        continue
    try:
        ADMIN_IDS.add(int(part))
    except Exception:
        continue
if ADMIN_ID:
    ADMIN_IDS.add(ADMIN_ID)
WEB_BASE = os.getenv("WEB_BASE", "http://127.0.0.1:8000")
RETURN_URL = os.getenv("RETURN_URL", f"{WEB_BASE.rstrip('/')}/payments/return")
MONTH_PLAN_PRICE = int(os.getenv("MONTH_PLAN_PRICE", "19900"))
MINI_APP_BASE_URL = f"{WEB_BASE.rstrip('/')}/clickpay/click_form.html"
# [SUBSCRIPTION-POLLING-BEGIN]
CLICK_INCLUDE_RETURN_URL = os.getenv("CLICK_INCLUDE_RETURN_URL", "false").lower() in {"1", "true", "yes"}
# [SUBSCRIPTION-POLLING-END]

NOTION_OFER_URL = "https://www.notion.so/OFERA-26a8fa17fd1f803f8025f07f98f89c87?source=copy_link"

# ====== PACKAGE BRIDGE ======
_BOT_MODULE_PATH = Path(__file__).resolve().parent / "bot"
if _BOT_MODULE_PATH.exists():
    _existing_bot = sys.modules.get("bot")
    if _existing_bot is None:
        _namespace = types.ModuleType("bot")
        _namespace.__path__ = [str(_BOT_MODULE_PATH)]
        sys.modules["bot"] = _namespace
    elif not hasattr(_existing_bot, "__path__"):
        _existing_bot.__path__ = [str(_BOT_MODULE_PATH)]

# ====== EXTERNAL MODULES ======
from bot.routers.subscription_plans import sub_router
from bot.routers.pay_debug import pay_debug_router
from subscription import PENDING_MANUAL_DIGITS, subscription_router

# ====== BOT ======
proxy_url = os.getenv("TELEGRAM_PROXY", "").strip()
session = AiohttpSession(proxy=proxy_url) if proxy_url else None
bot = Bot(
    BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()
rt = Router()
reports_range_router = Router()
cards_entry_router = Router()
debts_archive_router = Router()

# ====== VAQT/FMT ======
try:
    TASHKENT = ZoneInfo(TZ_NAME)
except Exception:
    TASHKENT = timezone(timedelta(hours=5))
now_tk = lambda: datetime.now(TASHKENT)
fmt_date = lambda d: d.strftime("%d.%m.%Y")
def fmt_amount(n):
    try: return f"{int(round(n)):,}".replace(",", " ")
    except: return str(n)


async def handle_paid_deeplink(uid: int, payload: str, message: Message) -> bool:
    if not payload or not payload.startswith("paid_"):
        return False
    invoice_id = payload[5:].strip()
    if not invoice_id:
        return False

    lang = get_lang(uid)
    T = L(lang)

    record = await payments_mark_payment_paid(invoice_id)
    if not record:
        await message.answer(T("pay_status_missing"))
        await message.answer(T("menu"), reply_markup=get_main_menu(lang))
        STEP[uid] = "main"
        return True

    owner_id = record.get("user_id")
    if owner_id and owner_id != uid:
        await ensure_subscription_state(owner_id)
        await message.answer(T("pay_status_missing"))
        await message.answer(T("menu"), reply_markup=get_main_menu(lang))
        STEP[uid] = "main"
        return True

    await ensure_subscription_state(uid)

    amount_dec = Decimal(str(record.get("amount", "0") or "0"))
    plan_info = payments_detect_plan(amount_dec)
    plan_key = plan_info[0] if plan_info else None
    plan_label = L(lang)(plan_key) if plan_key else f"{fmt_amount(int(amount_dec))} UZS"

    start_dt = _parse_dt(record.get("sub_start")) if record.get("sub_start") else None
    end_dt = _parse_dt(record.get("sub_end")) if record.get("sub_end") else None
    until_text = fmt_date(end_dt) if end_dt else "—"

    await message.answer(T("pay_status_paid", plan=plan_label, until=until_text))
    if start_dt and end_dt:
        await message.answer(T("sub_ok", start=fmt_date(start_dt), end=fmt_date(end_dt)))
    await message.answer(T("SUB_OK"))

    STEP[uid] = "main"
    await message.answer(T("menu"), reply_markup=get_main_menu(lang))
    return True

# ====== “DB” (RAM) ======
STEP: Dict[int,str] = {}
USER_LANG: Dict[int,str] = {}
SEEN_USERS: set[int] = set()
USER_ACTIVATED: Dict[int, bool] = {}
REPORT_RANGE_STATE: Dict[int, Dict[str, str]] = {}

ANALYSIS_COUNTERS: Dict[int, Dict[str, int]] = {}
LAST_RESET_YYYYMM: Optional[str] = None
ANALYSIS_STATE_PATH = Path("analysis_state.json")

CARDS_FILE = Path("cards.json")
USER_CARDS: Dict[int, List[dict]] = {}

DEBTS_ARCHIVE: Dict[int, List[dict]] = {}
DEBTS_ARCHIVE_FILE = Path("debts_archive.json")

USERS_PROFILE_FILE = Path("users.json")
USERS_PROFILE_CACHE: Dict[int, Dict[str, Any]] = {}


class CardAddStates(StatesGroup):
    label = State()
    pan = State()
    expires = State()
    owner = State()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_json(path: Path, payload: dict) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, default=str)
    except Exception:
        pass

def load_cards_storage() -> None:
    USER_CARDS.clear()
    data = _load_json(CARDS_FILE)
    if not isinstance(data, dict):
        return
    for key, items in data.items():
        try:
            uid = int(key)
        except Exception:
            continue
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            pan = str(item.get("pan") or item.get("pan_masked") or item.get("pan_last4") or "").strip()
            expires = str(item.get("expires", "")).strip()
            owner = str(item.get("owner") or item.get("owner_fullname") or "").strip()
            if not label or not pan:
                continue
            clean.append({
                "label": label[:32],
                "pan": pan,
                "expires": expires[:10],
                "owner": owner[:64],
            })
        if clean:
            USER_CARDS[uid] = clean


def save_cards_storage() -> None:
    payload = {str(uid): cards for uid, cards in USER_CARDS.items()}
    _save_json(CARDS_FILE, payload)


def get_cards(uid: int) -> List[dict]:
    return list(USER_CARDS.get(uid, []))


def save_card(uid: int, label: str, pan: str, expires: str, owner: str) -> None:
    pan_digits = re.sub(r"\s+", "", pan)
    cards = USER_CARDS.setdefault(uid, [])
    cards.append({
        "label": label[:32],
        "pan": pan_digits,
        "expires": expires[:10],
        "owner": owner[:64],
    })
    save_cards_storage()


def load_users_storage() -> None:
    global USERS_PROFILE_CACHE
    if not USERS_PROFILE_FILE.exists():
        USERS_PROFILE_CACHE = {}
        return
    try:
        raw = USERS_PROFILE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        USERS_PROFILE_CACHE = {}
        return
    if not isinstance(data, dict):
        USERS_PROFILE_CACHE = {}
        return
    cache: Dict[int, Dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        try:
            uid = int(key)
        except Exception:
            continue
        cache[uid] = value
    USERS_PROFILE_CACHE = cache


def save_users_storage() -> None:
    try:
        USERS_PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        dump = {str(uid): data for uid, data in USERS_PROFILE_CACHE.items()}
        USERS_PROFILE_FILE.write_text(
            json.dumps(dump, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def update_user_profile(user_id: int, **fields: Any) -> None:
    if not fields:
        return
    profile = USERS_PROFILE_CACHE.setdefault(user_id, {})
    changed = False
    now_iso = datetime.now(timezone.utc).isoformat()
    if "created_at" not in profile:
        profile["created_at"] = now_iso
        changed = True
    for key, value in fields.items():
        if value is None:
            continue
        if profile.get(key) != value:
            profile[key] = value
            changed = True
    if changed:
        profile["updated_at"] = now_iso
        save_users_storage()


def load_debts_archive() -> None:
    data = _load_json(DEBTS_ARCHIVE_FILE)
    items = data.get("items", {}) if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        return
    for k, v in items.items():
        try:
            uid = int(k)
        except Exception:
            continue
        if not isinstance(v, list):
            continue
        clean: List[dict] = []
        for it in v:
            if not isinstance(it, dict):
                continue
            copy = dict(it)
            ts_val = copy.get("ts")
            if isinstance(ts_val, str):
                try:
                    copy["ts"] = datetime.fromisoformat(ts_val)
                except Exception:
                    copy["ts"] = now_tk()
            archived_val = copy.get("archived_at")
            if isinstance(archived_val, str):
                try:
                    copy["archived_at"] = datetime.fromisoformat(archived_val)
                except Exception:
                    copy["archived_at"] = now_tk()
            clean.append(copy)
        if clean:
            DEBTS_ARCHIVE[uid] = clean


def save_debts_archive() -> None:
    payload = {"items": {str(uid): items for uid, items in DEBTS_ARCHIVE.items()}}
    _save_json(DEBTS_ARCHIVE_FILE, payload)


def archive_debt_record(uid:int, debt:dict) -> None:
    items = DEBTS_ARCHIVE.setdefault(uid, [])
    payload = dict(debt)
    payload["archived_at"] = now_tk()
    items.append(payload)
    save_debts_archive()


def load_analysis_state() -> None:
    global LAST_RESET_YYYYMM
    data = _load_json(ANALYSIS_STATE_PATH)
    value = data.get("last_reset") if isinstance(data, dict) else None
    if isinstance(value, str):
        LAST_RESET_YYYYMM = value


def save_analysis_state() -> None:
    if LAST_RESET_YYYYMM:
        _save_json(ANALYSIS_STATE_PATH, {"last_reset": LAST_RESET_YYYYMM})


def reset_analysis_counters() -> None:
    for counters in ANALYSIS_COUNTERS.values():
        counters["income"] = 0
        counters["expense"] = 0
        counters["tx_count"] = 0


def update_analysis_counters(uid: int, kind: str, amount: int, currency: str) -> None:
    counters = ANALYSIS_COUNTERS.setdefault(uid, {"income": 0, "expense": 0, "tx_count": 0})
    amt_uzs = to_uzs(amount, currency)
    if kind == "income":
        counters["income"] += amt_uzs
    else:
        counters["expense"] += amt_uzs
    counters["tx_count"] += 1


def revert_analysis_counters(uid: int, kind: str, amount: int, currency: str) -> None:
    counters = ANALYSIS_COUNTERS.get(uid)
    if not counters:
        return
    amt_uzs = to_uzs(amount, currency)
    if kind == "income":
        counters["income"] = max(0, counters["income"] - amt_uzs)
    else:
        counters["expense"] = max(0, counters["expense"] - amt_uzs)
    counters["tx_count"] = max(0, counters["tx_count"] - 1)


async def _ensure_subscription_columns(db: aiosqlite.Connection) -> None:
    cur = await db.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in await cur.fetchall()}
    statements = []
    if "sub_started_at" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN sub_started_at TIMESTAMP")
    if "sub_until" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN sub_until TIMESTAMP")
    if "sub_reminder_sent" not in cols:
        statements.append("ALTER TABLE users ADD COLUMN sub_reminder_sent INTEGER DEFAULT 0")
    for stmt in statements:
        try:
            await db.execute(stmt)
        except Exception:
            pass
    if statements:
        await db.commit()


async def ensure_month_rollover() -> None:
    global LAST_RESET_YYYYMM
    if LAST_RESET_YYYYMM is None:
        load_analysis_state()
    current = now_tk().strftime("%Y%m")
    if LAST_RESET_YYYYMM == current:
        return
    reset_analysis_counters()
    LAST_RESET_YYYYMM = current
    save_analysis_state()


async def update_bot_bio(total_users: int) -> None:
    """Keep legacy command but stop overriding the BotFather description."""
    try:
        current = await bot.get_my_description()
    except Exception:
        return
    desc = (current.description or "") if current else ""
    # Strip previously injected counters so the BotFather description stays intact.
    cleaned_desc = re.sub(r"(?:\s*\|)?\s*Foydalanuvchilar: \d+", "", desc).strip()
    if cleaned_desc == desc:
        return
    try:
        await bot.set_my_description(cleaned_desc)
    except Exception:
        pass

TRIAL_MIN = 15
TRIAL_START: Dict[int,datetime] = {}
SUB_EXPIRES: Dict[int,datetime] = {}
SUB_STARTED: Dict[int,datetime] = {}
SUB_REMINDER_DONE: Dict[int,bool] = {}
SUB_EXPIRED_NOTICE: set[int] = set()

# tranzaksiya: {id, ts, kind(income|expense), amount, currency(UZS|USD|EUR), account(cash|card), category, desc}
MEM_TX: Dict[int, List[dict]] = {}
MEM_TX_SEQ: Dict[int, int] = {}
# qarz: {id, ts, direction(mine|given), amount, currency, counterparty, due, status(wait|paid|received)}
MEM_DEBTS: Dict[int, List[dict]] = {}
MEM_DEBTS_SEQ: Dict[int,int] = {}
PENDING_DEBT: Dict[int,dict] = {}

# payment pending
# pid -> {"uid","plan","period_days","amount","currency","status","created"}
PENDING_PAYMENTS: Dict[str,dict] = {}

DEBT_REMIND_SENT: set[Tuple[int,int,str,str]] = set()

NAV_STACK: Dict[int, List[str]] = {}


def nav_stack(uid: int) -> List[str]:
    stack = NAV_STACK.setdefault(uid, ["main"])
    if not stack:
        stack.append("main")
    return stack


def nav_current(uid: int) -> str:
    return nav_stack(uid)[-1]


def nav_push(uid: int, state: str) -> None:
    stack = nav_stack(uid)
    if stack[-1] != state:
        stack.append(state)


def nav_back(uid: int) -> str:
    stack = nav_stack(uid)
    if len(stack) > 1:
        stack.pop()
    return stack[-1]


def nav_reset(uid: int) -> None:
    NAV_STACK[uid] = ["main"]


# ====== UTIL ======
def is_active(uid:int)->bool:
    e=SUB_EXPIRES.get(uid)
    return bool(e and e>=now_tk())

def is_sub(uid):
    return is_active(uid)


def is_card_admin(uid: int) -> bool:
    return uid in ADMIN_IDS if ADMIN_IDS else (ADMIN_ID and uid == ADMIN_ID)
def trial_active(uid):
    s=TRIAL_START.get(uid); return bool(s and (now_tk()-s)<=timedelta(minutes=TRIAL_MIN))
def has_access(uid): return is_sub(uid) or trial_active(uid)
def block_text(uid):
    if SUB_EXPIRES.get(uid) and not is_sub(uid): return "⛔️ Obuna muddati tugagan. Obunani yangilang."
    if TRIAL_START.get(uid) and not trial_active(uid): return "⌛️ 15 daqiqalik bepul sinov tugadi. Obuna tanlang."
    return "⛔️ Bu bo‘lim uchun obuna kerak."


def _parse_dt(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        dt = val
    else:
        try:
            dt = datetime.fromisoformat(str(val))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TASHKENT)
    return dt.astimezone(TASHKENT)


async def ensure_subscription_state(uid: int) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await _ensure_subscription_columns(db)
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT sub_started_at, sub_until, sub_reminder_sent FROM users WHERE user_id=?",
                (uid,),
            )
            row = await cur.fetchone()
    except Exception:
        row = None
    if not row:
        SUB_STARTED.pop(uid, None)
        SUB_EXPIRES.pop(uid, None)
        SUB_REMINDER_DONE.pop(uid, None)
        return
    start = _parse_dt(row.get("sub_started_at")) if isinstance(row, dict) else _parse_dt(row["sub_started_at"])
    until = _parse_dt(row.get("sub_until")) if isinstance(row, dict) else _parse_dt(row["sub_until"])
    reminder_sent_raw = row.get("sub_reminder_sent") if isinstance(row, dict) else row["sub_reminder_sent"]
    SUB_STARTED[uid] = start or SUB_STARTED.get(uid)
    if until:
        SUB_EXPIRES[uid] = until
    else:
        SUB_EXPIRES.pop(uid, None)
    SUB_REMINDER_DONE[uid] = bool(reminder_sent_raw)


async def set_user_subscription(uid: int, start_dt: datetime, end_dt: datetime) -> None:
    start_local = _parse_dt(start_dt) or start_dt.astimezone(TASHKENT)
    end_local = _parse_dt(end_dt) or end_dt.astimezone(TASHKENT)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(user_id) VALUES(?) ON CONFLICT(user_id) DO NOTHING", (uid,))
        await _ensure_subscription_columns(db)
        cur = await db.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in await cur.fetchall()}
        set_parts = ["sub_started_at=?", "sub_until=?", "sub_reminder_sent=0"]
        params = [start_local.isoformat(), end_local.isoformat()]
        if "activated" in cols:
            set_parts.append("activated=1")
        if "trial_used" in cols:
            set_parts.append("trial_used=1")
        sql = f"UPDATE users SET {', '.join(set_parts)} WHERE user_id=?"
        params.append(uid)
        await db.execute(sql, params)
        await db.commit()
    SUB_STARTED[uid] = start_local
    SUB_EXPIRES[uid] = end_local
    SUB_REMINDER_DONE[uid] = False
    SUB_EXPIRED_NOTICE.discard(uid)


async def mark_reminder_sent(uid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_subscription_columns(db)
        await db.execute(
            "UPDATE users SET sub_reminder_sent=1 WHERE user_id=?", (uid,)
        )
        await db.commit()
    SUB_REMINDER_DONE[uid] = True


async def send_expired_notice(uid: int, lang: str, send_callable) -> None:
    until = SUB_EXPIRES.get(uid)
    if until and until < now_tk() and uid not in SUB_EXPIRED_NOTICE:
        T = L(lang)
        try:
            await send_callable(T("sub_expired"))
        except Exception:
            pass
        SUB_EXPIRED_NOTICE.add(uid)

# ---- Localization helpers ----
def t_uz(k,**kw):
    D={
        "start_choose":"Assalomu alaykum, iltimos bot tilni tanlang.",
        "ask_name":"Ajoyib, tanishib olamiz, ismingiz nima?",
        "welcome":(
            "Xush kelibsiz, {name}! 👋\n\n"
            "📊 MoliyaUz – shaxsiy moliyani avtomatik boshqaruvchi yordamchi.\n"
            "— Matndan kirim/chiqimni tushunadi 💬\n"
            "— Avto-kategoriyalab saqlaydi 🏷\n"
            "— Qarz muddatini eslatadi ⏰\n\n"
            "Botdan foydalanib, <b>ofertamizga</b> rozilik bildirasiz.\n\n"
            "⏩ Davom etish uchun telefon raqamingizni yuboring:"
        ),
        "btn_share":"📱 Telefon raqamni yuborish",
        "btn_oferta":"📄 Ofertamiz",

        "menu":"Asosiy menyu:",
        "btn_hisobla":"🧮 Hisobla",
        "btn_hisobot":"📊 Hisobot",
        "btn_qarz":"🚨 Qarz",
        "btn_balance":"💼 Balans",
        "btn_obuna":"⭐️ Obuna",
        "btn_back":"⬅️ Ortga",
        "btn_analiz":"📊 Analiz",
        "btn_lang":"🌐 Tilni o‘zgartirish",

        "enter_tx":("Xarajat yoki kirimni yozing. Masalan: "
                    "<i>Kofe 15 ming</i>, <i>kirim 1.2 mln maosh</i>.\n"
                    "Agar <b>qarz oldim/qarz berdim</b> desangiz, muddatni so‘rayman."),
        "tx_exp":"Hisobotga qo‘shildi ✅\n\nChiqim:\nSana: {date}\n\nSumma: {cur} {amount}\nKategoriya: {cat}\nIzoh: {desc}",
        "tx_inc":"Hisobotga qo‘shildi ✅\n\nKirim:\nSana: {date}\n\nSumma: {cur} {amount}\nKategoriya: 💪 Mehnat daromadlari\nIzoh: {desc}",
        "need_sum":"Miqdor topilmadi. Masalan: <i>taksi 15 000</i>.",
        "report_main":"Qaysi hisobotni ko‘rasiz?",
        "rep_tx":"📒 Kirim-chiqim",
        "rep_debts":"💳 Qarzlar",
        "rep_day":"Kunlik","rep_week":"Haftalik","rep_month":"Oylik",
        "rep_range_custom":"📅 Sana bo‘yicha",
        "rep_range_start":"Boshlanish sanasini kiriting (YYYY-MM-DD).",
        "rep_range_end":"Tugash sanasini kiriting (YYYY-MM-DD).",
        "rep_range_invalid":"Sana formati noto‘g‘ri. Masalan: 2024-05-01",
        "rep_line":"{date} — {kind} — {cat} — {amount} {cur}",
        "rep_empty":"Bu bo‘lim uchun yozuv yo‘q.",

        "btn_cards":"💳 Kartalarim",
        "cards_header":"Kartalar ro‘yxati:",
        "cards_empty":"Hozircha karta qo‘shilmagan.",
        "cards_none":"Karta mavjud emas",
        "card_add_button":"➕ Karta qo‘shish",
        "cards_line":"{label}\nRaqam: {pan}\nEga: {owner}{default}",
        "cards_default_tag":" (asosiy)",
        "card_copy_pan":"📋 Raqamni nusxalash",
        "card_copy_owner":"📋 Egani nusxalash",
        "card_added":"Karta qo‘shildi ✅",
        "card_deleted":"Karta o‘chirildi ✅",
        "card_not_found":"Karta topilmadi.",
        "card_delete_btn":"🗑 O‘chirish",
        "card_add_usage":"Format: /add_card Nomi;8600 1234;Ega;default(0/1)",
        "card_del_usage":"Format: /del_card <id>",
        "card_access_denied":"Faqat admin uchun.",
        "card_add_ask_label":"Karta nomini kiriting:",
        "card_add_ask_number":"Karta raqamini kiriting:",
        "card_add_invalid_number":"Karta raqami 16 ta raqamdan iborat bo‘lishi kerak.",
        "card_add_ask_owner":"Karta egasini kiriting:",
        "CARDS_TITLE":"Kartalar:",
        "CARDS_EMPTY":"Karta mavjud emas",
        "CARDS_ADD_BUTTON":"➕ Karta qo‘shish",
        "CARDS_ASK_PAN":"Karta raqami (16 ta raqam)",
        "CARDS_INVALID_PAN":"Karta raqami noto‘g‘ri.",
        "CARDS_ASK_EXPIRES":"Amal qilish muddati (MM/YY)",
        "CARDS_INVALID_EXPIRES":"Amal qilish muddati noto‘g‘ri.",
        "CARDS_ASK_OWNER":"Ism familiyasi (kartadagi)",
        "CARDS_ASK_LABEL":"Karta nomi (masalan: Uzcard Ofis)",
        "CARDS_ADDED":"Karta saqlandi ✅",
        "CARDS_ADMIN_ONLY":"Bu amal faqat admin uchun.",
        "cards_menu_title":"Sizning kartalaringiz:",
        "cards_menu_empty":"Karta ro‘yxati hozircha mavjud emas.",
        "cards_prompt_label":"Karta nomini kiriting (masalan: ‘Asosiy’).",
        "cards_prompt_pan":"Karta raqamini kiriting (faqat raqamlar).",
        "cards_prompt_expires":"Amal qilish muddatini kiriting (MM/YY).",
        "cards_prompt_owner":"Karta egasining ism va familiyasini kiriting.",
        "cards_format_error":"❗ Noto‘g‘ri format. Qayta urinib ko‘ring.",
        "cards_saved":"✅ Karta saqlandi.",
        "SUB_OK":"1 oylik obuna faollashdi ✅",
        "SUB_PENDING":"To‘lov hali tasdiqlanmagan.",
        "SUB_MISSING":"Avval to‘lov yarating.",
        "DEBT_REMIND_TO_US":"📅 Bugun ({due}) {who} {amount} {cur} qaytarishi kerak. Nazorat qiling!",
        "DEBT_REMIND_BY_US":"📅 Bugun ({due}) siz {who} ga {amount} {cur} to‘lashingiz kerak. Unutmang!",
        "DEBT_REMIND_EVENING":"Eslatma: bugun muddati: {who} — {amount} {cur}",
        "bio_refresh_ok":"Bio yangilandi ✅",

        "debt_archive_btn":"🗂 Arxiv",
        "debt_archive_header":"🗂 Arxivdagi qarzlar:",
        "debt_archive_empty":"Arxiv bo‘sh.",
        "debt_archive_note":"📦 Arxivga o‘tgan sana: {date}",

        "start_gate_msg":"Iltimos, /start bosing",

        "debt_menu":"Qarz bo‘limi:",
        "debt_mine":"Qarzim","debt_given":"Qarzdorlar",
        "ask_due_mine":"Qachon <b>to‘laysiz</b>? Masalan: 25.09.2025, 25-09, ertaga…",
        "ask_due_given":"Qachon <b>qaytaradi</b>? Masalan: 25.09.2025, 25-09, ertaga…",
        "debt_saved_mine":"🧾 Qarz (Qarzim) qo‘shildi:\nKim: {who}\nSumma: {cur} {amount}\nTo‘lash sanasi: {due}",
        "debt_saved_given":"💸 Qarz (Qarzdor) qo‘shildi:\nKim: {who}\nSumma: {cur} {amount}\nQaytarish sanasi: {due}",
        "debt_need":"Qarz matnini tushunmadim. Ism va summani yozing.",
        "date_need":"Sanani tushunmadim. Masalan: 25.09.2025 yoki ertaga.",
        "card_debt":"— — —\n<b>QARZ</b>\nSana: {created}\nKim: {who}\nKategoriya: 💳 Qarzlar\nSumma: {cur} {amount}\nYo'nalish: {direction}\nBerilgan sana: {created}\nQaytadigan sana: {due}\nHolati: {status}",
        "debt_dir_mine":"Qarz olindi",
        "debt_dir_given":"Qarz berildi",
        "st_wait":"⏳ Kutilmoqda","st_paid":"✅ Tulangan","st_rcv":"✅ Qaytarilgan",
        "btn_paid":"✅ Tuladim","btn_rcv":"✅ Berildi",

        "sub_choose":(
            "⭐️ 1 oylik obunani tanlang va CLICK orqali to‘lovni amalga oshiring.\n\n"
            "To‘lov tugagach, “Obunani faollashtirish” tugmasini bosib, to‘lov qilingan kartaning oxirgi 4 raqamini yuboring.\n"
            "Obuna 10 daqiqagacha faollashadi va tasdiq xabari keladi."
        ),
        "sub_week":"1 haftalik obuna — 7 900 so‘m",
        "sub_month":"1 oylik obuna — 19 900 so‘m",
        "sub_created":(
            "To‘lov yaratildi.\n\n"
            "Reja: <b>{plan}</b>\nSumma: <b>{amount} so‘m</b>\n\n"
            "To‘lovni yakunlagach, “Obunani faollashtirish” tugmasi orqali to‘lov qilingan kartangizning oxirgi 4 raqamini yuboring."
        ),
        "sub_activated":"✅ Obuna faollashtirildi: {plan} (gacha {until})",
        "pay_click":"CLICK orqali to‘lash","pay_check":"To‘lovni tekshirish",
        "sub_manual_btn":"Obunani faollashtirish",
        "sub_manual_prompt":(
            "To‘lov qilingan kartangizning oxirgi 4 raqamini yuboring.\n"
            "Obuna 10 daqiqagacha ichida faollashadi va tasdiq xabarini olasiz."
        ),
        "pay_checking":"🔄 To‘lov holati tekshirilmoqda…","pay_notfound":"To‘lov topilmadi yoki tasdiqlanmagan.",
        "pay_status_paid":"✅ To‘lov tasdiqlandi: {plan}\nObuna {until} gacha faollashtirildi.",
        "pay_status_pending":"⏳ To‘lov hali tasdiqlanmadi. Birozdan so‘ng qayta tekshiring.",
        "pay_status_missing":"ℹ️ Avval to‘lov yarating.",
        "sub_ok":"1 oylik obuna faollashdi: {start} → {end}",
        "sub_remind_1d":"Obunangiz tugashiga 1 kun qoldi: {end}",
        "sub_expired":"Obunangiz muddati tugadi.",
        "sub_not_found_or_pending":"To‘lov topilmadi yoki hali tasdiqlanmagan.",
        "sub_pending_wait":"To‘lov hali tasdiqlanmagan. Iltimos, keyinroq qayta tekshiring.",
        "sub_create_first":"Avval to‘lov yarating.",
        "error_generic":"Xatolik yuz berdi.",

        "morning_ping":"🌅 Hayrli tong! Harajatlaringizni yozishni unutmang — MoliyaUz doim yoningizda.",
        "evening_ping":"🌙 Kun qanday o‘tdi? Harajatlaringizni yozishni unutmang. Atigi 15 soniya kifoya!",
        "daily":"🕗 Bugungi xarajatlaringizni yozdingizmi? 📝",
        "lang_again":"Tilni tanlang:","enter_text":"Matn yuboring.",

        "balance":(
            "💼 <b>Balans</b>\n\n"
            "Naqd: UZS <b>{cash_uzs}</b> | USD <b>{cash_usd}</b>\n"
            "Plastik: UZS <b>{card_uzs}</b> | USD <b>{card_usd}</b>\n\n"
            "Umumiy qarzdorlar (sizga qaytariladi): UZS <b>{they_uzs}</b> | USD <b>{they_usd}</b>\n"
            "Umumiy qarzlarim (siz to‘laysiz): UZS <b>{i_uzs}</b> | USD <b>{i_usd}</b>"
        ),
    }
    return D[k].format(**kw)

def t_ru(k, **kw):
    R = {
        "start_choose": "Здравствуйте, пожалуйста, выберите язык бота.",
        "ask_name": "Давайте знакомиться, как вас зовут?",
        "welcome":(
            "Добро пожаловать, {name}! 👋\n\n"
            "📊 MoliyaUz — ваш ассистент по личным финансам.\n"
            "— Понимает доходы/расходы из текста 💬\n"
            "— Автокатегоризация 🏷\n"
            "— Напоминает о сроках долгов ⏰\n\n"
            "Продолжая, вы соглашаетесь с нашей <b>офертой</b>.\n\n"
            "⏩ Для продолжения отправьте свой номер телефона:"
        ),
        "btn_share": "📱 Отправить номер телефона",
        "btn_oferta": "📄 Публичная оферта",

        "menu": "Главное меню:",
        "btn_hisobla": "🧮 Посчитать",
        "btn_hisobot": "📊 Отчет",
        "btn_qarz": "🚨 Долг",
        "btn_balance": "💼 Баланс",
        "btn_obuna": "⭐️ Подписка",
        "btn_back": "⬅️ Назад",
        "btn_analiz": "📊 Анализ",
        "btn_lang": "🌐 Сменить язык",

        "enter_tx": (
            "Напишите расход или доход. Например: "
            "<i>Кофе 15 тысяч</i>, <i>доход 1.2 млн зарплата</i>.\n"
            "Если напишете <b>в долг взял/дал</b>, спрошу срок."
        ),
        "tx_exp": "Добавлено ✅\n\nРасход:\nДата: {date}\n\nСумма: {cur} {amount}\nКатегория: {cat}\nКомментарий: {desc}",
        "tx_inc": "Добавлено ✅\n\nДоход:\nДата: {date}\n\nСумма: {cur} {amount}\nКатегория: 💪 Доход от труда\nКомментарий: {desc}",
        "need_sum": "Не понял сумму. Например: <i>такси 15 000</i>.",

        "report_main": "Какой отчет открыть?",
        "rep_tx": "📒 Доходы-расходы",
        "rep_debts": "💳 Долги",
        "rep_day": "Дневной", "rep_week": "Недельный", "rep_month": "Месячный",
        "rep_range_custom": "📅 По дате",
        "rep_range_start": "Введите начальную дату (YYYY-MM-DD).",
        "rep_range_end": "Введите конечную дату (YYYY-MM-DD).",
        "rep_range_invalid": "Неверный формат даты. Например: 2024-05-01",
        "rep_line": "{date} — {kind} — {cat} — {amount} {cur}",
        "rep_empty": "Пока нет записей для этого раздела.",

        "btn_cards": "💳 Мои карты",
        "cards_header": "Список карт:",
        "cards_empty": "Карты ещё не добавлены.",
        "cards_none": "Карта отсутствует",
        "card_add_button": "➕ Добавить карту",
        "cards_line": "{label}\nНомер: {pan}\nВладелец: {owner}{default}",
        "cards_default_tag": " (основная)",
        "card_copy_pan": "📋 Скопировать номер",
        "card_copy_owner": "📋 Скопировать владельца",
        "card_added": "Карта добавлена ✅",
        "card_deleted": "Карта удалена ✅",
        "card_not_found": "Карта не найдена.",
        "card_delete_btn": "🗑 Удалить",
        "card_add_usage": "Формат: /add_card Название;8600 1234;Владелец;default(0/1)",
        "card_del_usage": "Формат: /del_card <id>",
        "card_access_denied": "Только для админа.",
        "card_add_ask_label": "Введите название карты:",
        "card_add_ask_number": "Введите номер карты:",
        "card_add_invalid_number": "Номер карты должен содержать 16 цифр.",
        "card_add_ask_owner": "Введите владельца карты:",
        "CARDS_TITLE": "Карты:",
        "CARDS_EMPTY": "Карта отсутствует",
        "CARDS_ADD_BUTTON": "➕ Добавить карту",
        "CARDS_ASK_PAN": "Номер карты (16 цифр)",
        "CARDS_INVALID_PAN": "Неверный номер карты.",
        "CARDS_ASK_EXPIRES": "Срок действия (MM/YY)",
        "CARDS_INVALID_EXPIRES": "Неверный срок действия.",
        "CARDS_ASK_OWNER": "Имя и фамилия как на карте",
        "CARDS_ASK_LABEL": "Название карты (например: Uzcard Офис)",
        "CARDS_ADDED": "Карта сохранена ✅",
        "CARDS_ADMIN_ONLY": "Эта операция только для админа.",
        "cards_menu_title": "Ваши карты:",
        "cards_menu_empty": "Список карт пока пуст.",
        "cards_prompt_label": "Введите название карты (например: «Основная»).",
        "cards_prompt_pan": "Введите номер карты (только цифры).",
        "cards_prompt_expires": "Введите срок действия (MM/YY).",
        "cards_prompt_owner": "Введите имя и фамилию владельца карты.",
        "cards_format_error": "❗ Неверный формат. Попробуйте снова.",
        "cards_saved": "✅ Карта сохранена.",
        "SUB_OK": "1-месячная подписка активирована ✅",
        "SUB_PENDING": "Платеж ещё не подтвержден.",
        "SUB_MISSING": "Сначала создайте платеж.",
        "DEBT_REMIND_TO_US": "📅 Сегодня ({due}) {who} должен вернуть вам {amount} {cur}. Проверьте!",
        "DEBT_REMIND_BY_US": "📅 Сегодня ({due}) вы должны отдать {who} {amount} {cur}. Не забудьте!",
        "DEBT_REMIND_EVENING": "Напоминание: сегодня дедлайн: {who} — {amount} {cur}",
        "bio_refresh_ok": "Био обновлено ✅",

        "debt_archive_btn": "🗂 Архив",
        "debt_archive_header": "🗂 Архив долгов:",
        "debt_archive_empty": "Архив пуст.",
        "debt_archive_note": "📦 Дата архивирования: {date}",

        "start_gate_msg": "Пожалуйста, нажмите /start",

        "debt_menu": "Раздел долги:",
        "debt_mine": "Мой долг", "debt_given": "Должники",
        "ask_due_mine": "Когда <b>вернете</b>? Например: 25.09.2025, 25-09, завтра…",
        "ask_due_given": "Когда <b>он вернет</b>? Например: 25.09.2025, 25-09, завтра…",
        "debt_saved_mine": "🧾 Добавлен долг (я должен):\nКому: {who}\nСумма: {cur} {amount}\nДата возврата: {due}",
        "debt_saved_given": "💸 Добавлен должник:\nКто: {who}\nСумма: {cur} {amount}\nДата возврата: {due}",
        "debt_need": "Не понял долг. Укажите имя и сумму.",
        "date_need": "Не понял дату. Например: 25.09.2025 или завтра.",
        "card_debt": "— — —\n<b>ДОЛГ</b>\nСоздано: {created}\nКто/Кому: {who}\nКатегория: 💳 Долги\nСумма: {cur} {amount}\nНаправление: {direction}\nДата выдачи: {created}\nДата возврата: {due}\nСтатус: {status}",
        "debt_dir_mine": "Долг взяли",
        "debt_dir_given": "Долг выдали",
        "st_wait": "⏳ Ожидается", "st_paid": "✅ Оплачен", "st_rcv": "✅ Возвращен",
        "btn_paid": "✅ Оплатил", "btn_rcv": "✅ Вернул",

        "sub_choose":(
            "⭐️ Выберите месячную подписку и оплатите её через CLICK.\n\n"
            "После оплаты нажмите кнопку «Активировать подписку» и отправьте последние 4 цифры оплаченной карты.\n"
            "Подписка активируется в течение 10 минут, мы пришлём уведомление."
        ),
        "sub_week": "Подписка на 1 неделю — 7 900 сум",
        "sub_month": "Подписка на 1 месяц — 19 900 сум",
        "sub_created":(
            "Платеж создан.\n\n"
            "Тариф: <b>{plan}</b>\nСумма: <b>{amount} сум</b>\n\n"
            "После оплаты воспользуйтесь кнопкой «Активировать подписку» и отправьте последние 4 цифры оплаченной карты."
        ),
        "sub_activated": "✅ Подписка активирована: {plan} (до {until})",
        "pay_click": "Оплатить в CLICK", "pay_check": "Проверить платеж",
        "sub_manual_btn": "Активировать подписку",
        "sub_manual_prompt":(
            "Отправьте последние 4 цифры оплаченной карты.\n"
            "Подписка активируется в течение 10 минут, и мы уведомим вас о подтверждении."
        ),
        "pay_checking": "🔄 Проверяем статус платежа…", "pay_notfound": "Платеж не найден или не подтвержден.",
        "pay_status_paid": "✅ Платеж подтвержден: {plan}\nПодписка активна до {until}.",
        "pay_status_pending": "⏳ Платеж ещё не подтвержден. Попробуйте снова чуть позже.",
        "pay_status_missing": "ℹ️ Сначала создайте платеж.",
        "sub_ok": "Подписка на 1 месяц активирована: {start} → {end}",
        "sub_remind_1d": "До окончания подписки остался 1 день: {end}",
        "sub_expired": "Срок подписки истек.",
        "sub_not_found_or_pending": "Платеж не найден или не подтвержден.",
        "sub_pending_wait": "Платеж ещё не подтвержден. Попробуйте позже.",
        "error_generic": "Произошла ошибка.",
        "sub_create_first": "Сначала создайте платеж.",

        "morning_ping": "🌅 Доброе утро! Не забудьте записать расходы — MoliyaUz рядом.",
        "evening_ping": "🌙 Как прошёл день? Не забудьте внести траты. Всего 15 секунд!",
        "daily": "🕗 Вы сегодня записали расходы? 📝",
        "lang_again": "Выберите язык:",
        "enter_text": "Отправьте текст.",

        "balance": (
            "💼 <b>Баланс</b>\n\n"
            "Наличные: UZS <b>{cash_uzs}</b> | USD <b>{cash_usd}</b>\n"
            "Карта: UZS <b>{card_uzs}</b> | USD <b>{card_usd}</b>\n\n"
            "Должны вам: UZS <b>{they_uzs}</b> | USD <b>{they_usd}</b>\n"
            "Ваши долги: UZS <b>{i_uzs}</b> | USD <b>{i_usd}</b>"
        ),
    }
    return R.get(k, t_uz(k, **kw)).format(**kw)

def get_lang(uid:int)->str: return USER_LANG.get(uid,"uz")
def L(lang: str):
    return t_uz if lang=="uz" else t_ru

# ====== KB ======
def kb_lang():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🇺🇿 O‘zbek"),KeyboardButton(text="🇷🇺 Русский")]],
        resize_keyboard=True,one_time_keyboard=True
    )

def kb_share(lang="uz"):
    T=L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T("btn_share"),request_contact=True)]],
        resize_keyboard=True,one_time_keyboard=True
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logging.getLogger("aiosqlite").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def get_main_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    keyboard = [
        [KeyboardButton(text=T("btn_hisobla"))],
        [KeyboardButton(text=T("btn_hisobot")), KeyboardButton(text=T("btn_qarz"))],
        [KeyboardButton(text=T("btn_balance")), KeyboardButton(text=T("btn_obuna"))],
        [KeyboardButton(text=T("btn_analiz")), KeyboardButton(text=T("btn_cards"))],
        [KeyboardButton(text=T("btn_lang"))],
    ]

    menu = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )

    try:
        logger.debug(
            "main_menu_rendered",
            extra={
                "rows": [[button.text for button in row] for row in menu.keyboard]
            },
        )
    except Exception:
        logger.debug("main_menu_rendered")
    return menu


def kb_cards_menu(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("CARDS_ADD_BUTTON"))],
            [KeyboardButton(text=T("btn_back"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def kb_input_entry(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T("btn_back"))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def kb_card_cancel(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T("btn_back"))]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


CARD_MENU_TEXTS = {"💳 Kartalarim", "Kartalarim", "💳 Мои карты", "Мои карты"}
CARD_ADD_TEXTS = {"➕ Karta qo‘shish", "➕ Добавить карту", "Добавить карту"}
CARD_CANCEL_TEXTS = {"ortga", "назад"}


def _format_pan_display(pan: str) -> str:
    if not pan:
        return "----"
    digits = re.sub(r"\D", "", pan)
    if 13 <= len(digits) <= 19:
        return " ".join(digits[i:i+4] for i in range(0, len(digits), 4))
    return pan


async def show_cards_overview(message: Message, lang: str) -> None:
    uid = message.from_user.id
    cards = get_cards(uid)
    T = L(lang)
    if not cards:
        await message.answer(T("cards_menu_empty"), reply_markup=kb_cards_menu(lang))
        return
    lines = [T("cards_menu_title")]
    for card in cards:
        label = (card.get("label") or "—").strip() or "—"
        pan_value = card.get("pan") or card.get("pan_last4") or ""
        pan_display = _format_pan_display(pan_value)
        expires = (card.get("expires") or "—").strip() or "—"
        owner = (card.get("owner") or "—").strip() or "—"
        lines.append(f"{label} — {pan_display} — {expires} — {owner}")
    await message.answer("\n".join(lines), reply_markup=kb_cards_menu(lang))


async def enter_cards_menu(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id
    lang = get_lang(uid)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, message.answer)
        await message.answer(block_text(uid), reply_markup=get_main_menu(lang))
        return
    await state.clear()
    nav_push(uid, "cards_menu")
    await show_cards_overview(message, lang)


def _is_cancel(text: Optional[str], lang: str) -> bool:
    if not text:
        return False
    value = text.strip().lower()
    if value in CARD_CANCEL_TEXTS:
        return True
    back = L(lang)("btn_back").strip().lower()
    return value == back


def kb_debt_menu_reply(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("debt_mine"))],
            [KeyboardButton(text=T("debt_given"))],
            [KeyboardButton(text=T("debt_archive_btn"))],
            [KeyboardButton(text=T("btn_back"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def kb_sub_menu_reply(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("sub_month"))],
            [KeyboardButton(text=T("sub_manual_btn"))],
            [KeyboardButton(text=T("btn_back"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

def kb_oferta(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("btn_oferta"), url=NOTION_OFER_URL)]
    ])

def kb_rep_main(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("rep_tx"))],
            [KeyboardButton(text=T("rep_debts"))],
            [KeyboardButton(text=T("btn_back"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def kb_rep_range(lang: str = "uz") -> ReplyKeyboardMarkup:
    T = L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("rep_day"))],
            [KeyboardButton(text=T("rep_week"))],
            [KeyboardButton(text=T("rep_month"))],
            [KeyboardButton(text=T("rep_range_custom"))],
            [KeyboardButton(text=T("btn_back"))],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

def kb_debt_menu(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("debt_mine"),callback_data="debt:mine")],
        [InlineKeyboardButton(text=T("debt_given"),callback_data="debt:given")],
        [InlineKeyboardButton(text=T("debt_archive_btn"),callback_data="debt:archive")]
    ])

def kb_debt_done(direction,debt_id, lang="uz"):
    T=L(lang)
    lab=T("btn_paid") if direction=="mine" else T("btn_rcv")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lab,callback_data=f"debtdone:{direction}:{debt_id}")]
    ])

def kb_sub(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("sub_month"),callback_data="sub:month")]
    ])

def kb_payment(pid, pay_url, lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("pay_click"), url=pay_url)],
    ])


def kb_payment_with_miniapp(pid: str, pay_url: str, lang: str, mini_url: str) -> InlineKeyboardMarkup:
    T = L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 CLICK (Mini App)", web_app=WebAppInfo(url=mini_url))],
        [InlineKeyboardButton(text=T("pay_click"), url=pay_url)],
    ])


def kb_tx_cancel(tx_id: int, lang: str) -> InlineKeyboardMarkup:
    text = "❌ Bekor qilish" if lang == "uz" else "❌ Отменить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=f"txcancel:{tx_id}")]
        ]
    )


async def show_navigation_state(uid: int, lang: str, state: str, message: Message) -> None:
    T = L(lang)
    if state == "main":
        nav_reset(uid)
        STEP[uid] = "main"
        await message.answer(T("menu"), reply_markup=get_main_menu(lang))
        return

    if state == "report_main":
        STEP[uid] = "main"
        await message.answer(T("report_main"), reply_markup=kb_rep_main(lang))
        return

    if state == "debt_menu":
        STEP[uid] = "main"
        await message.answer(T("debt_menu"), reply_markup=kb_debt_menu_reply(lang))
        return

    if state == "sub_menu":
        STEP[uid] = "main"
        await message.answer(T("sub_choose"), reply_markup=kb_sub_menu_reply(lang))
        return

    if state == "cards_menu":
        STEP[uid] = "main"
        await show_cards_overview(message, lang)
        return

    if state == "input_tx":
        STEP[uid] = "input_tx"
        await message.answer(T("enter_tx"), reply_markup=kb_input_entry(lang))
        return

    STEP[uid] = "main"
    await message.answer(T("menu"), reply_markup=get_main_menu(lang))


async def handle_back_button(m: Message, uid: int, lang: str) -> None:
    PENDING_DEBT.pop(uid, None)
    REPORT_RANGE_STATE.pop(uid, None)
    STEP[uid] = "main"
    state = nav_back(uid)
    await show_navigation_state(uid, lang, state, m)

# ====== PARSERLAR ======
MULTI_AMOUNT_TOKEN_RE = re.compile(
    r"(?:(?:\d+[.,]?\d*)\s*(?:mln|million|млн|ming|min|тыс|k)\b|\b\d+\b)",
    re.IGNORECASE,
)
MULTI_LETTER_RE = re.compile(r"[A-Za-z\u0400-\u04FF]")
DUE_WORD_PREFIX_RE = re.compile(
    r"^(?P<due>(?:bugunlik|bugun|ertalikka|ertaga|indin|today|tomorrow|сегодня|завтра|послезавтра))\b[\s,.:;-]*",
    re.IGNORECASE,
)
DUE_DATE_PREFIX_RE = re.compile(
    r"^(?P<due>(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}-\d{2}-\d{2}))\b[\s,.:;-]*",
)


def split_tx_entries(text: str) -> list[str]:
    """Split text into transaction-sized chunks using detected amount tokens."""
    raw = (text or "").strip()
    if not raw:
        return [raw]

    matches = list(MULTI_AMOUNT_TOKEN_RE.finditer(raw))
    if len(matches) <= 1:
        return [raw]

    segments: list[str] = []
    segment_start = 0
    prev_end = matches[0].end()
    for match in matches[1:]:
        between = raw[prev_end:match.start()]
        has_words = bool(MULTI_LETTER_RE.search(between))
        has_split_char = any(ch in between for ch in ("\n", ",", ";", "/", "|"))
        if has_words or has_split_char:
            segment = raw[segment_start:prev_end].strip()
            if segment:
                segments.append(segment)
            segment_start = prev_end
        prev_end = match.end()

    last_segment = raw[segment_start:prev_end].strip()
    if last_segment:
        segments.append(last_segment)

    tail = raw[prev_end:].strip()
    if tail:
        if segments:
            segments[-1] = f"{segments[-1]} {tail}".strip()
        else:
            segments.append(tail)

    return segments or [raw]


def parse_amount(text:str)->Optional[int]:
    t=(text or "").lower().replace("’","'").strip()
    m=re.search(r"\b(\d+[.,]?\d*)\s*(mln|million|млн)\b",t)
    if m: return int(float(m.group(1).replace(",", "."))*1_000_000)
    m=re.search(r"\b(\d+[.,]?\d*)\s*(ming|min|тыс|k)\b",t)
    if m: return int(float(m.group(1).replace(",", "."))*1_000)
    m=re.search(r"\b(\d+)\s*k\b",t)
    if m: return int(m.group(1))*1000
    m=re.search(r"\b(\d[\d\s,\.]{0,15})\b",t)
    if m:
        raw=m.group(1).replace(" ","")
        raw=raw.replace(",", "") if raw.count(",")<=1 and raw.count(".")<=1 else raw.replace(",","").replace(".","")
        try: return int(float(raw))
        except: return None
    return None

def detect_currency(text:str)->str:
    t=(text or "").lower()
    if "$" in t or "usd" in t or "dollar" in t or "доллар" in t: return "USD"
    if "eur" in t or "€" in t: return "EUR"
    if any(w in t for w in ["uzs","so'm","so‘m","som","сум","soum"]): return "UZS"
    return "UZS"

def detect_account(text:str)->str:
    t=(text or "").lower()
    if any(w in t for w in ["karta","plastik","card","visa","master","uzcard","humo","bank"]): return "card"
    if any(w in t for w in ["naqd","cash","qo'lda","qolda","qo‘l","qol"]): return "cash"
    return "cash"

def guess_kind(text:str)->str:
    t=(text or "").lower()
    if "qarz berdim" in t or "qarzga berdim" in t or "qarz ber" in t: return "debt_given"
    if "qarz oldim" in t or "qarzga oldim" in t or "qarz ol" in t: return "debt_mine"
    if "avans" in t:
        if any(w in t for w in ["oldim","olindi","keldi","tushdi","berishdi","berildi"]):
            return "income"
        if any(w in t for w in ["to'ladim","tuladim","toladim","berdim","qaytardim","qaytardik"]):
            return "expense"
    if "sotib oldim" in t or "сотиб олдим" in t or "kiyim oldim" in t: return "expense"
    expense_hints = [
        "chiqim","xarajat","taksi","benzin","ovqat","kafe","restoran","market","kommunal","internet","telefon","ijara","arenda",
        "kiyim","kiyim","dress","oyoq kiyim","botinka","sumka","shop","magazin","bozor","dorixona","dori","apteka"
    ]
    if any(w in t for w in expense_hints):
        return "expense"
    income_hints = [
        "kirim","кирим","oylik","maosh","маош","keldi","tushdi","келди","тушди","stipendiya","premiya","bonus","dividend"
    ]
    if any(w in t for w in income_hints):
        return "income"
    if "oldim" in t and any(w in t for w in ["pul","oylik","maosh","bonus","premiya"]):
        return "income"
    if "oldim" in t:
        return "expense"
    if t.strip().startswith("+"): return "income"
    if t.strip().startswith("-"): return "expense"
    return "expense"

MONTHS_UZ={"yanvar":1,"fevral":2,"mart":3,"aprel":4,"may":5,"iyun":6,"iyul":7,"avgust":8,"sentabr":9,"sentyabr":9,"oktabr":10,"noyabr":11,"dekabr":12}
def parse_due_date(text:str)->Optional[str]:
    t=(text or "").lower().replace("–","-")
    if "ertaga" in t or "завтра" in t: return (now_tk().date()+timedelta(days=1)).strftime("%d.%m.%Y")
    if "bugun" in t or "сегодня" in t: return (now_tk().date()).strftime("%d.%m.%Y")
    m=re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b",t)
    if m:
        dd,mm,yy=int(m.group(1)),int(m.group(2)),m.group(3)
        year=(int(yy)+2000 if yy and int(yy)<100 else int(yy)) if yy else now_tk().year
        try: return datetime(year,mm,dd,tzinfo=TASHKENT).strftime("%d.%m.%Y")
        except: return None
    for name,num in MONTHS_UZ.items():
        m2=re.search(rf"\b(\d{{1,2}})\s*[- ]\s*{name}\b",t)
        if m2:
            dd=int(m2.group(1))
            try: return datetime(now_tk().year,num,dd,tzinfo=TASHKENT).strftime("%d.%m.%Y")
            except: return None
    return None

def parse_counterparty(text:str)->str:
    t=(text or "").lower()
    m=re.search(r"\b([a-zA-Z\u0400-\u04FF‘'ʼ`-]+)dan\b",t)
    if m: return m.group(1).replace("‘","'").replace("ʼ","'").capitalize()
    m=re.search(r"\b([a-zA-Z\u0400-\u04FF‘'ʼ`-]+)(?:\s+(akaga|opaga|ukaga|singlimga|брате|сестре))?\s*(ga|qa|га|ке)\b",t)
    if m:
        base=m.group(1).replace("‘","'").replace("ʼ","'").capitalize()
        suf=(" "+m.group(2)) if m.group(2) else ""
        return (base+suf).strip().capitalize()
    return "—"

# ====== SAVE ======
def next_debt_id(uid:int)->int:
    MEM_DEBTS_SEQ[uid]=MEM_DEBTS_SEQ.get(uid,0)+1
    return MEM_DEBTS_SEQ[uid]

async def save_tx(uid:int, kind:str, amount:int, currency:str, account:str, category:str, desc:str):
    await ensure_month_rollover()
    tx_list = MEM_TX.setdefault(uid, [])
    tx_id = MEM_TX_SEQ.get(uid, 0) + 1
    MEM_TX_SEQ[uid] = tx_id
    tx = {
        "id": tx_id,
        "ts": now_tk(),
        "kind": kind,
        "amount": amount,
        "currency": currency,
        "account": account,
        "category": category,
        "desc": desc,
    }
    tx_list.append(tx)
    update_analysis_counters(uid, kind, amount, currency)
    return tx

async def save_debt(uid:int, direction:str, amount:int, currency:str, counterparty:str, due:str)->int:
    await ensure_month_rollover()
    did=next_debt_id(uid)
    MEM_DEBTS.setdefault(uid,[]).append({
        "id":did, "ts":now_tk(), "direction":direction, "amount":amount,
        "currency":currency, "counterparty":counterparty, "due":due, "status":"wait"
    })
    return did

def debt_card(it:dict, lang="uz")->str:
    T=L(lang)
    s={"wait":T("st_wait"),"paid":T("st_paid"),"received":T("st_rcv")}[it.get("status","wait")]
    direction_key = "debt_dir_given" if it.get("direction") == "given" else "debt_dir_mine"
    dir_label = T(direction_key)
    return T("card_debt", created=fmt_date(it["ts"]), who=it["counterparty"], cur=it.get("currency","UZS"),
             amount=fmt_amount(it["amount"]), due=it["due"], status=s, direction=dir_label)

# ====== REPORT HELPERS ======
def report_range(kind:str):
    n=now_tk()
    if kind=="day": return n.replace(hour=0,minute=0,second=0,microsecond=0), n
    if kind=="week": return n-timedelta(days=7), n
    return n-timedelta(days=30), n


def parse_report_range_date(text:str)->Optional[datetime]:
    raw=(text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt=datetime.strptime(raw, fmt)
            return dt
        except Exception:
            continue
    return None


class StartGateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        await ensure_month_rollover()
        user_id=None
        if isinstance(event, Message):
            if not event.from_user:
                return await handler(event, data)
            user_id=event.from_user.id
            USER_ACTIVATED.setdefault(user_id, False)
            if USER_ACTIVATED.get(user_id):
                return await handler(event, data)
            text=event.text or ""
            if text.startswith("/start"):
                return await handler(event, data)
            lang=get_lang(user_id)
            T=L(lang)
            await event.answer(T("start_gate_msg"))
            return
        if isinstance(event, CallbackQuery):
            if not event.from_user:
                return await handler(event, data)
            user_id=event.from_user.id
            USER_ACTIVATED.setdefault(user_id, False)
            if USER_ACTIVATED.get(user_id):
                return await handler(event, data)
            lang=get_lang(user_id)
            T=L(lang)
            if event.message:
                await event.message.answer(T("start_gate_msg"))
            await event.answer()
            return
        return await handler(event, data)

# ====== ANALIZ HELPERS ======
def month_period():
    n = now_tk()
    start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, n

def to_uzs(amount:int, currency:str)->int:
    if currency == "USD":
        return int(round(amount * USD_UZS))
    return int(amount)

# ====== HANDLERS ======
@rt.message(CommandStart())
async def start(m:Message):
    uid=m.from_user.id
    USER_ACTIVATED[uid] = True
    SEEN_USERS.add(uid)
    TRIAL_START.setdefault(uid, now_tk())
    profile = USERS_PROFILE_CACHE.get(uid)
    lang_pref = None
    if isinstance(profile, dict):
        lang_pref = profile.get("lang")
    if lang_pref:
        USER_LANG[uid] = lang_pref
    await ensure_month_rollover()
    nav_reset(uid)
    payload = ""
    if m.text:
        parts = m.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()
    if await handle_paid_deeplink(uid, payload, m):
        return
    if profile:
        lang = lang_pref or get_lang(uid)
        USER_LANG[uid] = lang
        STEP[uid] = "main"
        await ensure_subscription_state(uid)
        await m.answer(L(lang)("menu"), reply_markup=get_main_menu(lang))
    else:
        STEP[uid]="lang"
        await m.answer(t_uz("start_choose"), reply_markup=kb_lang())

        # Debugging helper: log chat type and ID for group setup
        logger.info(
            "start-command chat-info type=%s id=%s title=%s",
            m.chat.type,
            m.chat.id,
            getattr(m.chat, "title", ""),
        )

@rt.message(Command("menu"))
async def menu_cmd(m: Message):
    uid = m.from_user.id
    await ensure_month_rollover()
    nav_reset(uid)
    await m.answer((t_uz if get_lang(uid)=="uz" else t_ru)("menu"), reply_markup=get_main_menu(get_lang(uid)))

@rt.message(Command("approve"))
async def approve_cmd(m:Message):
    if ADMIN_ID and m.from_user.id!=ADMIN_ID: return
    await ensure_month_rollover()
    parts=m.text.strip().split()
    if len(parts)!=2: await m.answer("format: /approve <pid>"); return
    pid=parts[1]
    pay=PENDING_PAYMENTS.get(pid)
    if not pay: await m.answer("pid topilmadi"); return
    pay["status"]="paid"
    await m.answer(f"{pid} -> paid")


@rt.message(Command("refresh_bio"))
async def refresh_bio_cmd(m:Message):
    if ADMIN_ID and m.from_user.id!=ADMIN_ID:
        return
    await ensure_month_rollover()
    total_users = sum(1 for _, flag in USER_ACTIVATED.items() if flag) or len(SEEN_USERS)
    await update_bot_bio(total_users)
    lang=get_lang(m.from_user.id)
    T=L(lang)
    await m.answer(T("bio_refresh_ok"))


@rt.message(F.text.in_({"📊 Analiz", "Analiz"}))
async def analiz_button_handler(m: Message):
    uid = m.from_user.id
    lang = get_lang(uid)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, m.answer)
        await m.answer(block_text(uid), reply_markup=get_main_menu(lang))
        return
    await analiz_cmd(m)


@rt.message(F.text)
async def on_text(m:Message):
    uid=m.from_user.id
    t=(m.text or "").strip()
    step=STEP.get(uid)

    lang = get_lang(uid)
    T = L(lang)
    try:
        await ensure_month_rollover()
        await ensure_subscription_state(uid)

        if PENDING_MANUAL_DIGITS.get(uid):
            # Waiting for manual activation digits; let subscription router handle input
            return

        if t == T("btn_back"):
            await handle_back_button(m, uid, lang)
            return

        if step is None and not USER_ACTIVATED.get(uid):
            USER_ACTIVATED.setdefault(uid, False)

        if step=="report_range_start":
            parsed=parse_report_range_date(t)
            if not parsed:
                await m.answer(T("rep_range_invalid")); return
            REPORT_RANGE_STATE[uid]={"start": parsed.strftime("%Y-%m-%d")}
            STEP[uid]="report_range_end"
            await m.answer(T("rep_range_end")); return

        if step=="report_range_end":
            parsed=parse_report_range_date(t)
            if not parsed:
                await m.answer(T("rep_range_invalid")); return
            payload=REPORT_RANGE_STATE.get(uid, {})
            start_str=payload.get("start")
            if not start_str:
                STEP[uid]="main"
                await m.answer(T("menu"), reply_markup=get_main_menu(lang)); return
            start_dt=datetime.strptime(start_str, "%Y-%m-%d")
            end_dt=parsed
            if end_dt < start_dt:
                start_dt, end_dt = end_dt, start_dt
            since=datetime(start_dt.year,start_dt.month,start_dt.day,tzinfo=TASHKENT)
            until=datetime(end_dt.year,end_dt.month,end_dt.day,23,59,59,tzinfo=TASHKENT)
            items=[it for it in MEM_TX.get(uid,[]) if since<=it["ts"]<=until]
            if not items:
                await m.answer(T("rep_empty"))
            else:
                lines=[]
                for it in items:
                    lines.append(T("rep_line",date=fmt_date(it["ts"]),kind=("Kirim" if it["kind"]=="income" else ("Расход" if lang=="ru" else "Chiqim")),cat=it["category"],amount=fmt_amount(it["amount"]),cur=it["currency"]))
                await m.answer("\n".join(lines))
            REPORT_RANGE_STATE.pop(uid, None)
            STEP[uid]="main"
            await m.answer(T("menu"), reply_markup=get_main_menu(lang)); return

        if step=="lang":
            low=t.lower()
            if "uz" in low or "o‘z" in low or "o'z" in low: USER_LANG[uid]="uz"
            elif "рус" in low or "ru" in low: USER_LANG[uid]="ru"
            else: return
            lang = get_lang(uid)
            existing_raw = USERS_PROFILE_CACHE.get(uid)
            existing = existing_raw if isinstance(existing_raw, dict) else {}
            raw_name = existing.get("name")
            if not raw_name:
                raw_name = (m.from_user.full_name or m.from_user.first_name or m.from_user.username or "") if m.from_user else ""
            fallback_name = raw_name or ("do‘stim" if lang == "uz" else "друг")
            update_user_profile(uid, lang=lang, name=raw_name or None)
            STEP[uid]="need_phone"
            await m.answer(L(lang)("welcome", name=fallback_name), reply_markup=kb_share(lang))
            await m.answer("—", reply_markup=kb_oferta(lang)); return

        if step=="name":
            lang=get_lang(uid); T=L(lang)
            clean_name = t.strip()
            if clean_name:
                update_user_profile(uid, name=clean_name, lang=lang)
            display_name = clean_name or (m.from_user.full_name if m.from_user else "") or ("do‘stim" if lang=="uz" else "друг")
            await m.answer(T("welcome", name=display_name), reply_markup=kb_share(lang))
            await m.answer("—", reply_markup=kb_oferta(lang))
            STEP[uid]="need_phone"; return

        if step=="need_phone": return

        if step in ("debt_mine_due","debt_given_due"):
            due=parse_due_date(t)
            if not due: await m.answer(T("date_need")); return
            tmp=PENDING_DEBT.get(uid)
            if not tmp:
                STEP[uid]="main"; await m.answer(T("enter_tx"), reply_markup=kb_input_entry(lang)); return

            did = await save_debt(uid, tmp["direction"], tmp["amount"], tmp["currency"], tmp["who"], due)

            # Debt create moment -> balansga darhol ta'sir
            if tmp["direction"]=="given":
                await save_tx(uid,"expense",tmp["amount"],tmp["currency"],"cash","💳 Qarz berildi","")
            else:
                await save_tx(uid,"income",tmp["amount"],tmp["currency"],"cash","💳 Qarz olindi","")

            if tmp["direction"]=="mine":
                await m.answer(T("debt_saved_mine", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
            else:
                await m.answer(T("debt_saved_given", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
            PENDING_DEBT.pop(uid, None); STEP[uid]="main"; return

        # menyular
        if t==T("btn_hisobla"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
            nav_push(uid, "input_tx")
            STEP[uid]="input_tx"; await m.answer(T("enter_tx"), reply_markup=kb_input_entry(lang)); return

        if t==T("btn_hisobot"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
            nav_push(uid, "report_main")
            await m.answer(T("report_main"), reply_markup=kb_rep_main(lang)); return

        if t==T("btn_qarz"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
            STEP[uid]="main"
            nav_push(uid, "debt_menu")
            await m.answer(T("debt_menu"), reply_markup=kb_debt_menu_reply(lang)); return

        if t==T("btn_balance"):
            await send_balance(uid, m); return

        if t==T("btn_obuna"):
            STEP[uid]="main"
            nav_push(uid, "sub_menu")
            await m.answer(T("sub_choose"), reply_markup=kb_sub_menu_reply(lang)); return

        if t==T("btn_analiz"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=get_main_menu(lang))
                return
            await analiz_cmd(m); return

        if t==T("btn_cards"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=get_main_menu(lang))
                return
            STEP[uid]="main"
            nav_push(uid, "cards_menu")
            await show_cards_overview(m, lang); return

        if t==T("btn_lang"):
            STEP[uid]="lang"; await m.answer(T("lang_again"), reply_markup=kb_lang()); return

        if t==T("rep_tx"):
            nav_push(uid, "report_range")
            await m.answer(T("report_main"), reply_markup=kb_rep_range(lang)); return

        if t==T("rep_debts"):
            nav_push(uid, "report_debts")
            debts=list(reversed(MEM_DEBTS.get(uid,[])))[:10]
            if not debts:
                await m.answer(T("rep_empty"), reply_markup=kb_rep_main(lang)); return
            for it in debts:
                txt=debt_card(it, lang)
                if it["status"]=="wait":
                    await m.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"], lang))
                else:
                    await m.answer(txt)
            return

        if t in {T("rep_day"), T("rep_week"), T("rep_month"), T("rep_range_custom")}:  # type: ignore[arg-type]
            if t == T("rep_range_custom"):
                STEP[uid] = "report_range_start"
                REPORT_RANGE_STATE.pop(uid, None)
                await m.answer(T("rep_range_start"), reply_markup=kb_input_entry(lang))
                return

            kind_map = {
                T("rep_day"): "day",
                T("rep_week"): "week",
                T("rep_month"): "month",
            }
            kind_key = kind_map.get(t)
            if not kind_key:
                await m.answer(T("error_generic"), reply_markup=kb_rep_main(lang)); return
            since, until = report_range(kind_key)
            items=[it for it in MEM_TX.get(uid,[]) if since<=it["ts"]<=until]
            if not items:
                await m.answer(T("rep_empty"), reply_markup=kb_rep_main(lang)); return
            lines=[]
            for it in items:
                lines.append(
                    T(
                        "rep_line",
                        date=fmt_date(it["ts"]),
                        kind=(
                            "Kirim"
                            if it["kind"]=="income"
                            else ("Расход" if lang=="ru" else "Chiqim")
                        ),
                        cat=it["category"],
                        amount=fmt_amount(it["amount"]),
                        cur=it["currency"],
                    )
                )
            await m.answer("\n".join(lines), reply_markup=kb_rep_main(lang))
            return

        if t==T("debt_mine"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang))
                return
            await send_debt_direction(uid, lang, "mine", m.answer, reply_markup=kb_debt_menu_reply(lang))
            return

        if t==T("debt_given"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang))
                return
            await send_debt_direction(uid, lang, "given", m.answer, reply_markup=kb_debt_menu_reply(lang))
            return

        if t==T("debt_archive_btn"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang))
                return
            await send_debt_archive_list(uid, lang, m.answer, reply_markup=kb_debt_menu_reply(lang))
            return

        if t==T("sub_month"):
            PENDING_MANUAL_DIGITS.pop(uid, None)
            await send_subscription_invoice_message(uid, lang, "month", m)
            return

        if t==T("sub_manual_btn"):
            record = await payments_get_latest_payment(uid)
            status = (record.get("status") or "").lower() if record else ""
            if not record or status != "pending":
                await send_subscription_invoice_message(uid, lang, "month", m)
                return

            invoice_id = record.get("invoice_id")
            entry = PENDING_MANUAL_DIGITS.setdefault(uid, {})
            entry["invoice_id"] = invoice_id
            await m.answer(T("sub_manual_prompt"))
            return

        if t==T("pay_check"):
            paid = await process_paycheck(uid, lang, m.answer, kb_sub_menu_reply(lang))
            if paid:
                nav_reset(uid)
                STEP[uid] = "main"
                await m.answer(T("menu"), reply_markup=get_main_menu(lang))
            return

        if step=="input_tx" and not has_access(uid):
            await send_expired_notice(uid, lang, m.answer)
            await m.answer(block_text(uid), reply_markup=kb_sub(lang))
            return

        kind=guess_kind(t)

        async def handle_debt(entry_text: str, kind_label: str) -> bool:
            entry = entry_text.strip()
            amount_val = parse_amount(entry) or 0
            if amount_val <= 0:
                await m.answer(T("debt_need"))
                return False
            curr_val = detect_currency(entry)
            who_val = parse_counterparty(entry)
            due_val = parse_due_date(entry)

            if due_val:
                await save_debt(uid, "mine" if kind_label == "debt_mine" else "given", amount_val, curr_val, who_val, due_val)

                # Debt create moment -> balansga darhol ta'sir
                if kind_label == "debt_mine":
                    await save_tx(uid, "income", amount_val, curr_val, "cash", "💳 Qarz olindi", "")
                    await m.answer(T("debt_saved_mine", who=who_val, cur=curr_val, amount=fmt_amount(amount_val), due=due_val))
                else:
                    await save_tx(uid, "expense", amount_val, curr_val, "cash", "💳 Qarz berildi", "")
                    await m.answer(T("debt_saved_given", who=who_val, cur=curr_val, amount=fmt_amount(amount_val), due=due_val))
                return True

            PENDING_DEBT[uid] = {
                "direction": "mine" if kind_label == "debt_mine" else "given",
                "amount": amount_val,
                "currency": curr_val,
                "who": who_val,
            }
            STEP[uid] = "debt_mine_due" if kind_label == "debt_mine" else "debt_given_due"
            await m.answer(T("ask_due_mine") if kind_label == "debt_mine" else T("ask_due_given"))
            return False

        async def handle_basic_entry(entry_text: str, pre_kind: Optional[str] = None) -> bool:
            entry = entry_text.strip()
            if not entry:
                return False

            entry_kind = pre_kind or guess_kind(entry)
            if entry_kind in ("debt_mine", "debt_given"):
                await m.answer(T("need_sum"))
                return False

            amount_val = parse_amount(entry)
            if amount_val is None:
                await m.answer(T("need_sum"))
                return False

            curr_val = detect_currency(entry)
            acc_val = detect_account(entry)

            if entry_kind == "income":
                title = "💪 Mehnat daromadlari" if lang == "uz" else "💪 Доход от труда"
                tx_saved = await save_tx(uid, "income", amount_val, curr_val, acc_val, title, entry)
                await m.answer(
                    T(
                        "tx_inc",
                        date=fmt_date(now_tk()),
                        cur=curr_val,
                        amount=fmt_amount(amount_val),
                        desc=entry,
                    ),
                    reply_markup=kb_tx_cancel(tx_saved["id"], lang),
                )
            else:
                cat_val = guess_category(entry)
                tx_saved = await save_tx(uid, "expense", amount_val, curr_val, acc_val, cat_val, entry)
                await m.answer(
                    T(
                        "tx_exp",
                        date=fmt_date(now_tk()),
                        cur=curr_val,
                        amount=fmt_amount(amount_val),
                        cat=cat_val,
                        desc=entry,
                    ),
                    reply_markup=kb_tx_cancel(tx_saved["id"], lang),
                )

            return True

        def extract_due_prefix(text: str) -> tuple[str, str]:
            raw = (text or "").lstrip()
            if not raw:
                return "", text
            for pattern in (DUE_WORD_PREFIX_RE, DUE_DATE_PREFIX_RE):
                m_pref = pattern.match(raw)
                if not m_pref:
                    continue
                candidate = (m_pref.group("due") or "").strip(" ,.;:-")
                if not candidate:
                    continue
                if parse_due_date(candidate) is None:
                    continue
                rest = raw[m_pref.end():].lstrip()
                return candidate, rest
            return "", text

        raw_entries = split_tx_entries(t)
        entries = [entry.strip() for entry in raw_entries if entry.strip()]
        if len(entries) > 1:
            processed_any = False
            idx = 0
            while idx < len(entries):
                entry = entries[idx]
                entry_kind = guess_kind(entry)
                if entry_kind in ("debt_mine", "debt_given"):
                    if parse_due_date(entry) is None:
                        attach_idx = idx + 1
                        while attach_idx < len(entries):
                            candidate, remainder = extract_due_prefix(entries[attach_idx])
                            if not candidate:
                                break
                            entry = f"{entry} {candidate}".strip()
                            entries[idx] = entry
                            if remainder:
                                entries[attach_idx] = remainder
                                break
                            entries.pop(attach_idx)
                        # After potential due merge, entry is updated
                    should_continue = await handle_debt(entry, entry_kind)
                    if not should_continue:
                        return
                    processed_any = True
                    idx += 1
                    continue

                ok = await handle_basic_entry(entry, entry_kind)
                if not ok:
                    return
                processed_any = True
                idx += 1

            if processed_any:
                return

        # hisobla input
        if step=="input_tx" and kind in ("debt_mine","debt_given"):
            await handle_debt(t, kind)
            return

        if step!="input_tx" and kind in ("debt_mine","debt_given"):
            if not has_access(uid):
                await send_expired_notice(uid, lang, m.answer)
                await m.answer(block_text(uid), reply_markup=kb_sub(lang))
                return
            await handle_debt(t, kind)
            return

        amount=parse_amount(t)
        if amount is None: await m.answer(T("need_sum")); return
        curr=detect_currency(t); acc=detect_account(t)
        if kind=="income":
            tx_saved = await save_tx(uid,"income",amount,curr,acc,"💪 Mehnat daromadlari" if lang=="uz" else "💪 Доход от труда",t)
            await m.answer(
                T("tx_inc",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),desc=t),
                reply_markup=kb_tx_cancel(tx_saved["id"], lang),
            )
            return
        else:
            cat=guess_category(t)
            tx_saved = await save_tx(uid,"expense",amount,curr,acc,cat,t)
            await m.answer(
                T("tx_exp",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),cat=cat,desc=t),
                reply_markup=kb_tx_cancel(tx_saved["id"], lang),
            )
            return

    except Exception as exc:
        logger.exception("on_text_error", exc_info=exc)
        nav_reset(uid)
        await m.answer(T("error_generic"), reply_markup=get_main_menu(lang))

@rt.message(F.contact)
async def on_contact(m:Message):
    uid=m.from_user.id
    if STEP.get(uid)!="need_phone": return
    await ensure_month_rollover()
    phone = None
    contact = m.contact
    if contact:
        phone = contact.phone_number
    username = m.from_user.username if m.from_user else None
    first_name = m.from_user.first_name if m.from_user else None
    last_name = m.from_user.last_name if m.from_user else None
    update_user_profile(
        uid,
        phone=phone,
        username=username,
        first_name=first_name,
        last_name=last_name,
        contact_verified_at=datetime.now(timezone.utc).isoformat(),
    )
    nav_reset(uid)
    await m.answer((t_uz if get_lang(uid)=="uz" else t_ru)("menu"), reply_markup=get_main_menu(get_lang(uid)))
    STEP[uid]="main"

# ====== REPORT/DEBT CALLBACKS ======
@reports_range_router.callback_query(F.data=="rep:range")
async def report_range_custom_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, c.message.answer)
        await c.message.answer(block_text(uid), reply_markup=kb_sub(lang))
        await c.answer()
        return
    nav_push(uid, "report_range")
    STEP[uid]="report_range_start"
    REPORT_RANGE_STATE.pop(uid, None)
    await c.message.answer(T("rep_range_start"))
    await c.answer()


async def send_debt_archive_list(uid: int, lang: str, answer_call, reply_markup=None) -> None:
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    items = DEBTS_ARCHIVE.get(uid, [])
    T = L(lang)
    if not items:
        if reply_markup is not None:
            await answer_call(T("debt_archive_empty"), reply_markup=reply_markup)
        else:
            await answer_call(T("debt_archive_empty"))
        return
    if reply_markup is not None:
        await answer_call(T("debt_archive_header"), reply_markup=reply_markup)
    else:
        await answer_call(T("debt_archive_header"))
    for it in reversed(items[-10:]):
        copy = dict(it)
        ts_val = copy.get("ts")
        if isinstance(ts_val, str):
            try:
                copy["ts"] = datetime.fromisoformat(ts_val)
            except Exception:
                copy["ts"] = now_tk()
        elif not isinstance(ts_val, datetime):
            copy["ts"] = now_tk()
        text = debt_card(copy, lang)
        arch_val = copy.get("archived_at")
        if isinstance(arch_val, str):
            try:
                arch_dt = datetime.fromisoformat(arch_val)
            except Exception:
                arch_dt = None
        elif isinstance(arch_val, datetime):
            arch_dt = arch_val
        else:
            arch_dt = None
        if arch_dt:
            text += f"\n{T('debt_archive_note', date=fmt_date(arch_dt))}"
        await answer_call(text)


async def send_debt_direction(uid: int, lang: str, direction: str, answer_call, reply_markup=None) -> None:
    await ensure_month_rollover()
    T = L(lang)
    head = (
        "🧾 Qarzim ro‘yxati:" if direction == "mine" and lang == "uz"
        else ("💸 Qarzdorlar ro‘yxati:" if lang == "uz" and direction == "given"
              else ("🧾 Мои долги:" if direction == "mine" else "💸 Должники:"))
    )
    if reply_markup is not None:
        await answer_call(head, reply_markup=reply_markup)
    else:
        await answer_call(head)
    debts = [x for x in MEM_DEBTS.get(uid, []) if x["direction"] == direction]
    if not debts:
        if reply_markup is not None:
            await answer_call(T("rep_empty"), reply_markup=reply_markup)
        else:
            await answer_call(T("rep_empty"))
        return
    for it in reversed(debts[-10:]):
        txt = debt_card(it, lang)
        if it["status"] == "wait":
            await answer_call(txt, reply_markup=kb_debt_done(it["direction"], it["id"], lang))
        else:
            await answer_call(txt)


@debts_archive_router.callback_query(F.data=="debt:archive")
async def debt_archive_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    await send_debt_archive_list(uid, lang, c.message.answer)
    await c.answer()


@rt.callback_query(F.data.startswith("txcancel:"))
async def tx_cancel_cb(c: CallbackQuery):
    user = c.from_user
    if not user:
        await c.answer()
        return
    uid = user.id
    lang = get_lang(uid)
    try:
        _, tx_id_raw = c.data.split(":", 1)
        tx_id = int(tx_id_raw)
    except Exception:
        await c.answer("Xatolik." if lang == "uz" else "Ошибка.", show_alert=True)
        return

    tx_list = MEM_TX.get(uid, [])
    index = next((i for i, item in enumerate(tx_list) if item.get("id") == tx_id), -1)
    if index == -1:
        msg = "Yozuv topilmadi yoki allaqachon bekor qilingan." if lang == "uz" else "Запись не найдена либо уже отменена."
        await c.answer(msg, show_alert=True)
        try:
            await c.message.edit_reply_markup()
        except Exception:
            pass
        return

    tx = tx_list.pop(index)
    if not tx_list:
        MEM_TX.pop(uid, None)
    revert_analysis_counters(uid, tx.get("kind", "expense"), tx.get("amount", 0), tx.get("currency", "UZS"))

    notification = "Bekor qilindi." if lang == "uz" else "Отменено."
    await c.answer(notification)
    final_text = "❌ Yozuv bekor qilindi." if lang == "uz" else "❌ Запись отменена."
    try:
        await c.message.edit_text(final_text)
    except Exception:
        try:
            await c.message.delete()
        except Exception:
            pass


@rt.callback_query(F.data.startswith("rep:"))
async def rep_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, c.message.answer)
        await c.message.answer(block_text(uid), reply_markup=kb_sub(lang)); await c.answer(); return
    kind=c.data.split(":")[1]
    if kind=="range":
        return
    if kind=="tx":
        nav_push(uid, "report_range")
        await c.message.answer(T("report_main"), reply_markup=kb_rep_range(lang)); await c.answer(); return
    if kind in ("day","week","month"):
        since,until=report_range(kind)
        items=[it for it in MEM_TX.get(uid,[]) if since<=it["ts"]<=until]
        if not items: await c.message.answer(T("rep_empty")); await c.answer(); return
        lines=[]
        for it in items:
            lines.append(T("rep_line",date=fmt_date(it["ts"]),kind=("Kirim" if it["kind"]=="income" else ("Расход" if lang=="ru" else "Chiqim")),cat=it["category"],amount=fmt_amount(it["amount"]),cur=it["currency"]))
        await c.message.answer("\n".join(lines)); await c.answer(); return
    if kind=="debts":
        nav_push(uid, "report_debts")
        debts=list(reversed(MEM_DEBTS.get(uid,[])))[:10]
        if not debts: await c.message.answer(T("rep_empty")); await c.answer(); return
        for it in debts:
            txt=debt_card(it, lang)
            if it["status"]=="wait": await c.message.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"], lang))
            else: await c.message.answer(txt)
        await c.answer(); return

@rt.callback_query(F.data.startswith("debt:"))
async def debt_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    await ensure_month_rollover()
    if not has_access(uid): await c.message.answer(block_text(uid), reply_markup=kb_sub(lang)); await c.answer(); return
    d=c.data.split(":")[1]
    if d=="archive":
        return
    direction="mine" if d=="mine" else "given"
    await send_debt_direction(uid, lang, direction, c.message.answer)
    await c.answer()


@rt.callback_query(F.data.startswith("debtdone:"))
async def debt_done(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid)
    await ensure_month_rollover()
    T=L(lang)
    _,direction,sid=c.data.split(":"); did=int(sid)
    for it in MEM_DEBTS.get(uid,[]):
        if it["id"]==did:
            if direction=="mine":
                it["status"]="paid"       # o'z qarzingizni to'ladingiz -> CHIQIM
                await save_tx(uid,"expense",it["amount"],it.get("currency","UZS"),"cash","💳 Qarz qaytarildi" if lang=="uz" else "💳 Долг оплачен","")
            else:
                it["status"]="received"   # sizga qarz qaytdi -> KIRIM
                await save_tx(uid,"income",it["amount"],it.get("currency","UZS"),"cash","💳 Qarz qaytdi" if lang=="uz" else "💳 Долг возвращен","")
            archive_debt_record(uid, it)
            MEM_DEBTS[uid]=[d for d in MEM_DEBTS.get(uid,[]) if d.get("id")!=did]
            note_date = fmt_date(now_tk())
            archived_items = DEBTS_ARCHIVE.get(uid, [])
            if archived_items:
                arch_last = next((item for item in reversed(archived_items) if item.get("id")==did), archived_items[-1])
                arch_dt = arch_last.get("archived_at")
                if isinstance(arch_dt, datetime):
                    note_date = fmt_date(arch_dt)
                elif isinstance(arch_dt, str):
                    try:
                        note_date = fmt_date(datetime.fromisoformat(arch_dt))
                    except Exception:
                        note_date = arch_dt
            text = debt_card(it, lang) + f"\n{T('debt_archive_note', date=note_date)}"
            await c.message.edit_text(text)
            await c.answer(("Holat yangilandi ✅" if lang=="uz" else "Статус обновлён ✅"))
            return
    await c.answer(("Topilmadi" if lang=="uz" else "Не найдено"), show_alert=True)

# ====== ANALIZ ======
@rt.message(Command("analiz"))
async def analiz_cmd(m: Message):
    uid = m.from_user.id
    lang=get_lang(uid); T=L(lang)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, m.answer)
        await m.answer(block_text(uid), reply_markup=get_main_menu(lang))
        return
    since, until = month_period()
    items = [it for it in MEM_TX.get(uid, []) if since <= it["ts"] <= until]

    income_uzs = 0
    expense_uzs = 0
    cat_map: Dict[str, int] = {}

    for it in items:
        amt_uzs = to_uzs(it["amount"], it.get("currency", "UZS"))
        if it["kind"] == "income":
            income_uzs += amt_uzs
        else:
            expense_uzs += amt_uzs
            cat = it.get("category", "🧾 Boshqa xarajatlar" if lang=="uz" else "🧾 Прочие расходы")
            cat_map[cat] = cat_map.get(cat, 0) + amt_uzs

    balance_uzs = income_uzs - expense_uzs
    jamgarma_percent = (balance_uzs / income_uzs * 100) if income_uzs > 0 else 0.0

    cat_lines = []
    for cat, total in sorted(cat_map.items(), key=lambda x: x[1], reverse=True):
        cat_lines.append(f"• {cat} — {fmt_amount(total)} so'm")
    cats_text = "\n".join(cat_lines) if cat_lines else ("• Hali sarf yozuvlari yo‘q" if lang=="uz" else "• Расходов пока нет")

    has_items = bool(items)
    gap_uzs = abs(balance_uzs)
    gap_fmt = fmt_amount(gap_uzs)
    jamgarma_abs = abs(jamgarma_percent)

    if not has_items:
        motiv = (
            "ℹ️ Hali bu oy moliyaviy yozuvlar yo‘q. Birinchi kirim yoki chiqimni kiriting."
            if lang == "uz"
            else "ℹ️ За месяц пока нет записей. Добавьте доход или расход, чтобы получать аналитику."
        )
    elif balance_uzs > 0:
        if jamgarma_percent >= 30:
            motiv = (
                f"🔥 Barakalla! Bu oy {gap_fmt} so'm jamg'ardingiz. Jamg‘arma ulushi {jamgarma_percent:.1f}% — shu tarzda davom eting."
                if lang == "uz"
                else f"🔥 Отлично! Вы отложили {gap_fmt} сум. Доля сбережений {jamgarma_percent:.1f}% — держите темп."
            )
        elif jamgarma_percent >= 10:
            motiv = (
                f"👏 Daromad chiqimdan {gap_fmt} so'mga yuqori. Jamg‘arma {jamgarma_percent:.1f}% — natija yaxshi, endi uni yanada oshiring."
                if lang == "uz"
                else f"👏 Доход выше расхода на {gap_fmt} сум. Сбережения {jamgarma_percent:.1f}% — отличный результат, усиливайте накопления."
            )
        else:
            motiv = (
                f"🙂 Balans musbat: {gap_fmt} so'm. Kichik xarajatlarni nazorat qilib jamg‘arma ulushini {jamgarma_percent:.1f}% dan yuqoriga ko‘taring."
                if lang == "uz"
                else f"🙂 Баланс в плюсе: {gap_fmt} сум. Контролируйте траты, чтобы поднять долю сбережений выше {jamgarma_percent:.1f}%."
            )
    elif balance_uzs < 0:
        if income_uzs <= 0:
            motiv = (
                "⚠️ Chiqimlar bor, lekin daromad yozilmagan. Kirimlarni qayd etib to‘liq manzarani ko‘ring."
                if lang == "uz"
                else "⚠️ Расходы есть, но доходы не отмечены. Добавьте поступления, чтобы видеть полную картину."
            )
        elif jamgarma_abs >= 30:
            motiv = (
                f"🚨 Chiqimlar daromaddan {gap_fmt} so'm ({jamgarma_abs:.1f}%) ko‘p. Zarur bo‘lmagan xarajatlarni qisqartirib balansni tiklang."
                if lang == "uz"
                else f"🚨 Расходы превысили доход на {gap_fmt} сум ({jamgarma_abs:.1f}%). Срочно урежьте необязательные траты и верните баланс."
            )
        else:
            motiv = (
                f"⚠️ Balans manfiy: {gap_fmt} so'm. Tejamkorlik rejasi tuzib xarajatlarni qisqartiring."
                if lang == "uz"
                else f"⚠️ Баланс в минусе на {gap_fmt} сум. Запланируйте экономию и сократите расходы."
            )
    else:
        motiv = (
            f"🙂 Daromad va chiqim teng: {fmt_amount(income_uzs)} so'm aylantirdingiz. Endi har oy ozgina jamg‘arishni boshlang."
            if lang == "uz"
            else f"🙂 Доход и расход совпали: оборот {fmt_amount(income_uzs)} сум. Попробуйте отложить хотя бы небольшую сумму."
        )

    text = (
        ( "<b>📊 1 oylik moliya analizi</b>\n\n"
          f"💵 Daromad: <b>{fmt_amount(income_uzs)} so'm</b>\n"
          f"💸 Xarajat: <b>{fmt_amount(expense_uzs)} so'm</b>\n"
          f"📈 Balans: <b>{fmt_amount(balance_uzs)} so'm</b>\n"
          f"💰 Jamg‘arma darajasi: <b>{jamgarma_percent:.1f}%</b>\n\n"
          f"<b>Toifalar bo‘yicha xarajatlar</b>\n{cats_text}\n\n{motiv}" )
        if lang=="uz" else
        ( "<b>📊 Финансовый анализ за месяц</b>\n\n"
          f"💵 Доход: <b>{fmt_amount(income_uzs)} сум</b>\n"
          f"💸 Расход: <b>{fmt_amount(expense_uzs)} сум</b>\n"
          f"📈 Баланс: <b>{fmt_amount(balance_uzs)} сум</b>\n"
          f"💰 Уровень сбережений: <b>{jamgarma_percent:.1f}%</b>\n\n"
          f"<b>Расходы по категориям</b>\n{cats_text}\n\n{motiv}" )
    )

    await m.answer(text)
    nav_reset(uid)
    await m.answer(T("menu"), reply_markup=get_main_menu(lang))

# ------ OBUNA (CLICK flow) ------
def _build_return_url(invoice_id: str) -> Optional[str]:
    template = PAYMENT_RETURN_URL or RETURN_URL
    if not template:
        return None
    if "<invoice_id>" in template:
        return template.replace("<invoice_id>", invoice_id)
    parsed = urlparse(template)
    query = dict(parse_qsl(parsed.query)) if parsed.query else {}
    if "invoice_id" not in query:
        query["invoice_id"] = invoice_id
    new_query = urlencode(query)
    return urlunparse(parsed._replace(query=new_query))


def create_click_link(pid: str, amount: int) -> str:
    params = (
        f"merchant_id={CLICK_MERCHANT_ID}"
        f"&service_id={CLICK_SERVICE_ID}"
        f"&merchant_user_id={CLICK_MERCHANT_USER_ID}"
        f"&transaction_param={pid}"
        f"&amount={amount}"
    )
    # [SUBSCRIPTION-POLLING-BEGIN]
    if CLICK_INCLUDE_RETURN_URL:
        return_url = _build_return_url(pid)
        if return_url:
            params += f"&return_url={quote_plus(return_url)}"
    # [SUBSCRIPTION-POLLING-END]
    return f"{CLICK_PAY_URL_BASE}?{params}"


async def send_subscription_invoice_message(uid: int, lang: str, code: str, message: Message) -> None:
    T = L(lang)
    plan = T("sub_month")
    days = 30
    price = MONTH_PLAN_PRICE
    amount_dec = Decimal(price)
    plan_info = payments_detect_plan(amount_dec)
    plan_key = plan_info[0] if plan_info else None
    invoice_id = await payments_create_invoice(uid, amount_dec, "UZS", plan_key)
    pid = str(invoice_id)
    PENDING_PAYMENTS[pid] = {
        "uid": uid,
        "invoice_id": pid,
        "plan": plan,
        "plan_key": plan_key,
        "period_days": days,
        "amount": price,
        "currency": "UZS",
        "status": "pending",
        "created": now_tk(),
    }
    link = create_click_link(pid, price)
    markup = kb_payment(pid, link, lang)
    await message.answer(T("sub_created", plan=plan, amount=price), reply_markup=markup)


@rt.callback_query(F.data.startswith("sub:"))
async def sub_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid)
    code=c.data.split(":")[1]
    await send_subscription_invoice_message(uid, lang, code, c.message)
    await c.answer()


async def process_paycheck(uid: int, lang: str, answer_call, reply_markup, pay: Optional[dict] = None) -> bool:
    T = L(lang)
    await answer_call(T("pay_checking"))
    await ensure_subscription_state(uid)
    record = await payments_get_latest_payment(uid)
    if not record:
        await answer_call(T("pay_status_missing"), reply_markup=reply_markup)
        return False
    status = (record.get("status") or "").lower()
    amount_dec = Decimal(str(record.get("amount", "0")))
    plan_info = payments_detect_plan(amount_dec)
    plan_key = plan_info[0] if plan_info else (pay.get("plan_key") if pay else None)
    period_days = plan_info[1] if plan_info else (pay.get("period_days") if pay else 0)
    plan_label = T(plan_key) if plan_key else (pay["plan"] if pay else "")
    if not plan_label:
        if amount_dec == Decimal("7900"):
            plan_label = T("sub_week")
            period_days = period_days or 7
        elif amount_dec == Decimal("19900"):
            plan_label = T("sub_month")
            period_days = period_days or 30
    if period_days <= 0:
        period_days = (pay.get("period_days") if pay else 0) or 30
    if status != "paid":
        await answer_call(T("pay_status_pending"), reply_markup=reply_markup)
        return False
    paid_iso = record.get("paid_at")
    try:
        paid_dt = datetime.fromisoformat(paid_iso) if paid_iso else now_tk()
        if paid_dt.tzinfo is None:
            paid_dt = paid_dt.replace(tzinfo=timezone.utc)
        paid_dt = paid_dt.astimezone(TASHKENT)
    except Exception:
        paid_dt = now_tk()
    until = paid_dt + timedelta(days=period_days or 0)
    if period_days <= 0 and pay:
        until = now_tk() + timedelta(days=pay.get("period_days", 0))
    uid_db = record.get("user_id") or uid
    SUB_EXPIRES[uid_db] = until
    await set_user_subscription(uid_db, paid_dt, until)
    if pay:
        pay["status"] = "paid"
    await answer_call(T("pay_status_paid", plan=plan_label, until=fmt_date(until)), reply_markup=reply_markup)
    await answer_call(T("sub_ok", start=fmt_date(paid_dt), end=fmt_date(until)), reply_markup=reply_markup)
    await answer_call(T("SUB_OK"), reply_markup=reply_markup)
    return True

@rt.callback_query(F.data.startswith("paycheck:"))
async def paycheck_cb(c:CallbackQuery):
    pid=c.data.split(":")[1]
    pay=PENDING_PAYMENTS.get(pid)
    lang=get_lang(pay["uid"]) if pay else get_lang(c.from_user.id)
    paid = await process_paycheck(c.from_user.id, lang, c.message.answer, get_main_menu(lang), pay)
    if paid:
        target_uid = pay.get("uid") if pay else c.from_user.id
        nav_reset(target_uid)
    await c.answer()

# ====== BALANS ======


async def send_balance(uid:int, m:Message):
    sums={("cash","UZS"):0,("cash","USD"):0,("card","UZS"):0,("card","USD"):0}
    for it in MEM_TX.get(uid,[]):
        sign = 1 if it["kind"]=="income" else -1
        k=(it["account"], it["currency"])
        if k not in sums: sums[k]=0
        sums[k]+= sign*it["amount"]
    they_uzs=0; i_uzs=0; they_usd=0; i_usd=0
    for d in MEM_DEBTS.get(uid,[]):
        if d["status"]!="wait": continue
        amt = d["amount"]
        cur = d.get("currency","UZS")
        if d["direction"]=="given":
            if cur == "USD":
                they_usd += amt
            else:
                they_uzs += amt
        else:
            if cur == "USD":
                i_usd += amt
            else:
                i_uzs += amt

    lang=get_lang(uid); T=L(lang)
    txt=T("balance",
        cash_uzs=fmt_amount(sums.get(("cash","UZS"),0)),
        cash_usd=fmt_amount(sums.get(("cash","USD"),0)),
        card_uzs=fmt_amount(sums.get(("card","UZS"),0)),
        card_usd=fmt_amount(sums.get(("card","USD"),0)),
        they_uzs=fmt_amount(they_uzs), they_usd=fmt_amount(they_usd),
        i_uzs=fmt_amount(i_uzs), i_usd=fmt_amount(i_usd)
    )
    await m.answer(txt)


# ====== CARDS MANAGEMENT ======


# ====== CATEGORY ======
def guess_category(text:str)->str:
    t=(text or "").lower()
    if any(w in t for w in ["taksi","yo‘l","yol","benzin","transport","metro","avtobus","такси","метро","автобус","транспорт"]): return "🚌 Transport"
    if any(w in t for w in ["ovqat","kafe","restoran","non","taom","fastfood","osh","shashlik","еда","кафе","ресторан","фастфуд"]): return "🍔 Oziq-ovqat"
    if any(w in t for w in ["kommunal","svet","gaz","suv","коммунал","свет","газ","вода"]): return "💡 Kommunal"
    if any(w in t for w in ["internet","telefon","uzmobile","beeline","ucell","uztelecom","интернет","телефон"]): return "📱 Aloqa"
    if any(w in t for w in ["ijara","kvartira","arenda","ipoteka","аренда","ипотека","квартира"]): return "🏠 Uy-ijara"
    if any(w in t for w in ["dorixona","shifokor","apteka","dori","аптека","врач","лекар"]): return "💊 Sog‘liq"
    if any(w in t for w in ["soliq","jarima","patent","налог","штраф","патент"]): return "💸 Soliq/Jarima"
    if any(w in t for w in ["kiyim","do‘kon","do'kon","bozor","market","savdo","shopping","supermarket","одежда","магазин","рынок","маркет"]): return "🛍 Savdo"
    if any(w in t for w in ["oylik","maosh","bonus","premiya","зарплата","премия","бонус"]): return "💪 Mehnat daromadlari"
    return "🧾 Boshqa xarajatlar" if "uz" in t or "so'm" in t else "🧾 Прочие расходы"

# ====== Eslatmalar ======
def _sec_until(h:int,mn:int=0):
    n=now_tk(); t=n.replace(hour=h,minute=mn,second=0,microsecond=0)
    if t<=n: t+=timedelta(days=1)
    return (t-n).total_seconds()

async def subscription_reminder_loop():
    logger = logging.getLogger(__name__)
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            cutoff_iso = (now_utc + timedelta(days=1)).isoformat()
            candidates = await payments_users_for_expiry_reminder(cutoff_iso)
            if candidates:
                for item in candidates:
                    user_id = item.get("user_id")
                    sub_until_raw = item.get("sub_until")
                    if not user_id or not sub_until_raw:
                        continue
                    try:
                        until_dt = datetime.fromisoformat(str(sub_until_raw))
                    except Exception:
                        continue
                    if until_dt.tzinfo is None:
                        until_dt = until_dt.replace(tzinfo=timezone.utc)
                    time_left = until_dt - now_utc
                    if time_left <= timedelta(0):
                        await mark_reminder_sent(user_id)
                        continue
                    if time_left > timedelta(days=1):
                        continue
                    await ensure_subscription_state(user_id)
                    lang = get_lang(user_id)
                    T = L(lang)
                    until_local = until_dt.astimezone(TASHKENT)
                    try:
                        await bot.send_message(
                            user_id,
                            T("sub_remind_1d", end=until_local.strftime("%d.%m.%Y")),
                        )
                        await mark_reminder_sent(user_id)
                    except Exception as exc:
                        logger.warning(
                            "subscription-reminder-send-failed",
                            extra={"user_id": user_id, "error": str(exc)},
                        )
        except Exception as exc:
            logger.warning("subscription-reminder-loop-error", exc_info=exc)
        await asyncio.sleep(3600)


async def daily_reminder():
    schedule = ((8, "morning_ping"), (20, "evening_ping"))
    while True:
        for hour, key in schedule:
            try:
                await asyncio.sleep(_sec_until(hour, 0))
                for uid in list(SEEN_USERS):
                    lang = get_lang(uid)
                    T = L(lang)
                    text = T(key)
                    try:
                        await bot.send_message(uid, text)
                    except Exception:
                        pass
            except Exception:
                pass
        await asyncio.sleep(5)

async def debt_reminder():
    schedule = ((10, "slot_a"), (16, "slot_b"))
    while True:
        for hour, slot_key in schedule:
            try:
                await asyncio.sleep(_sec_until(hour, 0))
                now = now_tk()
                today = fmt_date(now)
                for key in list(DEBT_REMIND_SENT):
                    if key[3] != today:
                        DEBT_REMIND_SENT.remove(key)

                for uid, debts in list(MEM_DEBTS.items()):
                    if not debts:
                        continue
                    lang = get_lang(uid)
                    T = L(lang)
                    for it in debts:
                        if it.get("status") != "wait":
                            continue
                        due_raw = it.get("due")
                        if not due_raw:
                            continue
                        try:
                            due_dt = datetime.strptime(due_raw, "%d.%m.%Y").date()
                        except Exception:
                            continue
                        if due_dt > now.date():
                            continue
                        debt_id = it.get("id")
                        if not debt_id:
                            continue
                        key = (uid, debt_id, slot_key, today)
                        if key in DEBT_REMIND_SENT:
                            continue
                        amount_text = fmt_amount(it.get("amount", 0))
                        currency = it.get("currency", "UZS")
                        who = it.get("counterparty")
                        if not who:
                            if it.get("direction") == "given":
                                who = "qarzdor" if lang == "uz" else "должник"
                            else:
                                who = "qarz bergan kishi" if lang == "uz" else "кредитор"
                        template = "DEBT_REMIND_TO_US" if it.get("direction") == "given" else "DEBT_REMIND_BY_US"
                        message = T(template, due=due_raw, who=who, amount=amount_text, cur=currency)
                        try:
                            await bot.send_message(uid, message)
                            DEBT_REMIND_SENT.add(key)
                        except Exception:
                            pass
            except Exception:
                pass
        await asyncio.sleep(5)

# ====== COMMANDS ======
async def set_cmds():
    # Eski buyruqlarni tozalaymiz va faqat /start qoldiramiz
    try:
        await bot.delete_my_commands()
        await bot.delete_my_commands(language_code="ru")
        await bot.delete_my_commands(language_code="uz")
    except Exception as exc:
        logging.getLogger(__name__).warning("delete-my-commands-failed: %s", exc)
    cmds = [BotCommand(command="start", description="Boshlash / Start")]
    for lang_code in (None, "ru", "uz"):
        try:
            if lang_code:
                await bot.set_my_commands(cmds, language_code=lang_code)
            else:
                await bot.set_my_commands(cmds)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "set-my-commands-failed lang=%s error=%s", lang_code or "default", exc
            )
            break

# ====== MAIN ======
async def main():
    await ensure_payment_schema()
    load_cards_storage()
    load_users_storage()
    load_debts_archive()
    load_analysis_state()
    await ensure_month_rollover()
    dp.update.middleware(StartGateMiddleware())
    dp.include_router(reports_range_router)
    dp.include_router(cards_entry_router)
    dp.include_router(debts_archive_router)
    dp.include_router(sub_router)
    dp.include_router(pay_debug_router)
    dp.include_router(subscription_router)
    dp.include_router(rt)
    try:
        await set_cmds()
    except Exception as exc:
        logging.getLogger(__name__).warning("set_cmds_failed: %s", exc)
    sample_invoice = create_invoice_id(0)
    sample_return = _build_return_url(sample_invoice) or RETURN_URL
    sample_url = build_miniapp_url(WEB_BASE, MONTH_PLAN_PRICE, sample_invoice, sample_return)
    print("MINI_APP_URL_FOR_BOTFATHER:", MINI_APP_BASE_URL)
    print("TEST_URL_SAMPLE:", sample_url)
    asyncio.create_task(subscription_reminder_loop())
    asyncio.create_task(daily_reminder())
    asyncio.create_task(debt_reminder())
    print("Bot ishga tushdi."); await dp.start_polling(bot)

@cards_entry_router.message(Command("kartalarim"))
@cards_entry_router.message(Command("kartam"))
async def cards_command_entry(message: Message, state: FSMContext):
    await enter_cards_menu(message, state)


@cards_entry_router.message(F.text.in_(CARD_MENU_TEXTS))
async def cards_text_entry(message: Message, state: FSMContext):
    await enter_cards_menu(message, state)


@cards_entry_router.message(F.text.in_(CARD_ADD_TEXTS))
async def cards_start_add(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid)
    await ensure_month_rollover()
    await ensure_subscription_state(uid)
    if not has_access(uid):
        await send_expired_notice(uid, lang, message.answer)
        await message.answer(block_text(uid), reply_markup=get_main_menu(lang))
        return
    await state.clear()
    await state.set_state(CardAddStates.label)
    await message.answer(L(lang)("cards_prompt_label"), reply_markup=kb_card_cancel(lang))


@cards_entry_router.message(CardAddStates.label)
async def cards_collect_label(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid)
    text = (message.text or "").strip()
    if _is_cancel(text, lang):
        await state.clear()
        await show_cards_overview(message, lang)
        return
    if not (2 <= len(text) <= 32):
        await message.answer(L(lang)("cards_format_error"), reply_markup=kb_card_cancel(lang))
        return
    await state.update_data(label=text)
    await state.set_state(CardAddStates.pan)
    await message.answer(L(lang)("cards_prompt_pan"), reply_markup=kb_card_cancel(lang))


@cards_entry_router.message(CardAddStates.pan)
async def cards_collect_pan(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid)
    raw = (message.text or "").strip()
    if _is_cancel(raw, lang):
        await state.clear()
        await show_cards_overview(message, lang)
        return
    digits = re.sub(r"\D", "", raw)
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        await message.answer(L(lang)("cards_format_error"), reply_markup=kb_card_cancel(lang))
        return
    formatted = " ".join(digits[i:i+4] for i in range(0, len(digits), 4))
    await state.update_data(pan=digits, pan_display=formatted)
    await state.set_state(CardAddStates.expires)
    await message.answer(L(lang)("cards_prompt_expires"), reply_markup=kb_card_cancel(lang))


@cards_entry_router.message(CardAddStates.expires)
async def cards_collect_expires(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid)
    text = (message.text or "").strip()
    if _is_cancel(text, lang):
        await state.clear()
        await show_cards_overview(message, lang)
        return
    if not re.fullmatch(r"(0[1-9]|1[0-2])/\d{2}", text):
        await message.answer(L(lang)("cards_format_error"), reply_markup=kb_card_cancel(lang))
        return
    await state.update_data(expires=text)
    await state.set_state(CardAddStates.owner)
    await message.answer(L(lang)("cards_prompt_owner"), reply_markup=kb_card_cancel(lang))


@cards_entry_router.message(CardAddStates.owner)
async def cards_collect_owner(message: Message, state: FSMContext):
    uid = message.from_user.id
    lang = get_lang(uid)
    owner = (message.text or "").strip()
    if _is_cancel(owner, lang):
        await state.clear()
        await show_cards_overview(message, lang)
        return
    if not owner:
        await message.answer(L(lang)("cards_format_error"), reply_markup=kb_card_cancel(lang))
        return
    data = await state.get_data()
    label = data.get("label", "")
    pan_value = data.get("pan", "")
    expires = data.get("expires", "")
    if not label or not pan_value:
        await state.clear()
        await show_cards_overview(message, lang)
        return
    save_card(uid, label, pan_value, expires, owner)
    await state.clear()
    await message.answer(L(lang)("cards_saved"), reply_markup=kb_cards_menu(lang))
    await show_cards_overview(message, lang)


if __name__ == "__main__":
    asyncio.run(main())
