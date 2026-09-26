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


def pick_link(obj: dict) -> str | None:
    if not obj or not isinstance(obj, dict):
        return None
    links = obj.get("links") or {}
    if isinstance(links, dict):
        for key in ("telegramBotLink", "webLink", "telegramMiniAppLink", "link", "url"):
            v = links.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    for key in ("link", "url", "webLink", "telegramBotLink", "telegramMiniAppLink"):
        v = obj.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


# ============ xRocket helpers ============

async def xrocket_request(method: str, path: str, json: dict | None = None) -> dict:
    url = f"{XROCKET_API}{path}"
    headers = {
        "Authorization": f"Bearer {XROCKET_TOKEN}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, headers=headers, json=json, timeout=30) as resp:
            raw = await resp.text()
            try:
                data = await resp.json()
            except Exception:
                data = {"raw": raw}
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
    data = await xrocket_request("POST", "/api/v1/invoices", payload)
    log.info("xRocket invoice response: %s", data)
    return data


async def create_cheque(user_id: int, amount: float, currency: str, description: str) -> dict:
    payload = {
        "asset": currency,
        "amount": str(amount),
        "description": description,
        "targetType": "telegram_user_id",
        "target": str(user_id),
    }
    data = await xrocket_request("POST", "/api/v1/cheques", payload)
    log.info("xRocket cheque response: %s", data)
    return data


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

    # ---------- topup_<petId>_<amount> ----------
    if payload.startswith("topup_"):
        parts = payload[6:].split("_")
        pet_id = parts[0]
        try:
            amount = float(parts[1]) if len(parts) > 1 else 1.0
        except ValueError:
            amount = 1.0
        if amount <= 0 or amount > 100:
            await message.answer("Некорректная сумма.")
            return

        pet_res = sb.table("pets").select("*").eq("id", pet_id).maybe_single().execute()
        pet = pet_res.data if pet_res else None
        if not pet:
            await message.answer("Питомец не найден.")
            return
        if pet["owner_id"] != user.id:
            await message.answer("Только владелец может пополнять банк.")
            return

        await message.answer(f"💳 Создаю счёт на {amount} {pet.get('currency','USDT')}…")
        try:
            invoice = await create_invoice(
                amount=amount,
                currency=pet.get("currency", "USDT"),
                description=f"Пополнение банка питомца «{pet['name']}»"
            )
        except Exception as e:
            log.error("invoice failed: %s", e)
            await message.answer(f"Не удалось создать счёт.\n{e}")
            return

        link = pick_link(invoice)
        if not link:
            await message.answer(
                "⚠️ xRocket вернул инвойс без ссылки.\n\n"
                "Ответ API:\n<code>" + str(invoice)[:800] + "</code>",
                parse_mode="HTML"
            )
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} {pet.get('currency','USDT')}", url=link)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"topup_confirm_{pet_id}_{amount}")],
        ])
        await message.answer(
            f"💳 Счёт на {amount} {pet.get('currency','USDT')} для банка питомца.\n"
            f"Оплати и нажми «Я оплатил».",
            reply_markup=kb
        )
        return

    # ---------- salary_<petId>_<amount>_<topN> ----------
    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3:
            await message.answer("Неверный формат команды зарплаты.")
            return
        pet_id = parts[0]
        try:
            amount = float(parts[1])
            top_n = int(parts[2])
        except ValueError:
            await message.answer("Неверные параметры зарплаты.")
            return
        await run_salary_pet(pet_id, user.id, amount, top_n, message)
        return

    # ---------- обычный /start ----------
    await message.answer(
        "👋 Это бот общего питомца.\n\nСоздай питомца — получишь ссылку для друзей.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")],
        ])
    )


@dp.message(Command("salary"))
async def cmd_salary(message: Message):
    """Команда /salary — по умолчанию раздать весь банк между всеми активными."""
    pet_res = sb.table("pets").select("*").eq("owner_id", message.from_user.id).maybe_single().execute()
    pet = pet_res.data if pet_res else None
    if not pet:
        await message.answer("У тебя нет питомца, где ты владелец.")
        return
    await run_salary_pet(pet["id"], message.from_user.id, None, None, message)


