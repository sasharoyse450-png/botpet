import os
import random
import asyncio
import logging
from datetime import date, datetime, timezone, timedelta

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

logging.basicConfig(level=logging.INFO)
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
NOTIFY_COOLDOWN_HOURS = 3

# Шаблоны ежедневных квестов
QUEST_TEMPLATES = [
    {"type": "feed_3",    "target": 3, "reward": 5,  "text": "Покормить 3 раза",     "emoji": "🍖"},
    {"type": "pet_5",     "target": 5, "reward": 3,  "text": "Погладить 5 раз",       "emoji": "✋"},
    {"type": "play_2",    "target": 2, "reward": 8,  "text": "Поиграть 2 раза",       "emoji": "🎾"},
    {"type": "wash_1",    "target": 1, "reward": 5,  "text": "Помыть 1 раз",          "emoji": "🧼"},
    {"type": "all_acts",  "target": 4, "reward": 15, "text": "Сделать 4 разных дела", "emoji": "🌟"},
]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(uid): return uid in ADMIN_IDS
def gen_code(n=6): return "".join(random.choice(CODE_CHARS) for _ in range(n))
def webapp_url(pid): return f"{WEBAPP_URL}?pet={pid}"
def escape_html(s): return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


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
                log.error("xRocket err %s %s: %s", method, path, data)
                raise err
            return data


async def create_invoice(amount, currency, description):
    data = await xrocket_request("POST", "/api/v1/invoices", {
        "priceAmount": str(amount), "priceCurrency": currency,
        "description": description, "numPayments": 1, "expiresIn": 3600000,
    })
    return data


async def get_invoice_status(iid):
    return await xrocket_request("GET", f"/api/v1/invoices/{iid}")


async def create_cheque(uid, amount, currency, description):
    data = await xrocket_request("POST", "/api/v1/cheques", {
        "asset": currency, "amount": str(amount), "description": description,
        "targetType": "telegram_user_id", "target": str(uid),
    })
    return data


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
        except Exception: continue
    return []


async def get_app_balance():
    for p in ("/api/v1/app/balance", "/api/v1/balance", "/api/v1/me"):
        try: return {"path": p, "data": await xrocket_request("GET", p)}
        except Exception: continue
    return None


def xrocket_error_text(e):
    d = getattr(e,"data",None) or {}
    kind = d.get("kind") or ""; title = d.get("title") or ""
    detail = d.get("detail") or str(e)
    if "amount_more_than_app_balance" in str(d) or "more than app balance" in detail.lower():
        return "⚠️ На балансе приложения xRocket недостаточно средств. Пополни @xRocket → Wallet."
    if "operation_disabled" in kind or "disabled" in detail.lower():
        return "⚠️ xRocket отключил эту операцию для твоего приложения. Проверь Permissions."
    return f"⚠️ xRocket {getattr(e,'status','?')}: {title or detail}"


# ============ КВЕСТЫ ============

def ensure_quests(pet_id, user_id):
    """Создаёт квесты на сегодня, если их ещё нет."""
    today = date.today().isoformat()
    existing = many(sb.table("quests").select("*")
                    .eq("pet_id", pet_id).eq("user_id", user_id).eq("quest_date", today))
    if existing: return existing
    created = []
    for t in QUEST_TEMPLATES:
        r = sb.table("quests").insert({
            "pet_id": pet_id, "user_id": user_id, "quest_date": today,
            "quest_type": t["type"], "target": t["target"], "progress": 0,
        }).execute()
        if r.data: created.append(r.data[0])
    return created


def quest_action_type(action):
    """Какое действие считаем для квеста."""
    return {"feed":"feed_3", "pet":"pet_5", "play":"play_2", "wash":"wash_1"}.get(action)


