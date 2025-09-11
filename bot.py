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
SEEN_USERS: set[int] = set()

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
def t_ru(k,**kw):
    R={"start_choose":"Здравствуйте, пожалуйста, выберите язык бота.",
       "ask_name":"Как к вам обращаться?","btn_share":"📱 Отправить номер телефона",
       "btn_oferta":"📄 Публичная оферта","lang_again":"Выберите язык:"}
    return R.get(k,t_uz(k,**kw))
get_lang=lambda uid: USER_LANG.get(uid,"uz")

# ====== KB ======
def kb_lang(): 
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🇺🇿 O‘zbek"),KeyboardButton(text="🇷🇺 Русский")]],resize_keyboard=True,one_time_keyboard=True)
def kb_share(lang="uz"):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t_uz("btn_share") if lang=="uz" else t_ru("btn_share"),request_contact=True)]],resize_keyboard=True,one_time_keyboard=True)
def kb_main(lang="uz"):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t_uz("btn_hisobla"))],
            [KeyboardButton(text=t_uz("btn_hisobot")), KeyboardButton(text=t_uz("btn_qarz"))],
            [KeyboardButton(text=t_uz("btn_balance")), KeyboardButton(text=t_uz("btn_obuna"))],
            [KeyboardButton(text=t_uz("btn_lang"))],
        ], resize_keyboard=True)
def kb_oferta(lang="uz"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t_uz("btn_oferta") if lang=="uz" else t_ru("btn_oferta"), url=NOTION_OFER_URL)]])
def kb_rep_main(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t_uz("rep_tx"),callback_data="rep:tx")],[InlineKeyboardButton(text=t_uz("rep_debts"),callback_data="rep:debts")]])
def kb_rep_range():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t_uz("rep_day"),callback_data="rep:day")],[InlineKeyboardButton(text=t_uz("rep_week"),callback_data="rep:week")],[InlineKeyboardButton(text=t_uz("rep_month"),callback_data="rep:month")]])
def kb_debt_menu():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t_uz("debt_mine"),callback_data="debt:mine")],[InlineKeyboardButton(text=t_uz("debt_given"),callback_data="debt:given")]])
def kb_debt_done(direction,debt_id):
    lab=t_uz("btn_paid") if direction=="mine" else t_uz("btn_rcv")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=lab,callback_data=f"debtdone:{direction}:{debt_id}")]])
def kb_sub(): 
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t_uz("sub_week"),callback_data="sub:week")],[InlineKeyboardButton(text=t_uz("sub_month"),callback_data="sub:month")]])

def kb_payment(pid, pay_url):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t_uz("pay_click"), url=pay_url)],
        [InlineKeyboardButton(text=t_uz("pay_check"), callback_data=f"paycheck:{pid}")]
    ])

# ====== KUTILGAN FUNKS ======
async def update_bot_descriptions():
    total = len(SEEN_USERS)
    try:
        await bot.set_my_short_description(short_description=f"MoliyaUz • foydalanuvchilar: {total}")
        await bot.set_my_short_description(short_description=f"MoliyaUz • пользователей: {total}", language_code="ru")
        await bot.set_my_description(description=f"Foydalanuvchilar: {total}")
        await bot.set_my_description(description=f"Пользователей: {total}", language_code="ru")
    except: pass

async def counter_refresher():
    while True:
        try: await update_bot_descriptions()
        except: pass
        await asyncio.sleep(900)

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
    if "ertaga" in t: return (now_tk().date()+timedelta(days=1)).strftime("%d.%m.%Y")
    if "bugun" in t: return (now_tk().date()).strftime("%d.%m.%Y")
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
    m=re.search(r"\b([a-zA-Z\u0400-\u04FF‘'ʼ`-]+)(?:\s+(akaga|opaga|ukaga|singlimga))?\s*(ga|qa)\b",t)
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

def debt_card(it:dict)->str:
    s={"wait":t_uz("st_wait"),"paid":t_uz("st_paid"),"received":t_uz("st_rcv")}[it.get("status","wait")]
    return t_uz("card_debt", created=fmt_date(it["ts"]), who=it["counterparty"], cur=it.get("currency","UZS"),
                amount=fmt_amount(it["amount"]), due=it["due"], status=s)

