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

# Минимум на один чек (у xRocket ограничение)
MIN_CHEQUE = 0.01

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()
sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def gen_code(length: int = 6) -> str:
    return "".join(random.choice(CODE_CHARS) for _ in range(length))


def webapp_url(pet_id: str) -> str:
    return f"{WEBAPP_URL}?pet={pet_id}"


def pick_link(obj: dict) -> str | None:
    if not obj or not isinstance(obj, dict):
        return None
    links = obj.get("links") or {}
    if isinstance(links, dict):
        for key in ("telegramMiniAppLink", "telegramBotLink", "webLink", "link", "url"):
            v = links.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    for key in ("link", "url", "webLink", "telegramBotLink", "telegramMiniAppLink"):
        v = obj.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def one(query):
    """Безопасная замена .maybe_single()."""
    try:
        res = query.limit(1).execute()
        data = getattr(res, "data", None) or []
        if isinstance(data, list):
            return data[0] if data else None
        return data if data else None
    except Exception as e:
        log.warning("one() query failed: %s", e)
        return None


def many(query):
    """Безопасный список."""
    try:
        res = query.execute()
        return getattr(res, "data", None) or []
    except Exception as e:
        log.warning("many() query failed: %s", e)
        return []


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
                data = await resp.json() if raw else {}
            except Exception:
                data = {"raw": raw}
            if resp.status >= 400:
                log.error("xRocket error %s: %s", resp.status, data)
                err = RuntimeError(f"xRocket {resp.status}: {data}")
                err.status = resp.status
                err.data = data
                raise err
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


async def get_invoice_status(invoice_id: str) -> dict:
    return await xrocket_request("GET", f"/api/v1/invoices/{invoice_id}")


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


async def delete_cheque(cheque_id: str) -> dict:
    """Отменить неактивированный чек. Вернёт зарезервированные деньги на баланс приложения."""
    # У xRocket пробуем два варианта — если один 404, используем другой.
    try:
        return await xrocket_request("DELETE", f"/api/v1/cheques/{cheque_id}")
    except Exception as e:
        status = getattr(e, "status", None)
        if status == 404 or status == 405:
            # альтернативный путь
            return await xrocket_request("DELETE", f"/api/v1/cheques?chequeId={cheque_id}")
        raise


async def get_cheque_info(cheque_id: str) -> dict:
    """Получить статус чека из xRocket."""
    return await xrocket_request("GET", f"/api/v1/cheques/{cheque_id}")


def xrocket_error_text(e: Exception) -> str:
    data = getattr(e, "data", None) or {}
    kind = data.get("kind") or ""
    title = data.get("title") or ""
    detail = data.get("detail") or str(e)

    if "amount_more_than_app_balance" in str(data) or "more than app balance" in detail.lower():
        return (
            "⚠️ На балансе приложения xRocket недостаточно средств.\n\n"
            "Пополни приложение: открой @xRocket → Wallet → пополни USDT, "
            "или переведи с основного баланса на баланс приложения (App 302777)."
        )
    if "operation_disabled" in kind or "disabled" in detail.lower():
        return (
            "⚠️ xRocket отключил эту операцию для твоего приложения.\n\n"
            "Проверь Permissions / Operations в @xRocket → xRocket API → 302777."
        )
    if "forbidden" in kind:
        return f"⚠️ xRocket запретил операцию: {detail}"
    return f"⚠️ Ошибка xRocket: {title or detail}"


# ============ /start ============

