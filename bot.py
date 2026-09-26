import os
import random
import asyncio
import logging
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from supabase import create_client, Client
import aiohttp

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pet-bot")

BOT_TOKEN     = os.environ["BOT_TOKEN"]
SUPABASE_URL  = os.environ["SUPABASE_URL"]
SUPABASE_KEY  = os.environ["SUPABASE_KEY"]
WEBAPP_URL    = os.environ["WEBAPP_URL"]
XROCKET_TOKEN = os.environ["XROCKET_TOKEN"]
XROCKET_API   = os.environ.get("XROCKET_API", "https://pay.api.xrocket.exchange")

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_code(length: int = 6) -> str:
    return "".join(random.choice(CODE_CHARS) for _ in range(length))


def webapp_url(pet_id: str) -> str:
    return f"{WEBAPP_URL}?pet={pet_id}"


# ============ xRocket helpers ============

async def xrocket_request(method: str, path: str, json: dict | None = None) -> dict:
    url = f"{XROCKET_API}{path}"
    headers = {
        "Authorization": f"Bearer {XROCKET_TOKEN}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json, timeout=30) as resp:
            data = await resp.json()
            if resp.status >= 400:
                log.error("xRocket error %s: %s", resp.status, data)
                raise RuntimeError(f"xRocket {resp.status}: {data}")
            return data


async def create_invoice(amount: float, currency: str, description: str) -> dict:
    payload = {
        "priceAmount": str(amount),
        "priceCurrency": currency,
        "description": description,
        "numPayments": 1,
        "expiresIn": 3600000,
    }
    return await xrocket_request("POST", "/api/v1/invoices", payload)


async def create_cheque(user_id: int, amount: float, currency: str, description: str) -> dict:
    payload = {
        "amount": str(amount),
        "asset": currency,
        "description": description,
        "targetType": "telegram_user_id",
        "target": str(user_id),
    }
    return await xrocket_request("POST", "/api/v1/cheques", payload)


# ============ /start ============

@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()
    user = message.from_user

    # ---------- join_ ----------
    if payload.startswith("join_"):
        code = payload[5:]
        res = sb.table("pets").select("*").eq("invite_code", code).maybe_single().execute()
        pet = res.data if res else None
        if not pet:
            await message.answer("Питомец не найден или ссылка устарела.")
            return
        sb.table("members").upsert({
            "pet_id": pet["id"], "user_id": user.id,
            "first_name": user.first_name or "Гость", "username": user.username,
        }, on_conflict="pet_id,user_id").execute()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐾 Открыть питомца",
                                  web_app=WebAppInfo(url=webapp_url(pet["id"])))]
        ])
        await message.answer(f"🐾 Ты ухаживаешь за «{pet['name']}»!", reply_markup=kb)
        return

    # ---------- topup_ ----------
    if payload.startswith("topup_"):
        pet_id = payload[6:]
        pet_res = sb.table("pets").select("*").eq("id", pet_id).maybe_single().execute()
        pet = pet_res.data if pet_res else None
        if not pet:
            await message.answer("Питомец не найден.")
            return
        if pet["owner_id"] != user.id:
            await message.answer("Только владелец может пополнять банк.")
            return

        await message.answer("💳 Создаю счёт…")
        try:
            invoice = await create_invoice(
                amount=1.0,
                currency=pet.get("currency", "USDT"),
                description=f"Пополнение банка питомца «{pet['name']}»"
            )
        except Exception as e:
            log.error("invoice failed: %s", e)
            await message.answer(f"Не удалось создать счёт.\n{e}")
            return

        link = (invoice.get("links") or {}).get("webLink") or invoice.get("link")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 1 USDT", url=link)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"topup_confirm_{pet_id}")],
        ])
        await message.answer(
            f"💳 Счёт на 1 {pet.get('currency','USDT')} для банка питомца.\n"
            f"Оплати и нажми «Я оплатил».",
            reply_markup=kb
        )
        return

    # ---------- salary ----------
    if payload == "salary":
        await run_salary(user.id, message)
        return

    # ---------- обычный /start ----------
    await message.answer(
        "👋 Это бот общего питомца.\n\nСоздай питомца — получишь ссылку для друзей.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")],
        ])
    )


# ============ /salary как команда ============

@dp.message(Command("salary"))
async def cmd_salary(message: Message):
    await run_salary(message.from_user.id, message)


# ============ Логика зарплаты ============

