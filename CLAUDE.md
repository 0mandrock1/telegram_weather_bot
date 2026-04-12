# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A single-file Python Telegram bot (`bot.py`) that recommends what to wear based on real-time local weather.  
Supports English and Ukrainian. Weather from Open-Meteo (free, no key). Outfit text from Gemini with a rule-based fallback.

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
outfit_cache: dict[tuple[int, int], tuple[float, str]]  # (user_id, day) → (unix_ts, suggestion_text), 30-min TTL
# day: 0=today, 1=tomorrow, 2=day after tomorrow, 3=day after that
```

### API calls
| Function | Service | Fallback |
|---|---|---|
| `fetch_weather(lat, lon, day=0)` | Open-Meteo hourly (4-day forecast), current UTC hour index for selected day | raises on failure |
| `get_outfit(temp, code, wind, language)` | Gemini text generation | `suggest_outfit_fallback()` — returns `(text, is_ai=False)` |

### Outfit flow (`_send_outfit`)
1. Load user from DB — bail if no location
2. Check `outfit_cache[(user_id, day)]` — if hit and age < 30 min, reply with cached text + `"(cached — X min ago)"` note
3. `fetch_weather(lat, lon, day)` → `get_outfit` (with automatic fallback to rule-based)
4. Append `"(AI unavailable, showing basic suggestion)"` note if fallback was used
5. Store result in `outfit_cache[(user_id, day)]`, reply with `outfit_keyboard()` (Check outfit + Update location buttons)

### Day selection flow
1. User taps "Check outfit" button → show day selector with 4 buttons (Today, Tomorrow, Day after, Day after that)
2. User selects day (0-3) via `callback_select_day`
3. `_send_outfit` called with `day` parameter
4. Result shown with outfit keyboard to allow checking other days

### User flow
1. `/start` → language inline buttons (`lang_en` / `lang_uk` callbacks)
2. Language chosen → location prompt via `ReplyKeyboardMarkup`
3. Location received → saved to DB, cache invalidated, outfit keyboard shown**
4. "Check outfit" button → day selector keyboard (Today/Tomorrow/etc.)
5. Day selected → fetch and show outfit for that day + outfit keyboard
6. Can repeat from step 4 to check other days

** Now always shows outfit keyboard after language setup, allowing day-based outfit checks
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
