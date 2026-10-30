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

# Group or channel (numeric id or @username) where every Bitcoin article is also posted. Empty = off.
BITCOIN_HUB = os.getenv("BITCOIN_HUB", "").strip()

# Sblocco "niente pubblicità", una tantum e a vita: prezzo e destinatario del pagamento.
AD_FREE_PRICE_SATS = int(os.getenv("AD_FREE_PRICE_SATS", "20000"))
AD_FREE_PAYEE = os.getenv("AD_FREE_PAYEE", "@AlanBimbati")

# YouTube cookies (Netscape format) of a logged-in account: without them, from the Raspberry's IP
# YouTube answers "Sign in to confirm you're not a bot". If the file is missing, go without.
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.txt")

# Set on a bot that has been replaced: it keeps working, but every message points to MOVED_TO
# and users who already started the new bot (found in HANDOFF_DB) get their subscriptions there.
MOVED_TO = os.getenv("MOVED_TO", "").strip()
HANDOFF_DB = os.getenv("HANDOFF_DB", "").strip()