def quest_progress(pet_id, user_id, action):
    """Обновляет прогресс квестов при действии. Вызывать после doAction."""
    today = date.today().isoformat()
    ensure_quests(pet_id, user_id)

    # Инкремент для конкретного действия
    qt = quest_action_type(action)
    if qt:
        q = one(sb.table("quests").select("*")
                .eq("pet_id", pet_id).eq("user_id", user_id)
                .eq("quest_date", today).eq("quest_type", qt))
        if q and q["progress"] < q["target"]:
            sb.table("quests").update({"progress": q["progress"] + 1}).eq("id", q["id"]).execute()

    # all_acts — считаем уникальные действия за сегодня (по events)
    acts = many(sb.table("events").select("action")
                .eq("pet_id", pet_id).eq("user_id", user_id)
                .gte("created_at", today + "T00:00:00"))
    unique_acts = len({a["action"] for a in acts if a["action"] in ("feed","pet","play","wash")})
    q_all = one(sb.table("quests").select("*")
                .eq("pet_id", pet_id).eq("user_id", user_id)
                .eq("quest_date", today).eq("quest_type", "all_acts"))
    if q_all:
        new_progress = min(unique_acts, q_all["target"])
        if new_progress != q_all["progress"]:
            sb.table("quests").update({"progress": new_progress}).eq("id", q_all["id"]).execute()


# ============ /start ============

@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()
    user = message.from_user
    log.info("cmd_start payload=%r uid=%s", payload, user.id)

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
        await message.answer(f"🐾 Ты ухаживаешь за «{pet['name']}»!", reply_markup=kb); return

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
            await message.answer("Нет доступа."); return
        await message.answer(f"💳 Создаю счёт на {amount}…")
        try:
            inv = await create_invoice(amount, pet.get("currency","USDT"),
                                       f"Банк питомца «{pet['name']}»")
        except Exception as e:
            await message.answer(xrocket_error_text(e)); return
        link = pick_link(inv); iid = inv.get("id") or inv.get("invoiceId")
        if not link or not iid:
            await message.answer("⚠️ Нет ссылки: <code>" + str(inv)[:600] + "</code>", parse_mode="HTML"); return
        sb.table("invoices").insert({
            "invoice_id": str(iid), "pet_id": pet_id, "owner_id": user.id,
            "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
        }).execute()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
        await message.answer(f"💳 Счёт на {amount}. После оплаты жми «Проверить».", reply_markup=kb); return

    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3: await message.answer("Ошибка зарплаты."); return
        try: cents = int(parts[1]); top_n = int(parts[2])
        except: await message.answer("Ошибка параметров."); return
        await run_salary_pet(parts[0], user.id, cents/100.0, top_n, message); return

    await message.answer(
        "👋 Это бот общего питомца.\n\nСоздай питомца — получишь ссылку для друзей.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")]]))


# ============ уведомления владельцу ============

async def notify_loop():
    """Раз в час проверяет статы всех питомцев и пишет владельцам."""
    while True:
        try:
            pets = many(sb.table("pets").select("*"))
            now = datetime.now(timezone.utc)
            for p in pets:
                last = p.get("last_notified_at")
                if last:
                    last_dt = datetime.fromisoformat(last.replace("Z","+00:00"))
                    if (now - last_dt).total_seconds() < NOTIFY_COOLDOWN_HOURS * 3600:
                        continue
                msgs = []
                if p.get("hunger",100) < 30: msgs.append("🍖 Я голоден!")
                if p.get("mood",100) < 30:   msgs.append("😢 Мне скучно…")
                if p.get("clean",100) < 30:  msgs.append("🧼 Я грязный…")
                if p.get("energy",100) < 20: msgs.append("💤 Я устал…")
                if p.get("health",100) < 30: msgs.append("💔 Мне плохо!")
                if not msgs: continue
                text = f"🐾 <b>Питомец «{escape_html(p['name'])}»</b>\n\n" + "\n".join(msgs)
                text += "\n\nОткрой приложение и позаботься о нём."
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🐾 Открыть питомца",
                                          web_app=WebAppInfo(url=webapp_url(p["id"])))]])
                try:
                    await bot.send_message(p["owner_id"], text, parse_mode="HTML", reply_markup=kb)
                    sb.table("pets").update({"last_notified_at": now.isoformat()}).eq("id", p["id"]).execute()
                except Exception as e:
                    log.warning("notify %s failed: %s", p["owner_id"], e)
        except Exception as e:
            log.error("notify_loop error: %s", e)
        await asyncio.sleep(3600)


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
        await call.answer(f"xRocket {getattr(e,'status','?')}: {(d.get('detail') or str(e))[:180]}", show_alert=True); return
    status = (st.get("status") or st.get("state") or (st.get("invoice") or {}).get("status") or "").lower()
    if status in ("paid","success","completed","paid_success"):
        pet = one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            nb = float(pet.get("bank_balance") or 0) + float(inv["amount"])
            sb.table("pets").update({"bank_balance": nb}).eq("id", inv["pet_id"]).execute()
            sb.table("invoices").update({"status": "paid"}).eq("invoice_id", iid).execute()
            await call.answer("Оплачено! ✅", show_alert=True)
            try: await call.message.edit_text(f"✅ Банк пополнен. Баланс: {nb:.4f}")
            except: pass
            return
    await call.answer(f"Статус: {status or 'неизвестно'}", show_alert=True)


