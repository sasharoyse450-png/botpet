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
SKINS = ["classic", "cat", "dragon", "space", "dino"]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(uid): return uid in ADMIN_IDS
def gen_code(n=6): return "".join(random.choice(CODE_CHARS) for _ in range(n))
def webapp_url(pid): return f"{WEBAPP_URL}?pet={pid}"


def pet_line(p):
    """Однострочное описание питомца с ID."""
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
        await message.answer(
            f"🐾 Ты ухаживаешь за «{pet['name']}»!\n\n"
            f"ID питомца: <code>{pet['id']}</code>",
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
            await message.answer(
                f"❌ Только владелец может пополнять банк.\n\n"
                f"Владелец питомца: <code>{pet.get('owner_id') if pet else '?'}</code>\n"
                f"Ты: <code>{user.id}</code>",
                parse_mode="HTML")
            return
        await message.answer(
            f"💳 Создаю счёт на {amount} USDT…\n"
            f"Питомец: {pet_line(pet)}",
            parse_mode="HTML")
        try:
            inv = await create_invoice(amount, pet.get("currency","USDT"),
                                       f"Банк питомца «{pet['name']}» ({pet['id'][:8]})")
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
            f"💳 <b>Счёт на {amount} USDT</b>\n"
            f"Питомец: <code>{pet['id']}</code>\n"
            f"Invoice: <code>{iid}</code>",
            parse_mode="HTML", reply_markup=kb)
        return

    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3: await message.answer("Ошибка формата зарплаты."); return
        try: cents = int(parts[1]); top_n = int(parts[2])
        except: await message.answer("Ошибка параметров."); return
        await run_salary_pet(parts[0], user.id, cents/100.0, top_n, message); return

    await message.answer(
        "👋 Это бот общего питомца.\n\nСоздай питомца — получишь ссылку для друзей.",
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
                    f"✅ <b>Банк пополнен на {inv['amount']} USDT</b>\n\n"
                    f"Питомец: {pet_line(pet)}\n"
                    f"Новый баланс: <b>{nb:.4f} USDT</b>",
                    parse_mode="HTML")
            except: pass
            return
    await call.answer(f"Статус: {status or 'неизвестно'}", show_alert=True)


# ============ владелец ============

