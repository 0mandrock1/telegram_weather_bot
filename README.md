# telegram_weather_bot

A Telegram bot that suggests what to wear based on your current location's weather.  
Powered by [Open-Meteo](https://open-meteo.com/) — free, no API key required.

## Usage

1. Send `/start`
2. Tap **📍 Share my location**
3. Tap **🌤 Check outfit** to get clothes / shoes / accessories advice

## Deploy on Railway

1. Fork / clone this repo and push it to GitHub
2. Create a new [Railway](https://railway.app/) project → **Deploy from GitHub repo**
3. Add environment variable: `BOT_TOKEN` = your token from [@BotFather](https://t.me/BotFather)
4. Railway will pick up the `Procfile` and start the worker automatically

## Run locally

```bash
git clone <repo-url>
cd telegram_weather_bot
python -m venv .venv && source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env and paste your BOT_TOKEN
python bot.py
```
