import os
import random
import asyncio
import logging
from datetime import date, datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from supabase import create_client, Client
from aiohttp import web
import aiohttp

# ---- ЧИСТЫЕ ЛОГИ ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("pet-bot")

BOT_TOKEN     = os.environ["BOT_TOKEN"]
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
WEBAPP_URL    = os.environ["WEBAPP_URL"]
XROCKET_TOKEN = os.environ["XROCKET_TOKEN"]
XROCKET_API   = os.environ.get("XROCKET_API", "https://pay.api.xrocket.exchange")
PORT          = int(os.environ.get("PORT", 8080))

ADMIN_IDS = {8130244626}
MIN_CHEQUE = 0.01
SKINS = ["classic", "cat", "dragon", "space", "dino"]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(uid): return uid in ADMIN_IDS
def gen_code(n=6): return "".join(random.choice(CODE_CHARS) for _ in range(n))
def webapp_url(pid): return f"{WEBAPP_URL}?pet={pid}"
def is_uuid(s): return s and len(s) >= 30 and "-" in s
def escape_html(s): return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def pet_line(p):
    if not p: return "—"
    lvl = min(30, (p.get("xp") or 0) // 50 + 1)
    return f"🐾 <b>{p.get('name','?')}</b> · ур. {lvl} · <code>{p.get('id')}</code>"


def pick_link(obj):
    if not obj or not isinstance(obj, dict): return None
    links = obj.get("links") or {}
    if isinstance(links, dict):
        for k in ("telegramMiniAppLink","telegramBotLink","webLink","link","url"):
            v = links.get(k)
            if isinstance(v, str) and v.startswith("http"): return v
    for k in ("link","url","webLink","telegramBotLink","telegramMiniAppLink"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"): return v
    return None


def one(q):
    try:
        r = q.limit(1).execute(); d = getattr(r,"data",None) or []
        return d[0] if d else None
    except Exception as e:
        log.warning("one(): %s", e); return None


def many(q):
    try:
        r = q.execute(); return getattr(r,"data",None) or []
    except Exception as e:
        log.warning("many(): %s", e); return []


def my_pets(uid):
    return many(sb.table("pets").select("*").eq("owner_id", uid))


async def resolve_pet(message: Message, args: list):
    if args and is_uuid(args[0].strip()):
        pid = args[0].strip()
        pet = one(sb.table("pets").select("*").eq("id", pid))
        if not pet:
            await message.answer(f"❌ Питомец <code>{pid}</code> не найден.", parse_mode="HTML")
            return None, None
        return pet, args[1:]

    pets = my_pets(message.from_user.id)
    if not pets:
        await message.answer("❌ У тебя нет питомцев.")
        return None, None
    if len(pets) > 1:
        lines = ["⚠️ У тебя несколько питомцев — укажи <b>ID первым аргументом</b>.\n"]
        for p in pets:
            lvl = min(30, (p.get("xp") or 0) // 50 + 1)
            lines.append(f"• <b>{p['name']}</b> · ур. {lvl}\n  <code>{p['id']}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return None, None
    return pets[0], args


# ============ xRocket ============

async def xrocket_request(method, path, json=None):
    url = f"{XROCKET_API}{path}"
    headers = {"Authorization": f"Bearer {XROCKET_TOKEN}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.request(method, url, headers=headers, json=json, timeout=30) as r:
            raw = await r.text()
            try: data = await r.json() if raw else {}
            except: data = {"raw": raw}
            if r.status >= 400:
                err = RuntimeError(f"xRocket {r.status}: {data}")
                err.status = r.status; err.data = data; err.path = path; err.method = method
                log.error("xRocket %s %s → %s", method, path, r.status)
                raise err
            return data


async def create_invoice(amount, currency, description):
    return await xrocket_request("POST", "/api/v1/invoices", {
        "priceAmount": str(amount), "priceCurrency": currency,
        "description": description, "numPayments": 1, "expiresIn": 3600000,
    })


async def get_invoice_status(iid):
    return await xrocket_request("GET", f"/api/v1/invoices/{iid}")


async def create_cheque(uid, amount, currency, description):
    return await xrocket_request("POST", "/api/v1/cheques", {
        "asset": currency, "amount": str(amount), "description": description,
        "targetType": "telegram_user_id", "target": str(uid),
    })


async def delete_cheque(cid):
    try: return await xrocket_request("DELETE", f"/api/v1/cheques/{cid}")
    except Exception as e1:
        if getattr(e1,"status",None) in (404,405):
            try: return await xrocket_request("DELETE", f"/api/v1/cheques?chequeId={cid}")
            except: return await xrocket_request("POST", f"/api/v1/cheques/{cid}/cancel")
        raise


async def list_xrocket_cheques():
    for p in ("/api/v1/cheques?status=active", "/api/v1/cheques", "/api/v1/cheques/my"):
        try:
            d = await xrocket_request("GET", p)
            if isinstance(d, list): return d
            if isinstance(d, dict):
                for k in ("cheques","items","results","data"):
                    if isinstance(d.get(k), list): return d[k]
        except: continue
    return []


async def get_app_balance():
    for p in ("/api/v1/app/balance", "/api/v1/balance", "/api/v1/me"):
        try: return {"path": p, "data": await xrocket_request("GET", p)}
        except: continue
    return None


def xrocket_error_text(e):
    d = getattr(e,"data",None) or {}
    kind = d.get("kind") or ""; title = d.get("title") or ""
    detail = d.get("detail") or str(e)
    if "amount_more_than_app_balance" in str(d) or "more than app balance" in detail.lower():
        return "⚠️ На балансе приложения xRocket недостаточно средств. Пополни @xRocket → Wallet."
    if "operation_disabled" in kind or "disabled" in detail.lower():
        return "⚠️ xRocket отключил эту операцию для твоего приложения."
    return f"⚠️ xRocket {getattr(e,'status','?')}: {title or detail}"


# ============ /start ============

@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()
    user = message.from_user

    if payload.startswith("join_"):
        code = payload[5:]
        pet = one(sb.table("pets").select("*").eq("invite_code", code))
        if not pet:
            await message.answer("Питомец не найден."); return
        sb.table("members").upsert({
            "pet_id": pet["id"], "user_id": user.id,
            "first_name": user.first_name or "Гость", "username": user.username,
        }, on_conflict="pet_id,user_id").execute()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐾 Открыть питомца",
                                  web_app=WebAppInfo(url=webapp_url(pet["id"])))]])
        await message.answer(
            f"🐾 Ты ухаживаешь за «{pet['name']}»!\n\nID: <code>{pet['id']}</code>",
            parse_mode="HTML", reply_markup=kb); return

    if payload.startswith("topup_"):
        parts = payload[6:].split("_")
        pet_id = parts[0]
        try: cents = int(parts[1]) if len(parts) > 1 else 100
        except: cents = 100
        amount = cents / 100.0
        if amount <= 0 or amount > 1000:
            await message.answer("Некорректная сумма."); return
        pet = one(sb.table("pets").select("*").eq("id", pet_id))
        if not pet or pet["owner_id"] != user.id:
            await message.answer("❌ Только владелец."); return
        await message.answer(f"💳 Создаю счёт на {amount} USDT…\n{pet_line(pet)}", parse_mode="HTML")
        try:
            inv = await create_invoice(amount, pet.get("currency","USDT"),
                                       f"Банк «{pet['name']}» ({pet['id'][:8]})")
        except Exception as e:
            await message.answer(xrocket_error_text(e)); return
        link = pick_link(inv); iid = inv.get("id") or inv.get("invoiceId")
        if not link or not iid:
            await message.answer("⚠️ Нет ссылки."); return
        sb.table("invoices").insert({
            "invoice_id": str(iid), "pet_id": pet_id, "owner_id": user.id,
            "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
        }).execute()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
        await message.answer(
            f"💳 <b>Счёт {amount} USDT</b>\n{pet_line(pet)}\nInvoice: <code>{iid}</code>",
            parse_mode="HTML", reply_markup=kb); return

    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3: await message.answer("Ошибка формата."); return
        try: cents = int(parts[1]); top_n = int(parts[2])
        except: await message.answer("Ошибка параметров."); return
        await run_salary_pet(parts[0], user.id, cents/100.0, top_n, message); return

    await message.answer(
        "👋 Бот общего питомца.\n\n"
        "Создай питомца — получишь ссылку для друзей.\n\n"
        "В группе можно управлять питомцем командами:\n"
        "<code>/pet имя покормить</code>\n"
        "<code>/pet имя погладить</code>\n"
        "<code>/pet имя играть</code>\n"
        "<code>/pet имя помыть</code>\n"
        "<code>/pet имя лечить</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")]]))