@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    await m.answer(
        f"🏦 <b>Баланс: {float(pet.get('bank_balance') or 0):.4f} USDT</b>\n\n"
        f"Питомец: {pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("topup"))
async def cmd_topup(m: Message):
    user = m.from_user
    pet = one(sb.table("pets").select("*").eq("owner_id", user.id))
    if not pet: await m.answer("Нет питомца."); return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(f"Формат: /topup 1\n\nПитомец: {pet_line(pet)}", parse_mode="HTML"); return
    try: amount = float(parts[1].replace(",","."))
    except: await m.answer("Не разобрать сумму."); return
    if amount <= 0 or amount > 1000: await m.answer("0.01 – 1000 USDT"); return

    await m.answer(
        f"💳 Создаю счёт на {amount} USDT…\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")
    try:
        inv = await create_invoice(amount, pet.get("currency","USDT"),
                                   f"Банк питомца «{pet['name']}» ({pet['id'][:8]})")
    except Exception as e:
        await m.answer(xrocket_error_text(e)); return
    link = pick_link(inv); iid = inv.get("id") or inv.get("invoiceId")
    if not link or not iid: await m.answer("⚠️ Нет ссылки."); return
    sb.table("invoices").insert({
        "invoice_id": str(iid), "pet_id": pet["id"], "owner_id": user.id,
        "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
    }).execute()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
    await m.answer(
        f"💳 <b>Счёт на {amount} USDT</b>\n"
        f"Питомец: <code>{pet['id']}</code>\n"
        f"Invoice: <code>{iid}</code>",
        parse_mode="HTML", reply_markup=kb)


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
    if not pet:
        await message.answer(f"Питомец <code>{pet_id}</code> не найден.", parse_mode="HTML"); return
    if pet["owner_id"] != owner_id:
        await message.answer(
            f"❌ Только владелец может запускать зарплату.\n\n"
            f"Питомец: {pet_line(pet)}\n"
            f"Владелец: <code>{pet['owner_id']}</code>\n"
            f"Ты: <code>{owner_id}</code>",
            parse_mode="HTML")
        return

    balance = float(pet.get("bank_balance") or 0)
    if balance <= 0:
        await message.answer(
            f"❌ Банк пуст.\n\nПитомец: {pet_line(pet)}",
            parse_mode="HTML"); return
    if amount is None or amount <= 0: amount = balance
    if amount > balance:
        await message.answer(f"В банке только {balance:.4f}. Уменьши сумму."); return

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
            f"❌ Сегодня никто не был активен.\n\n"
            f"Питомец: {pet_line(pet)}\n"
            f"Всего участников: {len(members)}\n"
            f"Пусть кто-то сделает действие в Mini App и повтори команду.",
            parse_mode="HTML"); return
    active.sort(key=lambda x: x["today_score"], reverse=True)
    winners = active[:top_n] if (top_n and 0 < top_n < len(active)) else active
    total = sum(w["today_score"] for w in winners)
    if total <= 0: await message.answer("Нет очков для распределения."); return
    min_share = min(round(amount * (w["today_score"]/total), 6) for w in winners)
    if min_share < MIN_CHEQUE:
        await message.answer(
            f"❌ Слишком мелкие чеки ({min_share:.4f} < {MIN_CHEQUE}).\n"
            f"Уменьши топ-N или увеличь сумму.",
            parse_mode="HTML"); return

    sb.table("pets").update({"bank_balance": balance - amount}).eq("id", pet["id"]).execute()
    po = sb.table("payouts").insert({
        "pet_id": pet["id"], "owner_id": owner_id, "total_amount": amount,
        "currency": pet.get("currency","USDT"), "member_count": len(winners)}).execute()
    payout = po.data[0]

    await message.answer(
        f"💸 <b>Раздаю {amount:.4f} USDT</b>\n"
        f"Питомец: {pet_line(pet)}\n"
        f"Получателей: {len(winners)} из {len(active)} активных\n"
        f"Payout ID: <code>{payout['id']}</code>",
        parse_mode="HTML")

    sent = 0; total_sent = 0.0; paid = []; failed_msg = ""
    for i, m in enumerate(winners):
        score = m["today_score"]; share = round(amount * (score/total), 6)
        if share < MIN_CHEQUE: continue
        try:
            ch = await create_cheque(m["user_id"], share, pet.get("currency","USDT"),
                                     f"Зарплата ({score} очков) · {pet['name']}")
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
                    f"ID: <code>{pet['id']}</code>\n\n"
                    f"Активность: {score} очков\n"
                    f"Начислено: <b>{share:.4f} USDT</b>\n"
                    f"Чек: <code>{cid}</code>\n\n"
                    f"Забрать: {link}",
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
    text = (f"✅ <b>Выплата завершена</b>\n\n"
            f"Питомец: {pet_line(pet)}\n"
            f"Раздано: {total_sent:.4f} из {amount:.4f} USDT\n"
            f"Чеков создано: {sent} из {len(winners)}\n"
            f"Остаток в банке: <b>{final:.4f} USDT</b>")
    if failed_msg: text += f"\n\n⚠️ {failed_msg}\nНевыплаченное вернулось в банк."
    await message.answer(text, parse_mode="HTML")


# ============ АДМИНКА ============

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer(
        "🛠 <b>Админка</b>\n\n"
        "<b>Очки:</b>\n"
        "/addscore 100 — начислить себе\n"
        "/setscore &lt;uid&gt; 100 — установить юзеру\n\n"
        "<b>XP питомца:</b>\n"
        "/xp — показать XP и уровень своего питомца\n"
        "/addxp 200 — добавить XP своему\n"
        "/addxp_pet &lt;pet_id&gt; 200 — добавить XP по ID\n"
        "/setxp_pet &lt;pet_id&gt; 1000 — установить XP по ID\n\n"
        "<b>Баланс:</b>\n"
        "/setbal 5 — своему питомцу\n"
        "/addbal 1 — прибавить\n"
        "/setbal_pet &lt;id&gt; 5 — питомцу по ID\n"
        "/addbal_pet &lt;id&gt; 1 — прибавить по ID\n\n"
        "<b>Сервис:</b>\n"
        "/list_pets — все питомцы с ID\n"
        "/setskin classic — сменить скин\n"
        "/reset_scores — обнулить дневные очки\n"
        "/cheques — активные чеки\n"
        "/cancel_cheques — отменить все\n"
        "/xr — баланс xRocket",
        parse_mode="HTML")


