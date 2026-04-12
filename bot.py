#!/usr/bin/env python3
"""Telegram Weather Outfit Bot.

Flow:
  /start → language selection → "🌤 Check outfit" button (always available)
  No location saved → random world megacity used with a note
  Location shared → saved to DB, used for all future outfit checks
  ⚙️ Settings → change language / location / delete location / delete all data
  Inline mode: @botname in any chat shares today's outfit from saved location

Storage: SQLite via aiosqlite (survives restarts)
Cache:   last AI suggestion per user, 30-minute TTL (in-memory)
"""

import datetime
import logging
import os
import random
import time

import aiosqlite
import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = "users.db"
CACHE_TTL = 30 * 60  # seconds

logging.basicConfig(
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CREDIT = "Made with ❤️ by @mandrockspalace — t.me/mandrockspalace"

# In-memory outfit cache: (user_id, day) → (unix_timestamp, suggestion_text)
# day: 0=today, 1=tomorrow, 2=day after tomorrow, 3=day after that
outfit_cache: dict[tuple[int, int], tuple[float, str]] = {}

# ── i18n strings ──────────────────────────────────────────────────────────────

STRINGS: dict[str, dict[str, str]] = {
    "English": {
        # buttons
        "btn_check_outfit":      "🌤 Check outfit",
        "btn_today":             "📅 Today",
        "btn_tomorrow":          "📅 Tomorrow",
        "btn_day_after":         "📅 Day after tomorrow",
        "btn_day_after_after":   "📅 Day after that",
        "btn_update_location":   "📍 Update location",
        "btn_settings":          "⚙️ Settings",
        "btn_share_location":    "📍 Share my location",
        "btn_change_language":   "🌐 Change language",
        "btn_change_location":   "📍 Change location",
        "btn_delete_location":   "🗑 Delete location",
        "btn_delete_data":       "⚠️ Delete all my data",
        "btn_back":              "← Back",
        "btn_confirm_yes":       "✅ Yes, delete",
        "btn_cancel":            "❌ Cancel",
        # messages
        "start_msg":             "👋 Hi! I'll suggest what to wear based on your local weather.\n\nFirst, choose your language:\n\n_{credit}_",
        "tap_outfit":            "Tap below to get your outfit recommendation!",
        "share_location_prompt": "Share your location:",
        "location_saved":        "📍 Location saved! Tap below to see what to wear today.",
        "location_cleared":      "📍 Location deleted. I'll use a random city until you share a new one.",
        "data_deleted":          "✅ All your data has been deleted. Send /start to set up again.",
        "settings_menu":         "⚙️ *Settings*\n\nWhat would you like to change?",
        "confirm_del_loc":       "Delete your saved location? I'll use a random city instead.",
        "confirm_del_data":      "Delete *all* your data from this bot? This cannot be undone.",
        "choose_language":       "Choose your language / Оберіть мову:",
        "no_loc_note":           "📍 No location saved — using *{city}* as a stand-in\n\n",
        "cache_note":            "\n\n(cached — weather checked {min} min ago)",
        "weather_error":         "⚠️ Couldn't fetch weather right now. Please try again.",
        "ai_unavailable":        "\n(AI unavailable, showing basic suggestion)",
        "no_loc_inline":         "Open a private chat with the bot first and share your location.",
        "no_loc_inline_title":   "No location saved",
        "choose_day":            "Which day would you like to check?",
    },
    "Ukrainian": {
        # buttons
        "btn_check_outfit":      "🌤 Перевірити образ",
        "btn_today":             "📅 Сьогодні",
        "btn_tomorrow":          "📅 Завтра",
        "btn_day_after":         "📅 Послезавтра",
        "btn_day_after_after":   "📅 Послепослезавтра",
        "btn_update_location":   "📍 Оновити локацію",
        "btn_settings":          "⚙️ Налаштування",
        "btn_share_location":    "📍 Поділитися локацією",
        "btn_change_language":   "🌐 Змінити мову",
        "btn_change_location":   "📍 Змінити локацію",
        "btn_delete_location":   "🗑 Видалити локацію",
        "btn_delete_data":       "⚠️ Видалити всі мої дані",
        "btn_back":              "← Назад",
        "btn_confirm_yes":       "✅ Так, видалити",
        "btn_cancel":            "❌ Скасувати",
        # messages
        "start_msg":             "👋 Привіт! Я підкажу, що вдягнути залежно від твоєї погоди.\n\nСпочатку оберіть мову:\n\n_{credit}_",
        "tap_outfit":            "Натисни нижче, щоб отримати пораду з одягу!",
        "share_location_prompt": "Поділися своїм місцем розташування:",
        "location_saved":        "📍 Локацію збережено! Натисни нижче, щоб отримати пораду.",
        "location_cleared":      "📍 Локацію видалено. До нового збереження використовуватиму випадкове місто.",
        "data_deleted":          "✅ Усі твої дані видалено. Надішли /start щоб налаштувати бота знову.",
        "settings_menu":         "⚙️ *Налаштування*\n\nЩо бажаєш змінити?",
        "confirm_del_loc":       "Видалити збережену локацію? Натомість використовуватиму випадкове місто.",
        "confirm_del_data":      "Видалити *всі* твої дані з бота? Це незворотно.",
        "choose_language":       "Choose your language / Оберіть мову:",
        "no_loc_note":           "📍 Локація не збережена — використовую *{city}*\n\n",
        "cache_note":            "\n\n(кешовано — погода перевірена {min} хв тому)",
        "weather_error":         "⚠️ Не вдалося отримати погоду. Спробуй пізніше.",
        "ai_unavailable":        "\n(AI недоступний, базова порада)",
        "no_loc_inline":         "Спочатку відкрий приватний чат з ботом та поділися місцем розташування.",
        "no_loc_inline_title":   "Місце не збережено",
        "choose_day":            "На який день перевірити?",
    },
}


def s(key: str, lang: str, **kwargs) -> str:
    """Return a localised string, interpolating any kwargs."""
    text = STRINGS.get(lang, STRINGS["English"]).get(key, key)
    return text.format(**kwargs) if kwargs else text


# ── World megacities fallback pool ────────────────────────────────────────────

MEGACITIES: list[tuple[str, float, float]] = [
    ("Tokyo", 35.6762, 139.6503),
    ("Delhi", 28.7041, 77.1025),
    ("Shanghai", 31.2304, 121.4737),
    ("São Paulo", -23.5505, -46.6333),
    ("Mexico City", 19.4326, -99.1332),
    ("Cairo", 30.0444, 31.2357),
    ("Mumbai", 19.0760, 72.8777),
    ("Beijing", 39.9042, 116.4074),
    ("Osaka", 34.6937, 135.5023),
    ("New York", 40.7128, -74.0060),
    ("Buenos Aires", -34.6037, -58.3816),
    ("Istanbul", 41.0082, 28.9784),
    ("Lagos", 6.5244, 3.3792),
    ("Rio de Janeiro", -22.9068, -43.1729),
    ("Los Angeles", 34.0522, -118.2437),
    ("Moscow", 55.7558, 37.6173),
    ("Paris", 48.8566, 2.3522),
    ("Jakarta", -6.2088, 106.8456),
    ("London", 51.5074, -0.1278),
    ("Bangkok", 13.7563, 100.5018),
    ("Kyiv", 50.4501, 30.5234),
    ("Berlin", 52.5200, 13.4050),
    ("Nairobi", -1.2921, 36.8219),
    ("Sydney", -33.8688, 151.2093),
    ("Toronto", 43.6532, -79.3832),
    ("Dubai", 25.2048, 55.2708),
    ("Singapore", 1.3521, 103.8198),
    ("Seoul", 37.5665, 126.9780),
    ("Madrid", 40.4168, -3.7038),
    ("Rome", 41.9028, 12.4964),
]


def random_city() -> tuple[str, float, float]:
    return random.choice(MEGACITIES)


# ── WMO weather-code descriptions (Open-Meteo subset) ────────────────────────

WMO: dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "icy fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "rain showers",
    81: "rain showers",
    82: "heavy rain showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}

RAINY_CODES = frozenset({*range(51, 68), *range(80, 83), 95, 96, 99})
SNOWY_CODES = frozenset({*range(71, 78), 85, 86})


# ── SQLite helpers ────────────────────────────────────────────────────────────

async def db_init() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                language  TEXT    NOT NULL DEFAULT 'English',
                latitude  REAL,
                longitude REAL
            )
        """)
        await db.commit()


async def db_get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT language, latitude, longitude FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def db_set_language(user_id: int, language: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, language) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET language = excluded.language
            """,
            (user_id, language),
        )
        await db.commit()


