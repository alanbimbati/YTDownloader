import os


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


WATCH_DIR = _env("PUBLISH_DIR", "/pubblica")
DONE_DIR = os.path.join(WATCH_DIR, "_pubblicati")
WORK_DIR = _env("PUBLISH_WORK_DIR", "/data/work")
POLL_SECONDS = int(_env("PUBLISH_POLL_SECONDS", "30"))
# Syncthing writes big files in several passes: a video counts as arrived once its size holds this long.
STABLE_SECONDS = int(_env("PUBLISH_STABLE_SECONDS", "120"))

BOT_TOKEN = _env("BOT_TOKEN")
ADMIN_USER_ID = _env("ADMIN_USER_ID")
API_BASE_URL = _env("API_BASE_URL", "https://api.telegram.org").rstrip("/")
TELEGRAM_CHANNEL = _env("PUBLISH_TELEGRAM_CHANNEL")

CLAUDE_MODEL = _env("PUBLISH_CLAUDE_MODEL", "claude-opus-5-5")
WHISPER_MODEL = _env("WHISPER_MODEL", "base")
# Enough for Claude to understand the video: transcribing a whole hour on the Pi would take longer still.
WHISPER_MAX_SECONDS = int(_env("WHISPER_MAX_SECONDS", "900"))

YOUTUBE_CLIENT_ID = _env("YOUTUBE_CLIENT_ID")
YOUTUBE_CLIENT_SECRET = _env("YOUTUBE_CLIENT_SECRET")
YOUTUBE_REFRESH_TOKEN = _env("YOUTUBE_REFRESH_TOKEN")
YOUTUBE_PRIVACY = _env("YOUTUBE_PRIVACY", "public")
YOUTUBE_CATEGORY = _env("YOUTUBE_CATEGORY", "22")

LINKEDIN_ACCESS_TOKEN = _env("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_TOKEN_EXPIRES_AT = int(_env("LINKEDIN_TOKEN_EXPIRES_AT", "0") or 0)
LINKEDIN_PERSON_URN = _env("LINKEDIN_PERSON_URN")
LINKEDIN_VERSION = _env("LINKEDIN_VERSION", "202610")

NOSTR_NSEC = _env("NOSTR_NSEC")
NOSTR_RELAYS = [r.strip() for r in _env("NOSTR_RELAYS", "wss://relay.damus.io,wss://nos.lol,wss://relay.primal.net").split(",") if r.strip()]
BLOSSOM_SERVER = _env("BLOSSOM_SERVER", "https://blossom.primal.net").rstrip("/")
BLOSSOM_MAX_BYTES = int(_env("BLOSSOM_MAX_MB", "500")) * 1024 * 1024

TELEGRAM_MAX_BYTES = 2000 * 1024 * 1024
LINKEDIN_MAX_BYTES = 500 * 1024 * 1024
SHORTS_MAX_SECONDS = 180
