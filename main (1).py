"""
Render entry-point.

- Starts a tiny Flask web server on $PORT  (Render Web Service requires this).
- Runs the Telegram bot (bot.py) in a background thread.
- A keep-alive thread pings the public URL every 2 minutes so the
  free Render instance does not go to sleep.
"""

import os
import time
import logging
import threading

import requests
from flask import Flask

import bot  # imports bot.py (the full Telegram bot)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("render-entry")

app = Flask(__name__)


@app.route("/")
def index():
    return "Bot is alive ✅", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


# ---------------------------------------------------------------------------
# Bot thread
# ---------------------------------------------------------------------------
def _run_bot():
    try:
        log.info("Starting Telegram bot…")
        bot.main()
    except Exception as e:
        log.exception("Bot crashed: %s", e)


# ---------------------------------------------------------------------------
# Keep-alive: ping our own public URL every 2 minutes
# ---------------------------------------------------------------------------
def _keep_alive():
    # Render exposes the public URL in this env var.
    url = (
        os.environ.get("RENDER_EXTERNAL_URL")
        or os.environ.get("PING_URL")
        or ""
    ).rstrip("/")
    if not url:
        log.warning("No RENDER_EXTERNAL_URL / PING_URL set — keep-alive disabled.")
        return

    ping_url = f"{url}/health"
    log.info("Keep-alive will ping %s every 2 minutes.", ping_url)
    # small delay so the web server is up first
    time.sleep(20)
    while True:
        try:
            r = requests.get(ping_url, timeout=10)
            log.info("Keep-alive ping → %s", r.status_code)
        except Exception as e:
            log.warning("Keep-alive ping failed: %s", e)
        time.sleep(120)  # 2 minutes


def _start_background_threads():
    threading.Thread(target=_run_bot, name="telegram-bot", daemon=True).start()
    threading.Thread(target=_keep_alive, name="keep-alive", daemon=True).start()


# Start the background threads as soon as this module is imported
# (Gunicorn imports it once per worker, plain `python main.py` runs it directly).
_start_background_threads()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    log.info("Starting Flask on 0.0.0.0:%s", port)
    app.run(host="0.0.0.0", port=port)