async def db_set_location(user_id: int, lat: float, lon: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, latitude, longitude) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                latitude  = excluded.latitude,
                longitude = excluded.longitude
            """,
            (user_id, lat, lon),
        )
        await db.commit()


async def db_clear_location(user_id: int) -> None:
    """Set latitude/longitude to NULL without touching language."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET latitude = NULL, longitude = NULL WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def db_delete_user(user_id: int) -> None:
    """Remove the user row entirely."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()


# ── Weather fetching ──────────────────────────────────────────────────────────

async def fetch_weather(lat: float, lon: float, day: int = 0) -> tuple[float, int, float]:
    """Return (temperature °C, WMO code, wind speed km/h) for the specified day at current UTC hour.
    
    Args:
        day: 0=today, 1=tomorrow, 2=day after tomorrow, 3=day after that
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,weathercode,windspeed_10m"
        "&timezone=UTC&forecast_days=4"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()

    hourly = response.json()["hourly"]
    hour = datetime.datetime.now(datetime.UTC).hour
    # Each day has 24 hourly entries
    idx = day * 24 + hour
    return (
        hourly["temperature_2m"][idx],
        hourly["weathercode"][idx],
        hourly["windspeed_10m"][idx],
    )


# ── Outfit generation ─────────────────────────────────────────────────────────

