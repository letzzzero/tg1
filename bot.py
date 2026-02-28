import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

DB_PATH = os.getenv("DB_PATH", "dating_bot.db")

db_lock = asyncio.Lock()
router = Router()

GENDER_RU = {"male": "Парень", "female": "Девушка", "other": "Другое"}
TARGET_GENDER_RU = {"any": "Любой", "male": "Парень", "female": "Девушка", "other": "Другое"}

BTN_ONLINE = "🟢 Онлайн"
BTN_OFFLINE = "🔴 Оффлайн"
BTN_PROFILE = "👤 Профиль"
BTN_EDIT_PROFILE = "✏️ Изменить анкету"
BTN_FILTERS = "⚙️ Фильтры"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "❌ Отмена"

BTN_DISCONNECT = "⛔ Разорвать связь"
BTN_NEXT = "⏭ Следующий"
BTN_REPORT = "🚨 Жалоба + разрыв"

REG_GENDER_BTNS = {"👨 Парень": "male", "👩 Девушка": "female", "🧩 Другое": "other"}
TARGET_GENDER_BTNS = {"🙋 Любой": "any", "👨 Парень": "male", "👩 Девушка": "female", "🧩 Другое": "other"}


class RegistrationSG(StatesGroup):
    gender = State()
    age = State()
    city = State()
    bio = State()


class FiltersSG(StatesGroup):
    gender = State()
    age_min = State()
    age_max = State()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main_menu_kb(is_online: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OFFLINE if is_online else BTN_ONLINE), KeyboardButton(text=BTN_PROFILE)],
            [KeyboardButton(text=BTN_FILTERS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )


def chat_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DISCONNECT), KeyboardButton(text=BTN_NEXT)],
            [KeyboardButton(text=BTN_REPORT)],
        ],
        resize_keyboard=True,
    )


def gender_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t)] for t in REG_GENDER_BTNS.keys()] + [[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def target_gender_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t)] for t in TARGET_GENDER_BTNS.keys()] + [[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def report_reason_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Спам", callback_data="report:spam")],
            [InlineKeyboardButton(text="Оскорбления", callback_data="report:abuse")],
            [InlineKeyboardButton(text="Мошенничество", callback_data="report:scam")],
            [InlineKeyboardButton(text="18+ / контент", callback_data="report:adult")],
            [InlineKeyboardButton(text="Отмена", callback_data="report:cancel")],
        ]
    )