async def run_salary(owner_id: int, message: Message):
    pet_res = sb.table("pets").select("*").eq("owner_id", owner_id).maybe_single().execute()
    pet = pet_res.data if pet_res else None
    if not pet:
        await message.answer("У тебя нет питомца, где ты владелец.")
        return

    balance = float(pet.get("bank_balance") or 0)
    if balance <= 0:
        await message.answer("Банк пуст. Сначала пополни его в приложении.")
        return

    await message.answer(f"💸 Запускаю выплату на {balance:.2f} USDT…")

    today = date.today().isoformat()
    members_res = sb.table("members").select("*").eq("pet_id", pet["id"]).execute()
    members = members_res.data or []

    total_score = 0
    for m in members:
        if m.get("today_date") != today:
            sb.table("members").update({
                "today_score": 0, "today_date": today
            }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
            m["today_score"] = 0
        total_score += m.get("today_score") or 0

    if total_score == 0:
        await message.answer("Сегодня никто не был активен — распределять нечего.")
        return

    payout_res = sb.table("payouts").insert({
        "pet_id": pet["id"],
        "owner_id": owner_id,
        "total_amount": balance,
        "currency": pet.get("currency", "USDT"),
        "member_count": len([m for m in members if (m.get("today_score") or 0) > 0]),
    }).execute()
    payout = payout_res.data[0]

    sent = 0
    failed = 0
    for m in members:
        score = m.get("today_score") or 0
        if score <= 0:
            continue
        share = round(balance * (score / total_score), 6)
        if share < 0.01:
            continue

        try:
            cheque = await create_cheque(
                user_id=m["user_id"],
                amount=share,
                currency=pet.get("currency", "USDT"),
                description=f"Зарплата за активность ({score} очков)"
            )
            cheque_id = cheque.get("chequeId") or cheque.get("id")
            link = (cheque.get("links") or {}).get("telegramMiniAppLink") or cheque.get("link")

            sb.table("cheques").insert({
                "payout_id": payout["id"],
                "pet_id": pet["id"],
                "user_id": m["user_id"],
                "amount": share,
                "cheque_id": cheque_id,
                "cheque_link": link,
                "status": "sent",
            }).execute()

            try:
                await bot.send_message(
                    m["user_id"],
                    f"💰 <b>Зарплата за заботу о питомце «{pet['name']}»!</b>\n\n"
                    f"Твоя активность: {score} очков\n"
                    f"Начислено: <b>{share:.4f} {pet.get('currency','USDT')}</b>\n\n"
                    f"Забрать: {link}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception as e:
                log.warning("send cheque to %s failed: %s", m["user_id"], e)
                failed += 1

        except Exception as e:
            log.error("cheque for %s failed: %s", m["user_id"], e)
            failed += 1

    sb.table("pets").update({"bank_balance": 0}).eq("id", pet["id"]).execute()
    for m in members:
        sb.table("members").update({
            "today_score": 0, "today_date": today
        }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()

    await message.answer(
        f"✅ Выплата завершена.\n"
        f"Отправлено чеков: {sent}\n"
        f"Ошибок: {failed}\n"
        f"Банк обнулён."
    )


# ============ topup_confirm ============

@dp.callback_query(F.data.startswith("topup_confirm_"))
async def cb_topup_confirm(call: CallbackQuery):
    pet_id = call.data[15:]
    user = call.from_user
    pet_res = sb.table("pets").select("*").eq("id", pet_id).maybe_single().execute()
    pet = pet_res.data if pet_res else None
    if not pet or pet["owner_id"] != user.id:
        await call.answer("Нет доступа", show_alert=True)
        return

    new_balance = float(pet.get("bank_balance") or 0) + 1.0
    sb.table("pets").update({"bank_balance": new_balance}).eq("id", pet_id).execute()

    await call.answer("Банк пополнен!")
    await call.message.edit_text(f"✅ Банк пополнен. Баланс: {new_balance:.2f} USDT")


# ============ create ============

@dp.callback_query(F.data == "create")
async def cb_create(call: CallbackQuery):
    user = call.from_user
    pet = None
    for _ in range(5):
        code = gen_code()
        try:
            res = sb.table("pets").insert({"owner_id": user.id, "invite_code": code}).execute()
            if res.data:
                pet = res.data[0]
                break
        except Exception as e:
            if "23505" not in str(e):
                break
    if not pet:
        await call.answer("Ошибка, попробуй ещё", show_alert=True)
        return
    sb.table("members").insert({
        "pet_id": pet["id"], "user_id": user.id,
        "first_name": user.first_name or "Гость", "username": user.username,
    }).execute()
    await call.answer("Питомец создан!")
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=join_{pet['invite_code']}"
    share = f"https://t.me/share/url?url={link}&text=Ухаживай за нашим питомцем!"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Открыть питомца",
                              web_app=WebAppInfo(url=webapp_url(pet["id"])))],
        [InlineKeyboardButton(text="📤 Поделиться", url=share)],
    ])
    await call.message.answer(
        f"🐣 <b>Питомец создан!</b>\n\nСсылка:\n<code>{link}</code>",
        parse_mode="HTML", reply_markup=kb
    )


# ============ my_pets ============

@dp.callback_query(F.data == "my_pets")
async def cb_my_pets(call: CallbackQuery):
    user = call.from_user
    await call.answer()
    res = sb.table("members").select("pet_id, score, pets!inner(id, name, xp)").eq("user_id", user.id).order("score", desc=True).limit(20).execute()
    data = res.data or []
    if not data:
        await call.message.answer("У тебя пока нет питомцев.")
        return
    rows = []
    for m in data:
        p = m["pets"]
        lvl = min(30, (p.get("xp") or 0) // 50 + 1)
        rows.append([InlineKeyboardButton(
            text=f"🐾 {p['name']} · ур. {lvl}",
            web_app=WebAppInfo(url=webapp_url(p["id"]))
        )])
    await call.message.answer("Твои питомцы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# ============ help ============

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🐾 <b>Как играть</b>\n\n"
        "1. Создай питомца\n2. Кинь ссылку друзьям\n3. Вместе кормите — он растёт\n\n"
        "<b>Для владельца:</b>\n"
        "• Пополнить банк — в приложении\n"
        "• Выплатить зарплату — /salary",
        parse_mode="HTML"
    )


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot @%s started", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