# ====== REPORT HELPERS ======
def report_range(kind:str):
    n=now_tk()
    if kind=="day": return n.replace(hour=0,minute=0,second=0,microsecond=0), n
    if kind=="week": return n-timedelta(days=7), n
    return n-timedelta(days=30), n

# ====== HANDLERS ======
@rt.message(CommandStart())
async def start(m:Message):
    uid=m.from_user.id
    SEEN_USERS.add(uid); TRIAL_START.setdefault(uid, now_tk())
    await update_bot_descriptions()
    STEP[uid]="lang"; await m.answer(t_uz("start_choose"), reply_markup=kb_lang())

@rt.message(Command("approve"))
async def approve_cmd(m:Message):
    # test: /approve <pid>
    if ADMIN_ID and m.from_user.id!=ADMIN_ID: return
    parts=m.text.strip().split()
    if len(parts)!=2: await m.answer("format: /approve <pid>"); return
    pid=parts[1]
    pay=PENDING_PAYMENTS.get(pid)
    if not pay: await m.answer("pid topilmadi"); return
    pay["status"]="paid"
    await m.answer(f"{pid} -> paid")

@rt.message(F.text)
async def on_text(m:Message):
    uid=m.from_user.id
    t=(m.text or "").strip()
    step=STEP.get(uid)

    if step=="lang":
        low=t.lower()
        if "uz" in low or "o‘z" in low: USER_LANG[uid]="uz"
        elif "рус" in low or "ru" in low: USER_LANG[uid]="ru"
        else: return
        STEP[uid]="name"
        await m.answer(t_uz("ask_name") if get_lang(uid)=="uz" else t_ru("ask_name"), reply_markup=ReplyKeyboardRemove()); return

    if step=="name":
        lang=get_lang(uid)
        await m.answer(t_uz("welcome") if lang=="uz" else t_ru("welcome"), reply_markup=kb_share(lang))
        await m.answer("—", reply_markup=kb_oferta(lang))
        STEP[uid]="need_phone"; return

    if step=="need_phone": return

    if step in ("debt_mine_due","debt_given_due"):
        due=parse_due_date(t)
        if not due: await m.answer(t_uz("date_need")); return
        tmp=PENDING_DEBT.get(uid); 
        if not tmp: STEP[uid]="main"; await m.answer(t_uz("enter_tx"), reply_markup=kb_main(get_lang(uid))); return
        await save_debt(uid, tmp["direction"], tmp["amount"], tmp["currency"], tmp["who"], due)
        if tmp["direction"]=="mine":
            await m.answer(t_uz("debt_saved_mine", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
        else:
            await m.answer(t_uz("debt_saved_given", who=tmp["who"], cur=tmp["currency"], amount=fmt_amount(tmp["amount"]), due=due))
        PENDING_DEBT.pop(uid, None); STEP[uid]="main"; return

    # menyular
    if t==t_uz("btn_hisobla"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub()); return
        STEP[uid]="input_tx"; await m.answer(t_uz("enter_tx"), reply_markup=kb_main(get_lang(uid))); return

    if t==t_uz("btn_hisobot"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub()); return
        await m.answer(t_uz("report_main"), reply_markup=kb_rep_main()); return

    if t==t_uz("btn_qarz"):
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub()); return
        await m.answer(t_uz("debt_menu"), reply_markup=kb_debt_menu()); return

    if t==t_uz("btn_balance"):
        await send_balance(uid, m); return

    if t==t_uz("btn_obuna"):
        await m.answer(t_uz("sub_choose"), reply_markup=kb_sub()); return

    if t==t_uz("btn_lang"):
        STEP[uid]="lang"; await m.answer(t_uz("lang_again"), reply_markup=kb_lang()); return

    # hisobla input
    if step=="input_tx":
        if not has_access(uid): await m.answer(block_text(uid), reply_markup=kb_sub()); return
        kind=guess_kind(t)
        if kind in ("debt_mine","debt_given"):
            amount=parse_amount(t) or 0
            if amount<=0: await m.answer(t_uz("debt_need")); return
            curr=detect_currency(t); who=parse_counterparty(t)
            due0=parse_due_date(t)
            if due0:
                await save_debt(uid, "mine" if kind=="debt_mine" else "given", amount, curr, who, due0)
                if kind=="debt_mine":
                    await m.answer(t_uz("debt_saved_mine", who=who, cur=curr, amount=fmt_amount(amount), due=due0))
                else:
                    await m.answer(t_uz("debt_saved_given", who=who, cur=curr, amount=fmt_amount(amount), due=due0))
            else:
                PENDING_DEBT[uid]={"direction":"mine" if kind=="debt_mine" else "given","amount":amount,"currency":curr,"who":who}
                STEP[uid]="debt_mine_due" if kind=="debt_mine" else "debt_given_due"
                await m.answer(t_uz("ask_due_mine") if kind=="debt_mine" else t_uz("ask_due_given"))
            return

        amount=parse_amount(t)
        if amount is None: await m.answer(t_uz("need_sum")); return
        curr=detect_currency(t); acc=detect_account(t)
        if guess_kind(t)=="income":
            await save_tx(uid,"income",amount,curr,acc,"💪 Mehnat daromadlari",t)
            await m.answer(t_uz("tx_inc",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),desc=t))
        else:
            cat=guess_category(t)
            await save_tx(uid,"expense",amount,curr,acc,cat,t)
            await m.answer(t_uz("tx_exp",date=fmt_date(now_tk()),cur=curr,amount=fmt_amount(amount),cat=cat,desc=t))
        return

    await m.answer(t_uz("enter_text"))

