#!/usr/bin/env python3
"""Telegram Weather Outfit Bot.

Flow:
  /start → language selection (inline buttons)
         → location request (geo keyboard button)
         → "🌤 Check outfit" inline button
  Button / /outfit → Open-Meteo hourly forecast (current UTC hour)
                   → Pollinations.ai text API → outfit suggestion in chosen language
"""

import datetime
import logging
import os
import urllib.parse

import httpx
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
    MessageHandler,
    filters,
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s  %(name)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────────────────────

user_locations: dict[int, tuple[float, float]] = {}  # user_id → (lat, lon)
user_languages: dict[int, str] = {}                  # user_id → "English" | "Ukrainian"

CREDIT = "Made with ❤️ by @mandrockspalace — t.me/mandrockspalace"

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


# ── API helpers ───────────────────────────────────────────────────────────────

async def fetch_weather(lat: float, lon: float) -> tuple[float, int, float]:
    """Fetch hourly weather from Open-Meteo and return values for the current UTC hour.

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
    hour = datetime.datetime.utcnow().hour  # match array index to current UTC hour
    return (
        hourly["temperature_2m"][hour],
        hourly["weathercode"][hour],
        hourly["windspeed_10m"][hour],
    )


async def fetch_outfit_suggestion(temp: float, code: int, wind: float, language: str) -> str:
    """Ask Pollinations.ai for a one-paragraph outfit suggestion.

    Falls back to a short error message (in the user's language) on timeout or error.
    """
    condition = WMO.get(code, "unknown conditions")
    prompt = (
        f"You are a fashion assistant. Suggest a short outfit (2-3 items) for this weather: "
        f"{temp:.1f}°C, {condition}, wind {wind:.0f}km/h. "
        f"Reply in exactly one short paragraph, no lists, no line breaks. "
        f"Casual tone. Reply in {language}."
    )
    url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt, safe='')}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url)
            response.raise_for_status()
        return response.text.strip()
    except Exception as exc:
        logger.error("Pollinations request failed: %s", exc)
        if language == "Ukrainian":
            return "⚠️ Не вдалося отримати пораду. Спробуй ще раз трохи пізніше."
        return "⚠️ Couldn't get a suggestion right now. Please try again in a moment."


# ── Reusable keyboard/markup builders ────────────────────────────────────────

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


def outfit_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🌤 Check outfit", callback_data="check_outfit"),
    ]])


# ── Core outfit dispatch (shared by /outfit and inline button) ────────────────

async def _send_outfit(
    reply_fn,
    user_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Fetch weather + AI suggestion and send the result.

    Args:
        reply_fn:  bound coroutine — either message.reply_text or query.message.reply_text
        user_id:   Telegram user ID (to look up location and language)
        chat_id:   Telegram chat ID (for the typing action)
        context:   handler context (for bot.send_chat_action)
    """
    lang = user_languages.get(user_id, "English")

    if user_id not in user_locations:
        msg = (
            "Я не знаю твого місця розташування. Надішли /start щоб налаштувати бота."
            if lang == "Ukrainian"
            else "I don't have your location yet. Send /start to set up the bot."
        )
        await reply_fn(msg)
        return

    lat, lon = user_locations[user_id]

    # Show typing indicator while we hit the two APIs
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

    try:
        temp, code, wind = await fetch_weather(lat, lon)
    except Exception as exc:
        logger.error("Weather fetch failed for user %d: %s", user_id, exc)
        msg = (
            "⚠️ Не вдалося отримати погоду. Спробуй пізніше."
            if lang == "Ukrainian"
            else "⚠️ Couldn't fetch weather right now. Please try again."
        )
        await reply_fn(msg)
        return

    condition = WMO.get(code, "unknown conditions")
    header = f"🌡 {temp:.1f}°C · {condition} · 💨 {wind:.0f} km/h\n\n"

    # Still typing while Pollinations generates the suggestion
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    suggestion = await fetch_outfit_suggestion(temp, code, wind, lang)

    await reply_fn(header + suggestion)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — show language picker; location request follows after selection."""
    await update.message.reply_text(
        "👋 Hi! I'll suggest what to wear based on your local weather.\n\n"
        f"First, choose your language:\n\n_{CREDIT}_",
        parse_mode="Markdown",
        reply_markup=language_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — list all commands and show credit."""
    await update.message.reply_text(
        "🤖 *Weather Outfit Bot*\n\n"
        "/start — set up the bot\n"
        "/language — change language\n"
        "/mylocation — show your saved location\n"
        "/outfit — get an outfit suggestion now\n"
        "/help — show this message\n\n"
        f"_{CREDIT}_",
        parse_mode="Markdown",
    )


async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/language — re-show language picker."""
    await update.message.reply_text(
        "Choose your language:",
        reply_markup=language_keyboard(),
    )


async def cmd_mylocation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mylocation — show saved coordinates or prompt to share."""
    user_id = update.effective_user.id
    lang = user_languages.get(user_id, "English")

    if user_id in user_locations:
        lat, lon = user_locations[user_id]
        if lang == "Ukrainian":
            text = f"📍 Твоє збережене місце: `{lat:.5f}, {lon:.5f}`\n\nНадішли нове місце будь-коли, щоб оновити."
        else:
            text = f"📍 Your saved location: `{lat:.5f}, {lon:.5f}`\n\nSend a new location anytime to update it."
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
    user_id = update.effective_user.id
    await _send_outfit(update.message.reply_text, user_id, update.effective_chat.id, context)


# ── Callback handlers ─────────────────────────────────────────────────────────

async def callback_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'English 🇬🇧' / 'Ukrainian 🇺🇦' inline button presses."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    language = "Ukrainian" if query.data == "lang_uk" else "English"
    user_languages[user_id] = language
    logger.info("User %d set language to %s", user_id, language)

    await query.edit_message_text(f"✅ Language set to *{language}*.", parse_mode="Markdown")

    # If we already have their location, go straight to the outfit button
    if user_id in user_locations:
        prompt = (
            "Натисни нижче, щоб отримати пораду з одягу!"
            if language == "Ukrainian"
            else "Tap below to get your outfit recommendation!"
        )
        await query.message.reply_text(prompt, reply_markup=outfit_button())
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
    user_id = query.from_user.id
    await _send_outfit(query.message.reply_text, user_id, query.message.chat_id, context)


# ── Location message handler ──────────────────────────────────────────────────

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save the user's coordinates, then show the outfit button (or ask for language first)."""
    location = update.message.location
    user_id = update.effective_user.id
    user_locations[user_id] = (location.latitude, location.longitude)
    logger.info("Saved location for user %d: %.4f, %.4f", user_id, location.latitude, location.longitude)

    if user_id not in user_languages:
        # Language not set yet — ask before continuing
        await update.message.reply_text(
            "📍 Location saved! Now choose your language:",
            reply_markup=language_keyboard(),
        )
        return

    lang = user_languages[user_id]
    msg = (
        "📍 Місце збережено! Натисни нижче, щоб отримати пораду."
        if lang == "Ukrainian"
        else "📍 Location saved! Tap below to see what to wear today."
    )
    await update.message.reply_text(msg, reply_markup=outfit_button())


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Add it to your .env file.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("mylocation", cmd_mylocation))
    app.add_handler(CommandHandler("outfit", cmd_outfit))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(CallbackQueryHandler(callback_language, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(callback_check_outfit, pattern="^check_outfit$"))

    logger.info("Bot started — polling for updates...")
    app.run_polling()


if __name__ == "__main__":
    main()
