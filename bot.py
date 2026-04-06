#!/usr/bin/env python3
"""Telegram Weather Outfit Bot.

Flow:
  /start → language selection → location request → "🌤 Check outfit" button
  Button / /outfit → Open-Meteo hourly (current UTC hour) → Pollinations.ai
                   → rule-based fallback if AI unavailable
  Inline mode: @botname in any chat shares today's outfit from saved location

Storage: SQLite via aiosqlite (survives restarts)
Cache:   last AI suggestion per user, 30-minute TTL (in-memory)
"""

import datetime
import logging
import os
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

# In-memory outfit cache: user_id → (unix_timestamp, suggestion_text)
outfit_cache: dict[int, tuple[float, str]] = {}

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
    """Create the users table if it doesn't exist."""
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
    """Return the user row as a dict, or None if not found."""
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


# ── Weather fetching ──────────────────────────────────────────────────────────

async def fetch_weather(lat: float, lon: float) -> tuple[float, int, float]:
    """Fetch hourly Open-Meteo forecast and return values for the current UTC hour.

    Returns:
        (temperature_2m °C, WMO weather code, wind speed km/h)
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,weathercode,windspeed_10m"
        "&timezone=UTC&forecast_days=1"
    )
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()

    hourly = response.json()["hourly"]
    hour = datetime.datetime.utcnow().hour  # index matches UTC hour
    return (
        hourly["temperature_2m"][hour],
        hourly["weathercode"][hour],
        hourly["windspeed_10m"][hour],
    )


# ── Outfit generation ─────────────────────────────────────────────────────────

def suggest_outfit_fallback(temp: float, code: int, wind: float) -> str:
    """Rule-based outfit suggestion — used when Pollinations.ai is unavailable."""
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
        text = (
            response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        )
        if text:
            return text, True
    except Exception as exc:
        logger.warning("Gemini unavailable, using fallback: %s", exc)

    return suggest_outfit_fallback(temp, code, wind), False


# ── Keyboard / markup builders ────────────────────────────────────────────────

def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("English 🇬🇧", callback_data="lang_en"),
        InlineKeyboardButton("Ukrainian 🇺🇦", callback_data="lang_uk"),
    ]])


def location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share my location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def outfit_keyboard() -> InlineKeyboardMarkup:
    """Shown after every outfit reply — re-check or update location."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🌤 Check outfit", callback_data="check_outfit"),
        InlineKeyboardButton("📍 Update location", callback_data="update_location"),
    ]])


# ── Core outfit dispatch ──────────────────────────────────────────────────────