@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()
    user = message.from_user
    log.info("cmd_start payload=%r user=%s", payload, user.id)

    if payload.startswith("join_"):
        code = payload[5:]
        pet = one(sb.table("pets").select("*").eq("invite_code", code))
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

    if payload.startswith("topup_"):
        parts = payload[6:].split("_")
        pet_id = parts[0]
        try:
            cents = int(parts[1]) if len(parts) > 1 else 100
        except ValueError:
            cents = 100
        amount = cents / 100.0
        if amount <= 0 or amount > 1000:
            await message.answer("Некорректная сумма.")
            return

        pet = one(sb.table("pets").select("*").eq("id", pet_id))
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
            await message.answer(xrocket_error_text(e))
            return

        link = pick_link(invoice)
        invoice_id = invoice.get("id") or invoice.get("invoiceId")
        if not link or not invoice_id:
            await message.answer(
                "⚠️ xRocket вернул инвойс без ссылки или ID.\n\n"
                "Ответ API:\n<code>" + str(invoice)[:800] + "</code>",
                parse_mode="HTML"
            )
            return

        sb.table("invoices").insert({
            "invoice_id": str(invoice_id),
            "pet_id": pet_id,
            "owner_id": user.id,
            "amount": amount,
            "currency": pet.get("currency", "USDT"),
            "status": "pending",
        }).execute()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} {pet.get('currency','USDT')}", url=link)],
            [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{invoice_id}")],
        ])
        await message.answer(
            f"💳 Счёт на {amount} {pet.get('currency','USDT')} для банка питомца.\n\n"
            f"Оплати по кнопке выше. После оплаты нажми «Проверить оплату».",
            reply_markup=kb
        )
        return

    if payload.startswith("salary_"):
        parts = payload[7:].split("_")
        if len(parts) < 3:
            await message.answer("Неверный формат команды зарплаты.")
            return
        pet_id = parts[0]
        try:
            cents = int(parts[1])
            top_n = int(parts[2])
        except ValueError:
            await message.answer("Неверные параметры зарплаты.")
            return
        amount = cents / 100.0
        await run_salary_pet(pet_id, user.id, amount, top_n, message)
        return

    help_lines = [
        "👋 Это бот общего питомца.",
        "",
        "Создай питомца — получишь ссылку для друзей.",
        "",
        "<b>Для владельца:</b>",
        "/balance — баланс банка",
        "/topup 1 — пополнить банк на 1 USDT",
        "/salary 1 3 — раздать 1 USDT топ-3",
        "/salary — раздать весь банк всем активным",
    ]
    if is_admin(user.id):
        help_lines += [
            "",
            "<b>🛠 Админ:</b>",
            "/admin — справка по админке",
            "/setbal 5 — установить баланс (себе)",
            "/addbal 1 — прибавить к балансу (себе)",
            "/setbal_pet &lt;pet_id&gt; 5 — установить баланс питомцу",
            "/list_pets — список всех питомцев",
            "/reset_scores — обнулить дневные очки",
            "/cancel_cheques — отменить все висящие чеки и вернуть деньги",
        ]

    await message.answer(
        "\n".join(help_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
            [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")],
        ])
    )


# ============ проверка оплаты ============

@dp.callback_query(F.data.startswith("check_"))
async def cb_check_payment(call: CallbackQuery):
    invoice_id = call.data[6:]
    user = call.from_user

    inv = one(sb.table("invoices").select("*").eq("invoice_id", invoice_id))
    if not inv:
        await call.answer("Инвойс не найден", show_alert=True)
        return
    if inv["owner_id"] != user.id:
        await call.answer("Это не твой счёт", show_alert=True)
        return
    if inv["status"] == "paid":
        await call.answer("Уже оплачен ✅", show_alert=True)
        return

    try:
        status_data = await get_invoice_status(invoice_id)
    except Exception as e:
        data = getattr(e, "data", None) or {}
        status_code = getattr(e, "status", "?")
        detail = data.get("detail") or data.get("title") or str(e)
        log.error("check invoice failed: %s", e)
        await call.answer(f"xRocket {status_code}: {detail[:180]}", show_alert=True)
        return

    log.info("invoice status: %s", status_data)

    status = (
        status_data.get("status")
        or status_data.get("state")
        or (status_data.get("invoice") or {}).get("status")
        or ""
    ).lower()

    if status in ("paid", "success", "completed", "paid_success"):
        pet = one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            new_balance = float(pet.get("bank_balance") or 0) + float(inv["amount"])
            sb.table("pets").update({"bank_balance": new_balance}).eq("id", inv["pet_id"]).execute()
            sb.table("invoices").update({"status": "paid"}).eq("invoice_id", invoice_id).execute()
            await call.answer("Оплачено! ✅", show_alert=True)
            try:
                await call.message.edit_text(
                    f"✅ Банк пополнен на {inv['amount']}.\nБаланс: {new_balance:.4f} USDT"
                )
            except Exception:
                pass
            return

    await call.answer(
        f"Статус: {status or 'неизвестно'}\n{str(status_data)[:200]}",
        show_alert=True
    )


