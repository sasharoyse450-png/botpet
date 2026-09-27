import os
import json
import time
import hmac
import hashlib
import random
import asyncio
import logging
from datetime import date, datetime, timezone
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, PreCheckoutQuery, LabeledPrice,
)
from supabase import create_async_client, AsyncClient
from aiohttp import web
import aiohttp

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

ALLOWED_ORIGINS = os.environ.get(
    "ALLOWED_ORIGINS",
    "https://sasharoyse450-png.github.io"
).split(",")

ADMIN_IDS = {8130244626}
MIN_CHEQUE = 0.01
PLATFORM_FEE_PCT = 0.05
GLOBAL_PET_LIMIT = 100
MAX_LEVEL = 30
XP_PER_LEVEL = 50
DAILY_BONUS = 20
SKIN_PRICE = 50
SKINS = ["classic", "cat", "space", "dino"]

ACCESSORY_PRICE_STARS = 1

ACCESSORIES = {
    "crown":      {"emoji": "👑", "slot": "head", "name": "Корона"},
    "tophat":     {"emoji": "🎩", "slot": "head", "name": "Цилиндр"},
    "cap":        {"emoji": "🧢", "slot": "head", "name": "Кепка"},
    "grad":       {"emoji": "🎓", "slot": "head", "name": "Выпускник"},
    "sunhat":     {"emoji": "👒", "slot": "head", "name": "Шляпка"},
    "helmet":     {"emoji": "⛑",  "slot": "head", "name": "Каска"},
    "pumpkin":    {"emoji": "🎃", "slot": "head", "name": "Тыква"},
    "party":      {"emoji": "🎉", "slot": "head", "name": "Хлопушка"},
    "sunglasses": {"emoji": "🕶",  "slot": "eyes", "name": "Тёмные очки"},
    "nerd":       {"emoji": "👓", "slot": "eyes", "name": "Очки"},
    "goggles":    {"emoji": "🥽", "slot": "eyes", "name": "Маска"},
    "bow":        {"emoji": "🎀", "slot": "neck", "name": "Бантик"},
    "scarf":      {"emoji": "🧣", "slot": "neck", "name": "Шарф"},
    "medal":      {"emoji": "🏅", "slot": "neck", "name": "Медаль"},
    "beads":      {"emoji": "📿", "slot": "neck", "name": "Бусы"},
    "ribbon":     {"emoji": "🎗",  "slot": "neck", "name": "Ленточка"},
}

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: AsyncClient = None
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(uid): return uid in ADMIN_IDS
def gen_code(n=6): return "".join(random.choice(CODE_CHARS) for _ in range(n))
def webapp_url(pid): return f"{WEBAPP_URL}?pet={pid}"
def is_uuid(s): return s and len(s) >= 30 and "-" in s
def escape_html(s): return str(s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")


def pet_line(p):
    if not p: return "—"
    lvl = min(30, (p.get("xp") or 0) // 50 + 1)
    dead = " 💀" if p.get("dead") else ""
    return f"🐾 <b>{p.get('name','?')}</b>{dead} · ур. {lvl} · <code>{p.get('id')}</code>"


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


async def one(q):
    try:
        r = await q.limit(1).execute()
        d = getattr(r, "data", None) or []
        return d[0] if d else None
    except Exception as e:
        log.warning("one(): %s", e); return None


async def many(q):
    try:
        r = await q.execute()
        return getattr(r, "data", None) or []
    except Exception as e:
        log.warning("many(): %s", e); return []


async def my_pets(uid, alive_only=False):
    q = sb.table("pets").select("*").eq("owner_id", uid)
    if alive_only: q = q.eq("dead", False)
    return await many(q)


async def count_alive_pets():
    try:
        r = await sb.table("pets").select("id").eq("dead", False).execute()
        return len(r.data or [])
    except Exception as e:
        log.warning("count_alive: %s", e); return 0


async def resolve_pet(message: Message, args: list):
    if args and is_uuid(args[0].strip()):
        pid = args[0].strip()
        pet = await one(sb.table("pets").select("*").eq("id", pid))
        if not pet:
            await message.answer(f"❌ Питомец <code>{pid}</code> не найден.", parse_mode="HTML")
            return None, None
        return pet, args[1:]

    pets = await my_pets(message.from_user.id)
    if not pets:
        await message.answer("❌ У тебя нет питомцев.")
        return None, None
    if len(pets) > 1:
        lines = ["⚠️ У тебя несколько питомцев — укажи <b>ID первым аргументом</b>.\n"]
        for p in pets:
            lvl = min(30, (p.get("xp") or 0) // 50 + 1)
            dead = " 💀" if p.get("dead") else ""
            lines.append(f"• <b>{p['name']}</b>{dead} · ур. {lvl}\n  <code>{p['id']}</code>")
        await message.answer("\n".join(lines), parse_mode="HTML")
        return None, None
    return pets[0], args


# ============================================================
# initData HMAC
# ============================================================

def verify_init_data(init_data: str):
    if not init_data: return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash: return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash): return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except Exception:
        return None
    if abs(time.time() - auth_date) > 48 * 3600: return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except Exception:
        return None
    if not user or not user.get("id"): return None
    return user


def get_user_from_request(request: web.Request):
    return verify_init_data(request.headers.get("X-Telegram-Init-Data", ""))


def json_error(msg: str, status: int = 400):
    return web.json_response({"ok": False, "error": msg}, status=status)


# ============================================================
# CORS
# ============================================================

@web.middleware
async def cors_middleware(request, handler):
    _t0 = time.time()
    origin = request.headers.get("Origin", "")
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex
        except Exception as e:
            log.exception("handler error: %s", e)
            response = web.json_response({"ok": False, "error": str(e)}, status=500)
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Telegram-Init-Data"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "3600"
    if request.path.startswith("/api/"):
        dt_ms = (time.time() - _t0) * 1000
        log.info("⏱ %s %s — %.0f ms", request.method, request.path, dt_ms)
    return response


# ============================================================
# xRocket
# ============================================================

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
    return await xrocket_request("GET", f"/api/v1/invoice?invoiceId={iid}")


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


# ============================================================
# /start
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()
    user = message.from_user

    if payload.startswith("join_"):
        code = payload[5:]
        pet = await one(sb.table("pets").select("*").eq("invite_code", code))
        if not pet:
            await message.answer("Питомец не найден."); return
        await sb.table("members").upsert({
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
        pet = await one(sb.table("pets").select("*").eq("id", pet_id))
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
        await sb.table("invoices").insert({
            "invoice_id": str(iid), "pet_id": pet_id, "owner_id": user.id,
            "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
        }).execute()
        fee = amount * PLATFORM_FEE_PCT
        net = amount - fee
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
        await message.answer(
            f"💳 <b>Счёт {amount} USDT</b>\n{pet_line(pet)}\n"
            f"<i>В банк: {net:.4f} · комиссия: {fee:.4f}</i>",
            parse_mode="HTML", reply_markup=kb); return

    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3: await message.answer("Ошибка формата."); return
        try: cents = int(parts[1]); top_n = int(parts[2])
        except: await message.answer("Ошибка параметров."); return
        await run_salary_pet(parts[0], user.id, cents/100.0, top_n, message); return

    await message.answer(
        "👋 Бот общего питомца.\n\n"
        "Открой приложение — создай питомца или управляй им.\n\n"
        "В группе доступны команды:\n"
        "<code>/pet help</code> — справка",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐾 Открыть приложение",
                                  web_app=WebAppInfo(url=WEBAPP_URL))],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")]]))


@dp.callback_query(F.data.startswith("check_"))
async def cb_check_payment(call: CallbackQuery):
    iid = call.data[6:]; user = call.from_user
    inv = await one(sb.table("invoices").select("*").eq("invoice_id", iid))
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
        pet = await one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            paid_amount = float(inv["amount"])
            fee = round(paid_amount * PLATFORM_FEE_PCT, 6)
            net = round(paid_amount - fee, 6)
            nb = float(pet.get("bank_balance") or 0) + net
            await sb.table("pets").update({"bank_balance": nb}).eq("id", inv["pet_id"]).execute()
            await sb.table("invoices").update({"status": "paid"}).eq("invoice_id", iid).execute()
            try:
                await sb.table("platform_fees").insert({
                    "pet_id": inv["pet_id"], "owner_id": inv["owner_id"],
                    "amount": fee, "currency": inv.get("currency", "USDT"),
                    "invoice_id": str(iid),
                }).execute()
            except Exception as e:
                log.warning("fee insert failed: %s", e)
            await call.answer("Оплачено! ✅", show_alert=True)
            try:
                await call.message.edit_text(
                    f"✅ <b>Банк пополнен на {net:.4f} USDT</b>\n"
                    f"<i>комиссия: {fee:.4f} USDT</i>\n"
                    f"{pet_line(pet)}\nБаланс: <b>{nb:.4f} USDT</b>",
                    parse_mode="HTML")
            except: pass
            return
    await call.answer(f"Статус: {status or 'неизвестно'}", show_alert=True)


# ============================================================
# TELEGRAM STARS
# ============================================================

@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    try:
        await q.answer(ok=True)
    except Exception as e:
        log.warning("pre_checkout answer: %s", e)


@dp.message(F.successful_payment)
async def on_successful_payment(m: Message):
    sp = m.successful_payment
    payload = sp.invoice_payload or ""
    log.info("💫 Успешная оплата: %s (charge=%s, stars=%s)",
             payload, sp.telegram_payment_charge_id, sp.total_amount)

    if not payload.startswith("buy_acc|"):
        return

    try:
        _, pet_id, acc_id = payload.split("|", 2)
    except Exception:
        log.warning("bad payload: %s", payload); return

    acc = ACCESSORIES.get(acc_id)
    if not acc:
        log.warning("unknown accessory: %s", acc_id); return

    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet:
        log.warning("pet not found: %s", pet_id); return

    owned = list(pet.get("accessories_owned") or [])
    if acc_id not in owned:
        owned.append(acc_id)

    equipped = dict(pet.get("accessories_equipped") or {})
    equipped[acc["slot"]] = acc_id

    await sb.table("pets").update({
        "accessories_owned": owned,
        "accessories_equipped": equipped,
    }).eq("id", pet_id).execute()

    try:
        await m.answer(
            f"✨ <b>{acc['name']}</b> — куплен и надет!\n"
            f"{pet_line(pet)}\n\n"
            f"Открой приложение, чтобы увидеть.",
            parse_mode="HTML")
    except Exception:
        pass


# ============================================================
# тики
# ============================================================

def _stage_idx(level):
    return 7 if level>=28 else 6 if level>=24 else 5 if level>=20 else 4 if level>=16 \
        else 3 if level>=12 else 2 if level>=8 else 1 if level>=4 else 0


def apply_pet_tick(pet):
    if pet.get("dead"): return pet
    now = datetime.now(timezone.utc)
    last_str = pet.get("last_tick_at")
    if not last_str: return pet
    try:
        last = datetime.fromisoformat(last_str.replace("Z","+00:00"))
    except Exception: return pet
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
    if c["health"] <= 0:
        c["health"] = 0; c["dead"] = True; c["dead_at"] = now.isoformat()
    return c


async def save_pet_tick(pet):
    if pet.get("dead"): return pet
    ticked = apply_pet_tick(pet)
    if ticked.get("last_tick_at") != pet.get("last_tick_at") or ticked.get("dead"):
        upd = {
            "hunger": ticked["hunger"], "mood": ticked["mood"],
            "energy": ticked["energy"], "clean": ticked["clean"],
            "health": ticked["health"], "last_tick_at": ticked["last_tick_at"],
        }
        if ticked.get("dead"):
            upd["dead"] = True; upd["dead_at"] = ticked.get("dead_at")
        await sb.table("pets").update(upd).eq("id", pet["id"]).execute()
    return ticked


async def death_watch_loop():
    await asyncio.sleep(60)
    while True:
        try:
            pets = await many(sb.table("pets").select("*").eq("dead", False))
            log.info("death_watch: %s живых", len(pets))
            for pet in pets:
                try:
                    ticked = apply_pet_tick(pet)
                    if ticked.get("last_tick_at") == pet.get("last_tick_at") and not ticked.get("dead"):
                        continue
                    upd = {
                        "hunger": ticked["hunger"], "mood": ticked["mood"],
                        "energy": ticked["energy"], "clean": ticked["clean"],
                        "health": ticked["health"], "last_tick_at": ticked["last_tick_at"],
                    }
                    if ticked.get("dead"):
                        upd["dead"] = True; upd["dead_at"] = ticked.get("dead_at")
                    await sb.table("pets").update(upd).eq("id", pet["id"]).execute()
                    if ticked.get("dead"):
                        log.info("💀 умер: %s", pet["name"])
                        try:
                            await bot.send_message(
                                pet["owner_id"],
                                f"💀 <b>{escape_html(pet['name'])} умер…</b>\n\n"
                                f"Ты слишком долго не заботился.\n"
                                f"Возроди его в приложении за 100 очков.",
                                parse_mode="HTML")
                        except Exception as e:
                            log.warning("death notify: %s", e)
                except Exception as e:
                    log.error("tick err %s: %s", pet.get("id"), e)
        except Exception as e:
            log.error("death_watch_loop: %s", e)
        await asyncio.sleep(30 * 60)


# ============================================================
# /pet
# ============================================================

PET_ACTIONS = {
    "feed": {"emoji":"🍖","label":"Покормил","cd": 5*60, "xp": 2,
             "effects": {"hunger": 25, "energy": 10}, "score": 1},
    "pet":  {"emoji":"✋","label":"Погладил","cd": 60, "xp": 1,
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
    "кд":"kd","kd":"kd","кулдаун":"kd","кулдауны":"kd","cooldown":"kd","cooldowns":"kd","таймер":"kd",
}

PET_STAGE_NAMES = {
    "classic": ["Яйцо","Птенец","Юнец","Подросток","Взрослый","Опытный","Старейшина","Легенда"],
    "cat":     ["Яйцо","Котёнок","Котик","Чёрный кот","Леопард","Тигр","Лев","Царь зверей"],
    "space":   ["Туманность","Луна","Звезда","Яркая звезда","Созвездие","Комета","Сверхновая","Солнце"],
    "dino":    ["Яйцо","Ящерка","Динозаврик","Юный дино","Ящер","Хищный дино","Древний ящер","Вулкан"],
}


async def _send_pet_info(message, pet):
    pet = await save_pet_tick(pet)
    if pet.get("dead"):
        await message.answer(f"💀 <b>{escape_html(pet['name'])} умер.</b>", parse_mode="HTML"); return
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
        f"<code>/pet {pet['name']} kd</code> — кулдауны"
    )
    await message.answer(text, parse_mode="HTML")


async def _send_pet_help(message):
    await message.answer(
        "🐾 <b>Управление питомцем</b>\n\n"
        "<code>/pet</code> — список\n"
        "<code>/pet ИМЯ</code> — статы\n"
        "<code>/pet ИМЯ kd</code> — кулдауны\n"
        "<code>/pet ИМЯ покормить</code>\n"
        "<code>/pet ИМЯ погладить</code>\n"
        "<code>/pet ИМЯ играть</code>\n"
        "<code>/pet ИМЯ помыть</code>\n"
        "<code>/pet ИМЯ лечить</code>",
        parse_mode="HTML")


async def _send_pet_kd(message, pet, user):
    mem = await one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", user.id))
    if not mem:
        await message.answer("Ты не участник."); return
    now = datetime.now(timezone.utc)
    lines = [f"⏱ <b>Кулдауны на {escape_html(pet['name'])}</b>\n"]
    any_cd = False
    for key in ("feed","pet","play","wash","heal"):
        cfg = PET_ACTIONS[key]
        ls = mem.get("last_"+key+"_at")
        if not ls:
            lines.append(f"{cfg['emoji']} {cfg['label']} — готово"); continue
        try: last_dt = datetime.fromisoformat(ls.replace("Z","+00:00"))
        except: lines.append(f"{cfg['emoji']} {cfg['label']} — готово"); continue
        left = cfg["cd"] - (now - last_dt).total_seconds()
        if left <= 0:
            lines.append(f"{cfg['emoji']} {cfg['label']} — готово")
        else:
            any_cd = True
            mm, ss = int(left // 60), int(left % 60)
            t = (f"{mm//60} ч {mm%60} мин" if mm>=60 else
                 f"{mm} мин {ss} сек" if mm>0 else f"{ss} сек")
            lines.append(f"{cfg['emoji']} {cfg['label']} — ⏳ <b>{t}</b>")
    if not any_cd: lines.append("\n✨ Всё готово!")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("pet"))
async def cmd_pet(message: Message):
    user = message.from_user
    parts = (message.text or "").split()
    args = parts[1:] if len(parts) > 1 else []

    if args and args[0].lower() in ("help","помощь","хелп","?","справка"):
        await _send_pet_help(message); return

    if not args:
        members = await many(sb.table("members").select("pets!inner(*)").eq("user_id", user.id))
        if not members:
            await message.answer("У тебя нет питомцев. Создай в приложении."); return
        lines = ["🐾 <b>Твои питомцы:</b>\n"]
        for m in members[:10]:
            p = m.get("pets") or {}
            lvl = min(30, (p.get("xp") or 0) // 50 + 1)
            dead = " 💀" if p.get("dead") else ""
            lines.append(f"• <b>{p['name']}</b>{dead} · ур. {lvl}\n  <code>/pet {p['name']}</code>")
        lines.append("\n<code>/pet help</code> — справка")
        await message.answer("\n".join(lines), parse_mode="HTML"); return

    name_query = args[0].strip()
    action_key = args[1].lower() if len(args) > 1 else None

    pet = None
    members = await many(sb.table("members").select("pets!inner(*)").eq("user_id", user.id))

    if is_uuid(name_query):
        candidate = await one(sb.table("pets").select("*").eq("id", name_query))
        if candidate:
            ok = await one(sb.table("members").select("*").eq("pet_id", candidate["id"]).eq("user_id", user.id))
            if ok: pet = candidate
    else:
        nq = name_query.lower()
        for m in members:
            p = m.get("pets") or {}
            if (p.get("name") or "").lower() == nq: pet = p; break
        if not pet:
            for m in members:
                p = m.get("pets") or {}
                if nq in (p.get("name") or "").lower(): pet = p; break

    if not pet:
        await message.answer(f"🐾 «{escape_html(name_query)}» не найден. /pet"); return

    if pet.get("dead"):
        await message.answer(f"💀 {escape_html(pet['name'])} умер. Возроди в приложении.",
                             parse_mode="HTML"); return

    if not action_key:
        await _send_pet_info(message, pet); return
    action = PET_ALIASES.get(action_key)
    if action == "info": await _send_pet_info(message, pet); return
    if action == "kd": await _send_pet_kd(message, pet, user); return
    if not action:
        await message.answer(f"Не понимаю «{escape_html(action_key)}». /pet help", parse_mode="HTML"); return

    cfg = PET_ACTIONS[action]
    pet = await one(sb.table("pets").select("*").eq("id", pet["id"]))
    if not pet or pet.get("dead"): return
    pet = await save_pet_tick(pet)
    if pet.get("dead"): return
    mem = await one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", user.id))
    if not mem: await message.answer("Ты не участник."); return

    last_key = "last_"+action+"_at"
    ls = mem.get(last_key)
    if ls:
        try:
            last_dt = datetime.fromisoformat(ls.replace("Z","+00:00"))
            left = cfg["cd"] - (datetime.now(timezone.utc) - last_dt).total_seconds()
            if left > 0:
                mm, ss = int(left // 60), int(left % 60)
                t = f"{mm} мин {ss} сек" if mm else f"{ss} сек"
                await message.answer(f"⌛ Ещё рано. Ждать {t}."); return
        except: pass

    score = mem.get("score") or 0
    if cfg.get("cost") and score < cfg["cost"]:
        await message.answer(f"❌ Нужно {cfg['cost']} очков, у тебя {score}."); return

    new_pet = dict(pet)
    for k, v in cfg["effects"].items():
        new_pet[k] = max(0, min(100, (new_pet.get(k) or 0) + v))
    new_pet["xp"] = (pet.get("xp") or 0) + cfg["xp"]
    old_st = _stage_idx(min(30, (pet.get("xp") or 0) // 50 + 1))
    new_st = _stage_idx(min(30, new_pet["xp"] // 50 + 1))

    await sb.table("pets").update({
        "hunger": new_pet["hunger"], "mood": new_pet["mood"],
        "energy": new_pet["energy"], "clean": new_pet["clean"],
        "health": new_pet["health"], "xp": new_pet["xp"],
        "last_tick_at": new_pet["last_tick_at"],
    }).eq("id", pet["id"]).execute()

    iso_now = datetime.now(timezone.utc).isoformat()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    new_score = score + cfg["score"]
    m_upd = {"score": new_score, last_key: iso_now}
    if mem.get("today_date") == today_iso:
        m_upd["today_score"] = (mem.get("today_score") or 0) + max(cfg["score"], 0)
    else:
        m_upd["today_score"] = max(cfg["score"], 0)
        m_upd["today_date"] = today_iso
    await sb.table("members").update(m_upd).eq("pet_id", pet["id"]).eq("user_id", user.id).execute()

    try:
        await sb.table("events").insert({
            "pet_id": pet["id"], "user_id": user.id,
            "first_name": user.first_name or "Гость", "action": action,
        }).execute()
    except: pass

    uname = escape_html(user.first_name or "Кто-то")
    changed = []
    for k, v in cfg["effects"].items():
        lbl = {"hunger":"сытость","mood":"настроение","energy":"энергия",
               "clean":"чистота","health":"здоровье"}.get(k, k)
        changed.append(f"{lbl} {'+' if v>0 else '−'}{abs(v)}")
    text = f"{cfg['emoji']} <b>{uname}</b> — {cfg['label'].lower()} <b>{escape_html(pet['name'])}</b>"
    if changed: text += "\n  " + " · ".join(changed)
    if new_st > old_st:
        names = PET_STAGE_NAMES.get(pet.get("skin","classic"), PET_STAGE_NAMES["classic"])
        text += f"\n\n✨ <b>Эволюция!</b> Теперь <b>{names[new_st]}</b>"
    await message.answer(text, parse_mode="HTML")


# ============================================================
# /balance, /topup, /salary
# ============================================================

@dp.message(Command("balance"))
async def cmd_balance(m: Message):
    pets = await my_pets(m.from_user.id)
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🏦 <b>Балансы:</b>\n"]
    for p in pets:
        dead = " 💀" if p.get("dead") else ""
        lines.append(f"• <b>{p['name']}</b>{dead} — <b>{float(p.get('bank_balance') or 0):.4f} USDT</b>\n  <code>{p['id']}</code>")
    await m.answer("\n".join(lines), parse_mode="HTML")


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
    await sb.table("invoices").insert({
        "invoice_id": str(iid), "pet_id": pet["id"], "owner_id": user.id,
        "amount": amount, "currency": pet.get("currency","USDT"), "status": "pending",
    }).execute()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount}", url=link)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{iid}")]])
    fee = amount * PLATFORM_FEE_PCT
    net = amount - fee
    await m.answer(
        f"💳 <b>Счёт {amount} USDT</b>\n{pet_line(pet)}\n"
        f"<i>В банк: {net:.4f} · комиссия: {fee:.4f}</i>",
        parse_mode="HTML", reply_markup=kb)


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
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
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
    members = await many(sb.table("members").select("*").eq("pet_id", pet["id"]))
    for m in members:
        if m.get("today_date") != today:
            await sb.table("members").update({"today_score": 0, "today_date": today})\
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

    await sb.table("pets").update({"bank_balance": balance - amount}).eq("id", pet["id"]).execute()
    po = await sb.table("payouts").insert({
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
        await sb.table("cheques").insert({
            "payout_id": payout["id"], "pet_id": pet["id"], "user_id": m["user_id"],
            "amount": share, "cheque_id": cid, "cheque_link": link,
            "status": "sent" if link else "no_link"}).execute()
        if link:
            try:
                await bot.send_message(m["user_id"],
                    f"💰 <b>Зарплата</b>\n\nПитомец: <b>{pet['name']}</b>\n"
                    f"ID: <code>{pet['id']}</code>\nАктивность: {score} очков\n"
                    f"Начислено: <b>{share:.4f} USDT</b>\n\nЗабрать: {link}",
                    parse_mode="HTML", disable_web_page_preview=True)
            except: pass
        sent += 1; total_sent += share; paid.append(m["user_id"])
    refund = round(amount - total_sent, 6)
    if refund > 0:
        pn = await one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        await sb.table("pets").update({"bank_balance": cb + refund}).eq("id", pet["id"]).execute()
    for m in winners:
        if m["user_id"] in paid:
            await sb.table("members").update({"today_score": 0, "today_date": today})\
              .eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
    pn = await one(sb.table("pets").select("*").eq("id", pet["id"]))
    final = float(pn.get("bank_balance") or 0)
    text = (f"✅ <b>Выплата</b>\n{pet_line(pet)}\n"
            f"Раздано: {total_sent:.4f} из {amount:.4f}\n"
            f"Чеков: {sent} из {len(winners)}\n"
            f"Остаток: <b>{final:.4f} USDT</b>")
    if failed_msg: text += f"\n\n⚠️ {failed_msg}"
    await message.answer(text, parse_mode="HTML")


# ============================================================
# АДМИНКА
# ============================================================

@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if not is_admin(m.from_user.id): return
    alive = await count_alive_pets()
    await m.answer(
        f"🛠 <b>Админка</b>\n\n"
        f"Живых питомцев: <b>{alive} / {GLOBAL_PET_LIMIT}</b>\n\n"
        "<b>Инфо:</b>\n"
        "/my_pets_admin — мои питомцы с ID\n"
        "/list_pets — все питомцы\n"
        "/xp [pet_id] — статистика питомца\n"
        "/balance — балансы\n\n"
        "<b>Очки:</b>\n"
        "/addscore [pet_id] 100 — себе\n"
        "/addscore_user [pet_id] &lt;uid&gt; 100\n"
        "/setscore_user [pet_id] &lt;uid&gt; 100\n\n"
        "<b>XP:</b>\n"
        "/addxp [pet_id] 200\n"
        "/setxp [pet_id] 1000\n\n"
        "<b>Баланс:</b>\n"
        "/setbal [pet_id] 5\n"
        "/addbal [pet_id] 1\n\n"
        "<b>Скин:</b>\n"
        "/setskin [pet_id] classic|cat|space|dino\n\n"
        "<b>Аксессуары:</b>\n"
        "/giveacc [pet_id] crown — выдать один\n"
        "/giveall [pet_id] — выдать ВСЕ\n"
        "/clearacc [pet_id] — снять всё\n\n"
        "<b>Смерть:</b>\n"
        "/revive [pet_id] · /kill [pet_id]\n\n"
        "<b>Прочее:</b>\n"
        "/fees — комиссии платформы\n"
        "/cheques [pet_id] — чеки\n"
        "/cancel_cheques [pet_id] — отменить\n"
        "/reset_scores\n"
        "/xr — xRocket",
        parse_mode="HTML")


@dp.message(Command("giveacc"))
async def cmd_giveacc(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest:
        accs = "\n".join(f"• <code>{k}</code> — {v['emoji']} {v['name']} ({v['slot']})"
                         for k, v in ACCESSORIES.items())
        await m.answer(f"Формат: /giveacc [pet_id] crown\n\n<b>Доступные:</b>\n{accs}", parse_mode="HTML"); return
    acc_id = rest[0].strip().lower()
    acc = ACCESSORIES.get(acc_id)
    if not acc:
        await m.answer(f"Нет такого аксессуара: <code>{acc_id}</code>"); return
    owned = list(pet.get("accessories_owned") or [])
    if acc_id not in owned: owned.append(acc_id)
    equipped = dict(pet.get("accessories_equipped") or {})
    equipped[acc["slot"]] = acc_id
    await sb.table("pets").update({
        "accessories_owned": owned, "accessories_equipped": equipped,
    }).eq("id", pet["id"]).execute()
    await m.answer(f"✅ Выдан {acc['emoji']} <b>{acc['name']}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("giveall"))
async def cmd_giveall(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, _ = await resolve_pet(m, args)
    if not pet: return
    owned = list(ACCESSORIES.keys())
    await sb.table("pets").update({
        "accessories_owned": owned,
    }).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ Выдал все <b>{len(owned)}</b> аксессуаров\n{pet_line(pet)}\n\n"
        f"Открой приложение → 🎩 → выбирай и надевай.",
        parse_mode="HTML")


@dp.message(Command("clearacc"))
async def cmd_clearacc(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, _ = await resolve_pet(m, args)
    if not pet: return
    await sb.table("pets").update({
        "accessories_owned": [], "accessories_equipped": {},
    }).eq("id", pet["id"]).execute()
    await m.answer(f"✅ Аксессуары сняты\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("fees"))
async def cmd_fees(m: Message):
    if not is_admin(m.from_user.id): return
    rows = await many(sb.table("platform_fees").select("*").order("created_at", desc=True).limit(100))
    if not rows: await m.answer("Комиссий пока нет."); return
    total = sum(float(r.get("amount") or 0) for r in rows)
    lines = [f"💰 <b>Комиссии (5%)</b>\n\nВсего: <b>{total:.4f} USDT</b> ({len(rows)} операций)\n"]
    by_owner = {}
    for r in rows:
        oid = r.get("owner_id")
        by_owner[oid] = by_owner.get(oid, 0) + float(r.get("amount") or 0)
    lines.append("<b>По владельцам:</b>")
    for oid, amt in sorted(by_owner.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"• <code>{oid}</code> — {amt:.4f}")
    lines.append("\n<b>Последние:</b>")
    for r in rows[:10]:
        dt = (r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(f"• {dt} — {float(r.get('amount') or 0):.4f} от <code>{r.get('owner_id')}</code>")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("revive"))
async def cmd_revive(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /revive <pet_id>"); return
    pid = parts[1]
    if not is_uuid(pid): await m.answer("UUID нужен."); return
    now = datetime.now(timezone.utc).isoformat()
    await sb.table("pets").update({
        "dead": False, "dead_at": None,
        "health": 50, "hunger": 50, "mood": 50, "clean": 50, "energy": 50,
        "last_tick_at": now,
    }).eq("id", pid).execute()
    await m.answer(f"✅ Возрождён: <code>{pid}</code>", parse_mode="HTML")


@dp.message(Command("kill"))
async def cmd_kill(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 2: await m.answer("Формат: /kill <pet_id>"); return
    pid = parts[1]
    if not is_uuid(pid): await m.answer("UUID нужен."); return
    await sb.table("pets").update({
        "dead": True, "dead_at": datetime.now(timezone.utc).isoformat(), "health": 0,
    }).eq("id", pid).execute()
    await m.answer(f"💀 Убит: <code>{pid}</code>", parse_mode="HTML")


@dp.message(Command("my_pets_admin"))
async def cmd_my_pets_admin(m: Message):
    if not is_admin(m.from_user.id): return
    pets = await my_pets(m.from_user.id)
    if not pets: await m.answer("Нет питомцев."); return
    lines = ["🐾 <b>Твои питомцы:</b>\n"]
    for p in pets:
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        dead = " 💀" if p.get("dead") else ""
        lines.append(f"• <b>{p['name']}</b>{dead} · ур. {lvl}\n  <code>{p['id']}</code>\n"
                     f"  Баланс: {float(p.get('bank_balance') or 0):.4f} · XP: {p.get('xp') or 0} · скин: {p.get('skin','classic')}")
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("list_pets"))
async def cmd_list_pets(m: Message):
    if not is_admin(m.from_user.id): return
    pets = await many(sb.table("pets").select("*").limit(50))
    alive = sum(1 for p in pets if not p.get("dead"))
    lines = [f"🌍 <b>Питомцы ({alive} живых / {GLOBAL_PET_LIMIT}):</b>\n"]
    for p in pets[:40]:
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        dead = " 💀" if p.get("dead") else ""
        mine = " ⭐" if p["owner_id"] == m.from_user.id else ""
        lines.append(
            f"• <b>{p['name']}</b>{dead}{mine} · ур. {lvl}\n"
            f"  <code>{p['id']}</code>\n"
            f"  owner <code>{p['owner_id']}</code> · {float(p.get('bank_balance') or 0):.4f} USDT · {p.get('xp') or 0} XP"
        )
    await m.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("addscore"))
async def cmd_addscore(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest: await m.answer("Формат: /addscore [pet_id] 100"); return
    try: amount = int(rest[0])
    except: await m.answer("Не число."); return
    mem = await one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", m.from_user.id))
    if not mem: await m.answer("Ты не участник."); return
    ns = (mem.get("score") or 0) + amount
    await sb.table("members").update({"score": ns}).eq("pet_id", pet["id"]).eq("user_id", m.from_user.id).execute()
    await m.answer(f"✅ <b>Очки: {mem.get('score') or 0} → {ns}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("addscore_user"))
async def cmd_addscore_user(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if len(rest) < 2: await m.answer("Формат: /addscore_user [pet_id] <uid> 100"); return
    try: uid = int(rest[0]); amount = int(rest[1])
    except: await m.answer("Не числа."); return
    mem = await one(sb.table("members").select("*").eq("pet_id", pet["id"]).eq("user_id", uid))
    if not mem: await m.answer(f"Юзер <code>{uid}</code> не участник.", parse_mode="HTML"); return
    ns = (mem.get("score") or 0) + amount
    await sb.table("members").update({"score": ns}).eq("pet_id", pet["id"]).eq("user_id", uid).execute()
    await m.answer(f"✅ <b>Очки {uid}: {mem.get('score') or 0} → {ns}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("setscore_user"))
async def cmd_setscore_user(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if len(rest) < 2: await m.answer("Формат: /setscore_user [pet_id] <uid> 100"); return
    try: uid = int(rest[0]); amount = int(rest[1])
    except: await m.answer("Не числа."); return
    await sb.table("members").update({"score": amount}).eq("pet_id", pet["id"]).eq("user_id", uid).execute()
    await m.answer(f"✅ <b>Очки {uid} = {amount}</b>\n{pet_line(pet)}", parse_mode="HTML")


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
    await sb.table("pets").update({"xp": nx}).eq("id", pet["id"]).execute()
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
    await sb.table("pets").update({"bank_balance": amount}).eq("id", pet["id"]).execute()
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
    await sb.table("pets").update({"bank_balance": nb}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ <b>Баланс: {nb:.4f} USDT</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("setbal_pet"))
async def cmd_setbal_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /setbal_pet <pet_id> 5"); return
    pid = parts[1].strip()
    try: amount = float(parts[2].replace(",","."))
    except: await m.answer("Не разобрать."); return
    if not is_uuid(pid): await m.answer("UUID нужен."); return
    pet = await one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    await sb.table("pets").update({"bank_balance": amount}).eq("id", pid).execute()
    await m.answer(f"✅ <b>Баланс: {amount:.4f}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("addbal_pet"))
async def cmd_addbal_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer("Формат: /addbal_pet <pet_id> 1"); return
    pid = parts[1].strip()
    try: amount = float(parts[2].replace(",","."))
    except: await m.answer("Не разобрать."); return
    if not is_uuid(pid): await m.answer("UUID нужен."); return
    pet = await one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    nb = float(pet.get("bank_balance") or 0) + amount
    await sb.table("pets").update({"bank_balance": nb}).eq("id", pid).execute()
    await m.answer(f"✅ <b>Баланс: {nb:.4f}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("setskin"))
async def cmd_setskin(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    pet, rest = await resolve_pet(m, args)
    if not pet: return
    if not rest:
        await m.answer(f"Формат: /setskin [pet_id] <{'|'.join(SKINS)}>\n\nТекущий: {pet.get('skin','classic')}"); return
    skin = rest[0].lower()
    if skin not in SKINS:
        await m.answer(f"Доступные: {', '.join(SKINS)}"); return
    await sb.table("pets").update({"skin": skin}).eq("id", pet["id"]).execute()
    await m.answer(f"✅ <b>Скин: {skin}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("setskin_pet"))
async def cmd_setskin_pet(m: Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split()
    if len(parts) < 3: await m.answer(f"Формат: /setskin_pet <pet_id> <{'|'.join(SKINS)}>"); return
    pid = parts[1].strip(); skin = parts[2].lower()
    if not is_uuid(pid): await m.answer("UUID нужен."); return
    if skin not in SKINS: await m.answer(f"Доступные: {', '.join(SKINS)}"); return
    pet = await one(sb.table("pets").select("*").eq("id", pid))
    if not pet: await m.answer("Не найден."); return
    await sb.table("pets").update({"skin": skin}).eq("id", pid).execute()
    await m.answer(f"✅ <b>Скин: {skin}</b>\n{pet_line(pet)}", parse_mode="HTML")


@dp.message(Command("reset_scores"))
async def cmd_reset_scores(m: Message):
    if not is_admin(m.from_user.id): return
    today = date.today().isoformat()
    await sb.table("members").update({"today_score": 0, "today_date": today}).neq("user_id", 0).execute()
    await m.answer("✅ Обнулено.")


@dp.message(Command("cheques"))
async def cmd_cheques(m: Message):
    if not is_admin(m.from_user.id): return
    args = m.text.split()[1:]
    xr = await list_xrocket_cheques()
    lines = [f"🧾 <b>Чеки xRocket:</b> {len(xr)}"]
    for c in xr[:10]:
        lines.append(f"• <code>{c.get('chequeId') or c.get('id')}</code> — {c.get('amount','?')}")
    pet, _ = await resolve_pet(m, args) if args else (None, None)
    if pet:
        db = await many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
        lines.append(f"\n<b>{pet['name']}</b>: {len(db)} в БД")
    else:
        pets = await my_pets(m.from_user.id)
        for p in pets[:5]:
            db = await many(sb.table("cheques").select("*").eq("pet_id", p["id"]).eq("status","sent"))
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
    db = await many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status","sent"))
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
            await sb.table("cheques").update({"status": "cancelled"}).eq("cheque_id", cid).execute()
        except: fail += 1
    if refund > 0:
        pn = await one(sb.table("pets").select("*").eq("id", pet["id"]))
        cb = float(pn.get("bank_balance") or 0)
        await sb.table("pets").update({"bank_balance": round(cb + refund, 6)}).eq("id", pet["id"]).execute()
    await m.answer(
        f"✅ Отменено: {ok}\nОшибок: {fail}\nВозвращено: {refund:.4f} USDT\n{pet_line(pet)}",
        parse_mode="HTML")


@dp.message(Command("xr"))
async def cmd_xr(m: Message):
    if not is_admin(m.from_user.id): return
    bal = await get_app_balance(); ch = await list_xrocket_cheques()
    lines = ["<b>📊 xRocket</b>\n"]
    if bal:
        lines.append(f"✅ {bal['path']}")
        lines.append(f"<pre>{str(bal['data'])[:400]}</pre>")
    else: lines.append("❌ Не получил баланс.")
    lines.append(f"🧾 Чеков: {len(ch)}")
    await m.answer("\n".join(lines), parse_mode="HTML")


# ============================================================
# create / my_pets (callbacks)
# ============================================================

@dp.callback_query(F.data == "create")
async def cb_create(call: CallbackQuery):
    user = call.from_user
    existing = await my_pets(user.id, alive_only=True)
    if existing:
        await call.answer("У тебя уже есть питомец", show_alert=True); return
    total = await count_alive_pets()
    if total >= GLOBAL_PET_LIMIT:
        await call.answer(f"Все {GLOBAL_PET_LIMIT} питомцев заняты", show_alert=True); return
    pet = None
    for _ in range(5):
        code = gen_code()
        try:
            r = await sb.table("pets").insert({"owner_id": user.id, "invite_code": code}).execute()
            if r.data: pet = r.data[0]; break
        except Exception as e:
            if "23505" not in str(e): break
    if not pet: await call.answer("Ошибка.", show_alert=True); return
    await sb.table("members").insert({
        "pet_id": pet["id"], "user_id": user.id,
        "first_name": user.first_name or "Гость", "username": user.username,
    }).execute()
    await call.answer("Готово!")
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=join_{pet['invite_code']}"
    share = f"https://t.me/share/url?url={link}&text=Ухаживай за питомцем!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Открыть", web_app=WebAppInfo(url=webapp_url(pet["id"])))],
        [InlineKeyboardButton(text="📤 Поделиться", url=share)]])
    left = GLOBAL_PET_LIMIT - (total + 1)
    await call.message.answer(
        f"🐣 <b>Питомец создан!</b>\n\n<b>{pet['name']}</b>\nID: <code>{pet['id']}</code>\n\n"
        f"Свободных мест: <b>{left} / {GLOBAL_PET_LIMIT}</b>\n\nСсылка:\n<code>{link}</code>",
        parse_mode="HTML", reply_markup=kb)


@dp.callback_query(F.data == "my_pets")
async def cb_my_pets(call: CallbackQuery):
    await call.answer()
    data = await many(sb.table("members").select("pet_id, pets!inner(id, name, xp, dead)")\
                .eq("user_id", call.from_user.id).order("score", desc=True).limit(20))
    if not data: await call.message.answer("Нет питомцев."); return
    rows = []
    lines = ["📋 <b>Твои питомцы:</b>\n"]
    for m in data:
        p = m.get("pets") or {}
        if not p: continue
        lvl = min(30, (p.get("xp") or 0)//50+1)
        dead = " 💀" if p.get("dead") else ""
        lines.append(f"• <b>{p['name']}</b>{dead} · ур. {lvl}\n  <code>{p['id']}</code>")
        rows.append([InlineKeyboardButton(text=f"🐾 {p['name']}{dead} · ур. {lvl}",
                     web_app=WebAppInfo(url=webapp_url(p["id"])))])
    await call.message.answer("\n".join(lines), parse_mode="HTML",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "🐾 <b>Игра про общего питомца</b>\n\n"
        "• /start — открыть приложение\n"
        "• /pet — мои питомцы\n"
        "• /pet help — справка по чату\n"
        "• /pet имя покормить — покормить\n"
        "• /pet имя kd — кулдауны",
        parse_mode="HTML")


# ============================================================
# API ДЛЯ ФРОНТА
# ============================================================

async def _log_event_bg(pet_id, user_id, first_name, action):
    try:
        await sb.table("events").insert({
            "pet_id": pet_id, "user_id": user_id,
            "first_name": first_name or "Гость", "action": action,
        }).execute()
    except Exception as e:
        log.warning("event insert: %s", e)


async def api_open(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    if not pet_id: return json_error("pet_id required")

    pet_task = sb.table("pets").select("*").eq("id", pet_id).limit(1).execute()
    mem_task = sb.table("members").select("*").eq("pet_id", pet_id).eq("user_id", user["id"]).limit(1).execute()
    r_pet, r_mem = await asyncio.gather(pet_task, mem_task, return_exceptions=True)
    pet = (getattr(r_pet, "data", None) or [None])[0] if not isinstance(r_pet, Exception) else None
    mem = (getattr(r_mem, "data", None) or [None])[0] if not isinstance(r_mem, Exception) else None

    if not pet: return json_error("pet not found", 404)

    joined = False
    if not mem:
        try:
            res = await sb.table("members").insert({
                "pet_id": pet_id, "user_id": user["id"],
                "username": user.get("username"),
                "first_name": user.get("first_name") or "Гость",
                "score": 0,
            }).execute()
            mem = res.data[0] if res.data else None
            joined = True
        except Exception as e:
            log.warning("member insert: %s", e)

    if not pet.get("dead"):
        ticked = apply_pet_tick(pet)
        if ticked.get("last_tick_at") != pet.get("last_tick_at") or ticked.get("dead"):
            upd = {
                "hunger": ticked["hunger"], "mood": ticked["mood"],
                "energy": ticked["energy"], "clean": ticked["clean"],
                "health": ticked["health"], "last_tick_at": ticked["last_tick_at"],
            }
            if ticked.get("dead"):
                upd["dead"] = True; upd["dead_at"] = ticked.get("dead_at")
            await sb.table("pets").update(upd).eq("id", pet_id).execute()
            pet = ticked

    return web.json_response({"ok": True, "pet": pet, "me": mem, "joined": joined})


async def api_action(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    action = body.get("action")
    if not pet_id or action not in PET_ACTIONS:
        return json_error("invalid params")

    pet_task = sb.table("pets").select("*").eq("id", pet_id).limit(1).execute()
    mem_task = sb.table("members").select("*").eq("pet_id", pet_id).eq("user_id", user["id"]).limit(1).execute()
    r_pet, r_mem = await asyncio.gather(pet_task, mem_task, return_exceptions=True)
    pet = (getattr(r_pet, "data", None) or [None])[0] if not isinstance(r_pet, Exception) else None
    mem = (getattr(r_mem, "data", None) or [None])[0] if not isinstance(r_mem, Exception) else None

    if not pet: return json_error("pet not found", 404)
    if not mem: return json_error("not a member", 403)
    if pet.get("dead"): return json_error("pet is dead", 400)

    cfg = PET_ACTIONS[action]

    last_key = "last_" + action + "_at"
    ls = mem.get(last_key)
    if ls:
        try:
            last_dt = datetime.fromisoformat(ls.replace("Z", "+00:00"))
            left = cfg["cd"] - (datetime.now(timezone.utc) - last_dt).total_seconds()
            if left > 0:
                return json_error(f"cooldown {int(left)}s", 429)
        except: pass

    score = mem.get("score") or 0
    if cfg.get("cost") and score < cfg["cost"]:
        return json_error(f"need {cfg['cost']} score", 400)

    ticked = apply_pet_tick(pet)
    if ticked.get("dead"):
        await sb.table("pets").update({
            "hunger": ticked["hunger"], "mood": ticked["mood"],
            "energy": ticked["energy"], "clean": ticked["clean"],
            "health": 0, "last_tick_at": ticked["last_tick_at"],
            "dead": True, "dead_at": ticked.get("dead_at"),
        }).eq("id", pet_id).execute()
        return json_error("pet died during tick", 400)

    new_pet = dict(ticked)
    for k, v in cfg["effects"].items():
        new_pet[k] = max(0, min(100, (new_pet.get(k) or 0) + v))
    new_pet["xp"] = (ticked.get("xp") or 0) + cfg["xp"]

    old_st = _stage_idx(min(30, (pet.get("xp") or 0) // 50 + 1))
    new_st = _stage_idx(min(30, new_pet["xp"] // 50 + 1))

    iso_now = datetime.now(timezone.utc).isoformat()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    new_score = score + cfg["score"]

    m_upd = {"score": new_score, last_key: iso_now}
    if mem.get("today_date") == today_iso:
        m_upd["today_score"] = (mem.get("today_score") or 0) + max(cfg["score"], 0)
    else:
        m_upd["today_score"] = max(cfg["score"], 0)
        m_upd["today_date"] = today_iso

    p_upd = {
        "hunger": new_pet["hunger"], "mood": new_pet["mood"],
        "energy": new_pet["energy"], "clean": new_pet["clean"],
        "health": new_pet["health"], "xp": new_pet["xp"],
        "last_tick_at": new_pet["last_tick_at"],
    }

    r1, r2 = await asyncio.gather(
        sb.table("pets").update(p_upd).eq("id", pet_id).execute(),
        sb.table("members").update(m_upd).eq("pet_id", pet_id).eq("user_id", user["id"]).execute(),
        return_exceptions=True,
    )
    pet_updated = (getattr(r1, "data", None) or [None])[0] if not isinstance(r1, Exception) else None
    mem_updated = (getattr(r2, "data", None) or [None])[0] if not isinstance(r2, Exception) else None
    if not pet_updated: pet_updated = {**pet, **p_upd}
    if not mem_updated: mem_updated = {**mem, **m_upd}

    asyncio.create_task(_log_event_bg(
        pet_id, user["id"], user.get("first_name") or "Гость", action
    ))

    return web.json_response({
        "ok": True, "pet": pet_updated, "me": mem_updated,
        "evolved": new_st > old_st,
    })


async def api_daily(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    if not pet_id: return json_error("pet_id required")
    mem = await one(sb.table("members").select("*").eq("pet_id", pet_id).eq("user_id", user["id"]))
    if not mem: return json_error("not a member", 403)
    last = mem.get("last_daily_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 24 * 3600:
                return json_error("already claimed", 429)
        except: pass
    streak = 1
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - last_dt).total_seconds() < 2 * 24 * 3600:
                streak = (mem.get("streak") or 0) + 1
        except: pass
    now = datetime.now(timezone.utc).isoformat()
    new_score = (mem.get("score") or 0) + DAILY_BONUS
    upd = {"score": new_score, "last_daily_at": now, "streak": streak}
    r = await sb.table("members").update(upd)\
          .eq("pet_id", pet_id).eq("user_id", user["id"]).execute()
    mem_updated = (getattr(r, "data", None) or [None])[0] if not isinstance(r, Exception) else None
    if not mem_updated: mem_updated = {**mem, **upd}
    return web.json_response({
        "ok": True, "score": new_score, "streak": streak,
        "bonus": DAILY_BONUS, "me": mem_updated,
    })


async def api_rename(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    name = (body.get("name") or "").strip()[:20]
    if not pet_id or not name: return json_error("invalid params")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    await sb.table("pets").update({"name": name}).eq("id", pet_id).execute()
    return web.json_response({"ok": True, "name": name})


async def api_skin(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    new_skin = body.get("skin")
    if not pet_id or new_skin not in SKINS: return json_error("invalid params")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    if pet.get("skin") == new_skin: return json_error("already this skin", 400)
    mem = await one(sb.table("members").select("*").eq("pet_id", pet_id).eq("user_id", user["id"]))
    if not mem: return json_error("not a member", 403)
    score = mem.get("score") or 0
    if score < SKIN_PRICE: return json_error(f"need {SKIN_PRICE} score", 400)
    new_score = score - SKIN_PRICE
    await asyncio.gather(
        sb.table("pets").update({"skin": new_skin}).eq("id", pet_id).execute(),
        sb.table("members").update({"score": new_score}).eq("pet_id", pet_id).eq("user_id", user["id"]).execute(),
    )
    return web.json_response({"ok": True, "skin": new_skin, "score": new_score})


async def api_revive(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    if not pet_id: return json_error("pet_id required")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    if not pet.get("dead"): return json_error("pet is alive", 400)
    mem = await one(sb.table("members").select("*").eq("pet_id", pet_id).eq("user_id", user["id"]))
    if not mem: return json_error("not a member", 403)
    score = mem.get("score") or 0
    if score < 100: return json_error("need 100 score", 400)
    now = datetime.now(timezone.utc).isoformat()
    new_score = score - 100
    await asyncio.gather(
        sb.table("pets").update({
            "dead": False, "dead_at": None,
            "health": 50, "hunger": 50, "mood": 50, "clean": 50, "energy": 50,
            "last_tick_at": now,
        }).eq("id", pet_id).execute(),
        sb.table("members").update({"score": new_score}).eq("pet_id", pet_id).eq("user_id", user["id"]).execute(),
    )
    return web.json_response({"ok": True})


async def api_create(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    existing_task = sb.table("pets").select("id").eq("owner_id", user["id"]).eq("dead", False).limit(1).execute()
    count_task = sb.table("pets").select("id").eq("dead", False).execute()
    r1, r2 = await asyncio.gather(existing_task, count_task, return_exceptions=True)
    existing = (getattr(r1, "data", None) or []) if not isinstance(r1, Exception) else []
    alive_count = len((getattr(r2, "data", None) or [])) if not isinstance(r2, Exception) else 0
    if existing: return json_error("already have pet", 400)
    if alive_count >= GLOBAL_PET_LIMIT: return json_error("limit reached", 400)

    pet = None
    for _ in range(5):
        code = gen_code()
        try:
            r = await sb.table("pets").insert({"owner_id": user["id"], "invite_code": code}).execute()
            if r.data: pet = r.data[0]; break
        except Exception as e:
            if "23505" not in str(e): break
    if not pet: return json_error("create failed", 500)
    await sb.table("members").insert({
        "pet_id": pet["id"], "user_id": user["id"],
        "first_name": user.get("first_name") or "Гость",
        "username": user.get("username"),
        "score": 0,
    }).execute()
    return web.json_response({"ok": True, "pet": pet})


async def api_leave(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    if not pet_id: return json_error("pet_id required")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] == user["id"]: return json_error("owner cannot leave", 400)
    await sb.table("members").delete().eq("pet_id", pet_id).eq("user_id", user["id"]).execute()
    return web.json_response({"ok": True})


async def api_delete(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    confirm_name = body.get("confirm_name")
    if not pet_id or not confirm_name: return json_error("invalid params")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    if confirm_name.strip() != pet["name"]: return json_error("name mismatch", 400)
    cleanup_tasks = [
        sb.table(t).delete().eq("pet_id", pet_id).execute()
        for t in ["events","members","invoices","cheques","payouts","platform_fees"]
    ]
    await asyncio.gather(*cleanup_tasks, return_exceptions=True)
    await sb.table("pets").delete().eq("id", pet_id).execute()
    return web.json_response({"ok": True})


# ============================================================
# АКСЕССУАРЫ — API
# ============================================================

async def api_acc_list(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    if not pet_id: return json_error("pet_id required")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    items = []
    for acc_id, acc in ACCESSORIES.items():
        items.append({
            "id": acc_id, "emoji": acc["emoji"], "slot": acc["slot"],
            "name": acc["name"], "price": ACCESSORY_PRICE_STARS,
        })
    return web.json_response({
        "ok": True,
        "items": items,
        "owned": pet.get("accessories_owned") or [],
        "equipped": pet.get("accessories_equipped") or {},
        "is_owner": pet["owner_id"] == user["id"],
    })


async def api_acc_buy(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    acc_id = body.get("accessory_id")
    if not pet_id or not acc_id: return json_error("invalid params")
    acc = ACCESSORIES.get(acc_id)
    if not acc: return json_error("unknown accessory", 404)
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)

    try:
        link = await bot.create_invoice_link(
            title=f"{acc['emoji']} {acc['name']}",
            description=f"Аксессуар для питомца «{pet['name']}»",
            payload=f"buy_acc|{pet_id}|{acc_id}",
            currency="XTR",
            prices=[LabeledPrice(label=acc["name"], amount=ACCESSORY_PRICE_STARS)],
        )
    except Exception as e:
        log.exception("create_invoice_link")
        return json_error(f"telegram: {e}", 500)
    return web.json_response({"ok": True, "invoice_url": link})


async def api_acc_equip(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    acc_id = body.get("accessory_id")
    if not pet_id or not acc_id: return json_error("invalid params")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    acc = ACCESSORIES.get(acc_id)
    if not acc: return json_error("unknown accessory", 404)
    owned = pet.get("accessories_owned") or []
    if acc_id not in owned: return json_error("not owned", 403)
    equipped = dict(pet.get("accessories_equipped") or {})
    equipped[acc["slot"]] = acc_id
    await sb.table("pets").update({"accessories_equipped": equipped}).eq("id", pet_id).execute()
    return web.json_response({"ok": True, "equipped": equipped})


async def api_acc_unequip(request: web.Request):
    user = get_user_from_request(request)
    if not user: return json_error("unauthorized", 401)
    try: body = await request.json()
    except: return json_error("bad json")
    pet_id = body.get("pet_id")
    slot = body.get("slot")
    if not pet_id or slot not in ("head", "eyes", "neck"):
        return json_error("invalid params")
    pet = await one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet: return json_error("not found", 404)
    if pet["owner_id"] != user["id"]: return json_error("not owner", 403)
    equipped = dict(pet.get("accessories_equipped") or {})
    equipped.pop(slot, None)
    await sb.table("pets").update({"accessories_equipped": equipped}).eq("id", pet_id).execute()
    return web.json_response({"ok": True, "equipped": equipped})


# ============================================================
# health + webhook + запуск
# ============================================================

async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "pet-bot"})


async def xrocket_webhook(request: web.Request):
    try: data = await request.json()
    except: return web.json_response({"ok": False}, status=400)
    iid = data.get("invoiceId") or data.get("id") or (data.get("payload") or {}).get("invoiceId")
    status = (data.get("status") or data.get("type") or "").lower()
    if not iid: return web.json_response({"ok": True})
    inv = await one(sb.table("invoices").select("*").eq("invoice_id", str(iid)))
    if not inv or inv["status"] == "paid": return web.json_response({"ok": True})
    if any(k in status for k in ("paid","success","completed","invoice_paid")):
        pet = await one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            paid_amount = float(inv["amount"])
            fee = round(paid_amount * PLATFORM_FEE_PCT, 6)
            net = round(paid_amount - fee, 6)
            nb = float(pet.get("bank_balance") or 0) + net
            await sb.table("pets").update({"bank_balance": nb}).eq("id", inv["pet_id"]).execute()
            await sb.table("invoices").update({"status": "paid"}).eq("invoice_id", str(iid)).execute()
            try:
                await sb.table("platform_fees").insert({
                    "pet_id": inv["pet_id"], "owner_id": inv["owner_id"],
                    "amount": fee, "currency": inv.get("currency", "USDT"),
                    "invoice_id": str(iid),
                }).execute()
            except Exception as e:
                log.warning("fee insert: %s", e)
    return web.json_response({"ok": True})


async def start_web_server():
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/xrocket", xrocket_webhook)

    app.router.add_post("/api/open",   api_open)
    app.router.add_post("/api/action", api_action)
    app.router.add_post("/api/daily",  api_daily)
    app.router.add_post("/api/rename", api_rename)
    app.router.add_post("/api/skin",   api_skin)
    app.router.add_post("/api/revive", api_revive)
    app.router.add_post("/api/create", api_create)
    app.router.add_post("/api/leave",  api_leave)
    app.router.add_post("/api/delete", api_delete)

    app.router.add_post("/api/acc/list",     api_acc_list)
    app.router.add_post("/api/acc/buy",      api_acc_buy)
    app.router.add_post("/api/acc/equip",    api_acc_equip)
    app.router.add_post("/api/acc/unequip",  api_acc_unequip)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Web server on :%s (API + CORS ready, optimized)", PORT)


async def main():
    global sb
    sb = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    log.info("✅ Async Supabase подключен")

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("🤖 @%s стартовал (admins: %s)", me.username, ADMIN_IDS)

    await start_web_server()
    asyncio.create_task(death_watch_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