async def _send_outfit(
    reply_fn,
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Fetch weather + suggestion, apply cache, and send via reply_fn.

    Shared by the inline button callback and the /outfit command.
    reply_fn must accept (text, reply_markup=...) and be awaitable.
    """
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    if not user or user["latitude"] is None:
        no_loc = (
            "Я ще не знаю твого місця. Надішли /start щоб налаштувати бота."
            if lang == "Ukrainian"
            else "I don't have your location yet. Send /start to set up the bot."
        )
        await reply_fn(no_loc)
        return

    # ── Cache hit ─────────────────────────────────────────────────────────────
    cached = outfit_cache.get(user_id)
    if cached:
        cached_at, cached_text = cached
        age = time.time() - cached_at
        if age < CACHE_TTL:
            age_min = int(age // 60)
            cache_note = (
                f"\n\n(кешовано — погода перевірена {age_min} хв тому)"
                if lang == "Ukrainian"
                else f"\n\n(cached — weather checked {age_min} min ago)"
            )
            await reply_fn(cached_text + cache_note, reply_markup=outfit_keyboard())
            return

    # ── Fresh fetch ───────────────────────────────────────────────────────────
    lat, lon = user["latitude"], user["longitude"]
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        temp, code, wind = await fetch_weather(lat, lon)
    except Exception as exc:
        logger.error("Weather fetch failed for user %d: %s", user_id, exc)
        err = (
            "⚠️ Не вдалося отримати погоду. Спробуй пізніше."
            if lang == "Ukrainian"
            else "⚠️ Couldn't fetch weather right now. Please try again."
        )
        await reply_fn(err)
        return

    condition = WMO.get(code, "unknown conditions")
    header = f"🌡 {temp:.1f}°C · {condition} · 💨 {wind:.0f} km/h\n\n"

    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    suggestion, is_ai = await get_outfit(temp, code, wind, lang)

    if not is_ai:
        ai_note = (
            "\n(AI недоступний, базова порада)"
            if lang == "Ukrainian"
            else "\n(AI unavailable, showing basic suggestion)"
        )
        suggestion += ai_note

    full_text = header + suggestion
    outfit_cache[user_id] = (time.time(), full_text)
    await reply_fn(full_text, reply_markup=outfit_keyboard())


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show language picker (location request follows after selection)."""
    await update.message.reply_text(
        "👋 Hi! I'll suggest what to wear based on your local weather.\n\n"
        f"First, choose your language:\n\n_{CREDIT}_",
        parse_mode="Markdown",
        reply_markup=language_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — list commands and show credit."""
    await update.message.reply_text(
        "🤖 *Weather Outfit Bot*\n\n"
        "/start — set up the bot\n"
        "/language — change language\n"
        "/outfit — get an outfit suggestion now\n"
        "/mylocation — show your saved location\n"
        "/help — show this message\n\n"
        "You can also use me inline: type `@<botname>` in any chat to share today's outfit.\n\n"
        f"_{CREDIT}_",
        parse_mode="Markdown",
    )


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language — re-show language picker."""
    await update.message.reply_text(
        "Choose your language / Оберіть мову:",
        reply_markup=language_keyboard(),
    )


async def cmd_mylocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mylocation — show saved coordinates or prompt to share."""
    user_id = update.effective_user.id
    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    if user and user["latitude"] is not None:
        lat, lon = user["latitude"], user["longitude"]
        text = (
            f"📍 Твоє збережене місце: `{lat:.5f}, {lon:.5f}`\n\nНадішли нове місце будь-коли, щоб оновити."
            if lang == "Ukrainian"
            else f"📍 Your saved location: `{lat:.5f}, {lon:.5f}`\n\nSend a new location anytime to update it."
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    else:
        msg = (
            "Я ще не знаю твого місця розташування."
            if lang == "Ukrainian"
            else "I don't have your location yet."
        )
        await update.message.reply_text(msg, reply_markup=location_keyboard())


async def cmd_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/outfit — trigger an outfit suggestion directly."""
    await _send_outfit(
        update.message.reply_text,
        update.effective_user.id,
        update.effective_chat.id,
        context,
    )


# ── Callback handlers ─────────────────────────────────────────────────────────

async def callback_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle English 🇬🇧 / Ukrainian 🇺🇦 language selection."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    language = "Ukrainian" if query.data == "lang_uk" else "English"
    await db_set_language(user_id, language)
    logger.info("User %d set language to %s", user_id, language)

    await query.edit_message_text(f"✅ Language set to *{language}*.", parse_mode="Markdown")

    user = await db_get_user(user_id)
    has_location = user and user["latitude"] is not None

    if has_location:
        prompt = (
            "Натисни нижче, щоб отримати пораду з одягу!"
            if language == "Ukrainian"
            else "Tap below to get your outfit recommendation!"
        )
        await query.message.reply_text(prompt, reply_markup=outfit_keyboard())
    else:
        prompt = (
            "Чудово! Тепер поділися своїм місцем розташування:"
            if language == "Ukrainian"
            else "Great! Now please share your location:"
        )
        await query.message.reply_text(prompt, reply_markup=location_keyboard())


async def callback_check_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the '🌤 Check outfit' inline button press."""
    query = update.callback_query
    await query.answer()
    await _send_outfit(
        query.message.reply_text,
        query.from_user.id,
        query.message.chat_id,
        context,
    )


async def callback_update_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the '📍 Update location' inline button — re-sends the location keyboard."""
    query = update.callback_query
    await query.answer()

    user = await db_get_user(query.from_user.id)
    lang = user["language"] if user else "English"
    msg = (
        "Поділися своїм новим місцем розташування:"
        if lang == "Ukrainian"
        else "Share your updated location:"
    )
    await query.message.reply_text(msg, reply_markup=location_keyboard())


# ── Location message handler ──────────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save coordinates to DB, clear cache, then show the outfit button."""
    location = update.message.location
    user_id = update.effective_user.id

    await db_set_location(user_id, location.latitude, location.longitude)
    outfit_cache.pop(user_id, None)  # invalidate cached suggestion for old location
    logger.info("Saved location for user %d: %.4f, %.4f", user_id, location.latitude, location.longitude)

    user = await db_get_user(user_id)

    if not user or user["language"] is None:
        # Language not chosen yet — ask before continuing
        await update.message.reply_text(
            "📍 Location saved! Now choose your language:",
            reply_markup=language_keyboard(),
        )
        return

    lang = user["language"]
    msg = (
        "📍 Місце збережено! Натисни нижче, щоб отримати пораду."
        if lang == "Ukrainian"
        else "📍 Location saved! Tap below to see what to wear today."
    )
    await update.message.reply_text(msg, reply_markup=outfit_keyboard())


# ── Inline query handler ──────────────────────────────────────────────────────

async def inline_outfit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline queries — lets users share outfit suggestions from any chat.

    Requires the user to have set their location via a private chat first.
    Enable inline mode in @BotFather: /setinline → set placeholder text.
    """
    query = update.inline_query
    user_id = query.from_user.id

    user = await db_get_user(user_id)
    lang = user["language"] if user else "English"

    if not user or user["latitude"] is None:
        no_loc = (
            "Спочатку відкрий приватний чат з ботом та поділися місцем розташування."
            if lang == "Ukrainian"
            else "Open a private chat with the bot first and share your location."
        )
        await query.answer(
            [InlineQueryResultArticle(
                id="no_location",
                title="No location saved" if lang == "English" else "Місце не збережено",
                description=no_loc,
                input_message_content=InputTextMessageContent(no_loc),
            )],
            cache_time=10,
        )
        return

    lat, lon = user["latitude"], user["longitude"]
    result_text: str

    # Try cache first to avoid API calls in inline mode
    cached = outfit_cache.get(user_id)
    if cached:
        cached_at, cached_text = cached
        age = time.time() - cached_at
        if age < CACHE_TTL:
            age_min = int(age // 60)
            note = (
                f"\n\n(кешовано — погода перевірена {age_min} хв тому)"
                if lang == "Ukrainian"
                else f"\n\n(cached — weather checked {age_min} min ago)"
            )
            result_text = cached_text + note
        else:
            cached = None  # expired

    if not cached:
        try:
            temp, code, wind = await fetch_weather(lat, lon)
            condition = WMO.get(code, "unknown conditions")
            header = f"🌡 {temp:.1f}°C · {condition} · 💨 {wind:.0f} km/h\n\n"

            suggestion, is_ai = await get_outfit(temp, code, wind, lang)
            if not is_ai:
                ai_note = (
                    "\n(AI недоступний, базова порада)"
                    if lang == "Ukrainian"
                    else "\n(AI unavailable, showing basic suggestion)"
                )
                suggestion += ai_note

            result_text = header + suggestion
            outfit_cache[user_id] = (time.time(), result_text)
        except Exception as exc:
            logger.error("Inline outfit failed for user %d: %s", user_id, exc)
            result_text = (
                "⚠️ Не вдалося отримати погоду. Спробуй пізніше."
                if lang == "Ukrainian"
                else "⚠️ Couldn't fetch weather right now. Please try again."
            )

    await query.answer(
        [InlineQueryResultArticle(
            id="outfit",
            title="🌤 My outfit today",
            description="Tap to share a personalized outfit suggestion",
            input_message_content=InputTextMessageContent(result_text),
        )],
        cache_time=0,  # don't let Telegram cache — conditions change
    )


# ── Startup ───────────────────────────────────────────────────────────────────

async def post_init(app: Application) -> None:
    """Run once on startup: init DB and register bot command menu."""
    await db_init()
    await app.bot.set_my_commands([
        BotCommand("start", "Set up the bot"),
        BotCommand("language", "Change language"),
        BotCommand("outfit", "Get outfit suggestion"),
        BotCommand("mylocation", "Show saved location"),
        BotCommand("help", "Show help"),
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("mylocation", cmd_mylocation))
    app.add_handler(CommandHandler("outfit", cmd_outfit))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(callback_language, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(callback_check_outfit, pattern="^check_outfit$"))
    app.add_handler(CallbackQueryHandler(callback_update_location, pattern="^update_location$"))
    app.add_handler(InlineQueryHandler(inline_outfit))

    logger.info("Bot started — polling for updates...")
    # allowed_updates=Update.ALL_TYPES is required to receive inline_query updates
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
