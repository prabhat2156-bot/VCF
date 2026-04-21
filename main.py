# main.py
# Render 24/7 Runner for your uploaded bot.py

import os
import threading
import time
import requests
from flask import Flask

# Import your uploaded bot.py
import bot

app = Flask(__name__)

@app.route("/")
def home():
    return "Telegram Bot Running 24/7"

@app.route("/health")
def health():
    return {"status": "ok"}

# Self ping every 2 min
def auto_ping():
    while True:
        try:
            url = os.getenv("RENDER_EXTERNAL_URL")
            if url:
                requests.get(url, timeout=10)
                print("Ping Success")
        except Exception as e:
            print("Ping Error:", e)

        time.sleep(120)

# Start your bot.py
def run_bot():
    try:
        if hasattr(bot, "main"):
            bot.main()
        elif hasattr(bot, "run"):
            bot.run()
        else:
            # python-telegram-bot async script detect
            import asyncio
            if hasattr(bot, "application"):
                asyncio.run(bot.application.run_polling())
            else:
                exec(open("bot.py").read())
    except Exception as e:
        print("Bot Error:", e)

if __name__ == "__main__":
    threading.Thread(target=run_bot).start()
    threading.Thread(target=auto_ping).start()

    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