async def run_salary_pet(pet_id: str, owner_id: int, amount: float | None, top_n: int | None, message: Message):
    """Основная логика выплаты. amount — сколько раздать. top_n — скольким лучшим."""
    pet_res = sb.table("pets").select("*").eq("id", pet_id).maybe_single().execute()
    pet = pet_res.data if pet_res else None
    if not pet:
        await message.answer("Питомец не найден.")
        return
    if pet["owner_id"] != owner_id:
        await message.answer("Только владелец может запускать зарплату.")
        return

    balance = float(pet.get("bank_balance") or 0)
    if balance <= 0:
        await message.answer("Банк пуст.")
        return

    # Определяем сумму к раздаче
    if amount is None or amount <= 0:
        amount = balance
    if amount > balance:
        await message.answer(f"В банке только {balance:.2f}. Уменьши сумму.")
        return

    # Получаем участников
    today = date.today().isoformat()
    members_res = sb.table("members").select("*").eq("pet_id", pet["id"]).execute()
    members = members_res.data or []

    # Сбрасываем дневной счёт у тех, кто не заходил сегодня
    for m in members:
        if m.get("today_date") != today:
            sb.table("members").update({
                "today_score": 0, "today_date": today
            }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
            m["today_score"] = 0

    # Оставляем только активных
    active = [m for m in members if (m.get("today_score") or 0) > 0]
    if not active:
        await message.answer("Сегодня никто не был активен — распределять нечего.")
        return

    # Сортируем по очкам и берём топ-N
    active.sort(key=lambda m: m["today_score"], reverse=True)
    if top_n and top_n > 0 and top_n < len(active):
        winners = active[:top_n]
    else:
        winners = active

    total_score = sum(m["today_score"] for m in winners)
    if total_score <= 0:
        await message.answer("Нет очков для распределения.")
        return

    # Запись о выплате
    payout_res = sb.table("payouts").insert({
        "pet_id": pet["id"],
        "owner_id": owner_id,
        "total_amount": amount,
        "currency": pet.get("currency", "USDT"),
        "member_count": len(winners),
    }).execute()
    payout = payout_res.data[0]

    await message.answer(
        f"💸 Раздаю {amount:.2f} {pet.get('currency','USDT')} "
        f"между топ-{len(winners)} участниками…"
    )

    sent = 0
    failed = 0
    for m in winners:
        score = m["today_score"]
        share = round(amount * (score / total_score), 6)
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
            link = pick_link(cheque)

            sb.table("cheques").insert({
                "payout_id": payout["id"],
                "pet_id": pet["id"],
                "user_id": m["user_id"],
                "amount": share,
                "cheque_id": cheque_id,
                "cheque_link": link,
                "status": "sent" if link else "no_link",
            }).execute()

            if not link:
                log.warning("cheque without link: %s", cheque)
                continue

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

    # Списываем только разданное
    new_balance = round(balance - amount, 6)
    sb.table("pets").update({"bank_balance": new_balance}).eq("id", pet["id"]).execute()

    # Обнуляем дневной счёт только у победителей
    for m in winners:
        sb.table("members").update({
            "today_score": 0, "today_date": today
        }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()

    await message.answer(
        f"✅ Выплата завершена.\n"
        f"Раздано: {amount:.2f}\n"
        f"Получателей: {len(winners)}\n"
        f"Чеков отправлено: {sent}\n"
        f"Ошибок: {failed}\n"
        f"Остаток в банке: {new_balance:.2f} USDT"
    )


# ============ topup_confirm_<petId>_<amount> ============

@dp.callback_query(F.data.startswith("topup_confirm_"))
async def cb_topup_confirm(call: CallbackQuery):
    parts = call.data[14:].split("_")
    pet_id = parts[0]
    try:
        amount = float(parts[1]) if len(parts) > 1 else 1.0
    except ValueError:
        amount = 1.0

    user = call.from_user
    pet_res = sb.table("pets").select("*").eq("id", pet_id).maybe_single().execute()
    pet = pet_res.data if pet_res else None
    if not pet or pet["owner_id"] != user.id:
        await call.answer("Нет доступа", show_alert=True)
        return

    new_balance = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": new_balance}).eq("id", pet_id).execute()

    await call.answer(f"Банк +{amount}")
    try:
        await call.message.edit_text(
            f"✅ Банк пополнен на {amount:.2f}.\nБаланс: {new_balance:.2f} USDT"
        )
    except Exception:
        pass


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


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🐾 <b>Как играть</b>\n\n"
        "1. Создай питомца\n2. Кинь ссылку друзьям\n3. Вместе кормите — он растёт\n\n"
        "<b>Для владельца:</b>\n"
        "• Пополнить банк — в приложении (любая сумма)\n"
        "• Зарплата — в приложении, с выбором суммы и топ-N",
        parse_mode="HTML"
    )


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot @%s started", me.username)
    await dp.start_polling(me and bot)


if __name__ == "__main__":
    asyncio.run(main())