# ---------------- DB ----------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                gender TEXT,
                age INTEGER,
                city TEXT,
                bio TEXT,
                target_gender TEXT DEFAULT 'any',
                target_age_min INTEGER DEFAULT 18,
                target_age_max INTEGER DEFAULT 99,
                is_online INTEGER DEFAULT 0,
                in_chat_with INTEGER,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS queue (
                user_id INTEGER PRIMARY KEY,
                joined_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS blocks (
                user_id INTEGER NOT NULL,
                blocked_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, blocked_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                against_user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def db_execute(query: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()


async def db_fetchone(query: str, params: tuple = ()) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        row = await cur.fetchone()
        await cur.close()
        return dict(row) if row else None


async def ensure_user_row(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    existing = await db_fetchone("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
    if existing:
        await db_execute(
            "UPDATE users SET username=?, full_name=?, updated_at=? WHERE user_id=?",
            (user.username, user.full_name, utc_now(), user.id),
        )
    else:
        now = utc_now()
        await db_execute(
            """
            INSERT INTO users (user_id, username, full_name, created_at, updated_at, is_online)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (user.id, user.username, user.full_name, now, now),
        )


async def get_user(user_id: int) -> Optional[dict]:
    return await db_fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))


def profile_ready(u: Optional[dict]) -> bool:
    if not u:
        return False
    return bool(u.get("gender") and u.get("age") and u.get("city") and u.get("bio"))


def format_profile(u: dict) -> str:
    username = f"@{u['username']}" if u.get("username") else "без username"
    status = "в диалоге" if u.get("in_chat_with") else ("онлайн" if u.get("is_online") else "оффлайн")
    return (
        f"<b>Твоя анкета</b>\n"
        f"• Ник: {html.quote(username)}\n"
        f"• Пол: {GENDER_RU.get(u.get('gender') or '', 'Не указано')}\n"
        f"• Возраст: {u.get('age') or 'Не указан'}\n"
        f"• Город: {html.quote(u.get('city') or 'Не указан')}\n"
        f"• О себе: {html.quote(u.get('bio') or '—')}\n"
        f"• Ищу: {TARGET_GENDER_RU.get(u.get('target_gender') or 'any', 'Любой')}, "
        f"{u.get('target_age_min', 18)}–{u.get('target_age_max', 99)}\n"
        f"• Статус: {status}"
    )


def format_partner_profile(u: dict) -> str:
    username = f"@{u['username']}" if u.get("username") else "без username"
    return (
        "<b>Собеседник найден 🎉</b>\n"
        f"• Ник: {html.quote(username)}\n"
        f"• Пол: {GENDER_RU.get(u.get('gender') or '', 'Не указано')}\n"
        f"• Возраст: {u.get('age') or 'Не указан'}\n"
        f"• Город: {html.quote(u.get('city') or 'Не указан')}\n"
        f"• О себе: {html.quote(u.get('bio') or '—')}\n\n"
        "Можешь писать текст, фото, видео, голосовые, стикеры — бот пересылает их анонимно."
    )


# ---------------- Matching ----------------

async def add_to_queue(user_id: int) -> None:
    await db_execute(
        "INSERT INTO queue (user_id, joined_at) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET joined_at=excluded.joined_at",
        (user_id, utc_now()),
    )


async def remove_from_queue(user_id: int) -> None:
    await db_execute("DELETE FROM queue WHERE user_id = ?", (user_id,))


async def set_online_status(user_id: int, online: bool) -> None:
    await db_execute(
        "UPDATE users SET is_online=?, updated_at=? WHERE user_id=?",
        (1 if online else 0, utc_now(), user_id),
    )


def compatible(a: dict, b: dict) -> bool:
    if not profile_ready(a) or not profile_ready(b):
        return False

    a_tg = a.get("target_gender") or "any"
    b_tg = b.get("target_gender") or "any"

    if a_tg != "any" and b.get("gender") != a_tg:
        return False
    if b_tg != "any" and a.get("gender") != b_tg:
        return False

    a_age = a.get("age")
    b_age = b.get("age")
    if a_age is None or b_age is None:
        return False

    if not (int(a.get("target_age_min") or 18) <= int(b_age) <= int(a.get("target_age_max") or 99)):
        return False
    if not (int(b.get("target_age_min") or 18) <= int(a_age) <= int(b.get("target_age_max") or 99)):
        return False

    return True


async def try_match(user_id: int) -> Optional[int]:
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
            me = await cur.fetchone()
            await cur.close()
            if not me:
                return None
            me = dict(me)

            if not me.get("is_online") or me.get("in_chat_with") is not None:
                return None

            cur = await db.execute(
                """
                SELECT q.user_id
                FROM queue q
                JOIN users u ON u.user_id = q.user_id
                WHERE q.user_id != ?
                  AND u.is_online = 1
                  AND u.in_chat_with IS NULL
                ORDER BY q.joined_at ASC
                """,
                (user_id,),
            )
            candidate_ids = [r[0] for r in await cur.fetchall()]
            await cur.close()

            for cand_id in candidate_ids:
                # blocks
                c = await db.execute(
                    "SELECT 1 FROM blocks WHERE (user_id=? AND blocked_user_id=?) OR (user_id=? AND blocked_user_id=?)",
                    (user_id, cand_id, cand_id, user_id),
                )
                blocked = await c.fetchone()
                await c.close()
                if blocked:
                    continue

                c = await db.execute("SELECT * FROM users WHERE user_id=?", (cand_id,))
                cand = await c.fetchone()
                await c.close()
                if not cand:
                    continue
                cand = dict(cand)

                if not compatible(me, cand):
                    continue

                now = utc_now()
                await db.execute("UPDATE users SET in_chat_with=?, updated_at=? WHERE user_id=?", (cand_id, now, user_id))
                await db.execute("UPDATE users SET in_chat_with=?, updated_at=? WHERE user_id=?", (user_id, now, cand_id))
                await db.execute("DELETE FROM queue WHERE user_id IN (?, ?)", (user_id, cand_id))
                await db.commit()
                return cand_id

            return None


async def start_search(bot: Bot, user_id: int) -> tuple[bool, Optional[int], str]:
    u = await get_user(user_id)

    if not profile_ready(u):
        return False, None, "Сначала заполни анкету через /start или кнопку «Профиль»."

    if u.get("in_chat_with"):
        return False, None, "Ты уже в диалоге. Нажми «Разорвать связь» или «Следующий»."

    await set_online_status(user_id, True)
    await add_to_queue(user_id)
    partner_id = await try_match(user_id)

    if partner_id:
        me = await get_user(user_id)
        partner = await get_user(partner_id)
        if me and partner:
            await bot.send_message(user_id, format_partner_profile(partner), reply_markup=chat_menu_kb())
            await bot.send_message(partner_id, format_partner_profile(me), reply_markup=chat_menu_kb())
        return True, partner_id, "MATCHED"

    return True, None, "Ты в очереди. Ищу собеседника... 🔎"


# ---------------- Chat end logic (FIXED) ----------------

async def end_chat_for(user_id: int, *, offline_me: bool, offline_partner: bool) -> Optional[int]:
    """
    Разрывает связь user_id <-> partner_id.
    Может переключать в оффлайн отдельно пользователя и партнёра.
    Возвращает partner_id или None.
    """
    async with db_lock:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            cur = await db.execute("SELECT in_chat_with FROM users WHERE user_id=?", (user_id,))
            row = await cur.fetchone()
            await cur.close()

            if not row or row["in_chat_with"] is None:
                return None

            partner_id = int(row["in_chat_with"])
            now = utc_now()

            # разрываем связь
            await db.execute(
                "UPDATE users SET in_chat_with=NULL, updated_at=? WHERE user_id IN (?, ?)",
                (now, user_id, partner_id),
            )
            # удаляем из очереди (на всякий)
            await db.execute("DELETE FROM queue WHERE user_id IN (?, ?)", (user_id, partner_id))

            # оффлайн статусы
            if offline_me and offline_partner:
                await db.execute(
                    "UPDATE users SET is_online=0, updated_at=? WHERE user_id IN (?, ?)",
                    (now, user_id, partner_id),
                )
            elif offline_me:
                await db.execute("UPDATE users SET is_online=0, updated_at=? WHERE user_id=?", (now, user_id))
            elif offline_partner:
                await db.execute("UPDATE users SET is_online=0, updated_at=? WHERE user_id=?", (now, partner_id))

            await db.commit()
            return partner_id


async def go_offline(bot: Bot, user_id: int) -> None:
    """
    Кнопка 🔴 Оффлайн: выключаем поиск у себя.
    Если был чат — разрываем его, но партнёра НЕ заставляем уходить в оффлайн.
    """
    partner_id = await end_chat_for(user_id, offline_me=True, offline_partner=False)
    await remove_from_queue(user_id)
    await set_online_status(user_id, False)

    if partner_id:
        partner = await get_user(partner_id)
        if partner:
            try:
                await bot.send_message(
                    partner_id,
                    "Собеседник завершил чат. Нажми «Онлайн», чтобы найти нового.",
                    reply_markup=main_menu_kb(bool(partner.get("is_online"))),
                )
            except Exception:
                pass


async def save_report_and_block(from_user_id: int, against_user_id: int, reason: str) -> None:
    now = utc_now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reports (from_user_id, against_user_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (from_user_id, against_user_id, reason, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO blocks (user_id, blocked_user_id, created_at) VALUES (?, ?, ?)",
            (from_user_id, against_user_id, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO blocks (user_id, blocked_user_id, created_at) VALUES (?, ?, ?)",
            (against_user_id, from_user_id, now),
        )
        await db.commit()


async def show_profile(message: Message) -> None:
    u = await get_user(message.from_user.id)
    if not u:
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_EDIT_PROFILE)],
            [KeyboardButton(text=BTN_OFFLINE if u.get("is_online") else BTN_ONLINE), KeyboardButton(text=BTN_FILTERS)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
    )
    await message.answer(format_profile(u), reply_markup=kb)


async def start_registration(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(RegistrationSG.gender)
    await message.answer("Заполним анкету 👇\n\nВыбери пол:", reply_markup=gender_kb())


async def start_filters_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FiltersSG.gender)
    await message.answer("Кого показывать в поиске?", reply_markup=target_gender_kb())


# ---------------- START / HELP ----------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await ensure_user_row(message)
    u = await get_user(message.from_user.id)

    if profile_ready(u):
        await message.answer(
            "Привет! Это анонимный чат знакомств.\n"
            "Нажми «Онлайн», чтобы встать в поиск, или «Профиль», чтобы изменить анкету.",
            reply_markup=main_menu_kb(bool(u.get("is_online"))),
        )
    else:
        await start_registration(message, state)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message):
    await ensure_user_row(message)
    u = await get_user(message.from_user.id)
    await message.answer(
        "Команды:\n"
        "/start — запуск/регистрация\n"
        "/online — встать в поиск\n"
        "/offline — выйти из поиска\n"
        "/next — следующий собеседник\n"
        "/stop — разорвать текущий чат\n"
        "/profile — показать анкету\n"
        "/filters — настроить фильтры\n"
        "/cancel — отменить ввод\n\n"
        "Во время чата можно отправлять текст, фото, видео, голосовые, стикеры — бот пересылает их собеседнику.",
        reply_markup=main_menu_kb(bool(u.get("is_online")) if u else False),
    )


# ---------------- PROFILE / FILTERS ----------------

@router.message(Command("profile"))
@router.message(F.text == BTN_PROFILE)
async def cmd_profile(message: Message):
    await ensure_user_row(message)
    u = await get_user(message.from_user.id)
    if not profile_ready(u):
        await message.answer("Анкета ещё не заполнена. Нажми /start.")
        return
    await show_profile(message)


@router.message(F.text == BTN_EDIT_PROFILE)
async def edit_profile(message: Message, state: FSMContext):
    await ensure_user_row(message)
    await start_registration(message, state)


@router.message(Command("filters"))
@router.message(F.text == BTN_FILTERS)
async def cmd_filters(message: Message, state: FSMContext):
    await ensure_user_row(message)
    u = await get_user(message.from_user.id)
    if not profile_ready(u):
        await message.answer("Сначала заполни анкету через /start.")
        return
    await start_filters_flow(message, state)


# ---------------- ONLINE / OFFLINE / CHAT CONTROL ----------------

@router.message(Command("online"))
@router.message(F.text == BTN_ONLINE)
async def cmd_online(message: Message):
    await ensure_user_row(message)
    ok, partner_id, text = await start_search(message.bot, message.from_user.id)

    if not ok:
        u = await get_user(message.from_user.id)
        await message.answer(text, reply_markup=main_menu_kb(bool(u.get("is_online")) if u else False))
        return

    if partner_id:
        return

    await message.answer(text, reply_markup=main_menu_kb(True))


@router.message(Command("offline"))
@router.message(F.text == BTN_OFFLINE)
async def cmd_offline(message: Message):
    await ensure_user_row(message)
    await go_offline(message.bot, message.from_user.id)
    await message.answer("Ты оффлайн. Поиск остановлен.", reply_markup=main_menu_kb(False))


@router.message(Command("stop"))
@router.message(F.text == BTN_DISCONNECT)
async def cmd_stop(message: Message):
    await ensure_user_row(message)

    partner_id = await end_chat_for(message.from_user.id, offline_me=True, offline_partner=True)

    if partner_id:
        try:
            await message.bot.send_message(
                partner_id,
                "Собеседник завершил чат. Вы теперь оффлайн. Нажми «Онлайн», чтобы найти нового.",
                reply_markup=main_menu_kb(False),
            )
        except Exception:
            pass

    await message.answer("Чат завершён. Ты теперь оффлайн.", reply_markup=main_menu_kb(False))


@router.message(Command("next"))
@router.message(F.text == BTN_NEXT)
async def cmd_next(message: Message):
    await ensure_user_row(message)

    partner_id = await end_chat_for(message.from_user.id, offline_me=False, offline_partner=True)

    if partner_id:
        try:
            await message.bot.send_message(
                partner_id,
                "Собеседник переключился на следующего. Ты теперь оффлайн. Нажми «Онлайн», чтобы найти нового.",
                reply_markup=main_menu_kb(False),
            )
        except Exception:
            pass

    ok, new_partner_id, text = await start_search(message.bot, message.from_user.id)

    if not ok:
        u = await get_user(message.from_user.id)
        await message.answer(text, reply_markup=main_menu_kb(bool(u.get("is_online")) if u else False))
        return

    if new_partner_id:
        return

    await message.answer(text, reply_markup=main_menu_kb(True))


# ---------------- REPORT ----------------

@router.message(F.text == BTN_REPORT)
async def btn_report(message: Message):
    await ensure_user_row(message)
    u = await get_user(message.from_user.id)
    if not u or not u.get("in_chat_with"):
        await message.answer("Сейчас ты не в диалоге.")
        return
    await message.answer("Выбери причину жалобы:", reply_markup=report_reason_kb())


@router.callback_query(F.data.startswith("report:"))
async def report_callback(callback: CallbackQuery):
    await callback.answer()
    action = callback.data.split(":", 1)[1]

    if action == "cancel":
        await callback.message.edit_text("Жалоба отменена.")
        return

    me = await get_user(callback.from_user.id)
    if not me or not me.get("in_chat_with"):
        await callback.message.edit_text("Диалог уже завершён.")
        return

    partner_id = int(me["in_chat_with"])
    await save_report_and_block(callback.from_user.id, partner_id, action)

    ended_partner = await end_chat_for(callback.from_user.id, offline_me=True, offline_partner=True)

    if ended_partner:
        try:
            await callback.bot.send_message(
                ended_partner,
                "Диалог завершён по жалобе. Ты теперь оффлайн. Нажми «Онлайн», чтобы найти нового.",
                reply_markup=main_menu_kb(False),
            )
        except Exception:
            pass

    await callback.message.edit_text("Жалоба отправлена. Этот пользователь больше не попадётся тебе в поиске.")
    await callback.message.answer("Чат завершён. Ты теперь оффлайн.", reply_markup=main_menu_kb(False))


# ---------------- CANCEL ----------------

@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cancel_any(message: Message, state: FSMContext):
    current = await state.get_state()
    if not current:
        return

    await state.clear()
    u = await get_user(message.from_user.id)
    in_chat = bool(u and u.get("in_chat_with"))
    await message.answer(
        "Отменено.",
        reply_markup=chat_menu_kb() if in_chat else main_menu_kb(bool(u.get("is_online")) if u else False),
    )


# ---------------- REGISTRATION FSM ----------------

@router.message(RegistrationSG.gender)
async def reg_gender(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    val = REG_GENDER_BTNS.get(message.text or "")
    if not val:
        await message.answer("Выбери кнопку с полом 👇", reply_markup=gender_kb())
        return

    await state.update_data(gender=val)
    await state.set_state(RegistrationSG.age)
    await message.answer("Сколько тебе лет? (18–99)", reply_markup=cancel_kb())


@router.message(RegistrationSG.age)
async def reg_age(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    try:
        age = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи возраст числом, например: 23")
        return

    if age < 18 or age > 99:
        await message.answer("Возраст должен быть в диапазоне 18–99.")
        return

    await state.update_data(age=age)
    await state.set_state(RegistrationSG.city)
    await message.answer("Из какого ты города?", reply_markup=cancel_kb())


@router.message(RegistrationSG.city)
async def reg_city(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    city = (message.text or "").strip()
    if len(city) < 2 or len(city) > 50:
        await message.answer("Напиши город (2–50 символов).")
        return

    await state.update_data(city=city)
    await state.set_state(RegistrationSG.bio)
    await message.answer("Коротко о себе (до 300 символов):", reply_markup=cancel_kb())


@router.message(RegistrationSG.bio)
async def reg_bio(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    bio = (message.text or "").strip()
    if len(bio) < 3 or len(bio) > 300:
        await message.answer("Описание должно быть 3–300 символов.")
        return

    data = await state.get_data()
    await db_execute(
        """
        UPDATE users
        SET gender=?, age=?, city=?, bio=?, updated_at=?
        WHERE user_id=?
        """,
        (data["gender"], data["age"], data["city"], bio, utc_now(), message.from_user.id),
    )
    await state.clear()

    u = await get_user(message.from_user.id)
    await message.answer("Анкета сохранена ✅", reply_markup=main_menu_kb(bool(u.get("is_online")) if u else False))
    await show_profile(message)


# ---------------- FILTERS FSM ----------------

@router.message(FiltersSG.gender)
async def filters_gender(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    val = TARGET_GENDER_BTNS.get(message.text or "")
    if val is None:
        await message.answer("Выбери вариант кнопкой 👇", reply_markup=target_gender_kb())
        return

    await state.update_data(target_gender=val)
    await state.set_state(FiltersSG.age_min)
    await message.answer("Минимальный возраст партнёра (18–99):", reply_markup=cancel_kb())


@router.message(FiltersSG.age_min)
async def filters_age_min(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    try:
        age_min = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи число.")
        return

    if age_min < 18 or age_min > 99:
        await message.answer("Диапазон 18–99.")
        return

    await state.update_data(age_min=age_min)
    await state.set_state(FiltersSG.age_max)
    await message.answer("Максимальный возраст партнёра (18–99):", reply_markup=cancel_kb())


@router.message(FiltersSG.age_max)
async def filters_age_max(message: Message, state: FSMContext):
    if message.text == BTN_CANCEL:
        await cancel_any(message, state)
        return

    try:
        age_max = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введи число.")
        return

    if age_max < 18 or age_max > 99:
        await message.answer("Диапазон 18–99.")
        return

    data = await state.get_data()
    age_min = int(data["age_min"])

    if age_max < age_min:
        await message.answer("Максимальный возраст не может быть меньше минимального.")
        return

    await db_execute(
        "UPDATE users SET target_gender=?, target_age_min=?, target_age_max=?, updated_at=? WHERE user_id=?",
        (data["target_gender"], age_min, age_max, utc_now(), message.from_user.id),
    )
    await state.clear()

    u = await get_user(message.from_user.id)
    await message.answer(
        f"Фильтры сохранены ✅\nИщу: {TARGET_GENDER_RU[data['target_gender']]}, {age_min}–{age_max}",
        reply_markup=main_menu_kb(bool(u.get("is_online")) if u else False),
    )


# ---------------- RELAY / FALLBACK ----------------

@router.message()
async def relay_or_fallback(message: Message, state: FSMContext):
    if not message.from_user:
        return

    await ensure_user_row(message)

    current_state = await state.get_state()
    if current_state:
        return

    u = await get_user(message.from_user.id)
    if not u:
        return

    partner_id = u.get("in_chat_with")
    if not partner_id:
        if message.text and message.text.startswith("/"):
            await message.answer("Неизвестная команда. Нажми /help")
            return

        await message.answer(
            "Нажми «Онлайн», чтобы начать поиск, или «Профиль», чтобы заполнить анкету.",
            reply_markup=main_menu_kb(bool(u.get("is_online"))),
        )
        return

    try:
        await message.bot.copy_message(
            chat_id=int(partner_id),
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramBadRequest:
        await message.answer("Этот тип сообщения нельзя переслать. Попробуй текст, фото, видео, стикер или голосовое.")
    except Exception:
        logging.exception("relay failed")
        await message.answer("Ошибка пересылки сообщения. Попробуй ещё раз.")


# ---------------- Polling runner (optional) ----------------

async def on_startup(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск / меню"),
            BotCommand(command="online", description="Встать в поиск"),
            BotCommand(command="offline", description="Выйти из поиска"),
            BotCommand(command="next", description="Следующий собеседник"),
            BotCommand(command="stop", description="Разорвать чат"),
            BotCommand(command="profile", description="Моя анкета"),
            BotCommand(command="filters", description="Фильтры поиска"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отмена ввода"),
        ]
    )


async def main_polling():
    logging.basicConfig(level=logging.INFO)
    load_dotenv()

    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Укажи BOT_TOKEN в .env или env переменной")

    await init_db()

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    await on_startup(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main_polling())