# ============ команды владельца ============

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    pet = one(sb.table("pets").select("*").eq("owner_id", message.from_user.id))
    if not pet:
        await message.answer("У тебя нет питомца, где ты владелец.")
        return
    await message.answer(
        f"🏦 Банк питомца «{pet['name']}»: <b>{float(pet.get('bank_balance') or 0):.4f} "
        f"{pet.get('currency','USDT')}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("topup"))
async def cmd_topup(message: Message):
    user = message.from_user
    pet = one(sb.table("pets").select("*").eq("owner_id", user.id))
    if not pet:
        await message.answer("У тебя нет питомца, где ты владелец.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /topup 1")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Не могу разобрать сумму. Пример: /topup 1")
        return
    if amount <= 0 or amount > 1000:
        await message.answer("Сумма должна быть от 0.01 до 1000 USDT")
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
        await message.answer(xrocket_error_text(e))
        return

    link = pick_link(invoice)
    invoice_id = invoice.get("id") or invoice.get("invoiceId")
    if not link or not invoice_id:
        await message.answer("⚠️ xRocket вернул инвойс без ссылки:\n<code>" + str(invoice)[:800] + "</code>", parse_mode="HTML")
        return

    sb.table("invoices").insert({
        "invoice_id": str(invoice_id),
        "pet_id": pet["id"],
        "owner_id": user.id,
        "amount": amount,
        "currency": pet.get("currency", "USDT"),
        "status": "pending",
    }).execute()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount} USDT", url=link)],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"check_{invoice_id}")],
    ])
    await message.answer(
        f"💳 Счёт на {amount} USDT. После оплаты нажми «Проверить оплату».",
        reply_markup=kb
    )


@dp.message(Command("salary"))
async def cmd_salary(message: Message):
    user = message.from_user
    pet = one(sb.table("pets").select("*").eq("owner_id", user.id))
    if not pet:
        await message.answer("У тебя нет питомца, где ты владелец.")
        return

    parts = message.text.split()
    amount = None
    top_n = None
    if len(parts) >= 2:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            await message.answer("Не могу разобрать сумму. Пример: /salary 1 3")
            return
    if len(parts) >= 3:
        try:
            top_n = int(parts[2])
        except ValueError:
            await message.answer("Не могу разобрать топ-N.")
            return

    await run_salary_pet(pet["id"], user.id, amount, top_n, message)


