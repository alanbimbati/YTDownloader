import os
import sqlite3
import time
from typing import Optional, TypedDict


# Alzarlo scarta le voci in cache prodotte da versioni precedenti (mp3 e miniature nuove).
CACHE_FORMAT = 3

SPONSOR_PERIOD_S = 30 * 24 * 3600
SPONSOR_WARN_S = 7 * 24 * 3600


class Sponsor(TypedDict):
    name: str
    owner_username: str
    added_at: int
    expires_at: int


class CacheEntry(TypedDict, total=False):
    yt_id: str
    title: str
    thumb_file_id: str
    video_file_id: str
    audio_file_id: str


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                yt_id TEXT PRIMARY KEY,
                title TEXT,
                thumb_file_id TEXT,
                video_file_id TEXT,
                audio_file_id TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sponsors (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                added_at INTEGER NOT NULL,
                owner_username TEXT,
                expires_at INTEGER,
                renew_notified_at INTEGER,
                expired_notified_at INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                added_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_whitelist (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                requested_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                yt_id TEXT NOT NULL,
                yt_title TEXT,
                downloaded_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                last_seen INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                audio_lang TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                telegraph_url TEXT NOT NULL,
                title TEXT NOT NULL,
                is_bitcoin INTEGER NOT NULL,
                hub_sent INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                sticker INTEGER NOT NULL,
                thumb INTEGER NOT NULL,
                video INTEGER NOT NULL,
                audio INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER NOT NULL,
                channel_url TEXT NOT NULL,
                PRIMARY KEY (user_id, channel_url)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels_state (
                channel_url TEXT PRIMARY KEY,
                last_video_id TEXT,
                channel_title TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS url_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ad_free (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                granted_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS callback_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_forwards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                msg_ids TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )

        # Migrations
        try:
            conn.execute("ALTER TABLE downloads_log ADD COLUMN yt_title TEXT")
        except sqlite3.OperationalError:
            pass # already exists

        # Where each download happened and whether it came from the cache, for the stats dashboard.
        for col, decl in (("chat_id", "INTEGER"), ("chat_type", "TEXT"), ("from_cache", "INTEGER")):
            try:
                conn.execute(f"ALTER TABLE downloads_log ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass # already exists

        try:
            conn.execute("ALTER TABLE chat_settings ADD COLUMN articles INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass # already exists

        try:
            conn.execute("ALTER TABLE channels_state ADD COLUMN channel_title TEXT")
        except sqlite3.OperationalError:
            pass # already exists

        try:
            conn.execute("ALTER TABLE channels_state ADD COLUMN channel_id TEXT")
        except sqlite3.OperationalError:
            pass # already exists

        try:
            conn.execute("ALTER TABLE cache ADD COLUMN fmt_v INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass # already exists

        for col, decl in (
            ("owner_username", "TEXT"),
            ("expires_at", "INTEGER"),
            ("renew_notified_at", "INTEGER"),
            ("expired_notified_at", "INTEGER"),
        ):
            try:
                conn.execute(f"ALTER TABLE sponsors ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass # already exists

        # Gli sponsor preesistenti non avevano scadenza: partono dal mese dalla data di inserimento.
        conn.execute(
            "UPDATE sponsors SET expires_at = added_at + ? WHERE expires_at IS NULL",
            (SPONSOR_PERIOD_S,),
        )


def get_unique_users(db_path: str) -> list[int]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT user_id FROM downloads_log").fetchall()
    return [r[0] for r in rows]

def get_downloads_last_hour(db_path: str, user_id: int) -> int:
    now = int(time.time())
    one_hour_ago = now - 3600
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM downloads_log WHERE user_id=? AND downloaded_at>=?",
            (user_id, one_hour_ago)
        ).fetchone()
    return row[0] if row else 0

def save_url_cache(db_path: str, url: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("INSERT INTO url_cache (url) VALUES (?)", (url,))
        return cur.lastrowid

def get_url_cache(db_path: str, url_id: int) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT url FROM url_cache WHERE id=?", (url_id,)).fetchone()
    return row[0] if row else None

def is_ad_free(db_path: str, user_id: int) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM ad_free WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
    return bool(row)


def grant_ad_free(db_path: str, user_id: int, username: str = "") -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ad_free (user_id, username, granted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
            """,
            (user_id, (username or "").lstrip("@").strip(), now),
        )


def revoke_ad_free(db_path: str, user_id: int) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM ad_free WHERE user_id=?", (user_id,))
    return bool(cur.rowcount)


def list_ad_free(db_path: str) -> list[tuple[int, str, int]]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT user_id, username, granted_at FROM ad_free ORDER BY granted_at DESC"
        ).fetchall()


def save_token(db_path: str, value: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("INSERT INTO callback_tokens (value) VALUES (?)", (value,))
        return cur.lastrowid


def get_token(db_path: str, token_id: int) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT value FROM callback_tokens WHERE id=?", (token_id,)).fetchone()
    return row[0] if row else None


def save_pending_forward(db_path: str, chat_id: str, msg_ids: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO pending_forwards (chat_id, msg_ids) VALUES (?, ?)",
            (chat_id, msg_ids)
        )
        return cur.lastrowid

def get_pending_forward(db_path: str, f_id: int) -> Optional[tuple[str, str]]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT chat_id, msg_ids FROM pending_forwards WHERE id=?", (f_id,)).fetchone()
    return row

def delete_pending_forward(db_path: str, f_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM pending_forwards WHERE id=?", (f_id,))



def list_sponsors(db_path: str) -> list[str]:
    """Solo gli sponsor ancora validi: sono quelli mostrati sotto 'Consigliati'."""
    now = int(time.time())
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sponsors WHERE expires_at > ? ORDER BY added_at ASC, name ASC",
            (now,),
        ).fetchall()
    return [r[0] for r in rows if r and r[0]]


def list_sponsors_detailed(db_path: str) -> list[Sponsor]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name, owner_username, added_at, expires_at
            FROM sponsors ORDER BY expires_at ASC, name ASC
            """
        ).fetchall()
    return [
        {"name": r[0], "owner_username": r[1] or "", "added_at": r[2] or 0, "expires_at": r[3] or 0}
        for r in rows
    ]


def get_sponsor(db_path: str, name: str) -> Optional[Sponsor]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT name, owner_username, added_at, expires_at
            FROM sponsors WHERE name=?
            """,
            (name.strip(),),
        ).fetchone()
    if not row:
        return None
    return {"name": row[0], "owner_username": row[1] or "", "added_at": row[2] or 0, "expires_at": row[3] or 0}


def add_sponsor(db_path: str, name: str, owner_username: str = "", period_s: int = SPONSOR_PERIOD_S) -> int:
    now = int(time.time())
    expires_at = now + period_s
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsors (name, added_at, owner_username, expires_at, renew_notified_at, expired_notified_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            ON CONFLICT(name) DO UPDATE SET
                added_at=excluded.added_at,
                owner_username=excluded.owner_username,
                expires_at=excluded.expires_at,
                renew_notified_at=NULL,
                expired_notified_at=NULL
            """,
            (name.strip(), now, (owner_username or "").lstrip("@").strip(), expires_at),
        )
    return expires_at


def rename_sponsor(db_path: str, old_name: str, new_name: str) -> bool:
    """Cambia solo il testo mostrato: scadenza e proprietario restano quelli."""
    with _connect(db_path) as conn:
        try:
            cur = conn.execute(
                "UPDATE sponsors SET name=? WHERE name=?", (new_name.strip(), old_name.strip())
            )
        except sqlite3.IntegrityError:
            return False
    return bool(cur.rowcount)


def set_sponsor_owner(db_path: str, name: str, owner_username: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE sponsors SET owner_username=? WHERE name=?",
            ((owner_username or "").lstrip("@").strip(), name.strip()),
        )
    return bool(cur.rowcount)


def renew_sponsor(db_path: str, name: str, period_s: int = SPONSOR_PERIOD_S) -> Optional[int]:
    """Il rinnovo riparte da oggi: una proroga decisa in ritardo non regala giorni già scaduti."""
    now = int(time.time())
    with _connect(db_path) as conn:
        row = conn.execute("SELECT expires_at FROM sponsors WHERE name=?", (name.strip(),)).fetchone()
        if not row:
            return None
        base = max(int(row[0] or 0), now)
        expires_at = base + period_s
        conn.execute(
            """
            UPDATE sponsors
            SET expires_at=?, renew_notified_at=NULL, expired_notified_at=NULL
            WHERE name=?
            """,
            (expires_at, name.strip()),
        )
    return expires_at


def sponsors_to_warn(db_path: str, window_s: int) -> list[Sponsor]:
    now = int(time.time())
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name, owner_username, added_at, expires_at
            FROM sponsors
            WHERE renew_notified_at IS NULL AND expires_at > ? AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (now, now + window_s),
        ).fetchall()
    return [
        {"name": r[0], "owner_username": r[1] or "", "added_at": r[2] or 0, "expires_at": r[3] or 0}
        for r in rows
    ]


def sponsors_just_expired(db_path: str) -> list[Sponsor]:
    now = int(time.time())
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name, owner_username, added_at, expires_at
            FROM sponsors
            WHERE expired_notified_at IS NULL AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (now,),
        ).fetchall()
    return [
        {"name": r[0], "owner_username": r[1] or "", "added_at": r[2] or 0, "expires_at": r[3] or 0}
        for r in rows
    ]


def mark_sponsor_warned(db_path: str, name: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute("UPDATE sponsors SET renew_notified_at=? WHERE name=?", (now, name))


def mark_sponsor_expired_notified(db_path: str, name: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute("UPDATE sponsors SET expired_notified_at=? WHERE name=?", (now, name))


def find_user_id_by_username(db_path: str, username: str) -> Optional[int]:
    uname = (username or "").lstrip("@").strip()
    if not uname:
        return None
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE username=? COLLATE NOCASE ORDER BY last_seen DESC LIMIT 1",
            (uname,),
        ).fetchone()
    return int(row[0]) if row else None


def remove_sponsor(db_path: str, name: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM sponsors WHERE name=?", (name.strip(),))
        return int(cur.rowcount or 0)


def clear_sponsors(db_path: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM sponsors")
        return int(cur.rowcount or 0)


def get_audio_lang(db_path: str, user_id: int) -> Optional[str]:
    """None = the video's original language."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT audio_lang FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row and row[0] else None


def set_audio_lang(db_path: str, user_id: int, audio_lang: Optional[str]) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, audio_lang) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET audio_lang=excluded.audio_lang
            """,
            (user_id, audio_lang),
        )


# By default a group gets only the content: sticker and preview are turned on on request.
CHAT_MEDIA_DEFAULTS = {"sticker": False, "thumb": False, "video": True, "audio": True, "articles": True}
# A private chat keeps getting everything, as it did before these settings existed.
PRIVATE_MEDIA_DEFAULTS = dict.fromkeys(CHAT_MEDIA_DEFAULTS, True)


def get_chat_settings(db_path: str, chat_id: int) -> dict[str, bool]:
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT {', '.join(CHAT_MEDIA_DEFAULTS)} FROM chat_settings WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not row:
        # Telegram gives users positive ids, groups and channels negative ones.
        return dict(PRIVATE_MEDIA_DEFAULTS if chat_id > 0 else CHAT_MEDIA_DEFAULTS)
    return dict(zip(CHAT_MEDIA_DEFAULTS, map(bool, row)))


def toggle_chat_setting(db_path: str, chat_id: int, key: str) -> dict[str, bool]:
    settings = get_chat_settings(db_path, chat_id)
    settings[key] = not settings[key]
    cols = ", ".join(CHAT_MEDIA_DEFAULTS)
    updates = ", ".join(f"{k}=excluded.{k}" for k in CHAT_MEDIA_DEFAULTS)
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO chat_settings (chat_id, {cols}) VALUES (?{", ?" * len(CHAT_MEDIA_DEFAULTS)})
            ON CONFLICT(chat_id) DO UPDATE SET {updates}
            """,
            (chat_id, *(int(settings[k]) for k in CHAT_MEDIA_DEFAULTS)),
        )
    return settings


def get_kv(db_path: str, key: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_kv(db_path: str, key: str, value: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_article(db_path: str, url: str) -> Optional[tuple[str, str, bool]]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT telegraph_url, title, is_bitcoin FROM articles WHERE url=?", (url,)).fetchone()
    return (row[0], row[1], bool(row[2])) if row else None


def save_article(db_path: str, url: str, telegraph_url: str, title: str, is_bitcoin: bool) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO articles (url, telegraph_url, title, is_bitcoin, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO NOTHING
            """,
            (url, telegraph_url, title, int(is_bitcoin), int(time.time())),
        )


def claim_article_for_hub(db_path: str, url: str) -> bool:
    """True only the first time: the same article reaches the hub once, however often it is shared."""
    with _connect(db_path) as conn:
        return conn.execute("UPDATE articles SET hub_sent=1 WHERE url=? AND hub_sent=0", (url,)).rowcount == 1


def get_cache(db_path: str, yt_id: str) -> Optional[CacheEntry]:
    with _connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT yt_id, title, thumb_file_id, video_file_id, audio_file_id
            FROM cache WHERE yt_id=? AND fmt_v=?
            """,
            (yt_id, CACHE_FORMAT),
        ).fetchone()
    if not row:
        return None
    return {
        "yt_id": row[0],
        "title": row[1] or "",
        "thumb_file_id": row[2] or "",
        "video_file_id": row[3] or "",
        "audio_file_id": row[4] or "",
    }


def upsert_cache(
    db_path: str,
    yt_id: str,
    *,
    title: str,
    thumb_file_id: str,
    video_file_id: str,
    audio_file_id: str,
) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cache (yt_id, title, thumb_file_id, video_file_id, audio_file_id, updated_at, fmt_v)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(yt_id) DO UPDATE SET
                title=excluded.title,
                -- A send without preview (groups) does not wipe the one already stored.
                thumb_file_id=COALESCE(NULLIF(excluded.thumb_file_id, ''), cache.thumb_file_id),
                video_file_id=excluded.video_file_id,
                audio_file_id=excluded.audio_file_id,
                updated_at=excluded.updated_at,
                fmt_v=excluded.fmt_v
            """,
            (yt_id, title, thumb_file_id, video_file_id, audio_file_id, now, CACHE_FORMAT),
        )


def is_whitelisted(db_path: str, user_id: int) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM whitelist WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone()
    return bool(row)


def add_whitelist(db_path: str, user_id: int, username: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO whitelist (user_id, username, added_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                added_at=excluded.added_at
            """,
            (user_id, username or "", now),
        )
        conn.execute("DELETE FROM pending_whitelist WHERE user_id=?", (user_id,))


def remove_whitelist(db_path: str, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))


def is_pending(db_path: str, user_id: int) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_whitelist WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone()
    return bool(row)


def add_pending(db_path: str, user_id: int, username: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO pending_whitelist (user_id, username, requested_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                requested_at=excluded.requested_at
            """,
            (user_id, username or "", now),
        )


def is_known_user(db_path: str, user_id: int) -> bool:
    """Read-only, so it is safe on another bot's database."""
    if not os.path.exists(db_path):
        return False
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            return conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is not None
    except sqlite3.Error:
        return False


def upsert_user(db_path: str, user_id: int, first_name: str, username: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                username=excluded.username,
                last_seen=excluded.last_seen
            """,
            (user_id, first_name, username, now),
        )


def has_downloaded(db_path: str, user_id: int, yt_id: str) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM downloads_log WHERE user_id=? AND yt_id=? LIMIT 1", (user_id, yt_id)
        ).fetchone()
    return row is not None


def log_download(db_path: str, user_id: int, yt_id: str, yt_title: Optional[str] = None, *,
                 chat_id: Optional[int] = None, chat_type: Optional[str] = None, from_cache: bool = False) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO downloads_log (user_id, yt_id, yt_title, downloaded_at, chat_id, chat_type, from_cache)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, yt_id, yt_title or "Video", now, chat_id, chat_type, int(from_cache)),
        )


def get_dashboard_stats(db_path: str, since: int = 0) -> dict:
    # Telegram calls basic groups "group" and the rest "supergroup": for the stats they are one thing.
    where = "CASE WHEN chat_type IN ('group', 'supergroup') THEN 'group' ELSE COALESCE(chat_type, 'unknown') END"
    with _connect(db_path) as conn:
        one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
        by_place = {
            place: (downloads, chats)
            for place, downloads, chats in conn.execute(
                f"""
                SELECT {where}, COUNT(*), COUNT(DISTINCT chat_id)
                FROM downloads_log WHERE downloaded_at >= ? GROUP BY 1
                """,
                (since,),
            )
        }
        return {
            "users_known": one("SELECT COUNT(*) FROM users"),
            "users_active": one("SELECT COUNT(DISTINCT user_id) FROM downloads_log WHERE user_id > 0 AND downloaded_at >= ?", since),
            "users_paying": one("SELECT COUNT(*) FROM ad_free"),
            "downloads": one("SELECT COUNT(*) FROM downloads_log WHERE downloaded_at >= ?", since),
            "from_cache": one("SELECT COUNT(*) FROM downloads_log WHERE from_cache = 1 AND downloaded_at >= ?", since),
            "distinct_videos": one("SELECT COUNT(DISTINCT yt_id) FROM downloads_log WHERE downloaded_at >= ?", since),
            "by_place": by_place,
            "cached_videos": one(
                "SELECT COUNT(*) FROM cache WHERE fmt_v = ? AND video_file_id <> '' AND audio_file_id <> ''", CACHE_FORMAT
            ),
            "subscriptions": one("SELECT COUNT(*) FROM subscriptions"),
            "subscribed_channels": one("SELECT COUNT(DISTINCT channel_url) FROM subscriptions"),
            "top_videos": conn.execute(
                """
                SELECT MAX(yt_title), COUNT(*) FROM downloads_log WHERE downloaded_at >= ?
                GROUP BY yt_id ORDER BY 2 DESC LIMIT 5
                """,
                (since,),
            ).fetchall(),
            "top_requesters": conn.execute(
                """
                SELECT l.user_id, u.first_name, u.username, COUNT(*) FROM downloads_log l
                LEFT JOIN users u ON u.user_id = l.user_id
                WHERE l.downloaded_at >= ? GROUP BY l.user_id ORDER BY 4 DESC LIMIT 5
                """,
                (since,),
            ).fetchall(),
        }


def add_subscription(db_path: str, user_id: int, channel_url: str) -> bool:
    with _connect(db_path) as conn:
        try:
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, channel_url)
                VALUES (?, ?)
                """,
                (user_id, channel_url),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_subscription(db_path: str, user_id: int, channel_url: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM subscriptions WHERE user_id=? AND channel_url=?", 
            (user_id, channel_url)
        )
        return cur.rowcount > 0


def get_channel_title(db_path: str, channel_url: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT channel_title FROM channels_state WHERE channel_url=?", (channel_url,)).fetchone()
    return row[0] if row else None


def get_user_subscriptions(db_path: str, user_id: int) -> list[str]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT channel_url FROM subscriptions WHERE user_id=?", (user_id,)).fetchall()
    return [r[0] for r in rows]

def get_user_subscriptions_with_titles(db_path: str, user_id: int) -> list[tuple[str, str]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.channel_url, cs.channel_title 
            FROM subscriptions s
            LEFT JOIN channels_state cs ON s.channel_url = cs.channel_url
            WHERE s.user_id = ?
            """,
            (user_id,)
        ).fetchall()
    return [(r[0], r[1] or r[0]) for r in rows]


def get_all_subscriptions(db_path: str) -> list[tuple[int, str]]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT user_id, channel_url FROM subscriptions").fetchall()
    return rows


def get_distinct_subscribed_channels(db_path: str) -> list[str]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT DISTINCT channel_url FROM subscriptions").fetchall()
    return [r[0] for r in rows]


def get_channel_state(db_path: str, channel_url: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_video_id FROM channels_state WHERE channel_url=?", (channel_url,)
        ).fetchone()
    if row:
        return row[0]
    return None


def get_channel_id(db_path: str, channel_url: str) -> Optional[str]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT channel_id FROM channels_state WHERE channel_url=?", (channel_url,)
        ).fetchone()
    return row[0] if row else None


def set_channel_id(db_path: str, channel_url: str, channel_id: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO channels_state (channel_url, channel_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_url) DO UPDATE SET channel_id=excluded.channel_id
            """,
            (channel_url, channel_id, int(time.time())),
        )


def update_channel_state(db_path: str, channel_url: str, last_video_id: Optional[str] = None, channel_title: Optional[str] = None) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        # Get existing values if not provided
        if last_video_id is None or channel_title is None:
            row = conn.execute("SELECT last_video_id, channel_title FROM channels_state WHERE channel_url=?", (channel_url,)).fetchone()
            if row:
                if last_video_id is None: last_video_id = row[0]
                if channel_title is None: channel_title = row[1]

        conn.execute(
            """
            INSERT INTO channels_state (channel_url, last_video_id, channel_title, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_url) DO UPDATE SET
                last_video_id=excluded.last_video_id,
                channel_title=excluded.channel_title,
                updated_at=excluded.updated_at
            """,
            (channel_url, last_video_id, channel_title, now),
        )