@dp.message(Command("addscore"))
async def cmd_addscore(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /addscore 100"); return
    try: amount = int(parts[1])
    except: await m.answer("Не число."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    mem = one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", m.from_user.id))
    if not mem: await m.answer("Ты не участник."); return
    new_score = (mem.get("score") or 0) + amount
    sb.table("members").update({"score": new_score})\
      .eq("pet_id", pet["id"]).eq("user_id", m.from_user.id).execute()
    await m.answer(
        f"✅ <b>Очки: {mem.get('score') or 0} → {new_score}</b>\n\n"
        f"Питомец: {pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("setscore"))
async def cmd_setscore(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /setscore <uid> 100"); return
    try: uid = int(parts[1]); amount = int(parts[2])
    except: await m.answer("Не число."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    sb.table("members").update({"score": amount})\
      .eq("pet_id", pet["id"]).eq("user_id", uid).execute()
    await m.answer(
        f"✅ uid <code>{uid}</code>: score = {amount}\n\n"
        f"Питомец: {pet_line(pet)}",
        parse_mode="HTML")


# ============ XP команды ============

@dp.message(Command("xp"))
async def cmd_xp(m: Message):
    if not is_admin(m.from_user.id): return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    xp = pet.get("xp") or 0
    lvl = min(30, xp // 50 + 1)
    stage_idx = 4 if lvl>=25 else 3 if lvl>=17 else 2 if lvl>=10 else 1 if lvl>=5 else 0
    stage_names = ["🥚 Яйцо","🐣 Птенец","🐥 Юнец","🐓 Взрослый","🦅 Старейшина"]
    await m.answer(
        f"📊 <b>Статистика питомца</b>\n\n"
        f"Питомец: {pet_line(pet)}\n"
        f"XP: <b>{xp}</b>\n"
        f"Уровень: <b>{lvl}</b> / 30\n"
        f"Стадия: {stage_names[stage_idx]}\n"
        f"До след. уровня: {50 - (xp % 50)} XP",
        parse_mode="HTML")


@dp.message(Command("addxp"))
async def cmd_addxp(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /addxp 200"); return
    try: amount = int(parts[1])
    except: await m.answer("Не число."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    await _apply_xp(pet, amount, m, "add")


@dp.message(Command("addxp_pet"))
async def cmd_addxp_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /addxp_pet <pet_id> 200"); return
    pid = parts[1].strip()
    try: amount = int(parts[2])
    except: await m.answer("Не число."); return
    if len(pid) < 30: await m.answer("Не UUID. Возьми ID из /list_pets."); return
    pet = one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer(f"Питомец <code>{pid}</code> не найден.", parse_mode="HTML"); return
    await _apply_xp(pet, amount, m, "add")


@dp.message(Command("setxp_pet"))
async def cmd_setxp_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /setxp_pet <pet_id> 1000"); return
    pid = parts[1].strip()
    try: amount = int(parts[2])
    except: await m.answer("Не число."); return
    if len(pid) < 30: await m.answer("Не UUID."); return
    pet = one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    await _apply_xp(pet, amount, m, "set")


async def _apply_xp(pet, amount, message, mode):
    ox = pet.get("xp") or 0
    nx = amount if mode == "set" else ox + amount
    if nx < 0: nx = 0
    sb.table("pets").update({"xp": nx}).eq("id", pet["id"]).execute()

    def lvl(x): return min(30, x // 50 + 1)
    def st(l): return 4 if l>=25 else 3 if l>=17 else 2 if l>=10 else 1 if l>=5 else 0
    ol, nl = lvl(ox), lvl(nx); os, ns = st(ol), st(nl)
    names = ["🥚","🐣","🐥","🐓","🦅"]

    text = (f"✅ <b>XP {'установлен' if mode=='set' else 'добавлен'}</b>\n\n"
            f"Питомец: <b>{pet['name']}</b>\n"
            f"ID: <code>{pet['id']}</code>\n\n"
            f"XP: {ox} → <b>{nx}</b>\n"
            f"Уровень: {ol} → <b>{nl}</b>")
    if ns != os:
        text += f"\n✨ <b>Эволюция!</b> {names[os]} → {names[ns]}"
    await message.answer(text, parse_mode="HTML")


# ============ Баланс команды ============

@dp.message(Command("setbal"))
async def cmd_setbal(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /setbal 5"); return
    try: amount = float(parts[1].replace(",","."))
    except: await m.answer("Не разобрать."); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ <b>Баланс: {amount:.4f} USDT</b>\n\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")


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
    await m.answer(
        f"✅ <b>Баланс: {nb:.4f} USDT</b>\n\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")


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
    await m.answer(
        f"✅ <b>Баланс: {amount:.4f} USDT</b>\n\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")


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
    await m.answer(
        f"✅ <b>Баланс: {nb:.4f} USDT</b>\n\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("list_pets"))
async def cmd_list_pets(m: Message):
    if not is_admin(m.from_user.id): return
    pets = many(sb.table("pets").select("id,name,owner_id,bank_balance,xp,skin"))
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🐾 <b>Все питомцы:</b>\n"]
    for p in pets[:30]:
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        lines.append(
            f"• <b>{p['name']}</b> · ур. {lvl}\n"
            f"  ID: <code>{p['id']}</code>\n"
            f"  Владелец: <code>{p['owner_id']}</code>\n"
            f"  Баланс: {float(p.get('bank_balance') or 0):.4f} · XP: {p.get('xp') or 0} · skin: {p.get('skin','classic')}"
        )
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("setskin"))
async def cmd_setskin(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2:
        await m.answer("Формат: /setskin classic|cat|dragon|space|dino"); return
    skin = parts[1].lower()
    if skin not in SKINS:
        await m.answer(f"Доступные: {', '.join(SKINS)}"); return
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if not pet: await m.answer("Нет питомца."); return
    sb.table("pets").update({"skin": skin}).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ Скин «{skin}» установлен\n\nПитомец: {pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("reset_scores"))
async def cmd_reset_scores(m: Message):
    if not is_admin(m.from_user.id): return
    today = date.today().isoformat()
    sb.table("members").update({"today_score": 0, "today_date": today}).neq("user_id", 0).execute()
    await m.answer("✅ Дневные очки обнулены у всех.")


@dp.message(Command("xr"))
async def cmd_xr(m: Message):
    if not is_admin(m.from_user.id): return
    await m.answer("🔍 Проверяю…")
    bal = await get_app_balance(); ch = await list_xrocket_cheques()
    lines = ["<b>📊 xRocket</b>\n"]
    if bal:
        lines.append(f"✅ {bal['path']}")
        lines.append(f"<pre>{str(bal['data'])[:400]}</pre>")
    else: lines.append("❌ Не удалось получить баланс.")
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
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')}")
    pet = one(sb.table("pets").select("*").eq("owner_id", m.from_user.id))
    if pet:
        db = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
        lines.append(f"\n<b>БД ({pet['name']}):</b> {len(db)}")
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
    if not ids: await m.answer("Нет чеков."); return
    await m.answer(f"🔍 Отменяю {len(ids)}…\nПитомец: {pet_line(pet)}", parse_mode="HTML")
    ok = 0; fail = 0; refund = 0.0
    for cid, info in ids.items():
        try:
            await delete_cheque(cid); ok += 1
            refund += float(info.get("amount") or 0)
            sb.table("cheques").update({"status": "cancelled"}).eq("cheque_id", cid).execute()
        except Exception as e:
            log.error("cancel %s: %s", cid, e); fail += 1
    if refund > 0:
        pn = one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        sb.table("pets").update({"bank_balance": round(cb + refund, 6)}).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ <b>Отмена завершена</b>\n\n"
        f"Питомец: {pet_line(pet)}\n"
        f"Отменено: {ok}\nОшибок: {fail}\n"
        f"Возвращено в банк: {refund:.4f} USDT",
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
    share = f"https://t.me/share/url?url={link}&text=Ухаживай за нашим питомцем!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Открыть", web_app=WebAppInfo(url=webapp_url(pet["id"])))],
        [InlineKeyboardButton(text="📤 Поделиться", url=share)]])
    await call.message.answer(
        f"🐣 <b>Питомец создан!</b>\n\n"
        f"Имя: <b>{pet['name']}</b>\n"
        f"ID: <code>{pet['id']}</code>\n\n"
        f"Ссылка для друзей:\n<code>{link}</code>",
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
        lines.append(f"• <b>{p['name']}</b> · ур. {lvl}\n  ID: <code>{p['id']}</code>")
        rows.append([InlineKeyboardButton(text=f"🐾 {p['name']} · ур. {lvl}",
                     web_app=WebAppInfo(url=webapp_url(p["id"])))])
    await call.message.answer("\n".join(lines), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer("🐾 Игра про общего питомца. /start чтобы начать.")


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
                    f"✅ <b>Оплата получена</b>\n\n"
                    f"Питомец: <b>{pet['name']}</b>\n"
                    f"ID: <code>{pet['id']}</code>\n"
                    f"Баланс: <b>{nb:.4f} USDT</b>",
                    parse_mode="HTML")
            except: pass
    return web.json_response({"ok": True})


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/xrocket", xrocket_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server on %s", PORT)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot @%s started (admins: %s)", me.username, ADMIN_IDS)
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