# ============ команды владельца ============

@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    await m.answer(f"🏦 Баланс: <b>{float(pet.get('bank_balance') or 0):.4f} USDT</b>", parse_mode="HTML")


@dp.message(Command("salary"))
async def cmd_salary(m: Message):
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    parts = m.text.split()
    amount = None; top_n = None
    if len(parts) >= 2:
        try: amount = float(parts[1].replace(",","."))
        except: await m.answer("Формат: /salary 1 3"); return
    if len(parts) >= 3:
        try: top_n = int(parts[2])
        except: await m.answer("Формат: /salary 1 3"); return
    await run_salary_pet(pet["id"], m.from_user.id, amount, top_n, m)


async def run_salary_pet(pet_id, owner_id, amount, top_n, message):
    pet = one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet or pet["owner_id"] != owner_id:
        await message.answer("Нет доступа."); return
    balance = float(pet.get("bank_balance") or 0)
    if balance <= 0: await message.answer("Банк пуст."); return
    if amount is None or amount <= 0: amount = balance
    if amount > balance: await message.answer(f"В банке только {balance:.4f}."); return

    today = date.today().isoformat()
    members = many(sb.table("members").select("*").eq("pet_id", pet["id"]))
    for m in members:
        if m.get("today_date") != today:
            sb.table("members").update({"today_score": 0, "today_date": today})\
              .eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
            m["today_score"] = 0
    active = [m for m in members if (m.get("today_score") or 0) > 0]
    if not active: await message.answer("Нет активных."); return
    active.sort(key=lambda x: x["today_score"], reverse=True)
    winners = active[:top_n] if (top_n and 0 < top_n < len(active)) else active
    total = sum(w["today_score"] for w in winners)
    if total <= 0: await message.answer("Нет очков."); return
    min_share = min(round(amount * (w["today_score"]/total), 6) for w in winners)
    if min_share < MIN_CHEQUE:
        await message.answer(f"❌ Доли слишком малы ({min_share:.4f} < {MIN_CHEQUE})."); return

    sb.table("pets").update({"bank_balance": balance - amount}).eq("id", pet["id"]).execute()
    po = sb.table("payouts").insert({
        "pet_id": pet["id"], "owner_id": owner_id, "total_amount": amount,
        "currency": pet.get("currency","USDT"), "member_count": len(winners)}).execute()
    payout = po.data[0]
    await message.answer(f"💸 Раздаю {amount:.4f} на топ-{len(winners)}…")
    sent = 0; total_sent = 0.0; paid = []; failed_msg = ""
    for i, m in enumerate(winners):
        score = m["today_score"]
        share = round(amount * (score/total), 6)
        if share < MIN_CHEQUE: continue
        try:
            ch = await create_cheque(m["user_id"], share, pet.get("currency","USDT"),
                                     f"Зарплата ({score} очков)")
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
                    f"💰 <b>Зарплата за питомца «{pet['name']}»</b>\n\n"
                    f"Активность: {score} очков\nНачислено: <b>{share:.4f} USDT</b>\n\nЗабрать: {link}",
                    parse_mode="HTML", disable_web_page_preview=True)
            except Exception as e: log.warning("send: %s", e)
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
    text = f"✅ Выплата.\nВыплачено: {total_sent:.4f} из {amount:.4f}\nЧеков: {sent}\nОстаток: {final:.4f}"
    if failed_msg: text += f"\n\n⚠️ Прервано: {failed_msg}"
    await message.answer(text)


