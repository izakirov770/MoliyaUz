APP_VERSION = "v2025-09-13-01:10"
# bot.py
import asyncio, os, uuid, re
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton, BotCommand,
)
from dotenv import load_dotenv

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

NOTION_OFER_URL = "https://www.notion.so/OFERA-26a8fa17fd1f803f8025f07f98f89c87?source=copy_link"

# ====== BOT ======
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
rt = Router()

# ====== VAQT/FMT ======
TASHKENT = timezone(timedelta(hours=5))
now_tk = lambda: datetime.now(TASHKENT)
fmt_date = lambda d: d.strftime("%d.%m.%Y")
def fmt_amount(n):
    try: return f"{int(round(n)):,}".replace(",", " ")
    except: return str(n)

# ====== “DB” (RAM) ======
STEP: Dict[int,str] = {}
USER_LANG: Dict[int,str] = {}
SEEN_USERS: set[int] = set()  # eslatmalar uchun

TRIAL_MIN = 15
TRIAL_START: Dict[int,datetime] = {}
SUB_EXPIRES: Dict[int,datetime] = {}

# tranzaksiya: {id, ts, kind(income|expense), amount, currency(UZS|USD|EUR), account(cash|card), category, desc}
MEM_TX: Dict[int, List[dict]] = {}
# qarz: {id, ts, direction(mine|given), amount, currency, counterparty, due, status(wait|paid|received)}
MEM_DEBTS: Dict[int, List[dict]] = {}
MEM_DEBTS_SEQ: Dict[int,int] = {}
PENDING_DEBT: Dict[int,dict] = {}

# payment pending
# pid -> {"uid","plan","period_days","amount","currency","status","created"}
PENDING_PAYMENTS: Dict[str,dict] = {}

DEBT_REMIND_SENT: set[Tuple[int,int,str]] = set()

# ====== UTIL ======
def is_sub(uid):
    e=SUB_EXPIRES.get(uid); return bool(e and e>now_tk())
def trial_active(uid):
    s=TRIAL_START.get(uid); return bool(s and (now_tk()-s)<=timedelta(minutes=TRIAL_MIN))
def has_access(uid): return is_sub(uid) or trial_active(uid)
def block_text(uid):
    if SUB_EXPIRES.get(uid) and not is_sub(uid): return "⛔️ Obuna muddati tugagan. Obunani yangilang."
    if TRIAL_START.get(uid) and not trial_active(uid): return "⌛️ 15 daqiqalik bepul sinov tugadi. Obuna tanlang."
    return "⛔️ Bu bo‘lim uchun obuna kerak."