def suggest_outfit_fallback(temp: float, code: int, wind: float) -> str:
    """Rule-based outfit suggestion — used when Gemini is unavailable."""
    is_rainy = code in RAINY_CODES
    is_snowy = code in SNOWY_CODES

    if temp < 0:
        clothes = "heavy winter coat and thermals"
    elif temp < 10:
        clothes = "warm coat"
    elif temp < 15:
        clothes = "light jacket or trench coat"
    elif temp < 20:
        clothes = "sweater or light jacket"
    elif temp < 25:
        clothes = "t-shirt"
    else:
        clothes = "t-shirt or dress"

    if is_snowy or temp < 5:
        shoes = "warm waterproof boots"
    elif is_rainy or temp < 15:
        shoes = "sneakers"
    elif temp >= 25:
        shoes = "sandals"
    else:
        shoes = "sneakers"

    extras: list[str] = []
    if is_rainy:
        extras.append("umbrella")
    if is_snowy or temp < 5:
        extras.append("gloves and scarf")
    elif temp < 10:
        extras.append("gloves")
    if wind > 30:
        extras.append("windproof layer")

    text = f"Wear a {clothes} with {shoes}."
    if extras:
        text += f" Don't forget: {', '.join(extras)}."
    return text


GEMINI_SYSTEM_PROMPT = (
    "You are a friendly, practical fashion assistant. "
    "When given current weather conditions, suggest a specific outfit in exactly one short paragraph "
    "(2–4 items: top, bottom or dress, footwear, and one optional accessory if truly needed). "
    "Be concrete — name actual garment types, not vague categories. "
    "No bullet points, no line breaks, no greetings, no sign-offs. "
    "Casual, warm tone. Reply in the language the user writes in."
)


async def get_outfit(temp: float, code: int, wind: float, language: str) -> tuple[str, bool]:
    """Ask Gemini for a one-paragraph outfit suggestion.

    Returns:
        (suggestion_text, is_ai) — is_ai=False when the rule-based fallback was used.
    """
    if not GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set, using fallback")
        return suggest_outfit_fallback(temp, code, wind), False

    condition = WMO.get(code, "unknown conditions")
    user_message = (
        f"Current weather: {temp:.1f}°C, {condition}, wind {wind:.0f} km/h. "
        f"What should I wear today? Reply in {language}."
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "system_instruction": {"parts": [{"text": GEMINI_SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": user_message}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 200},
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if text:
            return text, True
    except Exception as exc:
        logger.warning("Gemini unavailable, using fallback: %s", exc)

    return suggest_outfit_fallback(temp, code, wind), False


# ── Keyboard / markup builders ────────────────────────────────────────────────

def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
        InlineKeyboardButton("Українська 🇺🇦", callback_data="lang_uk"),
    ]])


def location_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(s("btn_check_outfit", lang))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def outfit_reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(s("btn_check_outfit", lang))]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def outfit_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Main keyboard shown after every outfit reply."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s("btn_check_outfit", lang), callback_data="check_outfit")],
        [
            InlineKeyboardButton(s("btn_update_location", lang), callback_data="update_location"),
            InlineKeyboardButton(s("btn_settings", lang), callback_data="settings"),
        ],
    ])