# ============ АДМИНКА ============

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer(
        "🛠 <b>Админка</b>\n\n"
        "/setbal 5 — баланс своему питомцу\n"
        "/addbal 1 — прибавить к балансу\n"
        "/setbal_pet &lt;id&gt; 5 — баланс по ID\n"
        "/addbal_pet &lt;id&gt; 1 — прибавить по ID\n"
        "/list_pets — все питомцы\n"
        "/addxp 200 — добавить XP\n"
        "/reset_scores — обнулить дневные очки\n"
        "/cheques — список чеков\n"
        "/cancel_cheques — отменить все\n"
        "/xr — баланс xRocket\n"
        "/test_notify — прислать уведомление сейчас",
        parse_mode="HTML")


@dp.message(Command("setbal"))
async def cmd_setbal(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /setbal 5"); return
    try: amount = float(parts[1].replace(",","."))
    except: await m.answer("Не разобрать сумму."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ {amount:.4f} USDT")


@dp.message(Command("addbal"))
async def cmd_addbal(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /addbal 1"); return
    try: amount = float(parts[1].replace(",","."))
    except: await m.answer("Не разобрать."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    nb = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": nb}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ {nb:.4f} USDT")


@dp.message(Command("setbal_pet"))
async def cmd_setbal_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /setbal_pet <id> 5"); return
    pid = parts[1].strip()
    try: amount = float(parts[2].replace(",","."))
    except: await m.answer("Не разобрать."); return
    if len(pid) < 30: await m.answer("Не UUID."); return
    pet = one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pid).execute()
    await m.answer(f"✅ «{pet['name']}»: {amount:.4f}")


@dp.message(Command("addbal_pet"))
async def cmd_addbal_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /addbal_pet <id> 1"); return
    pid = parts[1].strip()
    try: amount = float(parts[2].replace(",","."))
    except: await m.answer("Не разобрать."); return
    if len(pid) < 30: await m.answer("Не UUID."); return
    pet = one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    nb = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": nb}).eq("id", pid).execute()
    await m.answer(f"✅ «{pet['name']}»: {nb:.4f}")


@dp.message(Command("list_pets"))
async def cmd_list_pets(m: Message):
    if not is_admin(m.from_user.id): return
    pets = many(sb.table("pets").select("id,name,owner_id,bank_balance,xp"))
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🐾 <b>Все питомцы:</b>\n"]
    for p in pets[:30]:
        lines.append(f"• <code>{p['id']}</code>\n  {p['name']} — {float(p.get('bank_balance') or 0):.4f} USDT, XP {p.get('xp') or 0}")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("addxp"))
async def cmd_addxp(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /addxp 200"); return
    try: amount = int(parts[1])
    except: await m.answer("Не число."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    ox = pet.get("xp") or 0; nx = ox + amount
    sb.table("pets").update({"xp": nx}).eq("id", pet["id"]).execute()
    def lvl(x): return min(30, x//50+1)
    def st(l): return 4 if l>=25 else 3 if l>=17 else 2 if l>=10 else 1 if l>=5 else 0
    ol, nl = lvl(ox), lvl(nx); os, ns = st(ol), st(nl)
    names = ["🥚","🐣","🐥","🐓","🦅"]
    t = f"✅ XP: {ox} → {nx} (ур. {ol} → {nl})"
    if ns != os: t += f"\n✨ {names[os]} → {names[ns]}"
    await m.answer(t)


@dp.message(Command("reset_scores"))
async def cmd_reset_scores(m: Message):
    if not is_admin(m.from_user.id): return
    today = date.today().isoformat()
    sb.table("members").update({"today_score": 0, "today_date": today}).neq("user_id", 0).execute()
    await m.answer("✅ Обнулено.")


@dp.message(Command("test_notify"))
async def cmd_test_notify(m: Message):
    if not is_admin(m.from_user.id): return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    text = f"🐾 <b>Тест уведомления</b>\n\nПитомец «{pet['name']}» — так будет выглядеть напоминание."
    await bot.send_message(m.from_user.id, text, parse_mode="HTML")
    await m.answer("✅ Отправлено.")


@dp.message(Command("xr"))
async def cmd_xr(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("🔍 Проверяю…")
    bal = await get_app_balance()
    ch = await list_xrocket_cheques()
    lines = ["<b>📊 xRocket</b>\n"]
    if bal:
        lines.append(f"✅ {bal['path']}")
        lines.append(f"<pre>{str(bal['data'])[:400]}</pre>")
    else:
        lines.append("❌ Не удалось получить баланс.")
    lines.append(f"\n🧾 Чеков: {len(ch)}")
    for c in ch[:10]:
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')}")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cheques"))
async def cmd_cheques(m: Message):
    if not is_admin(m.from_user.id): return
    lines = ["🧾 <b>Чеки</b>\n"]
    xr = await list_xrocket_cheques()
    lines.append(f"<b>xRocket:</b> {len(xr)}")
    for c in xr[:10]:
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')} [{c.get('state','?')}]")
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if pet:
        db = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
        lines.append(f"\n<b>БД (sent):</b> {len(db)}")
        for c in db[:10]:
            lines.append(f"• <code>{c.get('cheque_id')}</code> — {float(c.get('amount') or 0):.4f}")
    lines.append("\n/cancel_cheques — отменить все")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cancel_cheques"))
async def cmd_cancel_cheques(m: Message):
    if not is_admin(m.from_user.id): return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    xr = await list_xrocket_cheques()
    db = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
    ids = {}
    for c in xr:
        cid = c.get("chequeId") or c.get("id")
        if cid: ids[str(cid)] = {"amount": c.get("amount") or 0}
    for c in db:
        cid = c.get("cheque_id")
        if cid and str(cid) not in ids: ids[str(cid)] = {"amount": c.get("amount") or 0}
    if not ids: await m.answer("Нет активных чеков."); return
    await m.answer(f"🔍 Отменяю {len(ids)}…")
    ok = 0; fail = 0; refund = 0.0; fails = []
    for cid, info in ids.items():
        try:
            await delete_cheque(cid); ok += 1
            refund += float(info.get("amount") or 0)
            sb.table("cheques").update({"status": "cancelled"}).eq("cheque_id", cid).execute()
        except Exception as e:
            log.error("cancel %s: %s", cid, e); fail += 1; fails.append(cid)
    if refund > 0:
        pn = one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        sb.table("pets").update({"bank_balance": round(cb + refund, 6)}).eq("id", pet["id"]).execute()
    t = f"✅ Отменено: {ok}\nОшибок: {fail}\nВозвращено: {refund:.4f}"
    if fails: t += "\n\nНе удалось:\n" + "\n".join(f"• <code>{i}</code>" for i in fails[:10])
    await m.answer(t, parse_mode="HTML")


# ============ create / my_pets / help ============

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
    share = f"https://t.me/share/url?url={link}&text=Ухаживай за нашим питомцем!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Открыть", web_app=WebAppInfo(url=webapp_url(pet["id"])))],
        [InlineKeyboardButton(text="📤 Поделиться", url=share)]])
    await call.message.answer(f"🐣 <b>Питомец создан!</b>\n\n<code>{link}</code>",
                              parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "my_pets")
async def cb_my_pets(call: CallbackQuery):
    await call.answer()
    data = many(sb.table("members").select("pet_id, score, pets!inner(id, name, xp)")\
                .eq("user_id", call.from_user.id).order("score", desc=True).limit(20))
    if not data: await call.message.answer("Нет питомцев."); return
    rows = []
    for m in data:
        p = m.get("pets") or {}
        if not p: continue
        lvl = min(30, (p.get("xp") or 0)//50+1)
        rows.append([InlineKeyboardButton(text=f"🐾 {p['name']} · ур. {lvl}",
                     web_app=WebAppInfo(url=webapp_url(p["id"])))])
    await call.message.answer("Твои питомцы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("🐾 Игра про общего питомца. Создай через /start.")


# ============ API для Mini App (инвентарь, квесты, мини-игра) ============

async def api_quest_claim(request: web.Request):
    """POST /api/quest/claim { pet_id, user_id, quest_id }"""
    try: data = await request.json()
    except: return web.json_response({"error": "bad json"}, status=400)
    qid = data.get("quest_id")
    if not qid: return web.json_response({"error": "no quest_id"}, status=400)
    q = one(sb.table("quests").select("*").eq("id", qid))
    if not q: return web.json_response({"error": "not found"}, status=404)
    if q["progress"] < q["target"]: return web.json_response({"error": "not completed"}, status=400)
    if q["claimed"]: return web.json_response({"error": "already claimed"}, status=400)

    # Награда
    tmpl = next((t for t in QUEST_TEMPLATES if t["type"] == q["quest_type"]), None)
    reward = tmpl["reward"] if tmpl else 5
    m = one(sb.table("members").select("*").eq("pet_id", q["pet_id"]).eq("user_id", q["user_id"]))
    if m:
        sb.table("members").update({"score": (m.get("score") or 0) + reward})\
          .eq("pet_id", q["pet_id"]).eq("user_id", q["user_id"]).execute()
    sb.table("quests").update({"claimed": True}).eq("id", qid).execute()
    return web.json_response({"ok": True, "reward": reward})


async def api_inventory_buy(request: web.Request):
    """POST /api/inventory/buy { pet_id, user_id, item_type }"""
    try: data = await request.json()
    except: return web.json_response({"error": "bad json"}, status=400)
    pid = data.get("pet_id"); uid = data.get("user_id"); itype = data.get("item_type")
    if not all([pid, uid, itype]): return web.json_response({"error": "missing"}, status=400)

    PRICES = {"food": 5, "toy": 8, "medicine": 15, "hat": 30}
    if itype not in PRICES: return web.json_response({"error": "unknown item"}, status=400)
    price = PRICES[itype]

    m = one(sb.table("members").select("*").eq("pet_id", pid).eq("user_id", uid))
    if not m or (m.get("score") or 0) < price:
        return web.json_response({"error": "not enough score"}, status=400)
    sb.table("members").update({"score": m["score"] - price})\
      .eq("pet_id", pid).eq("user_id", uid).execute()

    inv = one(sb.table("inventory").select("*").eq("pet_id", pid).eq("user_id", uid).eq("item_type", itype))
    if inv:
        sb.table("inventory").update({"count": (inv.get("count") or 0) + 1})\
          .eq("pet_id", pid).eq("user_id", uid).eq("item_type", itype).execute()
    else:
        sb.table("inventory").insert({"pet_id": pid, "user_id": uid, "item_type": itype, "count": 1}).execute()
    return web.json_response({"ok": True, "new_score": m["score"] - price})


async def api_inventory_use(request: web.Request):
    """POST /api/inventory/use { pet_id, user_id, item_type }"""
    try: data = await request.json()
    except: return web.json_response({"error": "bad json"}, status=400)
    pid = data.get("pet_id"); uid = data.get("user_id"); itype = data.get("item_type")
    if not all([pid, uid, itype]): return web.json_response({"error": "missing"}, status=400)

    inv = one(sb.table("inventory").select("*").eq("pet_id", pid).eq("user_id", uid).eq("item_type", itype))
    if not inv or (inv.get("count") or 0) <= 0:
        return web.json_response({"error": "no item"}, status=400)

    EFFECTS = {
        "food":     {"hunger": 30, "energy": 15},
        "toy":      {"mood": 30},
        "medicine": {"health": 40},
        "hat":      {},  # косметика, не тратится
    }
    pet = one(sb.table("pets").select("*").eq("id", pid))
    if not pet: return web.json_response({"error": "pet not found"}, status=404)

    eff = EFFECTS.get(itype, {})
    upd = {}
    for k, v in eff.items():
        upd[k] = max(0, min(100, (pet.get(k) or 0) + v))

    if upd: sb.table("pets").update(upd).eq("id", pid).execute()

    # Шапка не тратится
    if itype != "hat":
        sb.table("inventory").update({"count": inv["count"] - 1})\
          .eq("pet_id", pid).eq("user_id", uid).eq("item_type", itype).execute()

    return web.json_response({"ok": True, "effects": upd})


async def api_mouse_done(request: web.Request):
    """POST /api/mouse/done { pet_id, user_id, caught (0 или 1) }"""
    try: data = await request.json()
    except: return web.json_response({"error": "bad json"}, status=400)
    pid = data.get("pet_id"); uid = data.get("user_id"); caught = data.get("caught", 0)
    if not all([pid, uid]): return web.json_response({"error": "missing"}, status=400)

    # Проверяем кулдаун (раз в час)
    m = one(sb.table("members").select("*").eq("pet_id", pid).eq("user_id", uid))
    if not m: return web.json_response({"error": "no member"}, status=400)
    last = m.get("last_mouse_at")
    if last:
        last_dt = datetime.fromisoformat(last.replace("Z","+00:00"))
        if (datetime.now(timezone.utc) - last_dt).total_seconds() < 3600:
            return web.json_response({"error": "cooldown"}, status=400)

    sb.table("members").update({"last_mouse_at": datetime.now(timezone.utc).isoformat()})\
      .eq("pet_id", pid).eq("user_id", uid).execute()

    reward = 0
    if caught:
        reward = 3
        sb.table("members").update({"score": (m.get("score") or 0) + reward})\
          .eq("pet_id", pid).eq("user_id", uid).execute()
        pet = one(sb.table("pets").select("*").eq("id", pid))
        if pet:
            nx = (pet.get("xp") or 0) + 2
            sb.table("pets").update({"xp": nx, "mood": min(100, (pet.get("mood") or 0) + 5)})\
              .eq("id", pid).execute()

    return web.json_response({"ok": True, "reward": reward})


async def api_quests_list(request: web.Request):
    """GET /api/quests?pet_id=...&user_id=..."""
    pid = request.query.get("pet_id"); uid = request.query.get("user_id")
    if not all([pid, uid]): return web.json_response({"error": "missing"}, status=400)
    ensure_quests(pid, int(uid))
    today = date.today().isoformat()
    qs = many(sb.table("quests").select("*").eq("pet_id", pid).eq("user_id", int(uid)).eq("quest_date", today))
    return web.json_response({"quests": qs})


async def api_inventory_list(request: web.Request):
    pid = request.query.get("pet_id"); uid = request.query.get("user_id")
    if not all([pid, uid]): return web.json_response({"error": "missing"}, status=400)
    inv = many(sb.table("inventory").select("*").eq("pet_id", pid).eq("user_id", int(uid)))
    return web.json_response({"items": inv})


async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "pet-bot"})


# ============ вебхук ============

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
                    f"✅ Оплата получена. Баланс: {nb:.4f} USDT")
            except: pass
    return web.json_response({"ok": True})


# ============ запуск ============

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/xrocket", xrocket_webhook)
    # API для Mini App
    app.router.add_get("/api/quests", api_quests_list)
    app.router.add_post("/api/quest/claim", api_quest_claim)
    app.router.add_get("/api/inventory", api_inventory_list)
    app.router.add_post("/api/inventory/buy", api_inventory_buy)
    app.router.add_post("/api/inventory/use", api_inventory_use)
    app.router.add_post("/api/mouse/done", api_mouse_done)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server on port %s", PORT)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot @%s started", me.username)
    await start_web_server()
    asyncio.create_task(notify_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