# ============ проверка оплаты ============

@dp.callback_query(F.data.startswith("check_"))
async def cb_check_payment(call: CallbackQuery):
    iid = call.data[6:]; user = call.from_user
    inv = one(sb.table("invoices").select("*").eq("invoice_id", iid))
    if not inv or inv["owner_id"] != user.id:
        await call.answer("Нет доступа", show_alert=True); return
    if inv["status"] == "paid":
        await call.answer("Уже оплачен ✅", show_alert=True); return
    try:
        st = await get_invoice_status(iid)
    except Exception as e:
        d = getattr(e,"data",None) or {}
        await call.answer(f"xRocket: {(d.get('detail') or str(e))[:180]}", show_alert=True); return
    status = (st.get("status") or st.get("state") or (st.get("invoice") or {}).get("status") or "").lower()
    if status in ("paid","success","completed","paid_success"):
        pet = one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            nb = float(pet.get("bank_balance") or 0) + float(inv["amount"])
            sb.table("pets").update({"bank_balance": nb}).eq("id", inv["pet_id"]).execute()
            sb.table("invoices").update({"status": "paid"}).eq("invoice_id", iid).execute()
            await call.answer("Оплачено! ✅", show_alert=True)
            try:
                await call.message.edit_text(
                    f"✅ <b>Банк пополнен на {inv['amount']} USDT</b>\n"
                    f"{pet_line(pet)}\nБаланс: <b>{nb:.4f} USDT</b>",
                    parse_mode="HTML")
            except: pass
            return
    await call.answer(f"Статус: {status or 'неизвестно'}", show_alert=True)


# ============ /balance ============

@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    pets = my_pets(m.from_user.id)
    if not pets:
        await m.answer("Нет питомцев."); return
    lines = ["🏦 <b>Балансы:</b>\n"]
    for p in pets:
        lines.append(f"• <b>{p['name']}</b> — <b>{float(p.get('bank_balance') or 0):.4f} USDT</b>\n  <code>{p['id']}</code>")
    await m.answer("\n".join(lines), parse_mode="HTML")