def day_selector_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Keyboard for selecting which day's outfit to check."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s("btn_today", lang), callback_data="day_0")],
        [InlineKeyboardButton(s("btn_tomorrow", lang), callback_data="day_1")],
        [InlineKeyboardButton(s("btn_day_after", lang), callback_data="day_2")],
        [InlineKeyboardButton(s("btn_day_after_after", lang), callback_data="day_3")],
    ])


def settings_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s("btn_change_language", lang),  callback_data="settings_lang")],
        [InlineKeyboardButton(s("btn_change_location", lang),  callback_data="settings_location")],
        [InlineKeyboardButton(s("btn_delete_location", lang),  callback_data="settings_del_loc")],
        [InlineKeyboardButton(s("btn_delete_data", lang),      callback_data="settings_del_data")],
        [InlineKeyboardButton(s("btn_back", lang),             callback_data="settings_back")],
    ])


def confirm_keyboard(lang: str, action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(s("btn_confirm_yes", lang), callback_data=f"confirm_{action}")],
        [InlineKeyboardButton(s("btn_cancel", lang),      callback_data="settings")],
    ])


# ── Core outfit dispatch ──────────────────────────────────────────────────────

async def _send_outfit(
    reply_fn,
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    day: int = 0,
) -> None:
    """Fetch weather + suggestion, apply cache, and send via reply_fn.

    Args:
        day: 0=today, 1=tomorrow, 2=day after tomorrow, 3=day after that
    If the user has no saved location, a random megacity is used.
    """
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    # Resolve location: saved or random city
    if user and user["latitude"] is not None:
        lat, lon = user["latitude"], user["longitude"]
        city_note = ""
    else:
        city, lat, lon = random_city()
        city_note = s("no_loc_note", lang, city=city)

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cache_key = (user_id, day)
    cached = outfit_cache.get(cache_key)
    if cached and not city_note:  # skip cache when using random city
        cached_at, cached_text = cached
        age = time.time() - cached_at
        if age < CACHE_TTL:
            age_min = int(age // 60)
            cache_note = s("cache_note", lang, min=age_min)
            await reply_fn(cached_text + cache_note, reply_markup=outfit_keyboard(lang))
            return

    # ── Fresh fetch ───────────────────────────────────────────────────────────
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        temp, code, wind = await fetch_weather(lat, lon, day=day)
    except Exception as exc:
        logger.error("Weather fetch failed for user %d: %s", user_id, exc)
        await reply_fn(s("weather_error", lang))
        return

    condition = WMO.get(code, "unknown conditions")
    header = city_note + f"🌡 {temp:.1f}°C · {condition} · 💨 {wind:.0f} km/h\n\n"

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    suggestion, is_ai = await get_outfit(temp, code, wind, lang)

    if not is_ai:
        suggestion += s("ai_unavailable", lang)

    full_text = header + suggestion

    # Only cache when using the real saved location
    if not city_note:
        outfit_cache[cache_key] = (time.time(), full_text)

    await reply_fn(full_text, reply_markup=outfit_keyboard(lang))


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show language picker."""
    await update.message.reply_text(
        s("start_msg", "English", credit=CREDIT),
        parse_mode="Markdown",
        reply_markup=language_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await db_get_user(update.effective_user.id)
    lang = user["language"] if user else "English"
    if lang == "Ukrainian":
        text = (
            "🤖 *Weather Outfit Bot*\n\n"
            "/start — налаштувати бота\n"
            "/outfit — отримати пораду з одягу\n"
            "/settings — налаштування\n"
            "/mylocation — показати збережену локацію\n"
            "/help — ця довідка\n\n"
            "Також можна написати `@botname` у будь-якому чаті, щоб поділитися порадою.\n\n"
            f"_{CREDIT}_"
        )
    else:
        text = (
            "🤖 *Weather Outfit Bot*\n\n"
            "/start — set up the bot\n"
            "/outfit — get an outfit suggestion now\n"
            "/settings — open settings\n"
            "/mylocation — show your saved location\n"
            "/help — show this message\n\n"
            "You can also type `@botname` in any chat to share today's outfit.\n\n"
            f"_{CREDIT}_"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await db_get_user(update.effective_user.id)
    lang = user["language"] if user else "English"
    await update.message.reply_text(s("choose_language", lang), reply_markup=language_keyboard())


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await db_get_user(update.effective_user.id)
    lang = user["language"] if user else "English"
    await update.message.reply_text(
        s("settings_menu", lang),
        parse_mode="Markdown",
        reply_markup=settings_keyboard(lang),
    )


async def cmd_mylocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    if user and user["latitude"] is not None:
        lat, lon = user["latitude"], user["longitude"]
        if lang == "Ukrainian":
            text = f"📍 Твоє збережене місце: `{lat:.5f}, {lon:.5f}`\n\nНадішли нове місце будь-коли, щоб оновити."
        else:
            text = f"📍 Your saved location: `{lat:.5f}, {lon:.5f}`\n\nSend a new location anytime to update it."
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        msg = "Локацію ще не збережено." if lang == "Ukrainian" else "No location saved yet."
        await update.message.reply_text(msg, reply_markup=location_keyboard(lang))


async def cmd_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_outfit(
        update.message.reply_text,
        update.effective_user.id,
        update.effective_chat.id,
        context,
    )


# ── Callback handlers ─────────────────────────────────────────────────────────

async def callback_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    language = "Ukrainian" if query.data == "lang_uk" else "English"
    await db_set_language(user_id, language)
    logger.info("User %d set language to %s", user_id, language)

    if language == "Ukrainian":
        confirm = "✅ Мову встановлено: *Українська*."
    else:
        confirm = "✅ Language set to *English*."
    await query.edit_message_text(confirm, parse_mode="Markdown")

    await query.message.reply_text(
        s("tap_outfit", language),
        reply_markup=outfit_keyboard(language),
    )


async def callback_check_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(
        s("choose_day", lang),
        reply_markup=day_selector_keyboard(lang),
    )


async def callback_select_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle day selection (day_0, day_1, day_2, day_3)."""
    query = update.callback_query
    await query.answer()
    
    # Extract day number from callback_data (e.g., "day_0" -> 0)
    day = int(query.data.split("_")[1])
    
    await _send_outfit(
        query.message.reply_text,
        query.from_user.id,
        query.message.chat_id,
        context,
        day=day,
    )


async def callback_update_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.message.reply_text(
        s("share_location_prompt", lang),
        reply_markup=location_keyboard(lang),
    )


async def callback_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open or return to the settings menu."""
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(
        s("settings_menu", lang),
        parse_mode="Markdown",
        reply_markup=settings_keyboard(lang),
    )


async def callback_settings_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return from settings to the outfit keyboard."""
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(
        s("tap_outfit", lang),
        reply_markup=outfit_keyboard(lang),
    )


async def callback_settings_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(s("choose_language", lang), reply_markup=language_keyboard())


async def callback_settings_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    # Close the settings message, then ask for location via ReplyKeyboard
    await query.delete_message()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=s("share_location_prompt", lang),
        reply_markup=location_keyboard(lang),
    )


