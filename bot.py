import os
import random
import string
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo
)
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pet-bot")

BOT_TOKEN   = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
WEBAPP_URL   = os.environ["WEBAPP_URL"]

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def gen_code(length: int = 6) -> str:
    return "".join(random.choice(CODE_CHARS) for _ in range(length))


def webapp_url(pet_id: str) -> str:
    return f"{WEBAPP_URL}?pet={pet_id}"


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐣 Создать питомца", callback_data="create")],
        [InlineKeyboardButton(text="📋 Мои питомцы", callback_data="my_pets")],
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message):
    payload = ""
    if message.text and " " in message.text:
        payload = message.text.split(" ", 1)[1].strip()

    user = message.from_user

    if payload.startswith("join_"):
        code = payload[5:]

        res = sb.table("pets").select("*").eq("invite_code", code).maybe_single().execute()
        pet = res.data if res else None

        if not pet:
            await message.answer("Питомец не найден или ссылка устарела. Попроси владельца создать новую.")
            return

        sb.table("members").upsert({
            "pet_id": pet["id"],
            "user_id": user.id,
            "first_name": user.first_name or "Гость",
            "username": user.username,
        }, on_conflict="pet_id,user_id").execute()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🐾 Открыть питомца",
                web_app=WebAppInfo(url=webapp_url(pet["id"]))
            )]
        ])

        await message.answer(
            f"🐾 Ты теперь ухаживаешь за «{pet['name']}»!\n\n"
            f"Питомец общий — всё, что ты делаешь, видят другие участники.",
            reply_markup=kb
        )
        return

    await message.answer(
        "👋 Привет! Это бот общего питомца.\n\n"
        "Создай питомца — получишь ссылку. Кинь её друзьям, и они станут совладельцами.",
        reply_markup=main_menu_kb()
    )


@dp.callback_query(F.data == "create")
async def cb_create(call: CallbackQuery):
    user = call.from_user
    pet = None
    last_error = None

    for _ in range(5):
        code = gen_code()
        try:
            res = sb.table("pets").insert({
                "owner_id": user.id,
                "invite_code": code,
            }).execute()
            if res.data:
                pet = res.data[0]
                break
        except Exception as e:
            last_error = e
            # 23505 = уникальность invite_code нарушена, пробуем ещё
            if "23505" not in str(e):
                break

    if not pet:
        log.error("create pet failed: %s", last_error)
        await call.answer("Ошибка, попробуй ещё", show_alert=True)
        return

    sb.table("members").insert({
        "pet_id": pet["id"],
        "user_id": user.id,
        "first_name": user.first_name or "Гость",
        "username": user.username,
    }).execute()

    await call.answer("Питомец создан!")

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=join_{pet['invite_code']}"
    share_url = (
        "https://t.me/share/url"
        f"?url={link}"
        "&text=Ухаживай за нашим питомцем!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🐾 Открыть питомца",
            web_app=WebAppInfo(url=webapp_url(pet["id"]))
        )],
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
    ])

    await call.message.answer(
        f"🐣 <b>Питомец создан!</b>\n\n"
        f"Ссылка для друзей:\n<code>{link}</code>\n\n"
        f"Кто перейдёт — станет совладельцем. Питомец общий, действия видны всем сразу.",
        parse_mode="HTML",
        reply_markup=kb
    )


@dp.callback_query(F.data == "my_pets")
async def cb_my_pets(call: CallbackQuery):
    user = call.from_user
    await call.answer()

    try:
        res = (
            sb.table("members")
            .select("pet_id, score, pets!inner(id, name, xp)")
            .eq("user_id", user.id)
            .order("score", desc=True)
            .limit(20)
            .execute()
        )
        data = res.data or []
    except Exception as e:
        log.error("my_pets failed: %s", e)
        await call.message.answer("Ошибка загрузки.")
        return

    if not data:
        await call.message.answer("У тебя пока нет питомцев. Нажми /start → «Создать питомца».")
        return

    rows = []
    for m in data:
        pet = m["pets"]
        lvl = min(30, (pet.get("xp") or 0) // 50 + 1)
        rows.append([InlineKeyboardButton(
            text=f"🐾 {pet['name']} · ур. {lvl}",
            web_app=WebAppInfo(url=webapp_url(pet["id"]))
        )])

    await call.message.answer(
        "Твои питомцы:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🐾 <b>Как играть</b>\n\n"
        "1. Нажми /start и создай питомца\n"
        "2. Получи ссылку и кинь друзьям\n"
        "3. Вместе кормите, играйте, мойте — он растёт\n\n"
        "Каждый может действовать раз в N минут. Очки за действия — в топе.",
        parse_mode="HTML"
    )


async def main():
    me = await bot.get_me()
    log.info("Bot @%s started", me.username)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())