@rt.message(F.contact)
async def on_contact(m:Message):
    uid=m.from_user.id
    if STEP.get(uid)!="need_phone": return
    await m.answer(t_uz("menu"), reply_markup=kb_main(get_lang(uid))); STEP[uid]="main"

# ====== CALLBACKS ======
@rt.callback_query(F.data.startswith("rep:"))
async def rep_cb(c:CallbackQuery):
    uid=c.from_user.id
    if not has_access(uid): await c.message.answer(block_text(uid), reply_markup=kb_sub()); await c.answer(); return
    kind=c.data.split(":")[1]
    if kind=="tx":
        await c.message.answer(t_uz("report_main"), reply_markup=kb_rep_range()); await c.answer(); return
    if kind in ("day","week","month"):
        since,until=report_range(kind)
        items=[it for it in MEM_TX.get(uid,[]) if since<=it["ts"]<=until]
        if not items: await c.message.answer(t_uz("rep_empty")); await c.answer(); return
        lines=[]
        for it in items:
            lines.append(t_uz("rep_line",date=fmt_date(it["ts"]),kind=("Kirim" if it["kind"]=="income" else "Chiqim"),cat=it["category"],amount=fmt_amount(it["amount"]),cur=it["currency"]))
        await c.message.answer("\n".join(lines)); await c.answer(); return
    if kind=="debts":
        debts=list(reversed(MEM_DEBTS.get(uid,[])))[:10]
        if not debts: await c.message.answer(t_uz("rep_empty")); await c.answer(); return
        for it in debts:
            txt=debt_card(it)
            if it["status"]=="wait": await c.message.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"]))
            else: await c.message.answer(txt)
        await c.answer(); return

@rt.callback_query(F.data.startswith("debt:"))
async def debt_cb(c:CallbackQuery):
    uid=c.from_user.id
    if not has_access(uid): await c.message.answer(block_text(uid), reply_markup=kb_sub()); await c.answer(); return
    d=c.data.split(":")[1]
    direction="mine" if d=="mine" else "given"
    head="🧾 Qarzim ro‘yxati:" if direction=="mine" else "💸 Qarzdorlar ro‘yxati:"
    await c.message.answer(head)
    debts=[x for x in MEM_DEBTS.get(uid,[]) if x["direction"]==direction]
    if not debts: await c.message.answer(t_uz("rep_empty")); await c.answer(); return
    for it in reversed(debts[-10:]):
        txt=debt_card(it)
        if it["status"]=="wait": await c.message.answer(txt, reply_markup=kb_debt_done(it["direction"],it["id"]))
        else: await c.message.answer(txt)
    await c.answer()

@rt.callback_query(F.data.startswith("debtdone:"))
async def debt_done(c:CallbackQuery):
    uid=c.from_user.id
    _,direction,sid=c.data.split(":"); did=int(sid)
    for it in MEM_DEBTS.get(uid,[]):
        if it["id"]==did:
            it["status"]="paid" if direction=="mine" else "received"
            await c.message.edit_text(debt_card(it)); await c.answer("Holat yangilandi ✅"); return
    await c.answer("Topilmadi", show_alert=True)

