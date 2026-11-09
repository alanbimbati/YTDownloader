"""The old bot, kept only to point its users to the new one: a full second copy cost the Pi ~250 MB."""
import os
import re
import sqlite3
import time

import telebot
from telebot import apihelper, types

MOVED_TO = os.environ["MOVED_TO"]
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.telegram.org").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot_cache.sqlite3")
TEXT = (f"📢 Questo bot ora è {MOVED_TO}.\n\n"
        f"Apri {MOVED_TO}, premi Avvia e mandagli i tuoi link: video in massima qualità, articoli puliti, iscrizioni a canali e siti.")
GROUP_TEXT = (f"📢 Questo bot ora è {MOVED_TO}: aggiungetelo al gruppo al posto mio "
              "per continuare a scaricare video e leggere articoli.")
LINK_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

apihelper.API_URL = f"{API_BASE_URL}/bot{{0}}/{{1}}"
apihelper.FILE_URL = f"{API_BASE_URL}/file/bot{{0}}/{{1}}"
bot = telebot.TeleBot(os.environ["BOT_TOKEN"])


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS moved_notices (chat_id INTEGER PRIMARY KEY, sent_at INTEGER NOT NULL)")
    return conn


def _first_notice(chat_id: int) -> bool:
    # A group is told once: repeating it at every link would be the spam this bot is meant to stop.
    with _db() as conn:
        return conn.execute("INSERT OR IGNORE INTO moved_notices VALUES (?, ?)", (chat_id, int(time.time()))).rowcount > 0


def _has_link(message: types.Message) -> bool:
    return bool(LINK_RE.search(message.text or message.caption or ""))


@bot.message_handler(func=lambda m: m.chat.type == "private",
                     content_types=["text", "photo", "video", "animation", "document", "audio", "voice"])
def private(message: types.Message) -> None:
    bot.send_message(message.chat.id, TEXT, reply_markup=types.ReplyKeyboardRemove())


@bot.message_handler(func=lambda m: m.chat.type != "private" and _has_link(m),
                     content_types=["text", "photo", "video", "animation", "document"])
def group(message: types.Message) -> None:
    if _first_notice(message.chat.id):
        bot.reply_to(message, GROUP_TEXT)


@bot.channel_post_handler(func=_has_link, content_types=["text", "photo", "video", "animation", "document"])
def channel(message: types.Message) -> None:
    if _first_notice(message.chat.id):
        bot.send_message(message.chat.id, GROUP_TEXT)


if __name__ == "__main__":
    print(f"Redirect to {MOVED_TO} running")
    bot.infinity_polling(skip_pending=False, timeout=20, long_polling_timeout=20)
