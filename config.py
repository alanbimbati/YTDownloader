import os

# Preferisci impostare la variabile d'ambiente `BOT_TOKEN`.
# Se lasci il token qui, chiunque abbia accesso al file può controllare il bot.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8338930015:AAFxnibqHt3hfpnkDNQzk4cY1DtS83ZLn5c")

# Il tuo user_id Telegram (dall'errore: chat_id=62716473).
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "62716473"))

DB_PATH = os.getenv("DB_PATH", "bot_cache.sqlite3")

# Telegram dichiara limite ~2GB per file; teniamo un piccolo margine.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024 - 10 * 1024 * 1024)))

# Base URL Bot API. Di default usa i server ufficiali.
# Per upload fino a 2000 MB serve un Local Bot API Server (es. http://127.0.0.1:8081).
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.telegram.org").rstrip("/")
