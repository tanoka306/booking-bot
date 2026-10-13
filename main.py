"""
Телеграм-бот бронирования столов: Tanoka / Hit me baby
Хостинг: Bothost (Python), работает через polling — ответы мгновенные.

Сценарий гостя:
  /start → кнопка заведения → схема зала (фото + текст) → имя →
  номер столика (кнопки) → дата и время → телефон →
  запись в Google Таблицу + уведомление администратору
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# ================== НАСТРОЙКИ ==================
# Секреты задаются в панели Bothost → «Переменные окружения»
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")      # куда приходят заявки (узнать: /myid)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")    # часть ссылки на таблицу между /d/ и /edit
SHEET_NAME = os.getenv("SHEET_NAME", "Брони")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Omsk")

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"

# Заведения: текст под схемой и номера столов (поменяйте под свои схемы)
VENUES = {
    "tanoka": {
        "name": "Tanoka",
        "plan_text": "Схема зала Tanoka",
        "tables": [str(i) for i in range(1, 11)],
    },
    "hit": {
        "name": "Hit me baby",
        "plan_text": "Схема зала Hit me baby",
        "tables": [str(i) for i in range(1, 11)],
    },
}

HEADERS = ["Создано", "Заведение", "Имя", "Номер столика",
           "Дата и время визита", "Телефон", "Telegram", "Chat ID"]
# ===============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

router = Router()
photo_cache: dict[str, str] = {}  # file_id уже отправленных схем — повторно шлются мгновенно


class Booking(StatesGroup):
    name = State()
    table = State()
    datetime = State()
    phone = State()


# ---------- Google Таблица ----------

_worksheet = None


def _open_worksheet():
    """Подключение к таблице через сервисный аккаунт Google."""
    creds_env = os.getenv("GOOGLE_CREDENTIALS", "").strip()
    if creds_env:
        gc = gspread.service_account_from_dict(json.loads(creds_env))
    else:
        gc = gspread.service_account(filename=str(BASE_DIR / "credentials.json"))

    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))

    if ws.row_values(1) != HEADERS:
        ws.update(range_name="A1", values=[HEADERS])
        ws.freeze(rows=1)
    return ws


def _append_row(row: list) -> None:
    global _worksheet
    if _worksheet is None:
        _worksheet = _open_worksheet()
    try:
        _worksheet.append_row(row, value_input_option="RAW")
    except Exception:
        # переподключаемся один раз (например, если истекла сессия)
        _worksheet = _open_worksheet()
        _worksheet.append_row(row, value_input_option="RAW")


def _init_worksheet() -> None:
    global _worksheet
    _worksheet = _open_worksheet()


async def save_booking(row: list) -> bool:
    try:
        await asyncio.to_thread(_append_row, row)
        return True
    except Exception as e:
        log.exception("Ошибка записи в Google Таблицу: %s", e)
        return False


# ---------- Клавиатуры ----------

def venues_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Забронировать стол в Tanoka", callback_data="venue:tanoka")],
        [InlineKeyboardButton(text="Забронировать стол в Hit me baby", callback_data="venue:hit")],
    ])


def tables_kb(venue_key: str) -> InlineKeyboardMarkup:
    tables = VENUES[venue_key]["tables"]
    rows = [
        [InlineKeyboardButton(text=f"№{t}", callback_data=f"table:{t}") for t in tables[i:i + 5]]
        for i in range(0, len(tables), 5)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ---------- Вспомогательное ----------

def find_plan_image(venue_key: str) -> Path | None:
    for ext in ("jpg", "jpeg", "png", "webp"):
        p = IMAGES_DIR / f"{venue_key}.{ext}"
        if p.exists():
            return p
    return None


async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(ADMIN_CHAT_ID, text)
    except Exception as e:
        log.warning("Не удалось написать администратору: %s", e)


async def send_plan(message: Message, venue_key: str) -> None:
    """Отправляет картинку схемы зала с текстом."""
    venue = VENUES[venue_key]
    caption = venue["plan_text"]

    file_id = photo_cache.get(venue_key)
    if file_id:
        try:
            await message.answer_photo(file_id, caption=caption)
            return
        except Exception:
            photo_cache.pop(venue_key, None)

    path = find_plan_image(venue_key)
    if path:
        try:
            sent = await message.answer_photo(FSInputFile(path), caption=caption)
            photo_cache[venue_key] = sent.photo[-1].file_id
            return
        except Exception as e:
            log.exception("Не удалось отправить схему %s: %s", venue_key, e)

    await message.answer(caption)
    await notify_admin(message.bot, f"⚠️ Схема зала {venue['name']} не найдена: "
                                    f"положите файл images/{venue_key}.jpg в проект")


# ---------- Сценарий ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Здравствуйте! Где хотите забронировать стол?",
                         reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите заведение:", reply_markup=venues_kb())


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Chat ID: {message.chat.id}")


# Шаг 1: выбор заведения → схема зала → вопрос имени
@router.callback_query(F.data.startswith("venue:"))
async def choose_venue(callback: CallbackQuery, state: FSMContext):
    venue_key = callback.data.split(":", 1)[1]
    await callback.answer()
    if venue_key not in VENUES:
        return
    await state.clear()
    await state.update_data(venue=venue_key)
    await send_plan(callback.message, venue_key)
    await callback.message.answer("Ваше имя")
    await state.set_state(Booking.name)


# Шаг 2: имя → выбор столика
@router.message(Booking.name, F.text)
async def get_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    data = await state.get_data()
    await message.answer("Выберите номер столика (номера указаны на схеме зала)",
                         reply_markup=tables_kb(data["venue"]))
    await state.set_state(Booking.table)


# Шаг 3: столик (кнопкой или текстом) → дата и время
async def _set_table(message: Message, state: FSMContext, table: str):
    await state.update_data(table=table)
    await message.answer(f"Столик №{table} выбран.\n\nДата и время визита")
    await state.set_state(Booking.datetime)


@router.callback_query(Booking.table, F.data.startswith("table:"))
async def choose_table_button(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _set_table(callback.message, state, callback.data.split(":", 1)[1])


@router.message(Booking.table, F.text)
async def choose_table_text(message: Message, state: FSMContext):
    await _set_table(message, state, message.text.strip().lstrip("№").strip())


# Шаг 4: дата и время → телефон
@router.message(Booking.datetime, F.text)
async def get_datetime(message: Message, state: FSMContext):
    await state.update_data(datetime=message.text.strip())
    await message.answer("Контактный телефон", reply_markup=phone_kb())
    await state.set_state(Booking.phone)


# Шаг 5: телефон → сохранение
@router.message(Booking.phone, F.contact | F.text)
async def get_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    data = await state.get_data()
    await state.clear()

    venue = VENUES[data["venue"]]["name"]
    user = message.from_user
    username = f"@{user.username}" if user and user.username else ""
    created = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")

    await message.answer(
        f"Спасибо! Заявка на бронь в {venue} принята.\n"
        "Мы свяжемся с вами для подтверждения.\n\nНовая бронь: /start",
        reply_markup=ReplyKeyboardRemove(),
    )

    saved = await save_booking([
        created, venue, data["name"], data["table"], data["datetime"],
        phone, username, str(message.chat.id),
    ])

    await notify_admin(message.bot,
        "Новая бронь\n\n"
        f"Заведение: {venue}\n"
        f"Имя: {data['name']}\n"
        f"Столик: №{data['table']}\n"
        f"Дата и время: {data['datetime']}\n"
        f"Телефон: {phone}\n"
        + (f"Telegram: {username}\n" if username else "")
        + ("" if saved else "\n⚠️ Не удалось записать в Google Таблицу — проверьте настройки")
    )


# Если гость прислал не то (стикер, фото и т.п.) на любом шаге
@router.message(Booking.name)
@router.message(Booking.datetime)
async def need_text(message: Message):
    await message.answer("Пожалуйста, ответьте текстом.")


@router.message(Booking.phone)
async def need_phone(message: Message):
    await message.answer("Напишите номер или нажмите кнопку «Отправить мой номер».",
                         reply_markup=phone_kb())


# Любое сообщение вне сценария → предлагаем начать
@router.message()
async def fallback(message: Message):
    await message.answer("Выберите заведение:", reply_markup=venues_kb())


# ---------- Запуск ----------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в переменных окружения")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Проверяем таблицу при старте — ошибка сразу видна в логах Bothost
    try:
        await asyncio.to_thread(_init_worksheet)
        log.info("Google Таблица подключена")
    except Exception as e:
        log.error("Google Таблица НЕ подключена: %s", e)

    for key in VENUES:
        if not find_plan_image(key):
            log.warning("Нет картинки схемы: images/%s.jpg", key)

    await bot.delete_webhook(drop_pending_updates=True)  # отключаем старый webhook, если был
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
