import os

# Solo da ambiente: un default qui finisce nel repo e chiunque lo legga controlla il bot.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN mancante: impostalo nel .env o nell'ambiente.")

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "62716473"))

DB_PATH = os.getenv("DB_PATH", "bot_cache.sqlite3")

# Telegram dichiara limite ~2GB per file; teniamo un piccolo margine.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024 - 10 * 1024 * 1024)))

# Base URL Bot API. Di default usa i server ufficiali.
# Per upload fino a 2000 MB serve un Local Bot API Server (es. http://127.0.0.1:8081).
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.telegram.org").rstrip("/")

# Username Telegram a cui gli sponsor devono scrivere per rinnovare (senza @). Vuoto = riga omessa.
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "").lstrip("@").strip()

# Canale su cui il bot propone la pubblicazione dei video a tema bitcoin.
BITCOIN_CHANNEL = os.getenv("BITCOIN_CHANNEL", "@BitcoinPodcastTelegram")