async def callback_settings_del_loc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(
        s("confirm_del_loc", lang),
        reply_markup=confirm_keyboard(lang, "del_loc"),
    )


async def callback_settings_del_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    await query.edit_message_text(
        s("confirm_del_data", lang),
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(lang, "del_data"),
    )


async def callback_confirm_del_loc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    await db_clear_location(user_id)
    # Clear all cached outfits for this user (all days)
    for day in range(4):
        outfit_cache.pop((user_id, day), None)
    logger.info("Cleared location for user %d", user_id)

    await query.edit_message_text(
        s("location_cleared", lang),
        reply_markup=outfit_keyboard(lang),
    )


async def callback_confirm_del_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    await db_delete_user(user_id)
    # Clear all cached outfits for this user (all days)
    for day in range(4):
        outfit_cache.pop((user_id, day), None)
    logger.info("Deleted all data for user %d", user_id)

    await query.edit_message_text(s("data_deleted", lang))


# ── Location message handler ──────────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    location = update.message.location
    user_id = update.effective_user.id

    await db_set_location(user_id, location.latitude, location.longitude)
    # Clear all cached outfits for this user (all days)
    for day in range(4):
        outfit_cache.pop((user_id, day), None)
    logger.info("Saved location for user %d: %.4f, %.4f", user_id, location.latitude, location.longitude)

    user = await db_get_user(user_id)
    if not user or user["language"] is None:
        await update.message.reply_text(
            "📍 Location saved! Now choose your language:",
            reply_markup=language_keyboard(),
        )
        return

    lang = user["language"]
    await update.message.reply_text(
        s("location_saved", lang),
        reply_markup=outfit_keyboard(lang),
    )