async def run_salary_pet(pet_id: str, owner_id: int, amount: float | None, top_n: int | None, message: Message):
    pet = one(sb.table("pets").select("*").eq("id", pet_id))
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

    if amount is None or amount <= 0:
        amount = balance
    if amount > balance:
        await message.answer(f"В банке только {balance:.4f}. Уменьши сумму.")
        return

    today = date.today().isoformat()
    members = many(sb.table("members").select("*").eq("pet_id", pet["id"]))

    for m in members:
        if m.get("today_date") != today:
            sb.table("members").update({
                "today_score": 0, "today_date": today
            }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()
            m["today_score"] = 0

    active = [m for m in members if (m.get("today_score") or 0) > 0]
    if not active:
        await message.answer("Сегодня никто не был активен — распределять нечего.")
        return

    active.sort(key=lambda m: m["today_score"], reverse=True)
    if top_n and top_n > 0 and top_n < len(active):
        winners = active[:top_n]
    else:
        winners = active

    total_score = sum(m["today_score"] for m in winners)
    if total_score <= 0:
        await message.answer("Нет очков для распределения.")
        return

    min_share = min(
        round(amount * (m["today_score"] / total_score), 6)
        for m in winners
    )
    if min_share < MIN_CHEQUE:
        await message.answer(
            f"❌ Слишком мелкие чеки.\n\n"
            f"Сумма: {amount:.4f}, получателей: {len(winners)}, "
            f"минимальная доля: {min_share:.4f} USDT.\n\n"
            f"Минимум на один чек: {MIN_CHEQUE} USDT.\n"
            f"Уменьши число получателей или увеличь сумму."
        )
        return

    sb.table("pets").update({"bank_balance": balance - amount}).eq("id", pet["id"]).execute()

    payout_res = sb.table("payouts").insert({
        "pet_id": pet["id"],
        "owner_id": owner_id,
        "total_amount": amount,
        "currency": pet.get("currency", "USDT"),
        "member_count": len(winners),
    }).execute()
    payout = payout_res.data[0]

    await message.answer(
        f"💸 Раздаю {amount:.4f} {pet.get('currency','USDT')} "
        f"между топ-{len(winners)}…"
    )

    sent = 0
    total_sent_amount = 0.0
    paid_user_ids = []
    failed_msg = ""

    for i, m in enumerate(winners):
        score = m["today_score"]
        share = round(amount * (score / total_score), 6)
        if share < MIN_CHEQUE:
            continue

        try:
            cheque = await create_cheque(
                user_id=m["user_id"],
                amount=share,
                currency=pet.get("currency", "USDT"),
                description=f"Зарплата за активность ({score} очков)"
            )
        except Exception as e:
            log.error("cheque #%s for %s failed: %s", i + 1, m["user_id"], e)
            data = getattr(e, "data", None) or {}
            failed_msg = data.get("detail") or data.get("title") or str(e)
            break

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

        if link:
            try:
                await bot.send_message(
                    m["user_id"],
                    f"💰 <b>Зарплата за питомца «{pet['name']}»!</b>\n\n"
                    f"Твоя активность: {score} очков\n"
                    f"Начислено: <b>{share:.4f} {pet.get('currency','USDT')}</b>\n\n"
                    f"Забрать: {link}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.warning("send to %s failed: %s", m["user_id"], e)

        sent += 1
        total_sent_amount += share
        paid_user_ids.append(m["user_id"])

    refund = round(amount - total_sent_amount, 6)
    if refund > 0:
        pet_now = one(sb.table("pets").select("*").eq("id", pet["id"]))
        current_bank = float(pet_now.get("bank_balance") or 0)
        sb.table("pets").update({"bank_balance": current_bank + refund}).eq("id", pet["id"]).execute()

    for m in winners:
        if m["user_id"] in paid_user_ids:
            sb.table("members").update({
                "today_score": 0, "today_date": today
            }).eq("pet_id", pet["id"]).eq("user_id", m["user_id"]).execute()

    pet_now = one(sb.table("pets").select("*").eq("id", pet["id"]))
    final_bank = float(pet_now.get("bank_balance") or 0)

    text = (
        f"✅ Выплата завершена.\n"
        f"Выплачено: {total_sent_amount:.4f} из {amount:.4f}\n"
        f"Чеков создано: {sent} из {len(winners)}\n"
        f"Остаток в банке: {final_bank:.4f} USDT"
    )
    if failed_msg:
        text += f"\n\n⚠️ Прервано на чеке #{sent + 1}: {failed_msg}\nНевыплаченное вернулось в банк."
    await message.answer(text)


# ============ АДМИНКА ============

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/setbal 5 — установить баланс (своему питомцу)\n"
        "/addbal 1 — прибавить к балансу\n"
        "/setbal_pet &lt;pet_id&gt; 5 — установить баланс питомцу по ID\n"
        "/addbal_pet &lt;pet_id&gt; 1 — прибавить питомцу по ID\n"
        "/list_pets — все питомцы с балансами\n"
        "/reset_scores — обнулить today_score у всех\n"
        "/cancel_cheques — отменить все висящие чеки и вернуть деньги\n"
        "/cheques — список висящих чеков",
        parse_mode="HTML"
    )


@dp.message(Command("setbal"))
async def cmd_setbal(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /setbal 5")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Не могу разобрать сумму.")
        return
    pet = one(sb.table("pets").select("*").eq("owner_id", message.from_user.id))
    if not pet:
        await message.answer("У тебя нет питомца.")
        return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pet["id"]).execute()
    await message.answer(f"✅ Баланс питомца «{pet['name']}»: {amount:.4f} USDT")