# ---- Localization helpers ----
def t_uz(k,**kw):
    D={
        "start_choose":"Assalomu alaykum, iltimos bot tilni tanlang.",
        "ask_name":"Ajoyib, tanishib olamiz, ismingiz nima?",
        "welcome":(
            "Xush kelibsiz! 👋\n\n"
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
        "btn_qarz":"💳 Qarz",
        "btn_balance":"💼 Balans",
        "btn_obuna":"⭐️ Obuna",
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
        "rep_line":"{date} — {kind} — {cat} — {amount} {cur}",
        "rep_empty":"Bu bo‘lim uchun yozuv yo‘q.",

        "debt_menu":"Qarz bo‘limi:",
        "debt_mine":"Qarzim","debt_given":"Qarzdorlar",
        "ask_due_mine":"Qachon <b>to‘laysiz</b>? Masalan: 25.09.2025, 25-09, ertaga…",
        "ask_due_given":"Qachon <b>qaytaradi</b>? Masalan: 25.09.2025, 25-09, ertaga…",
        "debt_saved_mine":"🧾 Qarz (Qarzim) qo‘shildi:\nKim: {who}\nSumma: {cur} {amount}\nTo‘lash sanasi: {due}",
        "debt_saved_given":"💸 Qarz (Qarzdor) qo‘shildi:\nKim: {who}\nSumma: {cur} {amount}\nQaytarish sanasi: {due}",
        "debt_need":"Qarz matnini tushunmadim. Ism va summani yozing.",
        "date_need":"Sanani tushunmadim. Masalan: 25.09.2025 yoki ertaga.",
        "card_debt":"— — —\n<b>QARZ</b>\nSana: {created}\nKim: {who}\nKategoriya: 💳 Qarzlar\nSumma: {cur} {amount}\nBerilgan sana: {created}\nQaytadigan sana: {due}\nHolati: {status}",
        "st_wait":"⏳ Kutilmoqda","st_paid":"✅ Tulangan","st_rcv":"✅ Qaytarilgan",
        "btn_paid":"✅ Tuladim","btn_rcv":"✅ Berildi",

        "sub_choose":"Obuna turini tanlang:",
        "sub_week":"1 haftalik obuna — 7 900 so‘m",
        "sub_month":"1 oylik obuna — 19 900 so‘m",
        "sub_created":"To‘lov yaratildi.\n\nReja: <b>{plan}</b>\nSumma: <b>{amount} so‘m</b>\n\n⬇️ CLICK orqali to‘lang, so‘ng <b>“To‘lovni tekshirish”</b> tugmasini bosing.",
        "sub_activated":"✅ Obuna faollashtirildi: {plan} (gacha {until})",
        "pay_click":"CLICK orqali to‘lash","pay_check":"To‘lovni tekshirish",
        "pay_checking":"🔄 To‘lov holati tekshirilmoqda…","pay_notfound":"To‘lov topilmadi yoki tasdiqlanmagan.",

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
            "Добро пожаловать! 👋\n\n"
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
        "btn_qarz": "💳 Долг",
        "btn_balance": "💼 Баланс",
        "btn_obuna": "⭐️ Подписка",
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
        "rep_line": "{date} — {kind} — {cat} — {amount} {cur}",
        "rep_empty": "Пока нет записей для этого раздела.",

        "debt_menu": "Раздел долги:",
        "debt_mine": "Мой долг", "debt_given": "Должники",
        "ask_due_mine": "Когда <b>вернете</b>? Например: 25.09.2025, 25-09, завтра…",
        "ask_due_given": "Когда <b>он вернет</b>? Например: 25.09.2025, 25-09, завтра…",
        "debt_saved_mine": "🧾 Добавлен долг (я должен):\nКому: {who}\nСумма: {cur} {amount}\nДата возврата: {due}",
        "debt_saved_given": "💸 Добавлен должник:\nКто: {who}\nСумма: {cur} {amount}\nДата возврата: {due}",
        "debt_need": "Не понял долг. Укажите имя и сумму.",
        "date_need": "Не понял дату. Например: 25.09.2025 или завтра.",
        "card_debt": "— — —\n<b>ДОЛГ</b>\nСоздано: {created}\nКто/Кому: {who}\nКатегория: 💳 Долги\nСумма: {cur} {amount}\nДата выдачи: {created}\nДата возврата: {due}\nСтатус: {status}",
        "st_wait": "⏳ Ожидается", "st_paid": "✅ Оплачен", "st_rcv": "✅ Возвращен",
        "btn_paid": "✅ Оплатил", "btn_rcv": "✅ Вернул",

        "sub_choose": "Выберите тип подписки:",
        "sub_week": "Подписка на 1 неделю — 7 900 сум",
        "sub_month": "Подписка на 1 месяц — 19 900 сум",
        "sub_created": "Платеж создан.\n\nТариф: <b>{plan}</b>\nСумма: <b>{amount} сум</b>\n\n⬇️ Оплатите через CLICK, затем нажмите <b>«Проверить платеж»</b>.",
        "sub_activated": "✅ Подписка активирована: {plan} (до {until})",
        "pay_click": "Оплатить в CLICK", "pay_check": "Проверить платеж",
        "pay_checking": "🔄 Проверяем статус платежа…", "pay_notfound": "Платеж не найден или не подтвержден.",

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

def kb_main(lang="uz"):
    T=L(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("btn_hisobla"))],
            [KeyboardButton(text=T("btn_hisobot")), KeyboardButton(text=T("btn_qarz"))],
            [KeyboardButton(text=T("btn_balance")), KeyboardButton(text=T("btn_obuna"))],
            [KeyboardButton(text=T("btn_analiz"))],
            [KeyboardButton(text=T("btn_lang"))],
        ], resize_keyboard=True
    )

def kb_oferta(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("btn_oferta"), url=NOTION_OFER_URL)]
    ])

def kb_rep_main(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("rep_tx"),callback_data="rep:tx")],
        [InlineKeyboardButton(text=T("rep_debts"),callback_data="rep:debts")]
    ])