# ============ /topup ============

@dp.message(Command("topup"))
async def cmd_topup(m: Message):
    user = m.from_user
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return

    if not rest:
        await m.answer(f"Формат: /topup [pet_id] 1\n\n{pet_line(pet)}", parse_mode="HTML"); return
    try: amount = float(rest[0].replace(",","."))
    except: await m.answer("Не разобрать сумму."); return
    if amount <= 0 or amount > 1000:
        await m.answer("0.01 – 1000 USDT"); return

    await m.answer(f"💳 Создаю счёт на {amount} USDT…\n{pet_line(pet)}", parse_mode="HTML")
    try:
        inv = await create_invoice(amount, pet.get("currency","USDT"),
                                   f"Банк «{pet['name']}» ({pet['id'][:8]})")
    except Exception as e:
        await m.answer(xrocket_error_text(e)); return
    link = pick_link(inv); iid = inv.get("id") or inv.get("invoiceId")
    if not link or not iid:
        await m.answer("⚠️ Нет ссылки."); return
    sb.table("invoices").insert({
        "invoice_id": str(iid), "pet_id": pet["id"], "owner_id": user.id,
        "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
    }).execute()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
    await m.answer(
        f"💳 <b>Счёт {amount} USDT</b>\n{pet_line(pet)}\nInvoice: <code>{iid}</code>",
        parse_mode="HTML", reply_markup=kb)


# ============ /salary ============

@dp.message(Command("salary"))
async def cmd_salary(m: Message):
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return

    amount = None; top_n = None
    if len(rest) >= 1:
        try: amount = float(rest[0].replace(",","."))
        except: await m.answer("Формат: /salary [pet_id] 1 3"); return
    if len(rest) >= 2:
        try: top_n = int(rest[1])
        except: await m.answer("Формат: /salary [pet_id] 1 3"); return

    await run_salary_pet(pet["id"], m.from_user.id, amount, top_n, m)


async def run_salary_pet(pet_id, owner_id, amount, top_n, message):
    pet = one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet:
        await message.answer("Питомец не найден."); return
    if pet["owner_id"] != owner_id:
        await message.answer(f"❌ Только владелец.\n{pet_line(pet)}", parse_mode="HTML"); return

    balance = float(pet.get("bank_balance") or 0)
    if balance <= 0:
        await message.answer(f"❌ Банк пуст.\n{pet_line(pet)}", parse_mode="HTML"); return
    if amount is None or amount <= 0: amount = balance
    if amount > balance:
        await message.answer(f"В банке только {balance:.4f}."); return

    today = date.today().isoformat()
    members = many(sb.table("members").select("*").eq("pet_id", pet["id"]))
    for m in members:
        if m.get("today_date") != today:
            sb.table("members").update({"today_score": 0, "today_date": today})\
              .eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
            m["today_score"] = 0
    active = [m for m in members if (m.get("today_score") or 0) > 0]
    if not active:
        await message.answer(
            f"❌ Сегодня никто не активен.\n{pet_line(pet)}\nВсего: {len(members)}",
            parse_mode="HTML"); return
    active.sort(key=lambda x: x["today_score"], reverse=True)
    winners = active[:top_n] if (top_n and 0 < top_n < len(active)) else active
    total = sum(w["today_score"] for w in winners)
    if total <= 0: await message.answer("Нет очков."); return
    min_share = min(round(amount * (w["today_score"]/total), 6) for w in winners)
    if min_share < MIN_CHEQUE:
        await message.answer(f"❌ Слишком мелкие чеки ({min_share:.4f})."); return

    sb.table("pets").update({"bank_balance": balance - amount}).eq("id", pet["id"]).execute()
    po = sb.table("payouts").insert({
        "pet_id": pet["id"], "owner_id": owner_id, "total_amount": amount,
        "currency": pet.get("currency","USDT"), "member_count": len(winners)}).execute()
    payout = po.data[0]

    await message.answer(
        f"💸 Раздаю <b>{amount:.4f} USDT</b>\n{pet_line(pet)}\n"
        f"Получателей: {len(winners)}",
        parse_mode="HTML")

    sent = 0; total_sent = 0.0; paid = []; failed_msg = ""
    for m in winners:
        score = m["today_score"]; share = round(amount * (score/total), 6)
        if share < MIN_CHEQUE: continue
        try:
            ch = await create_cheque(m["user_id"], share, pet.get("currency","USDT"),
                                     f"Зарплата · {pet['name']}")
        except Exception as e:
            d = getattr(e,"data",None) or {}
            failed_msg = d.get("detail") or d.get("title") or str(e); break
        cid = ch.get("chequeId") or ch.get("id"); link = pick_link(ch)
        sb.table("cheques").insert({
            "payout_id": payout["id"], "pet_id": pet["id"], "user_id": m["user_id"],
            "amount": share, "cheque_id": cid, "cheque_link": link,
            "status": "sent" if link else "no_link"}).execute()
        if link:
            try:
                await bot.send_message(m["user_id"],
                    f"💰 <b>Зарплата</b>\n\n"
                    f"Питомец: <b>{pet['name']}</b>\n"
                    f"ID: <code>{pet['id']}</code>\n"
                    f"Активность: {score} очков\n"
                    f"Начислено: <b>{share:.4f} USDT</b>\n"
                    f"Чек: <code>{cid}</code>\n\nЗабрать: {link}",
                    parse_mode="HTML", disable_web_page_preview=True)
            except: pass
        sent += 1; total_sent += share; paid.append(m["user_id"])
    refund = round(amount - total_sent, 6)
    if refund > 0:
        pn = one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        sb.table("pets").update({"bank_balance": cb + refund}).eq("id", pet["id"]).execute()
    for m in winners:
        if m["user_id"] in paid:
            sb.table("members").update({"today_score": 0, "today_date": today})\
              .eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
    pn = one(sb.table("pets").select("*").eq("id", pet["id"]))
    final = float(pn.get("bank_balance") or 0)
    text = (f"✅ <b>Выплата</b>\n{pet_line(pet)}\n"
            f"Раздано: {total_sent:.4f} из {amount:.4f}\n"
            f"Чеков: {sent} из {len(winners)}\n"
            f"Остаток: <b>{final:.4f} USDT</b>")
    if failed_msg: text += f"\n\n⚠️ {failed_msg}"
    await message.answer(text, parse_mode="HTML")