# ── Inline query handler ──────────────────────────────────────────────────────

async def inline_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    user_id = query.from_user.id

    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    if not user or user["latitude"] is None:
        no_loc = s("no_loc_inline", lang)
        await query.answer(
            [InlineQueryResultArticle(
                id="no_location",
                title=s("no_loc_inline_title", lang),
                description=no_loc,
                input_message_content=InputTextMessageContent(no_loc),
            )],
            cache_time=10,
        )
        return

    lat, lon = user["latitude"], user["longitude"]
    result_text: str

    cache_key = (user_id, 0)  # inline mode always shows today
    cached = outfit_cache.get(cache_key)
    if cached:
        cached_at, cached_text = cached
        age = time.time() - cached_at
        if age < CACHE_TTL:
            age_min = int(age // 60)
            result_text = cached_text + s("cache_note", lang, min=age_min)
        else:
            cached = None

    if not cached:
        try:
            temp, code, wind = await fetch_weather(lat, lon, day=0)
            condition = WMO.get(code, "unknown conditions")
            header = f"🌡 {temp:.1f}°C · {condition} · 💨 {wind:.0f} km/h\n\n"
            suggestion, is_ai = await get_outfit(temp, code, wind, lang)
            if not is_ai:
                suggestion += s("ai_unavailable", lang)
            result_text = header + suggestion
            outfit_cache[cache_key] = (time.time(), result_text)
        except Exception as exc:
            logger.error("Inline outfit failed for user %d: %s", user_id, exc)
            result_text = s("weather_error", lang)

    await query.answer(
        [InlineQueryResultArticle(
            id="outfit",
            title=s("btn_check_outfit", lang),
            description="Tap to share a personalized outfit suggestion",
            input_message_content=InputTextMessageContent(result_text),
        )],
        cache_time=0,
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    await db_init()
    await app.bot.set_my_commands([
        BotCommand("start",      "Set up the bot"),
        BotCommand("outfit",     "Get outfit suggestion"),
        BotCommand("settings",   "Settings"),
        BotCommand("mylocation", "Show saved location"),
        BotCommand("help",       "Show help"),
    ])
    logger.info("DB ready. Bot commands registered.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add it to your .env file.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("language",   cmd_language))
    app.add_handler(CommandHandler("settings",   cmd_settings))
    app.add_handler(CommandHandler("mylocation", cmd_mylocation))
    app.add_handler(CommandHandler("outfit",     cmd_outfit))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    # language selection
    app.add_handler(CallbackQueryHandler(callback_language,           pattern="^lang_"))
    # main outfit keyboard
    app.add_handler(CallbackQueryHandler(callback_check_outfit,       pattern="^check_outfit$"))
    app.add_handler(CallbackQueryHandler(callback_select_day,         pattern="^day_[0-3]$"))
    app.add_handler(CallbackQueryHandler(callback_update_location,    pattern="^update_location$"))
    # settings navigation
    app.add_handler(CallbackQueryHandler(callback_settings,           pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(callback_settings_back,      pattern="^settings_back$"))
    app.add_handler(CallbackQueryHandler(callback_settings_lang,      pattern="^settings_lang$"))
    app.add_handler(CallbackQueryHandler(callback_settings_location,  pattern="^settings_location$"))
    app.add_handler(CallbackQueryHandler(callback_settings_del_loc,   pattern="^settings_del_loc$"))
    app.add_handler(CallbackQueryHandler(callback_settings_del_data,  pattern="^settings_del_data$"))
    # confirmations
    app.add_handler(CallbackQueryHandler(callback_confirm_del_loc,    pattern="^confirm_del_loc$"))
    app.add_handler(CallbackQueryHandler(callback_confirm_del_data,   pattern="^confirm_del_data$"))

    app.add_handler(InlineQueryHandler(inline_outfit))

    logger.info("Bot started — polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