def kb_rep_range(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("rep_day"),callback_data="rep:day")],
        [InlineKeyboardButton(text=T("rep_week"),callback_data="rep:week")],
        [InlineKeyboardButton(text=T("rep_month"),callback_data="rep:month")]
    ])

def kb_debt_menu(lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("debt_mine"),callback_data="debt:mine")],
        [InlineKeyboardButton(text=T("debt_given"),callback_data="debt:given")]
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
        [InlineKeyboardButton(text=T("sub_week"),callback_data="sub:week")],
        [InlineKeyboardButton(text=T("sub_month"),callback_data="sub:month")]
    ])

def kb_payment(pid, pay_url, lang="uz"):
    T=L(lang)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T("pay_click"), url=pay_url)],
        [InlineKeyboardButton(text=T("pay_check"), callback_data=f"paycheck:{pid}")]
    ])

# ====== PARSERLAR ======
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
    if "sotib oldim" in t or "сотиб олдим" in t: return "expense"
    if any(w in t for w in ["kirim","кирим","oylik","maosh","маош","keldi","tushdi","oldim","келди","тушди"]): return "income"
    if t.strip().startswith("+"): return "income"
    if any(w in t for w in ["chiqim","xarajat","taksi","benzin","ovqat","kafe","restoran","market","kommunal","internet","telefon","ijara","arenda"]): return "expense"
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
    MEM_TX.setdefault(uid,[]).append({
        "id": len(MEM_TX.get(uid,[]))+1, "ts": now_tk(),
        "kind":kind, "amount":amount, "currency":currency, "account":account,
        "category":category, "desc":desc
    })

async def save_debt(uid:int, direction:str, amount:int, currency:str, counterparty:str, due:str)->int:
    did=next_debt_id(uid)
    MEM_DEBTS.setdefault(uid,[]).append({
        "id":did, "ts":now_tk(), "direction":direction, "amount":amount,
        "currency":currency, "counterparty":counterparty, "due":due, "status":"wait"
    })
    return did

def debt_card(it:dict, lang="uz")->str:
    T=L(lang)
    s={"wait":T("st_wait"),"paid":T("st_paid"),"received":T("st_rcv")}[it.get("status","wait")]
    return T("card_debt", created=fmt_date(it["ts"]), who=it["counterparty"], cur=it.get("currency","UZS"),
             amount=fmt_amount(it["amount"]), due=it["due"], status=s)

# ====== REPORT HELPERS ======
def report_range(kind:str):
    n=now_tk()
    if kind=="day": return n.replace(hour=0,minute=0,second=0,microsecond=0), n
    if kind=="week": return n-timedelta(days=7), n
    return n-timedelta(days=30), n

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
    SEEN_USERS.add(uid)
    TRIAL_START.setdefault(uid, now_tk())
    STEP[uid]="lang"
    await m.answer(t_uz("start_choose"), reply_markup=kb_lang())

@rt.message(Command("approve"))
async def approve_cmd(m:Message):
    if ADMIN_ID and m.from_user.id!=ADMIN_ID: return
    parts=m.text.strip().split()
    if len(parts)!=2: await m.answer("format: /approve <pid>"); return
    pid=parts[1]
    pay=PENDING_PAYMENTS.get(pid)
    if not pay: await m.answer("pid topilmadi"); return
    pay["status"]="paid"
    await m.answer(f"{pid} -> paid")

@rt.message(Command("menu"))
async def menu_cmd(m: Message):
    uid = m.from_user.id
    await m.answer((t_uz if get_lang(uid)=="uz" else t_ru)("menu"), reply_markup=kb_main(get_lang(uid)))

