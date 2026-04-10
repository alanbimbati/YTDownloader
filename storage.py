import os
import sqlite3
import time
from typing import Optional, TypedDict


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
                added_at INTEGER NOT NULL
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
            CREATE TABLE IF NOT EXISTS pending_forwards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                msg_ids TEXT NOT NULL
            )
            """
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
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sponsors ORDER BY added_at ASC, name ASC").fetchall()
    return [r[0] for r in rows if r and r[0]]


def add_sponsor(db_path: str, name: str) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sponsors (name, added_at)
            VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET
                added_at=excluded.added_at
            """,
            (name.strip(), now),
        )


def remove_sponsor(db_path: str, name: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM sponsors WHERE name=?", (name.strip(),))
        return int(cur.rowcount or 0)


def clear_sponsors(db_path: str) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM sponsors")
        return int(cur.rowcount or 0)


def get_cache(db_path: str, yt_id: str) -> Optional[CacheEntry]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT yt_id, title, thumb_file_id, video_file_id, audio_file_id FROM cache WHERE yt_id=?",
            (yt_id,),
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
            INSERT INTO cache (yt_id, title, thumb_file_id, video_file_id, audio_file_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(yt_id) DO UPDATE SET
                title=excluded.title,
                thumb_file_id=excluded.thumb_file_id,
                video_file_id=excluded.video_file_id,
                audio_file_id=excluded.audio_file_id,
                updated_at=excluded.updated_at
            """,
            (yt_id, title, thumb_file_id, video_file_id, audio_file_id, now),
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


def log_download(db_path: str, user_id: int, yt_id: str, yt_title: Optional[str] = None) -> None:
    now = int(time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO downloads_log (user_id, yt_id, yt_title, downloaded_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, yt_id, yt_title or "Video", now),
        )


def get_report_stats(db_path: str) -> dict:
    with _connect(db_path) as conn:
        total_downloads = conn.execute("SELECT COUNT(*) FROM downloads_log").fetchone()[0]
        unique_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM downloads_log").fetchone()[0]
        recent = conn.execute(
            """
            SELECT l.user_id, l.yt_id, l.yt_title, l.downloaded_at, u.first_name, u.username
            FROM downloads_log l
            LEFT JOIN users u ON l.user_id = u.user_id
            ORDER BY l.downloaded_at DESC LIMIT 10
            """
        ).fetchall()
        
        subs_count = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    
    return {
        "total_downloads": total_downloads,
        "unique_users": unique_users,
        "recent_downloads": recent,
        "total_subscriptions": subs_count,
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

