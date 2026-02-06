import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import aiofiles

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest, RetryAfter
from telegram.constants import ParseMode

# ────────────────────────────────────────────── НАСТРОЙКИ ──────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ТОКЕН БЕРЁТСЯ ИЗ ПЕРЕМЕННОЙ ОКРУЖЕНИЯ (Railway → Variables)
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("❌ Переменная окружения BOT_TOKEN не задана")

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

BIRTHDAYS = []
DUTIES_TEXT = ""
SCHEDULES = {}

chat_states = defaultdict(lambda: {
    "votes": {},
    "poll_message_id": None,
    "results_message_id": None,
    "last_save": 0.0,
    "dirty": False
})

file_write_lock = asyncio.Lock()

last_birthday_sent_date = None
last_pinned_birthday_msg_id = {}

# ────────────────────────────────────────────── ФАЙЛЫ СОСТОЯНИЯ ──────────────────────────────────────────────

def get_file(chat_id: int, chat_title: str) -> Path:
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', chat_title or f"chat_{chat_id}")[:40]
    return DATA_DIR / f"stolovaya_{safe}_{chat_id}.json"


async def save_state_periodically(chat_id: int, chat_title: str):
    now_ts = datetime.utcnow().timestamp()
    state = chat_states[chat_id]

    if not state["dirty"] or now_ts - state["last_save"] < 12:
        return

    async with file_write_lock:
        path = get_file(chat_id, chat_title)
        tmp = path.with_suffix(".tmp")

        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps({
                "date": date.today().isoformat(),
                "votes": state["votes"],
                "poll_message_id": state["poll_message_id"],
                "results_message_id": state["results_message_id"],
            }, ensure_ascii=False, separators=(",", ":")))

        tmp.replace(path)
        state["last_save"] = now_ts
        state["dirty"] = False


async def load_state_from_file(chat_id: int, chat_title: str):
    path = get_file(chat_id, chat_title)
    if not path.exists():
        return None

    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        return json.loads(await f.read())


# ────────────────────────────────────────────── ДНИ РОЖДЕНИЯ ──────────────────────────────────────────────

async def save_last_birthday_date(date_str: str):
    async with aiofiles.open(DATA_DIR / "last_birthday_sent.json", "w", encoding="utf-8") as f:
        await f.write(json.dumps({"date": date_str}))


async def load_last_birthday_date():
    global last_birthday_sent_date
    path = DATA_DIR / "last_birthday_sent.json"
    if not path.exists():
        return

    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        last_birthday_sent_date = json.loads(await f.read()).get("date")


def load_static_data():
    global BIRTHDAYS, DUTIES_TEXT, SCHEDULES

    with (DATA_DIR / "data_birthdays.json").open("r", encoding="utf-8") as f:
        BIRTHDAYS = json.load(f)

    with (DATA_DIR / "data_duties.json").open("r", encoding="utf-8") as f:
        DUTIES_TEXT = json.load(f)["text"]

    with (DATA_DIR / "data_schedules.json").open("r", encoding="utf-8") as f:
        SCHEDULES = json.load(f)


# ────────────────────────────────────────────── МЕНЮ ──────────────────────────────────────────────

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📅 Расписание", callback_data="menu_schedule")],
    [InlineKeyboardButton("🍽 Столовая", callback_data="menu_stolovaya")],
    [InlineKeyboardButton("🧹 Дежурства", callback_data="duties")],
    [InlineKeyboardButton("🎂 Дни рождения", callback_data="menu_birthdays")],
])

PROFILE_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📐 Математика (профиль)", callback_data="profile_math")],
    [InlineKeyboardButton("🧪 Химия (профиль)", callback_data="profile_chem")],
    [InlineKeyboardButton("📘 База", callback_data="profile_base")],
    [InlineKeyboardButton("↩️ Назад", callback_data="back_main")],
])


def days_menu(profile):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Пн", callback_data=f"day*{profile}*pn"),
            InlineKeyboardButton("Вт", callback_data=f"day*{profile}*vt"),
            InlineKeyboardButton("Ср", callback_data=f"day*{profile}*sr"),
        ],
        [
            InlineKeyboardButton("Чт", callback_data=f"day*{profile}*cht"),
            InlineKeyboardButton("Пт", callback_data=f"day*{profile}*pt"),
        ],
        [InlineKeyboardButton("↩️ Назад", callback_data="menu_schedule")],
    ])


STOL_MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Создать опрос", callback_data="stol_create_poll")],
    [InlineKeyboardButton("📈 Показать итоги", callback_data="stol_show_results")],
    [InlineKeyboardButton("↩️ Назад", callback_data="back_main")],
])

STOL_POLL_MARKUP = InlineKeyboardMarkup([
    [InlineKeyboardButton("🍽 Буду есть", callback_data="stol_eat")],
    [InlineKeyboardButton("🙅 Не буду есть", callback_data="stol_no_eat")],
    [InlineKeyboardButton("🏫 Не буду в школе", callback_data="stol_absent")],
])

# ────────────────────────────────────────────── ЛОГИКА ──────────────────────────────────────────────

def get_results_text(votes):
    def fmt(v):
        return f"{v['name']} (@{v['username']})" if v.get("username") else v["name"]

    eat = [fmt(v) for v in votes.values() if v["status"] == "eat"]
    no_eat = [fmt(v) for v in votes.values() if v["status"] == "no_eat"]
    absent = [fmt(v) for v in votes.values() if v["status"] == "absent"]

    return (
        f"Результаты опроса: {len(votes)} голосов\n\n"
        f"🍽 Будут есть ({len(eat)}):\n" + ("\n".join(eat) or "—") + "\n\n"
        f"🙅 Не будут есть ({len(no_eat)}):\n" + ("\n".join(no_eat) or "—") + "\n\n"
        f"🏫 Не придут ({len(absent)}):\n" + ("\n".join(absent) or "—")
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери раздел:", reply_markup=MAIN_MENU)


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    chat_id = q.message.chat.id
    chat_title = q.message.chat.title or str(chat_id)
    user = q.from_user
    uid = str(user.id)

    state = chat_states[chat_id]

    if not state["votes"]:
        loaded = await load_state_from_file(chat_id, chat_title)
        if loaded:
            state.update(loaded)

    data = q.data

    if data == "menu_stolovaya":
        await q.edit_message_text("Выбери действие:", reply_markup=STOL_MAIN_MENU)
        return

    if data == "stol_create_poll":
        state["votes"].clear()
        state["dirty"] = True

        poll = await context.bot.send_message(chat_id, "🍽 Опрос на завтра", reply_markup=STOL_POLL_MARKUP)
        state["poll_message_id"] = poll.message_id

        res = await context.bot.send_message(chat_id, get_results_text(state["votes"]))
        state["results_message_id"] = res.message_id
        return

    if data in ("stol_eat", "stol_no_eat", "stol_absent"):
        state["votes"][uid] = {
            "name": user.first_name or "Без имени",
            "username": user.username,
            "status": data.replace("stol_", "")
        }
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=state["results_message_id"],
            text=get_results_text(state["votes"])
        )
        return


# ────────────────────────────────────────────── ЗАПУСК ──────────────────────────────────────────────

async def main():
    load_static_data()
    await load_last_birthday_date()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