# ============ /pet — управление через чат ============

PET_ACTIONS = {
    "feed": {"emoji":"🍖","label":"Покормил","cd": 5*60, "xp": 2,
             "effects": {"hunger": 25, "energy": 10}, "score": 1},
    "pet":  {"emoji":"✋","label":"Погладил","cd": 60,   "xp": 1,
             "effects": {"mood": 10}, "score": 1},
    "play": {"emoji":"🎾","label":"Поиграл","cd": 10*60, "xp": 5,
             "effects": {"mood": 20, "energy": -15}, "score": 2},
    "wash": {"emoji":"🧼","label":"Помыл","cd": 15*60, "xp": 2,
             "effects": {"clean": 30}, "score": 1},
    "heal": {"emoji":"💊","label":"Полечил","cd": 60*60, "xp": 3,
             "effects": {"health": 20}, "score": -30, "cost": 30},
}

PET_ALIASES = {
    "покормить":"feed","покорми":"feed","кормить":"feed","feed":"feed","еда":"feed","кушать":"feed",
    "погладить":"pet","погладь":"pet","гладить":"pet","ласка":"pet","pet":"pet",
    "играть":"play","поиграть":"play","поиграй":"play","play":"play","игра":"play",
    "помыть":"wash","помой":"wash","мыть":"wash","купать":"wash","искупать":"wash","wash":"wash",
    "лечить":"heal","полечить":"heal","вылечить":"heal","heal":"heal","лечение":"heal",
    "инфо":"info","статы":"info","stats":"info","информация":"info",
}

PET_STAGE_NAMES = {
    "classic": ["Яйцо","Птенец","Юнец","Подросток","Взрослый","Опытный","Старейшина","Легенда"],
    "cat":     ["Яйцо","Котёнок","Котик","Подросший кот","Крупный кот","Хищник","Царь зверей","Тигр"],
    "dragon":  ["Яйцо","Ящерка","Дракончик","Юный дракон","Дракон","Взрослый дракон","Древний дракон","Огненный владыка"],
    "space":   ["Туманность","Луна","Звезда","Яркая звезда","Созвездие","Комета","Сверхновая","Солнце"],
    "dino":    ["Яйцо","Ящерка","Динозаврик","Юный дино","Ящер","Хищный дино","Древний ящер","Вулкан"],
}


def _stage_idx(level):
    return 7 if level>=28 else 6 if level>=24 else 5 if level>=20 else 4 if level>=16 \
        else 3 if level>=12 else 2 if level>=8 else 1 if level>=4 else 0


def apply_pet_tick(pet):
    now = datetime.now(timezone.utc)
    last_str = pet.get("last_tick_at")
    if not last_str: return pet
    try:
        last = datetime.fromisoformat(last_str.replace("Z","+00:00"))
    except Exception:
        return pet
    minutes = (now - last).total_seconds() / 60
    if minutes < 1: return pet
    hours = minutes / 60
    m = now.month
    is_winter = m in (12,1,2); is_summer = m in (6,7,8)
    e_rate = 2.5 if is_winter else 2
    m_rate = 2.5 if is_summer else 3

    def cl(v, mn=0, mx=100): return max(mn, min(mx, round(v)))

    c = dict(pet)
    c["hunger"] = cl((c.get("hunger") or 100) - 3*hours)
    c["mood"]   = cl((c.get("mood") or 100) - m_rate*hours)
    c["energy"] = cl((c.get("energy") or 100) - e_rate*hours)
    c["clean"]  = cl((c.get("clean") or 100) - 2*hours)
    if c["hunger"] < 20 or c["clean"] < 20:
        c["health"] = cl((c.get("health") or 100) - 5*hours)
    elif c["hunger"] > 60 and c["mood"] > 60 and c["clean"] > 60:
        c["health"] = cl((c.get("health") or 100) + 2*hours)
    c["last_tick_at"] = now.isoformat()
    return c


def save_pet_tick(pet):
    ticked = apply_pet_tick(pet)
    if ticked.get("last_tick_at") != pet.get("last_tick_at"):
        sb.table("pets").update({
            "hunger": ticked["hunger"], "mood": ticked["mood"],
            "energy": ticked["energy"], "clean": ticked["clean"],
            "health": ticked["health"], "last_tick_at": ticked["last_tick_at"],
        }).eq("id", pet["id"]).execute()
    return ticked