@dp.message(Command("addbal"))
async def cmd_addbal(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /addbal 1")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Не могу разобрать сумму.")
        return
    pet = one(sb.table("pets").select("*").eq("owner_id", message.from_user.id))
    if not pet:
        await message.answer("У тебя нет питомца.")
        return
    new_balance = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": new_balance}).eq("id", pet["id"]).execute()
    await message.answer(f"✅ Баланс питомца «{pet['name']}»: {new_balance:.4f} USDT")


@dp.message(Command("setbal_pet"))
async def cmd_setbal_pet(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /setbal_pet <pet_id> 5")
        return
    pet_id = parts[1].strip()
    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("Не могу разобрать сумму.")
        return
    if len(pet_id) < 30:
        await message.answer("Похоже, это не UUID. Возьми ID из /list_pets.")
        return
    pet = one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet:
        await message.answer("Питомец с таким ID не найден.")
        return
    sb.table("pets").update({"bank_balance": amount}).eq("id", pet_id).execute()
    await message.answer(f"✅ «{pet['name']}»: {amount:.4f} USDT")


@dp.message(Command("addbal_pet"))
async def cmd_addbal_pet(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /addbal_pet <pet_id> 1")
        return
    pet_id = parts[1].strip()
    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("Не могу разобрать сумму.")
        return
    if len(pet_id) < 30:
        await message.answer("Похоже, это не UUID. Возьми ID из /list_pets.")
        return
    pet = one(sb.table("pets").select("*").eq("id", pet_id))
    if not pet:
        await message.answer("Питомец с таким ID не найден.")
        return
    new_balance = float(pet.get("bank_balance") or 0) + amount
    sb.table("pets").update({"bank_balance": new_balance}).eq("id", pet_id).execute()
    await message.answer(f"✅ «{pet['name']}»: {new_balance:.4f} USDT")


@dp.message(Command("list_pets"))
async def cmd_list_pets(message: Message):
    if not is_admin(message.from_user.id):
        return
    pets = many(sb.table("pets").select("id,name,owner_id,bank_balance,xp"))
    if not pets:
        await message.answer("Питомцев нет.")
        return
    lines = ["🐾 <b>Все питомцы:</b>\n"]
    for p in pets[:30]:
        lines.append(
            f"• <code>{p['id']}</code>\n"
            f"  {p['name']} — {float(p.get('bank_balance') or 0):.4f} USDT "
            f"(xp {p.get('xp') or 0})"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("reset_scores"))
async def cmd_reset_scores(message: Message):
    if not is_admin(message.from_user.id):
        return
    today = date.today().isoformat()
    sb.table("members").update({"today_score": 0, "today_date": today}).neq("user_id", 0).execute()
    await message.answer("✅ Дневные очки обнулены у всех.")


@dp.message(Command("cheques"))
async def cmd_cheques(message: Message):
    """Список активных чеков."""
    if not is_admin(message.from_user.id):
        return
    pet = one(sb.table("pets").select("*").eq("owner_id", message.from_user.id))
    if not pet:
        await message.answer("У тебя нет питомца.")
        return

    cheques = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status", "sent"))
    if not cheques:
        await message.answer("Активных чеков нет.")
        return

    lines = ["🧾 <b>Активные чеки:</b>\n"]
    for c in cheques[:20]:
        lines.append(
            f"• <code>{c.get('cheque_id')}</code>\n"
            f"  {float(c.get('amount') or 0):.4f} USDT → user <code>{c['user_id']}</code>"
        )
    lines.append(f"\nВсего: {len(cheques)}")
    lines.append("Отменить все: /cancel_cheques")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("cancel_cheques"))
async def cmd_cancel_cheques(message: Message):
    """Отменяет все висящие чеки и возвращает деньги в банк питомца."""
    if not is_admin(message.from_user.id):
        return

    pet = one(sb.table("pets").select("*").eq("owner_id", message.from_user.id))
    if not pet:
        await message.answer("У тебя нет питомца.")
        return

    cheques = many(sb.table("cheques").select("*").eq("pet_id", pet["id"]).eq("status", "sent"))

    if not cheques:
        await message.answer("Нет активных чеков для отмены.")
        return

    await message.answer(f"🔍 Найдено {len(cheques)} активных чеков. Пробую отменить...")

    cancelled = 0
    failed = 0
    refund_amount = 0.0
    failed_ids = []

    for c in cheques:
        cheque_id = c.get("cheque_id")
        if not cheque_id:
            continue
        try:
            # Пробуем отменить чек через API xRocket
            await delete_cheque(cheque_id)
            sb.table("cheques").update({"status": "cancelled"}).eq("id", c["id"]).execute()
            cancelled += 1
            refund_amount += float(c.get("amount") or 0)
            log.info("Cancelled cheque %s", cheque_id)
        except Exception as e:
            log.error("Failed to cancel cheque %s: %s", cheque_id, e)
            failed += 1
            failed_ids.append(cheque_id)

    # Возвращаем зарезервированные деньги в банк питомца
    if refund_amount > 0:
        pet_now = one(sb.table("pets").select("*").eq("id", pet["id"]))
        current_bank = float(pet_now.get("bank_balance") or 0)
        sb.table("pets").update({
            "bank_balance": round(current_bank + refund_amount, 6)
        }).eq("id", pet["id"]).execute()

    text = (
        f"✅ Готово.\n"
        f"Отменено чеков: {cancelled}\n"
        f"Ошибок: {failed}\n"
        f"Возвращено в банк: {refund_amount:.4f} USDT"
    )
    if failed_ids:
        text += "\n\nНе удалось отменить:\n" + "\n".join(f"• <code>{i}</code>" for i in failed_ids[:10])
    await message.answer(text, parse_mode="HTML")


# ============ create / my_pets / help ============

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
    data = many(sb.table("members").select("pet_id, score, pets!inner(id, name, xp)").eq("user_id", user.id).order("score", desc=True).limit(20))
    if not data:
        await call.message.answer("У тебя пока нет питомцев.")
        return
    rows = []
    for m in data:
        p = m.get("pets") or {}
        if not p:
            continue
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
        "/balance — баланс банка\n"
        "/topup 1 — пополнить на 1 USDT\n"
        "/salary — раздать весь банк\n"
        "/salary 1 — раздать 1 USDT всем\n"
        "/salary 1 3 — раздать 1 USDT топ-3",
        parse_mode="HTML"
    )