# ------ OBUNA (CLICK flow) ------
def create_click_link(pid:str, amount:int)->str:
    # Sizning CLICK tizimingizga mos parametrlarni qo‘ying
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
    code=c.data.split(":")[1]
    if code=="week":
        plan=t_uz("sub_week"); days=7; price=7900
    else:
        plan=t_uz("sub_month"); days=30; price=19900
    pid=str(uuid.uuid4())
    PENDING_PAYMENTS[pid]={"uid":uid,"plan":plan,"period_days":days,"amount":price,"currency":"UZS","status":"pending","created":now_tk()}
    link=create_click_link(pid, price)
    await c.message.answer(t_uz("sub_created", plan=plan, amount=price), reply_markup=kb_payment(pid, link))
    await c.answer()

@rt.callback_query(F.data.startswith("paycheck:"))
async def paycheck_cb(c:CallbackQuery):
    pid=c.data.split(":")[1]
    pay=PENDING_PAYMENTS.get(pid)
    await c.message.answer(t_uz("pay_checking"))
    if not pay or pay["status"]!="paid":
        await c.message.answer(t_uz("pay_notfound"))
        await c.answer(); return
    uid=pay["uid"]; until=now_tk()+timedelta(days=pay["period_days"])
    SUB_EXPIRES[uid]=until
    await c.message.answer(t_uz("sub_activated", plan=pay["plan"], until=fmt_date(until)))
    await update_bot_descriptions()
    await c.answer()

# ====== BALANS ======
async def send_balance(uid:int, m:Message):
    # TX balans (cash / card, UZS / USD)
    sums={("cash","UZS"):0,("cash","USD"):0,("card","UZS"):0,("card","USD"):0}
    for it in MEM_TX.get(uid,[]):
        sign = 1 if it["kind"]=="income" else -1
        k=(it["account"], it["currency"])
        if k not in sums: sums[k]=0
        sums[k]+= sign*it["amount"]
    # Debts
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

    txt=t_uz("balance",
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
    if any(w in t for w in ["taksi","yo‘l","yol","benzin","transport","metro","avtobus"]): return "🚌 Transport"
    if any(w in t for w in ["ovqat","kafe","restoran","non","taom","fastfood","osh","shashlik"]): return "🍔 Oziq-ovqat"
    if any(w in t for w in ["kommunal","svet","gaz","suv"]): return "💡 Kommunal"
    if any(w in t for w in ["internet","telefon","uzmobile","beeline","ucell","uztelecom"]): return "📱 Aloqa"
    if any(w in t for w in ["ijara","kvartira","arenda","ipoteka"]): return "🏠 Uy-ijara"
    if any(w in t for w in ["dorixona","shifokor","apteka","dori"]): return "💊 Sog‘liq"
    if any(w in t for w in ["soliq","jarima","patent"]): return "💸 Soliq/Jarima"
    if any(w in t for w in ["kiyim","do‘kon","do'kon","bozor","market","savdo","shopping","supermarket"]): return "🛍 Savdo"
    if any(w in t for w in ["oylik","maosh","bonus","premiya"]): return "💪 Mehnat daromadlari"
    return "🧾 Boshqa xarajatlar"

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
                try: await bot.send_message(uid, t_uz("daily"))
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
                                    txt=f"⏰ Bugun {it['due']} — UZS {fmt_amount(it['amount'])} to‘lashni unutmang."
                                else:
                                    txt=f"⏰ Bugun {it['due']} — UZS {fmt_amount(it['amount'])} qaytarilishini tekshiring."
                                await bot.send_message(uid, txt); DEBT_REMIND_SENT.add(key)
                            except: pass
        except: pass
        await asyncio.sleep(60)

# ====== COMMANDS ======
async def set_cmds():
    await bot.set_my_commands([BotCommand(command="start", description="Boshlash / Start")])

# ====== MAIN ======
async def main():
    dp.include_router(rt)
    await set_cmds()
    asyncio.create_task(counter_refresher())
    asyncio.create_task(daily_reminder())
    asyncio.create_task(debt_reminder())
    print("Bot ishga tushdi."); await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())