async def _send_pet_info(message, pet):
    pet = save_pet_tick(pet)
    lvl = min(30, (pet.get("xp") or 0) // 50 + 1)
    st = _stage_idx(lvl)
    names = PET_STAGE_NAMES.get(pet.get("skin","classic"), PET_STAGE_NAMES["classic"])
    text = (
        f"🐾 <b>{pet['name']}</b> · {names[st]} · ур. {lvl}\n\n"
        f"🍖 Сытость: {pet.get('hunger',0)}%\n"
        f"😊 Настроение: {pet.get('mood',0)}%\n"
        f"⚡ Энергия: {pet.get('energy',0)}%\n"
        f"🧼 Чистота: {pet.get('clean',0)}%\n"
        f"❤️ Здоровье: {pet.get('health',0)}%\n\n"
        f"<b>Действия:</b>\n"
        f"<code>/pet {pet['name']} покормить</code>\n"
        f"<code>/pet {pet['name']} погладить</code>\n"
        f"<code>/pet {pet['name']} играть</code>\n"
        f"<code>/pet {pet['name']} помыть</code>\n"
        f"<code>/pet {pet['name']} лечить</code>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("pet"))
async def cmd_pet(message: Message):
    user = message.from_user
    parts = (message.text or "").split()
    args = parts[1:] if len(parts) > 1 else []

    if not args:
        members = many(sb.table("members").select("pets!inner(*)").eq("user_id", user.id))
        if not members:
            await message.answer("У тебя нет питомцев. Создай через /start")
            return
        lines = ["🐾 <b>Твои питомцы:</b>\n"]
        for m in members[:10]:
            p = m.get("pets") or {}
            lvl = min(30, (p.get("xp") or 0) // 50 + 1)
            lines.append(f"• <b>{p['name']}</b> · ур. {lvl}\n  <code>/pet {p['name']}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    name_query = args[0].strip()
    action_key = args[1].lower() if len(args) > 1 else None

    pet = None
    members = many(sb.table("members").select("pets!inner(*)").eq("user_id", user.id))

    if len(name_query) >= 30 and "-" in name_query:
        candidate = one(sb.table("pets").select("*").eq("id", name_query))
        if candidate:
            ok = one(sb.table("members").select("*").eq("pet_id", candidate["id"]).eq("user_id", user.id))
            if ok: pet = candidate
    else:
        nq = name_query.lower()
        for m in members:
            p = m.get("pets") or {}
            if (p.get("name") or "").lower() == nq:
                pet = p; break
        if not pet:
            for m in members:
                p = m.get("pets") or {}
                if nq in (p.get("name") or "").lower():
                    pet = p; break

    if not pet:
        await message.answer(f"🐾 Питомец «{escape_html(name_query)}» не найден среди твоих. Список: /pet")
        return

    if not action_key:
        await _send_pet_info(message, pet)
        return

    action = PET_ALIASES.get(action_key)
    if action == "info":
        await _send_pet_info(message, pet)
        return
    if not action:
        await message.answer(
            f"Не понимаю действие «{escape_html(action_key)}».\n\n"
            f"<b>Что можно:</b> покормить · погладить · играть · помыть · лечить\n"
            f"Например: <code>/pet {escape_html(pet['name'])} покормить</code>",
            parse_mode="HTML"
        )
        return

    cfg = PET_ACTIONS[action]
    user_id = user.id

    pet = one(sb.table("pets").select("*").eq("id", pet["id"]))
    if not pet:
        await message.answer("Питомец не найден"); return
    pet = save_pet_tick(pet)

    mem = one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", user_id))
    if not mem:
        await message.answer("Ты не участник этого питомца. Открой ссылку-приглашение.")
        return

    last_key = "last_" + action + "_at"
    last_str = mem.get(last_key)
    if last_str:
        try:
            last_dt = datetime.fromisoformat(last_str.replace("Z","+00:00"))
            passed = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if passed < cfg["cd"]:
                left = int(cfg["cd"] - passed)
                mm, ss = left // 60, left % 60
                t = f"{mm} мин {ss} сек" if mm else f"{ss} сек"
                await message.answer(f"⌛ {escape_html(user.first_name or 'Гость')}, ещё рано. Подожди {t}.")
                return
        except Exception: pass

    score = mem.get("score") or 0
    if cfg.get("cost") and score < cfg["cost"]:
        await message.answer(f"❌ Не хватает очков: нужно {cfg['cost']}, у тебя {score}.")
        return

    new_pet = dict(pet)
    for k, v in cfg["effects"].items():
        old = new_pet.get(k) or 0
        new_pet[k] = max(0, min(100, old + v))
    new_pet["xp"] = (pet.get("xp") or 0) + cfg["xp"]

    old_lvl = min(30, (pet.get("xp") or 0) // 50 + 1)
    new_lvl = min(30, new_pet["xp"] // 50 + 1)
    old_st = _stage_idx(old_lvl); new_st = _stage_idx(new_lvl)

    sb.table("pets").update({
        "hunger": new_pet["hunger"], "mood": new_pet["mood"],
        "energy": new_pet["energy"], "clean": new_pet["clean"],
        "health": new_pet["health"], "xp": new_pet["xp"],
        "last_tick_at": new_pet["last_tick_at"],
    }).eq("id", pet["id"]).execute()

    iso_now = datetime.now(timezone.utc).isoformat()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    new_score = score + cfg["score"]
    member_upd = {"score": new_score, last_key: iso_now}
    if mem.get("today_date") == today_iso:
        member_upd["today_score"] = (mem.get("today_score") or 0) + max(cfg["score"], 0)
    else:
        member_upd["today_score"] = max(cfg["score"], 0)
        member_upd["today_date"] = today_iso
    sb.table("members").update(member_upd).eq("pet_id", pet["id"]).eq("user_id", user_id).execute()

    try:
        sb.table("events").insert({
            "pet_id": pet["id"], "user_id": user_id,
            "first_name": user.first_name or "Гость",
            "action": action,
        }).execute()
    except Exception: pass

    uname = escape_html(user.first_name or "Кто-то")
    changed = []
    for k, v in cfg["effects"].items():
        label = {"hunger":"сытость","mood":"настроение","energy":"энергия",
                 "clean":"чистота","health":"здоровье"}.get(k, k)
        sign = "+" if v > 0 else "−"
        changed.append(f"{label} {sign}{abs(v)}")

    text = f"{cfg['emoji']} <b>{uname}</b> — {cfg['label'].lower()} <b>{escape_html(pet['name'])}</b>"
    if changed:
        text += "\n  " + " · ".join(changed)

    if new_st > old_st:
        names = PET_STAGE_NAMES.get(pet.get("skin","classic"), PET_STAGE_NAMES["classic"])
        text += f"\n\n✨ <b>Эволюция!</b> {escape_html(pet['name'])} теперь <b>{names[new_st]}</b>"

    await message.answer(text, parse_mode="HTML")


# ============ АДМИНКА ============

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer(
        "🛠 <b>Админка</b>\n\n"
        "<b>Мои питомцы:</b>\n"
        "/my_pets_admin — мои питомцы с ID\n"
        "/list_pets — все питомцы бота\n\n"
        "<b>Очки:</b>\n"
        "/addscore [pet_id] 100 — себе\n"
        "/addscore_user [pet_id] &lt;uid&gt; 100 — юзеру\n"
        "/setscore_user [pet_id] &lt;uid&gt; 100 — установить\n\n"
        "<b>XP:</b>\n"
        "/xp [pet_id] — статистика\n"
        "/addxp [pet_id] 200 — добавить\n"
        "/setxp [pet_id] 1000 — установить\n\n"
        "<b>Баланс:</b>\n"
        "/setbal [pet_id] 5\n"
        "/addbal [pet_id] 1\n\n"
        "<b>Скин:</b>\n"
        "/setskin [pet_id] classic|cat|dragon|space|dino\n\n"
        "<b>Прочее:</b>\n"
        "/reset_scores — обнулить дневные очки\n"
        "/cheques [pet_id] — чеки питомца\n"
        "/cancel_cheques [pet_id] — отменить чеки\n"
        "/xr — баланс xRocket",
        parse_mode="HTML")


@dp.message(Command("my_pets_admin"))
async def cmd_my_pets_admin(m: Message):
    if not is_admin(m.from_user.id): return
    pets = my_pets(m.from_user.id)
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🐾 <b>Твои питомцы:</b>\n"]
    for p in pets:
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        lines.append(
            f"• <b>{p['name']}</b> · ур. {lvl}\n"
            f"  ID: <code>{p['id']}</code>\n"
            f"  Баланс: {float(p.get('bank_balance') or 0):.4f} · XP: {p.get('xp') or 0} · скин: {p.get('skin','classic')}"
        )
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("list_pets"))
async def cmd_list_pets(m: Message):
    if not is_admin(m.from_user.id): return
    pets = many(sb.table("pets").select("id,name,owner_id,bank_balance,xp,skin"))
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🌍 <b>Все питомцы бота:</b>\n"]
    for p in pets[:40]:
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        mine = " ⭐" if p["owner_id"] == m.from_user.id else ""
        lines.append(
            f"• <b>{p['name']}</b>{mine} · ур. {lvl}\n"
            f"  <code>{p['id']}</code> · owner <code>{p['owner_id']}</code>\n"
            f"  {float(p.get('bank_balance') or 0):.4f} USDT · {p.get('xp') or 0} XP"
        )
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("addscore"))
async def cmd_addscore(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest:
        await m.answer("Формат: /addscore [pet_id] 100"); return
    try: amount = int(rest[0])
    except: await m.answer("Не число."); return
    mem = one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", m.from_user.id))
    if not mem: await m.answer("Ты не участник."); return
    ns = (mem.get("score") or 0) + amount
    sb.table("members").update({"score": ns}).eq("pet_id", pet["id"]).eq("user_id", m.from_user.id).execute()
    await m.answer(
        f"✅ <b>Очки: {mem.get('score') or 0} → {ns}</b>\n{pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("addscore_user"))
async def cmd_addscore_user(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if len(rest) < 2:
        await m.answer("Формат: /addscore_user [pet_id] <uid> 100"); return
    try: uid = int(rest[0]); amount = int(rest[1])
    except: await m.answer("Не числа."); return
    mem = one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", uid))
    if not mem: await m.answer(f"Юзер <code>{uid}</code> не участник.", parse_mode="HTML"); return
    ns = (mem.get("score") or 0) + amount
    sb.table("members").update({"score": ns}).eq("pet_id", pet["id"]).eq("user_id", uid).execute()
    await m.answer(
        f"✅ <b>Очки юзера {uid}: {mem.get('score') or 0} → {ns}</b>\n{pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("setscore_user"))
async def cmd_setscore_user(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if len(rest) < 2:
        await m.answer("Формат: /setscore_user [pet_id] <uid> 100"); return
    try: uid = int(rest[0]); amount = int(rest[1])
    except: await m.answer("Не числа."); return
    sb.table("members").update({"score": amount}).eq("pet_id", pet["id"]).eq("user_id", uid).execute()
    await m.answer(
        f"✅ <b>Очки юзера {uid} = {amount}</b>\n{pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("xp"))
async def cmd_xp(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, _ = await resolve_pet(m, args)
    if not pet: return
    xp = pet.get("xp") or 0
    lvl = min(30, xp // 50 + 1)
    sidx = _stage_idx(lvl)
    names = PET_STAGE_NAMES.get(pet.get("skin","classic"), PET_STAGE_NAMES["classic"])
    await m.answer(
        f"📊 <b>Статистика</b>\n{pet_line(pet)}\n"
        f"XP: <b>{xp}</b>\nУр.: <b>{lvl}</b>/30\n"
        f"Стадия: {names[sidx]}\nДо след.: {50 - (xp % 50)} XP",
        parse_mode="HTML")


@dp.message(Command("addxp"))
async def cmd_addxp(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest: await m.answer("Формат: /addxp [pet_id] 200"); return
    try: amount = int(rest[0])
    except: await m.answer("Не число."); return
    await _apply_xp(pet, amount, m, "add")


@dp.message(Command("setxp"))
async def cmd_setxp(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest: await m.answer("Формат: /setxp [pet_id] 1000"); return
    try: amount = int(rest[0])
    except: await m.answer("Не число."); return
    await _apply_xp(pet, amount, m, "set")


async def _apply_xp(pet, amount, message, mode):
    ox = pet.get("xp") or 0
    nx = amount if mode == "set" else ox + amount
    if nx < 0: nx = 0
    sb.table("pets").update({"xp": nx}).eq("id", pet["id"]).execute()
    def lvl(x): return min(30, x // 50 + 1)
    ol, nl = lvl(ox), lvl(nx)
    os_, ns_ = _stage_idx(ol), _stage_idx(nl)
    names = PET_STAGE_NAMES.get(pet.get("skin","classic"), PET_STAGE_NAMES["classic"])
    text = (f"✅ <b>XP {'установлен' if mode=='set' else 'добавлен'}</b>\n{pet_line(pet)}\n\n"
            f"XP: {ox} → <b>{nx}</b>\nУр.: {ol} → <b>{nl}</b>")
    if ns_ != os_: text += f"\n✨ <b>Эволюция!</b> {names[os_]} → {names[ns_]}"
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("setbal"))
async def cmd_setbal(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest: await m.answer("Формат: /setbal [pet_id] 5"); return
    try: amount = float(rest[0].replace(",","."))
    except: await m.answer("Не разобрать."); return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ <b>Баланс: {amount:.4f} USDT</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("addbal"))
async def cmd_addbal(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest: await m.answer("Формат: /addbal [pet_id] 1"); return
    try: amount = float(rest[0].replace(",","."))
    except: await m.answer("Не разобрать."); return
    nb = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": nb}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ <b>Баланс: {nb:.4f} USDT</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("setskin"))
async def cmd_setskin(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest:
        await m.answer(f"Формат: /setskin [pet_id] <{'|'.join(SKINS)}>\n\nТекущий скин: {pet.get('skin','classic')}"); return
    skin = rest[0].lower()
    if skin not in SKINS:
        await m.answer(f"Доступные: {', '.join(SKINS)}"); return
    sb.table("pets").update({"skin": skin}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ <b>Скин: {skin}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("reset_scores"))
async def cmd_reset_scores(m: Message):
    if not is_admin(m.from_user.id): return
    today = date.today().isoformat()
    sb.table("members").update({"today_score": 0, "today_date": today}).neq("user_id", 0).execute()
    await m.answer("✅ Дневные очки обнулены у всех.")


@dp.message(Command("xr"))
async def cmd_xr(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("🔍 Проверяю xRocket…")
    bal = await get_app_balance(); ch = await list_xrocket_cheques()
    lines = ["<b>📊 xRocket</b>\n"]
    if bal:
        lines.append(f"✅ {bal['path']}")
        lines.append(f"<pre>{str(bal['data'])[:400]}</pre>")
    else: lines.append("❌ Не получил баланс.")
    lines.append(f"\n🧾 Чеков: {len(ch)}")
    for c in ch[:10]:
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')}")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cheques"))
async def cmd_cheques(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    xr = await list_xrocket_cheques()
    lines = ["🧾 <b>Чеки xRocket:</b> " + str(len(xr))]
    for c in xr[:10]:
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')}")

    pet, _ = await resolve_pet(m, args) if args else (None, None)
    if pet:
        db = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
        lines.append(f"\n<b>{pet['name']}</b>: {len(db)} в БД")
    else:
        pets = my_pets(m.from_user.id)
        for p in pets[:5]:
            db = many(sb.table("cheques").select("*").eq("pet_id", p["id"]).eq("status","sent"))
            lines.append(f"\n<b>{p['name']}</b>: {len(db)}")
    lines.append("\n/cancel_cheques [pet_id] — отменить")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cancel_cheques"))
async def cmd_cancel_cheques(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, _ = await resolve_pet(m, args)
    if not pet: return

    xr = await list_xrocket_cheques()
    db = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
    ids = {}
    for c in xr:
        cid = c.get("chequeId") or c.get("id")
        if cid: ids[str(cid)] = {"amount": c.get("amount") or 0}
    for c in db:
        cid = c.get("cheque_id")
        if cid and str(cid) not in ids: ids[str(cid)] = {"amount": c.get("amount") or 0}
    if not ids:
        await m.answer(f"Нет чеков.\n{pet_line(pet)}", parse_mode="HTML"); return
    await m.answer(f"🔍 Отменяю {len(ids)}…\n{pet_line(pet)}", parse_mode="HTML")
    ok = 0; fail = 0; refund = 0.0
    for cid, info in ids.items():
        try:
            await delete_cheque(cid); ok += 1
            refund += float(info.get("amount") or 0)
            sb.table("cheques").update({"status": "cancelled"}).eq("cheque_id", cid).execute()
        except: fail += 1
    if refund > 0:
        pn = one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        sb.table("pets").update({"bank_balance": round(cb + refund, 6)}).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ Отменено: {ok}\nОшибок: {fail}\nВозвращено: {refund:.4f} USDT\n{pet_line(pet)}",
        parse_mode="HTML")


# ============ create / my_pets ============

@dp.callback_query(F.data == "create")
async def cb_create(call: CallbackQuery):
    user = call.from_user; pet = None
    for _ in range(5):
        code = gen_code()
        try:
            r = sb.table("pets").insert({"owner_id": user.id, "invite_code": code}).execute()
            if r.data: pet = r.data[0]; break
        except Exception as e:
            if "23505" not in str(e): break
    if not pet: await call.answer("Ошибка.", show_alert=True); return
    sb.table("members").insert({"pet_id": pet["id"], "user_id": user.id,
        "first_name": user.first_name or "Гость", "username": user.username}).execute()
    await call.answer("Готово!")
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=join_{pet['invite_code']}"
    share = f"https://t.me/share/url?url={link}&text=Ухаживай за питомцем!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Открыть", web_app=WebAppInfo(url=webapp_url(pet["id"])))],
        [InlineKeyboardButton(text="📤 Поделиться", url=share)]])
    await call.message.answer(
        f"🐣 <b>Питомец создан!</b>\n\n<b>{pet['name']}</b>\nID: <code>{pet['id']}</code>\n\nСсылка:\n<code>{link}</code>",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "my_pets")
async def cb_my_pets(call: CallbackQuery):
    await call.answer()
    data = many(sb.table("members").select("pet_id, score, pets!inner(id, name, xp)")\
                .eq("user_id", call.from_user.id).order("score", desc=True).limit(20))
    if not data:
        await call.message.answer("Нет питомцев."); return
    lines = ["📋 <b>Твои питомцы:</b>\n"]
    rows = []
    for m in data:
        p = m.get("pets") or {}
        if not p: continue
        lvl = min(30, (p.get("xp") or 0)//50+1)
        lines.append(f"• <b>{p['name']}</b> · ур. {lvl}\n  <code>{p['id']}</code>")
        rows.append([InlineKeyboardButton(text=f"🐾 {p['name']} · ур. {lvl}",
                     web_app=WebAppInfo(url=webapp_url(p["id"])))])
    await call.message.answer("\n".join(lines), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "🐾 <b>Игра про общего питомца</b>\n\n"
        "• /start — создать питомца\n"
        "• /pet — мои питомцы\n"
        "• /pet имя покормить — покормить питомца\n"
        "• /pet имя погладить — погладить\n"
        "• /pet имя играть — поиграть\n"
        "• /pet имя помыть — помыть\n"
        "• /pet имя лечить — полечить (30 очков)",
        parse_mode="HTML")


# ============ health + webhook ============

async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "pet-bot"})


async def xrocket_webhook(request: web.Request):
    try: data = await request.json()
    except: return web.json_response({"ok": False}, status=400)
    iid = data.get("invoiceId") or data.get("id") or (data.get("payload") or {}).get("invoiceId")
    status = (data.get("status") or data.get("type") or "").lower()
    if not iid: return web.json_response({"ok": True})
    inv = one(sb.table("invoices").select("*").eq("invoice_id", str(iid)))
    if not inv or inv["status"] == "paid": return web.json_response({"ok": True})
    if any(k in status for k in ("paid","success","completed","invoice_paid")):
        pet = one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            nb = float(pet.get("bank_balance") or 0) + float(inv["amount"])
            sb.table("pets").update({"bank_balance": nb}).eq("id", inv["pet_id"]).execute()
            sb.table("invoices").update({"status": "paid"}).eq("invoice_id", str(iid)).execute()
            try:
                await bot.send_message(inv["owner_id"],
                    f"✅ <b>Оплата получена</b>\n{pet_line(pet)}\nБаланс: <b>{nb:.4f} USDT</b>",
                    parse_mode="HTML")
            except: pass
    return web.json_response({"ok": True})


# ============ запуск ============

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/xrocket", xrocket_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server on :%s", PORT)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("🤖 @%s стартовал (admins: %s)", me.username, ADMIN_IDS)
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