# ============ вебхук + health ============

async def xrocket_webhook(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    log.info("xRocket webhook: %s", data)

    invoice_id = data.get("invoiceId") or data.get("id") or (data.get("payload") or {}).get("invoiceId")
    status = (data.get("status") or data.get("type") or "").lower()

    if not invoice_id:
        return web.json_response({"ok": True, "ignored": "no invoiceId"})

    inv = one(sb.table("invoices").select("*").eq("invoice_id", str(invoice_id)))
    if not inv or inv["status"] == "paid":
        return web.json_response({"ok": True})

    if any(k in status for k in ("paid", "success", "completed", "invoice_paid")):
        pet = one(sb.table("pets").select("*").eq("id", inv["pet_id"]))
        if pet:
            new_balance = float(pet.get("bank_balance") or 0) + float(inv["amount"])
            sb.table("pets").update({"bank_balance": new_balance}).eq("id", inv["pet_id"]).execute()
            sb.table("invoices").update({"status": "paid"}).eq("invoice_id", str(invoice_id)).execute()

            try:
                await bot.send_message(
                    inv["owner_id"],
                    f"✅ Оплата получена. Банк пополнен на {inv['amount']} USDT.\n"
                    f"Текущий баланс: {new_balance:.4f} USDT"
                )
            except Exception as e:
                log.warning("notify owner failed: %s", e)

    return web.json_response({"ok": True})


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "pet-bot"})


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
    log.info("Web server listening on port %s", PORT)


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot @%s started (admin ids: %s)", me.username, ADMIN_IDS)

    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
