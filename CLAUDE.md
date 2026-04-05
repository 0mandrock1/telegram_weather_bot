# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A single-file Python Telegram bot (`bot.py`) that recommends what to wear based on real-time local weather.  
Supports English and Ukrainian. Weather from Open-Meteo (free, no key). Outfit text from Pollinations.ai (free, no key) with a rule-based fallback.

## Commands

```bash
pip install -r requirements.txt   # install deps (includes aiosqlite)
python bot.py                     # run locally — creates users.db on first start
```

## Architecture

Everything lives in `bot.py`. Key layers:

### Storage
- **SQLite** (`users.db`) via `aiosqlite` — persists `user_id`, `language`, `latitude`, `longitude` across restarts
- Schema created in `db_init()` called from `post_init` hook
- Four helpers: `db_get_user`, `db_set_language`, `db_set_location`, `db_init`

### State
```python
outfit_cache: dict[int, tuple[float, str]]  # user_id → (unix_ts, suggestion_text), 30-min TTL
```

### API calls
| Function | Service | Fallback |
|---|---|---|
| `fetch_weather(lat, lon)` | Open-Meteo hourly, current UTC hour index | raises on failure |
| `get_outfit(temp, code, wind, language)` | Pollinations.ai text GET | `suggest_outfit_fallback()` — returns `(text, is_ai=False)` |

### Outfit flow (`_send_outfit`)
1. Load user from DB — bail if no location
2. Check `outfit_cache` — if hit and age < 30 min, reply with cached text + `"(cached — X min ago)"` note
3. `fetch_weather` → `get_outfit` (with automatic fallback to rule-based)
4. Append `"(AI unavailable, showing basic suggestion)"` note if fallback was used
5. Store result in `outfit_cache`, reply with `outfit_keyboard()` (Check outfit + Update location buttons)

### User flow
1. `/start` → language inline buttons (`lang_en` / `lang_uk` callbacks)
2. Language chosen → location `ReplyKeyboardMarkup` **or** outfit button if location already in DB
3. Location received → saved to DB, cache invalidated, outfit button shown
4. `update_location` callback → re-sends location keyboard

### Inline mode
`InlineQueryHandler(inline_outfit)` — user types `@botname` in any chat.  
Uses cache if available, otherwise fetches live. Requires BotFather `/setinline` to be enabled.  
`run_polling(allowed_updates=Update.ALL_TYPES)` is needed to receive inline query events.

### Startup
`post_init` coroutine passed to `Application.builder().post_init(...)`:
- `db_init()` — creates schema
- `bot.set_my_commands([...])` — registers the command menu in Telegram

## Environment

| Variable | Where |
|---|---|
| `BOT_TOKEN` | `.env` locally; Railway env vars in production |

`users.db` is created at the working directory on first run — add it to `.gitignore` if not already excluded.

## Deployment

- **Procfile:** `worker: python bot.py` (Railway worker dyno)
- No web server — long-polling only (`app.run_polling()`)
- `users.db` will be ephemeral on Railway unless a volume is mounted; for production persistence, swap `aiosqlite` for a Postgres connection