@rt.message(F.text)
async def on_text(m:Message):
    uid=m.from_user.id
    t=(m.text or "").strip()
    step=STEP.get(uid)

    lang = get_lang(uid)
    T = L(lang)

    if step=="lang":
        low=t.lower()
        if "uz" in low or "o‘z" in low or "o'z" in low: USER_LANG[uid]="uz"
        elif "рус" in low or "ru" in low: USER_LANG[uid]="ru"
        else: return
        STEP[uid]="name"
        await m.answer((t_uz if get_lang(uid)=="uz" else t_ru)("ask_name"), reply_markup=ReplyKeyboardRemove()); return

    if step=="name":
        lang=get_lang(uid); T=L(lang)
        await m.answer(T("welcome"), reply_markup=kb_share(lang))
        await m.answer("—", reply_markup=kb_oferta(lang))
        STEP[uid]="need_phone"; return

    if step=="need_phone": return

    if step in ("debt_mine_due","debt_given_due"):
        due=parse_due_date(t)
        if not due: await m.answer(T("date_need")); return
        tmp=PENDING_DEBT.get(uid)
        if not tmp:
            STEP[uid]="main"; await m.answer(T("enter_tx"), reply_markup=kb_main(lang)); return

        # QARZNI SAQLAYMIZ
        did = await save_debt(uid, tmp["direction"], tmp["amount"], tmp["currency"], tmp["who"], due)

        # BALANS HARAKATI (DEBT CREATE MOMENT)
        if tmp["direction"]=="given":
            await save_tx(uid,"expense",tmp["amount"],tmp["currency"],"cash","💳 Qarz berildi",f"Qarz berildi: {tmp['who']} (ID {did})")
        else:
            await save_tx(uid,"income",tmp["amount"],tmp["currency"],"cash","💳 Qarz olindi",f"Qarz olindi: {tmp['who']} (ID {did})")

        if tmp["direction"]=="mine":
            await m.answer(T("debt_saved_mine", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
        else:
            await m.answer(T("debt_saved_given", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
        PENDING_DEBT.pop(uid, None); STEP[uid]="main"; return

    # menyular
    if t==T("btn_hisobla"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
        STEP[uid]="input_tx"; await m.answer(T("enter_tx"), reply_markup=kb_main(lang)); return

    if t==T("btn_hisobot"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
        await m.answer(T("report_main"), reply_markup=kb_rep_main(lang)); return

    if t==T("btn_qarz"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
        await m.answer(T("debt_menu"), reply_markup=kb_debt_menu(lang)); return

    if t==T("btn_balance"):
        await send_balance(uid, m); return

    if t==T("btn_obuna"):
        await m.answer(T("sub_choose"), reply_markup=kb_sub(lang)); return

    if t==T("btn_analiz"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
        await analiz_cmd(m); return

    if t==T("btn_lang"):
        STEP[uid]="lang"; await m.answer(T("lang_again"), reply_markup=kb_lang()); return

    # hisobla input
    if step=="input_tx":
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub(lang)); return
        kind=guess_kind(t)
        if kind in ("debt_mine","debt_given"):
            amount=parse_amount(t) or 0
            if amount<=0: await m.answer(T("debt_need")); return
            curr=detect_currency(t); who=parse_counterparty(t)
            due0=parse_due_date(t)

            if due0:
                did = await save_debt(uid, "mine" if kind=="debt_mine" else "given", amount, curr, who, due0)

                # BALANS HARAKATI (DEBT CREATE MOMENT)
                if kind=="debt_mine":
                    await save_tx(uid,"income",amount,curr,"cash","💳 Qarz olindi",f"Qarz olindi: {who} (ID {did})")
                else:
                    await save_tx(uid,"expense",amount,curr,"cash","💳 Qarz berildi",f"Qarz berildi: {who} (ID {did})")

                if kind=="debt_mine":
                    await m.answer(T("debt_saved_mine", who=who, cur=curr, amount=fmt_amount(amount), due=due0))
                else:
                    await m.answer(T("debt_saved_given", who=who, cur=curr, amount=fmt_amount(amount), due=due0))
            else:
                PENDING_DEBT[uid]={"direction":"mine" if kind=="debt_mine" else "given","amount":amount,"currency":curr,"who":who}
                STEP[uid]="debt_mine_due" if kind=="debt_mine" else "debt_given_due"
                await m.answer(T("ask_due_mine") if kind=="debt_mine" else T("ask_due_given"))
            return

        amount=parse_amount(t)
        if amount is None: await m.answer(T("need_sum")); return
        curr=detect_currency(t); acc=detect_account(t)
        if guess_kind(t)=="income":
            await save_tx(uid,"income",amount,curr,acc,"💪 Mehnat daromadlari" if lang=="uz" else "💪 Доход от труда",t)
            await m.answer(T("tx_inc",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),desc=t))
        else:
            cat=guess_category(t)
            await save_tx(uid,"expense",amount,curr,acc,cat,t)
            await m.answer(T("tx_exp",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),cat=cat,desc=t))
        return

    await m.answer(T("enter_text"))

@rt.message(F.contact)
async def on_contact(m:Message):
    uid=m.from_user.id
    if STEP.get(uid)!="need_phone": return
    await m.answer((t_uz if get_lang(uid)=="uz" else t_ru)("menu"), reply_markup=kb_main(get_lang(uid))); STEP[uid]="main"

# ====== CALLBACKS ======
@rt.callback_query(F.data.startswith("rep:"))
async def rep_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    if not has_access(uid): await c.message.answer(block_text(uid), reply_markup=kb_sub(lang)); await c.answer(); return
    kind=c.data.split(":")[1]
    if kind=="tx":
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
        debts=list(reversed(MEM_DEBTS.get(uid,[])))[:10]
        if not debts: await c.message.answer(T("rep_empty")); await c.answer(); return
        for it in debts:
            txt=debt_card(it, lang)
            if it["status"]=="wait": await c.message.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"], lang))
            else: await c.message.answer(txt)
        await c.answer(); return

# ====== ANALIZ HANDLER ======
@rt.message(Command("analiz"))
async def analiz_cmd(m: Message):
    uid = m.from_user.id
    lang=get_lang(uid); T=L(lang)
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

    if balance_uzs > 0:
        motiv = "👏 A'lo! Bu oy daromad sarflardan yuqori. Intizomga 5 baho — jamg‘arma o‘smoqda! 💹" if lang=="uz" else \
                "👏 Отлично! В этом месяце доход выше расходов. Дисциплина — огонь, сбережения растут! 💹"
    elif balance_uzs < 0:
        motiv = "⚠️ E'tiborli bo‘laylik: bu oy chiqim daromaddan oshib ketdi. Keyingi oy maqsad — sarfni biroz qisqartirib, kichik jamg‘arma boshlash! ✅" if lang=="uz" else \
                "⚠️ Внимательнее: в этом месяце расходы превысили доход. Цель на следующий — чуть ужаться и начать маленькую подушку! ✅"
    else:
        motiv = "🙂 Balans nolga yaqin. Yaxshi start! Endi har kuni mayda tejamkorlik bilan jamg‘armani yo‘lga qo‘ysak bo‘ladi." if lang=="uz" else \
                "🙂 Баланс около нуля. Хорошее начало! Понемногу экономим ежедневно — и пойдут сбережения."

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

@rt.callback_query(F.data.startswith("debt:"))
async def debt_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    if not has_access(uid): await c.message.answer(block_text(uid), reply_markup=kb_sub(lang)); await c.answer(); return
    d=c.data.split(":")[1]
    direction="mine" if d=="mine" else "given"
    head="🧾 Qarzim ro‘yxati:" if direction=="mine" and lang=="uz" else ("💸 Qarzdorlar ro‘yxati:" if lang=="uz" else ("🧾 Мои долги:" if direction=="mine" else "💸 Должники:"))
    await c.message.answer(head)
    debts=[x for x in MEM_DEBTS.get(uid,[]) if x["direction"]==direction]
    if not debts: await c.message.answer(T("rep_empty")); await c.answer(); return
    for it in reversed(debts[-10:]):
        txt=debt_card(it, lang)
        if it["status"]=="wait": await c.message.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"], lang))
        else: await c.message.answer(txt)
    await c.answer()

@rt.callback_query(F.data.startswith("debtdone:"))
async def debt_done(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid)
    _,direction,sid=c.data.split(":"); did=int(sid)
    for it in MEM_DEBTS.get(uid,[]):
        if it["id"]==did:
            if direction=="mine":
                it["status"]="paid"       # o'z qarzingizni to'ladingiz -> CHIQIM
                await save_tx(uid,"expense",it["amount"],it.get("currency","UZS"),"cash","💳 Qarz qaytarildi" if lang=="uz" else "💳 Долг оплачен",f"ID {did}")
            else:
                it["status"]="received"   # sizga qarz qaytdi -> KIRIM
                await save_tx(uid,"income",it["amount"],it.get("currency","UZS"),"cash","💳 Qarz qaytdi" if lang=="uz" else "💳 Долг возвращен",f"ID {did}")
            await c.message.edit_text(debt_card(it, lang)); await c.answer(("Holat yangilandi ✅" if lang=="uz" else "Статус обновлён ✅")); return
    await c.answer(("Topilmadi" if lang=="uz" else "Не найдено"), show_alert=True)

# ------ OBUNA (CLICK flow) ------
def create_click_link(pid:str, amount:int)->str:
    params = (
        f"merchant_id={CLICK_MERCHANT_ID}"
        f"&service_id={CLICK_SERVICE_ID}"
        f"&merchant_user_id={CLICK_MERCHANT_USER_ID}"
        f"&transaction_param={pid}"
        f"&amount={amount}"
    )
    if PAYMENT_RETURN_URL:
        params += f"&return_url={PAYMENT_RETURN_URL}"
    return f"{CLICK_PAY_URL_BASE}?{params}"

@rt.callback_query(F.data.startswith("sub:"))
async def sub_cb(c:CallbackQuery):
    uid=c.from_user.id
    lang=get_lang(uid); T=L(lang)
    code=c.data.split(":")[1]
    if code=="week":
        plan=T("sub_week"); days=7; price=7900
    else:
        plan=T("sub_month"); days=30; price=19900
    pid=str(uuid.uuid4())
    PENDING_PAYMENTS[pid]={"uid":uid,"plan":plan,"period_days":days,"amount":price,"currency":"UZS","status":"pending","created":now_tk()}
    link=create_click_link(pid, price)
    await c.message.answer(T("sub_created", plan=plan, amount=price), reply_markup=kb_payment(pid, link, lang))
    await c.answer()

@rt.callback_query(F.data.startswith("paycheck:"))
async def paycheck_cb(c:CallbackQuery):
    pid=c.data.split(":")[1]
    pay=PENDING_PAYMENTS.get(pid)
    lang=get_lang(pay["uid"]) if pay else get_lang(c.from_user.id)
    T=L(lang)
    await c.message.answer(T("pay_checking"))
    if not pay or pay["status"]!="paid":
        await c.message.answer(T("pay_notfound"))
        await c.answer(); return
    uid=pay["uid"]; until=now_tk()+timedelta(days=pay["period_days"])
    SUB_EXPIRES[uid]=until
    await c.message.answer(T("sub_activated", plan=pay["plan"], until=fmt_date(until)))
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
            they_uzs += amt if cur=="UZS" else amt*USD_UZS
            they_usd += amt if cur=="USD" else amt/USD_UZS
        else:
            i_uzs += amt if cur=="UZS" else amt*USD_UZS
            i_usd += amt if cur=="USD" else amt/USD_UZS

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

async def daily_reminder():
    while True:
        try:
            await asyncio.sleep(_sec_until(20,0))
            for uid in list(SEEN_USERS):
                try: await bot.send_message(uid, (t_uz if get_lang(uid)=="uz" else t_ru)("daily"))
                except: pass
        except: pass
        await asyncio.sleep(5)

async def debt_reminder():
    while True:
        try:
            today=fmt_date(now_tk()); hh=now_tk().strftime("%H")
            if hh in ("08","14"):
                for uid,debts in list(MEM_DEBTS.items()):
                    for it in debts:
                        if it["due"]==today and it["status"]=="wait":
                            key=(uid,it["id"],hh)
                            if key in DEBT_REMIND_SENT: continue
                            try:
                                if it["direction"]=="mine":
                                    txt=f"⏰ {today} — UZS {fmt_amount(it['amount'])} to‘lashni unutmang."
                                else:
                                    txt=f"⏰ {today} — UZS {fmt_amount(it['amount'])} qaytarilishini tekshiring."
                                await bot.send_message(uid, txt); DEBT_REMIND_SENT.add(key)
                            except: pass
        except: pass
        await asyncio.sleep(60)

# ====== COMMANDS ======
async def set_cmds():
    await bot.set_my_commands([
        BotCommand(command="version", description="Ishlayotgan versiya"),
        BotCommand(command="start", description="Boshlash / Start"),
        BotCommand(command="analiz", description="Oylik moliya tahlili"),
        BotCommand(command="menu", description="Menyuni yangilash"),
    ])

# ====== MAIN ======
async def main():
    dp.include_router(rt)
    await set_cmds()
    asyncio.create_task(daily_reminder())
    asyncio.create_task(debt_reminder())
    print("Bot ishga tushdi."); await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
