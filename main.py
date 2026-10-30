import os
import re
import time
import html
import mimetypes
import subprocess
from typing import Any, Optional
import itertools
import queue
import threading
import xml.etree.ElementTree as ET

import requests
import telebot
import yt_dlp
from telebot import apihelper, types

import articles
import storage
from config import (
    AD_FREE_PAYEE,
    AD_FREE_PRICE_SATS,
    ADMIN_CONTACT,
    ADMIN_USER_ID,
    API_BASE_URL,
    BITCOIN_CHANNEL,
    BITCOIN_HUB,
    BOT_TOKEN,
    HANDOFF_DB,
    MOVED_TO,
    COOKIES_FILE,
    DB_PATH,
    MAX_UPLOAD_BYTES,
)


DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _ydl(opts: dict) -> yt_dlp.YoutubeDL:
    # Read on every call: cookies expire and get replaced without restarting the bot.
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts = {**opts, "cookiefile": COOKIES_FILE}
    return yt_dlp.YoutubeDL(opts)


# Two workers can get the same video (subscribers of the same channel, a link sent twice):
# they would write the same files and one's cleanup would delete the other's pieces.
_video_locks: dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


def _video_lock(yt_id: str) -> threading.Lock:
    with _video_locks_guard:
        return _video_locks.setdefault(yt_id, threading.Lock())


_bot_check_alerted_at = 0.0


def _alert_bot_check() -> None:
    global _bot_check_alerted_at
    if time.time() - _bot_check_alerted_at < 6 * 3600:
        return
    _bot_check_alerted_at = time.time()
    state = "scaduti o non validi" if os.path.isfile(COOKIES_FILE) else f"assenti ({COOKIES_FILE})"
    try:
        bot.send_message(
            ADMIN_USER_ID,
            f"⚠️ YouTube chiede la verifica anti-bot: i cookie sono {state}. "
            "Esportali da un browser loggato e copiali nel container.",
        )
    except Exception as e:
        print(f"Avviso anti-bot all'admin fallito: {e}")

storage.init_db(DB_PATH)
storage.add_whitelist(DB_PATH, ADMIN_USER_ID, "admin")

# Upload grossi: più tolleranza su timeout e retry.
# Nota: il retry interno di pyTelegramBotAPI riusa lo stesso file-handle e può inviare 0 byte
# nei tentativi successivi. Gestiamo i retry noi per gli upload.
apihelper.RETRY_ON_ERROR = False
apihelper.MAX_RETRIES = 0
apihelper.CONNECT_TIMEOUT = 30
apihelper.READ_TIMEOUT = 60 * 15
apihelper.API_URL = f"{API_BASE_URL}/bot{{0}}/{{1}}"
apihelper.FILE_URL = f"{API_BASE_URL}/file/bot{{0}}/{{1}}"

bot = telebot.TeleBot(BOT_TOKEN)

# Nota: sui server ufficiali il limite di upload documentato è molto più basso rispetto al Local Bot API Server.
OFFICIAL_UPLOAD_LIMIT_BYTES = 50 * 1024 * 1024 - 2 * 1024 * 1024
EFFECTIVE_MAX_UPLOAD_BYTES = (
    min(MAX_UPLOAD_BYTES, OFFICIAL_UPLOAD_LIMIT_BYTES)
    if API_BASE_URL.startswith("https://api.telegram.org")
    else MAX_UPLOAD_BYTES
)

TG_API_BASE = f"{API_BASE_URL}/bot{BOT_TOKEN}"

print(f"Telegram API base: {API_BASE_URL}")
print(f"Max upload bytes (requested): {MAX_UPLOAD_BYTES}")
print(f"Max upload bytes (effective): {EFFECTIVE_MAX_UPLOAD_BYTES}")

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
SPONSOR_HEADER = "Consigliati:"
# Teniamo corto il blocco sponsor per non mangiare il testo nei caption.
SPONSOR_BLOCK_MAX = 300


def _sponsor_block(max_chars: int) -> str:
    block = SPONSOR_HEADER
    for name in storage.list_sponsors(DB_PATH):
        name = (name or "").strip()
        if not name:
            continue
        candidate = f"{block}\n• {name}"
        if len(candidate) > max_chars:
            break
        block = candidate
    return "" if block == SPONSOR_HEADER else block


def _is_ad_free(chat_id: Optional[int]) -> bool:
    # L'admin no: i suoi messaggi vengono copiati sul canale, che deve restare con gli sponsor.
    if chat_id is None or chat_id == ADMIN_USER_ID:
        return False
    try:
        return storage.is_ad_free(DB_PATH, int(chat_id))
    except (TypeError, ValueError):
        return False


def _moved_notice() -> str:
    return f"📢 Ci siamo spostati su {MOVED_TO}: aprilo e premi Avvia." if MOVED_TO else ""


def _append_sponsor(text: str, *, limit: int, for_chat: Optional[int] = None) -> str:
    base = (text or "").strip()
    if SPONSOR_HEADER.lower() in base.lower() or (MOVED_TO and MOVED_TO in base):
        return base[:limit]

    sponsor = "" if _is_ad_free(for_chat) else _sponsor_block(min(SPONSOR_BLOCK_MAX, limit))
    # The notice is not an ad: ad-free users get it too.
    suffix = "\n\n".join(p for p in (_moved_notice(), sponsor) if p)
    if not suffix:
        return base[:limit]

    sep = "\n\n" if base else ""
    if len(base) + len(sep) + len(suffix) <= limit:
        return f"{base}{sep}{suffix}".strip()

    max_base = max(0, limit - len(sep) - len(suffix))
    if max_base <= 1:
        base_trunc = ""
    elif len(base) > max_base:
        base_trunc = base[: max_base - 1] + "…"
    else:
        base_trunc = base

    sep2 = "\n\n" if base_trunc else ""
    combined = f"{base_trunc}{sep2}{suffix}".strip()
    if len(combined) > limit:
        combined = combined[: max(0, limit - 1)] + "…"
    return combined


def _media_caption(text: str, *, for_chat: Optional[int] = None, source_url: str = "",
                   limit: int = CAPTION_LIMIT) -> str:
    """HTML: the video title can contain < > &, so it is escaped before the link is added."""
    source = f'<a href="{html.escape(source_url, quote=True)}">Fonte</a>' if source_url else ""
    sponsor = "" if _is_ad_free(for_chat) else _sponsor_block(min(SPONSOR_BLOCK_MAX, limit))
    sponsor = "\n\n".join(p for p in (_moved_notice(), sponsor) if p)

    # Telegram's limit counts the visible text, not the tags.
    tail = ("\n\nFonte" if source else "") + (f"\n\n{sponsor}" if sponsor else "")
    base = (text or "").strip()
    room = limit - len(tail)
    if len(base) > room:
        base = base[: max(0, room - 1)] + "…" if room > 1 else ""

    out = html.escape(base)
    if source:
        out = f"{out}\n\n{source}" if out else source
    if sponsor:
        out = f"{out}\n\n{html.escape(sponsor)}" if out else html.escape(sponsor)
    return out


def _get_msg_id(m: Any) -> Optional[int]:
    if m is None: return None
    if isinstance(m, dict):
        return m.get("message_id")
    return getattr(m, "message_id", None)


def _tg_post(method: str, data: dict, files: Optional[dict] = None, *, timeout_s: int) -> dict:
    url = f"{TG_API_BASE}/{method}"
    resp = requests.post(url, data=data, files=files, timeout=(apihelper.CONNECT_TIMEOUT, timeout_s))
    try:
        payload = resp.json()
    except Exception:
        raise RuntimeError(f"Telegram API non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")
    if not payload.get("ok"):
        raise RuntimeError(
            f"Telegram API error {payload.get('error_code')}: {payload.get('description')}"
        )
    return payload["result"]


def _guess_mime(file_path: str, default: str) -> str:
    mt, _ = mimetypes.guess_type(file_path)
    return mt or default


def _tg_send_video_file(
    chat_id: int,
    file_path: str,
    caption: str,
    *,
    title: str = "video",
    thumb_path: Optional[str] = None,
    duration: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> dict:
    filename = f"{_safe_filename(title)}{os.path.splitext(file_path)[1] or '.mp4'}"
    mime = _guess_mime(file_path, "video/mp4")
    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML", "supports_streaming": "true"}
    # Senza questi Telegram deve indovinare il frame di copertina, e spesso ne pesca uno nero.
    if duration:
        data["duration"] = str(int(duration))
    if width:
        data["width"] = str(int(width))
    if height:
        data["height"] = str(int(height))

    with open(file_path, "rb") as f:
        if not f.read(1):
            raise ValueError("File video vuoto (stream).")
        f.seek(0)
        files = {"video": (filename, f, mime)}
        thumb_f = None
        try:
            if thumb_path and os.path.exists(thumb_path):
                thumb_f = open(thumb_path, "rb")
                files["thumbnail"] = ("thumb.jpg", thumb_f, "image/jpeg")
            return _tg_post("sendVideo", data, files=files, timeout_s=60 * 20)
        finally:
            if thumb_f:
                thumb_f.close()


def _tg_send_document_file(chat_id: int, file_path: str, caption: str, title: str = "") -> dict:
    ext = os.path.splitext(file_path)[1]
    filename = f"{_safe_filename(title)}{ext}" if title else (os.path.basename(file_path) or "file.bin")
    mime = _guess_mime(file_path, "application/octet-stream")
    with open(file_path, "rb") as f:
        if not f.read(1):
            raise ValueError("File documento vuoto (stream).")
        f.seek(0)
        return _tg_post(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
            files={"document": (filename, f, mime)},
            timeout_s=60 * 20,
        )


def _tg_send_audio_file(
    chat_id: int,
    file_path: str,
    caption: str,
    title: str,
    *,
    thumb_path: Optional[str] = None,
    duration: Optional[int] = None,
    performer: Optional[str] = None,
) -> dict:
    ext = os.path.splitext(file_path)[1].lower() or ".mp3"
    # Il nome che il telefono propone al salvataggio: deve avere l'estensione giusta.
    filename = f"{_safe_filename(title)}{ext}"
    mime = "audio/mpeg" if ext == ".mp3" else _guess_mime(file_path, "audio/mp4")
    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML", "title": title}
    if duration:
        data["duration"] = str(int(duration))
    if performer:
        data["performer"] = performer[:64]

    with open(file_path, "rb") as f:
        if not f.read(1):
            raise ValueError("File audio vuoto (stream).")
        f.seek(0)
        files = {"audio": (filename, f, mime)}
        thumb_f = None
        try:
            if thumb_path and os.path.exists(thumb_path):
                thumb_f = open(thumb_path, "rb")
                files["thumbnail"] = ("thumb.jpg", thumb_f, "image/jpeg")
            return _tg_post("sendAudio", data, files=files, timeout_s=60 * 20)
        finally:
            if thumb_f:
                thumb_f.close()


def _safe_filename(title: str, max_len: int = 60) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:max_len].strip() or "media"


def _tg_send_photo(chat_id: int, photo: str, caption: str) -> dict:
    # photo può essere URL oppure "attach://..." (non usato qui). Se serve file locale, usare sendPhoto con files.
    return _tg_post(
        "sendPhoto",
        {"chat_id": str(chat_id), "photo": photo, "caption": caption, "parse_mode": "HTML"},
        timeout_s=60,
    )


def _tg_send_photo_file(chat_id: int, file_path: str, caption: str, title: str = "") -> dict:
    filename = f"{_safe_filename(title)}.jpg" if title else (os.path.basename(file_path) or "photo.jpg")
    mime = _guess_mime(file_path, "image/jpeg")
    with open(file_path, "rb") as f:
        if not f.read(1):
            raise ValueError("File foto vuoto (stream).")
        f.seek(0)
        return _tg_post(
            "sendPhoto",
            {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"},
            files={"photo": (filename, f, mime)},
            timeout_s=60,
        )


def _file_id_from_result(result: dict, kind: str) -> str:
    if kind == "photo":
        photos = result.get("photo") or []
        if not photos:
            return ""
        return photos[-1].get("file_id") or ""
    obj = result.get(kind) or {}
    return obj.get("file_id") or ""


# Solo termini che indicano bitcoin in modo esplicito: "blockchain" o "wallet" da soli
# valgono per qualunque cripto e riempirebbero il canale di proposte sbagliate.
BITCOIN_TERMS = (
    "bitcoin", "bitcoiner", "bitcoiners", "btc", "₿",
    "satoshi", "satoshis", "sats", "nakamoto",
    "halving", "hodl", "lightning network", "taproot",
)

_BITCOIN_RE = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(t) for t in BITCOIN_TERMS) + r")(?!\w)",
    re.IGNORECASE,
)


def _bitcoin_terms_in(info: dict) -> list[str]:
    fields = [
        info.get("title") or "",
        info.get("description") or "",
        info.get("channel") or "",
        info.get("uploader") or "",
    ]
    fields += [str(t) for t in (info.get("tags") or [])]
    fields += [str(c) for c in (info.get("categories") or [])]
    found = {m.group(1).lower() for m in _BITCOIN_RE.finditer("\n".join(fields))}
    return sorted(found)


def _publish_to_channel(from_chat_id, msg_ids: list[int], sticker_file_id: Optional[str] = None) -> None:
    try:
        if not sticker_file_id:
            sticker_set = bot.get_sticker_set("BitcoinPodcast")
            if sticker_set.stickers:
                sticker_file_id = sticker_set.stickers[0].file_id
        if sticker_file_id:
            bot.send_sticker(BITCOIN_CHANNEL, sticker_file_id)
    except Exception:
        pass

    for m_id in msg_ids:
        try:
            bot.copy_message(BITCOIN_CHANNEL, from_chat_id, m_id)
        except Exception as e:
            print(f"Pubblicazione sul canale fallita per {m_id}: {e}")


def _propose_channel_publication(message: types.Message, title: str, terms: list[str], msg_ids: list[int]) -> None:
    fwd_id = storage.save_pending_forward(DB_PATH, str(message.chat.id), ",".join(str(m) for m in msg_ids))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Pubblica", callback_data=f"fwd:ok:{fwd_id}"),
        types.InlineKeyboardButton("❌ Scarta", callback_data=f"fwd:no:{fwd_id}"),
    )
    bot.send_message(
        ADMIN_USER_ID,
        f"₿ Video a tema bitcoin: «{title}»\n"
        f"Termini trovati: {', '.join(terms)}\n"
        f"Richiesto da: {_requester(message)[1]}\n\n"
        f"Lo pubblico su {BITCOIN_CHANNEL}?",
        reply_markup=kb,
    )


SUPPORTED_DOMAINS = (
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "twitter.com", "x.com", "reddit.com",
    "pinterest.com", "pinterest.it", "pin.it", "linkedin.com", "lnkd.in",
)

_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|(?<![\w.])(?:" + "|".join(re.escape(d) for d in SUPPORTED_DOMAINS) + r")/\S+",
    re.IGNORECASE,
)


def _is_supported_url(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(domain in t for domain in SUPPORTED_DOMAINS)


def _extract_supported_url(text: str) -> Optional[str]:
    """Il link può stare in mezzo a un messaggio inoltrato: qui si isola dal testo attorno."""
    for raw in _URL_RE.findall(text or ""):
        candidate = raw.strip().rstrip(".,;:!?)]}>»\"'")
        if not _is_supported_url(candidate):
            continue
        return candidate if candidate.lower().startswith("http") else f"https://{candidate}"
    return None


def _link_for(message: types.Message) -> Optional[str]:
    # Any link is taken: a video goes to yt-dlp, which knows far more sites than the listed ones,
    # and anything else is read as an article.
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return None
    for raw in _URL_RE.findall(text):
        candidate = raw.strip().rstrip(".,;:!?)]}>»\"'")
        return candidate if candidate.lower().startswith("http") else f"https://{candidate}"
    for ent in list(message.entities or []) + list(message.caption_entities or []):
        if getattr(ent, "url", ""):
            return ent.url
    return None


def _user_label(user: Any) -> str:
    username = getattr(user, "username", "") or ""
    first_name = getattr(user, "first_name", "") or ""
    if username:
        return f"@{username} (id {user.id})"
    if first_name:
        return f"{first_name} (id {user.id})"
    return f"id {user.id}"


def _requester(message: types.Message) -> tuple[int, str]:
    # Channel posts have no user: the requester is the channel itself.
    if message.from_user:
        return message.from_user.id, _user_label(message.from_user)
    return message.chat.id, f"canale {message.chat.title or message.chat.id}"


def _ensure_authorized(message: types.Message) -> bool:
    # Only private contacts count: the bot can write only to whoever started it.
    if message.from_user and message.chat.type == "private":
        storage.upsert_user(
            DB_PATH, 
            message.from_user.id, 
            message.from_user.first_name or "Utente", 
            message.from_user.username or ""
        )
    # Il bot è ora accessibile a tutti, quindi ritorniamo sempre True
    return True

    user_id = message.from_user.id
    if user_id == ADMIN_USER_ID or storage.is_whitelisted(DB_PATH, user_id):
        return True

    if not storage.is_pending(DB_PATH, user_id):
        storage.add_pending(DB_PATH, user_id, message.from_user.username or "")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton(
                "✅ Approva", callback_data=f"wl:approve:{user_id}"
            ),
            types.InlineKeyboardButton(
                "❌ Rifiuta", callback_data=f"wl:deny:{user_id}"
            ),
        )
        bot.send_message(
            ADMIN_USER_ID,
            _append_sponsor(
                f"Richiesta whitelist: {_user_label(message.from_user)}\nVuoi che questo utente utilizzi il bot?",
                limit=TEXT_LIMIT,
                for_chat=ADMIN_USER_ID,
            ),
            reply_markup=kb,
        )

    bot.reply_to(
        message,
        _append_sponsor(
            "⛔ Non sei autorizzato. Ho inviato una richiesta di approvazione all'admin.",
            limit=TEXT_LIMIT,
            for_chat=message.chat.id,
        ),
    )
    return False

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("wl:"))
def handle_whitelist_callback(call: types.CallbackQuery) -> None:
    try:
        if call.from_user.id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "Non autorizzato.")
            return

        parts = (call.data or "").split(":")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Callback non valida.")
            return

        action, target_s = parts[1], parts[2]
        target_id = int(target_s)

        if action == "approve":
            storage.add_whitelist(DB_PATH, target_id, "")
            bot.send_message(
                target_id,
                _append_sponsor("✅ Sei stato approvato. Ora puoi usare il bot.", limit=TEXT_LIMIT, for_chat=target_id),
            )
            bot.answer_callback_query(call.id, "Approvato.")
            bot.edit_message_text(
                _append_sponsor("✅ Utente approvato.", limit=TEXT_LIMIT, for_chat=call.message.chat.id),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
            return

        if action == "deny":
            bot.send_message(
                target_id,
                _append_sponsor("❌ Richiesta rifiutata.", limit=TEXT_LIMIT, for_chat=target_id),
            )
            bot.answer_callback_query(call.id, "Rifiutato.")
            bot.edit_message_text(
                _append_sponsor("❌ Utente rifiutato.", limit=TEXT_LIMIT, for_chat=call.message.chat.id),
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
            )
            return

        bot.answer_callback_query(call.id, "Azione non valida.")
    except Exception:
        bot.answer_callback_query(call.id, "Errore durante la gestione.")


SPONSOR_PERIOD_DAYS = storage.SPONSOR_PERIOD_S // 86400


def _is_admin(message: types.Message) -> bool:
    if message.from_user and message.from_user.id == ADMIN_USER_ID:
        return True
    bot.reply_to(message, _append_sponsor("Non autorizzato.", limit=TEXT_LIMIT, for_chat=message.chat.id))
    return False


def _fmt_day(ts: int) -> str:
    return time.strftime("%d/%m/%Y", time.localtime(ts))


def _sponsor_line(sponsor: dict, now: int) -> str:
    owner = f"@{sponsor['owner_username']}" if sponsor["owner_username"] else "nessun contatto"
    left = sponsor["expires_at"] - now
    if left <= 0:
        state = f"⚠️ scaduto il {_fmt_day(sponsor['expires_at'])}"
    else:
        state = f"scade il {_fmt_day(sponsor['expires_at'])} (fra {max(1, left // 86400)} gg)"
    return f"• {sponsor['name']} — {owner} — {state}"


def render_sponsor_panel(chat_id: int, message_id_to_edit: Optional[int] = None) -> None:
    now = int(time.time())
    sponsors = storage.list_sponsors_detailed(DB_PATH)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Aggiungi sponsor", callback_data="sp:add"))
    for s in sponsors:
        # Il nome può superare i 64 byte di callback_data: passiamo un token.
        tid = storage.save_token(DB_PATH, s["name"])
        kb.add(types.InlineKeyboardButton(f"⚙️ {s['name'][:40]}", callback_data=f"sp:ed:{tid}"))

    body = "\n".join(_sponsor_line(s, now) for s in sponsors) if sponsors else "Nessuno sponsor configurato."
    text = f"💼 Sponsor (durata {SPONSOR_PERIOD_DAYS} giorni)\n\n{body}\n\nTocca uno sponsor per modificarlo."

    _show(chat_id, text, kb, message_id_to_edit)


def render_sponsor_edit(chat_id: int, name: str, message_id_to_edit: Optional[int] = None) -> None:
    sponsor = storage.get_sponsor(DB_PATH, name)
    if not sponsor:
        render_sponsor_panel(chat_id, message_id_to_edit)
        return

    tid = storage.save_token(DB_PATH, sponsor["name"])
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✏️ Testo mostrato", callback_data=f"sp:txt:{tid}"),
        types.InlineKeyboardButton("👤 Proprietario", callback_data=f"sp:own:{tid}"),
    )
    kb.add(
        types.InlineKeyboardButton(f"🔄 Rinnova {SPONSOR_PERIOD_DAYS} giorni", callback_data=f"sp:renew:{tid}"),
        types.InlineKeyboardButton("🗑️ Togli", callback_data=f"sp:rm:{tid}"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Indietro", callback_data="sp:back"))

    owner = f"@{sponsor['owner_username']}" if sponsor["owner_username"] else "nessuno"
    text = (
        f"⚙️ {sponsor['name']}\n\n"
        f"Mostrato sotto «Consigliati» esattamente così.\n"
        f"Proprietario: {owner}\n"
        f"Scadenza: {_fmt_day(sponsor['expires_at'])}"
    )
    _show(chat_id, text, kb, message_id_to_edit)


def _show(chat_id: int, text: str, kb: types.InlineKeyboardMarkup, message_id_to_edit: Optional[int]) -> None:
    if message_id_to_edit:
        try:
            bot.edit_message_text(text, chat_id, message_id_to_edit, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)


@bot.message_handler(commands=["sponsor", "sponsor_list"])
def handle_sponsor_panel(message: types.Message) -> None:
    if not _is_admin(message):
        return
    render_sponsor_panel(message.chat.id)


@bot.message_handler(func=lambda m: m.text == "💼 Sponsor")
def handle_btn_sponsor(message: types.Message) -> None:
    handle_sponsor_panel(message)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("sp:"))
def handle_sponsor_callbacks(call: types.CallbackQuery) -> None:
    try:
        if call.from_user.id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "Solo l'admin gestisce gli sponsor.")
            return

        parts = (call.data or "").split(":")
        action = parts[1]
        chat_id = call.message.chat.id
        msg_id = _get_msg_id(call.message)

        if action == "back":
            bot.answer_callback_query(call.id)
            render_sponsor_panel(chat_id, msg_id)
            return

        if action == "add":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                chat_id,
                "Come si chiama lo sponsor?\n"
                "Scrivilo esattamente come vuoi che appaia (es. @IlBarattoloBot).\n"
                "(/annulla per uscire)",
                reply_markup=types.ForceReply(),
            )
            bot.register_next_step_handler(msg, _sponsor_ask_owner)
            return

        name = storage.get_token(DB_PATH, int(parts[2]))
        if not name or not storage.get_sponsor(DB_PATH, name):
            bot.answer_callback_query(call.id, "Sponsor non più presente, riapri il pannello.")
            render_sponsor_panel(chat_id, msg_id)
            return

        if action == "ed":
            bot.answer_callback_query(call.id)
            render_sponsor_edit(chat_id, name, msg_id)
            return

        if action == "txt":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                chat_id,
                f"Come deve apparire «{name}» sotto «Consigliati»?\n"
                "Scrivi il testo esatto (es. @IlBarattoloBot).\n(/annulla per uscire)",
                reply_markup=types.ForceReply(),
            )
            bot.register_next_step_handler(msg, _sponsor_rename, name)
            return

        if action == "own":
            bot.answer_callback_query(call.id)
            msg = bot.send_message(
                chat_id,
                f"Username Telegram del proprietario di «{name}»?\n"
                "Serve per avvisarlo del rinnovo. Scrivi - per toglierlo.\n(/annulla per uscire)",
                reply_markup=types.ForceReply(),
            )
            bot.register_next_step_handler(msg, _sponsor_set_owner, name)
            return

        # "renewn" arriva da un avviso di scadenza: lì confermiamo sul posto invece di aprire il pannello.
        if action in ("renew", "renewn"):
            expires_at = storage.renew_sponsor(DB_PATH, name)
            if expires_at is None:
                bot.answer_callback_query(call.id, "Sponsor non più presente.")
                return
            bot.answer_callback_query(call.id, f"Rinnovato fino al {_fmt_day(expires_at)}")
            _notify_sponsor_owner(
                storage.get_sponsor(DB_PATH, name) or {},
                f"✅ Il tuo spazio «{name}» è stato rinnovato fino al {_fmt_day(expires_at)}.",
            )
            if action == "renewn":
                try:
                    bot.edit_message_text(f"✅ «{name}» rinnovato fino al {_fmt_day(expires_at)}.", chat_id, msg_id)
                except Exception:
                    pass
                return
            render_sponsor_edit(chat_id, name, msg_id)
            return

        if action == "rm":
            storage.remove_sponsor(DB_PATH, name)
            bot.answer_callback_query(call.id, f"Rimosso: {name}")
            render_sponsor_panel(chat_id, msg_id)
            return

        bot.answer_callback_query(call.id, "Azione non valida.")
    except Exception as e:
        try:
            bot.answer_callback_query(call.id, f"Errore: {e}")
        except Exception:
            pass


def _sponsor_step_allowed(message: types.Message) -> bool:
    return bool(message.from_user) and message.from_user.id == ADMIN_USER_ID


def _sponsor_cancelled(message: types.Message) -> bool:
    if (message.text or "").strip().lower() != "/annulla":
        return False
    bot.send_message(message.chat.id, "Operazione annullata.", reply_markup=main_menu_keyboard(ADMIN_USER_ID))
    return True


def _sponsor_rename(message: types.Message, name: str) -> None:
    if not _sponsor_step_allowed(message) or _sponsor_cancelled(message):
        return

    new_name = (message.text or "").strip()
    if not new_name or len(new_name) > 64:
        bot.send_message(message.chat.id, "Testo mancante o troppo lungo (max 64).")
        render_sponsor_edit(message.chat.id, name)
        return

    if new_name != name and not storage.rename_sponsor(DB_PATH, name, new_name):
        bot.send_message(message.chat.id, f"Esiste già uno sponsor chiamato «{new_name}».")
        render_sponsor_edit(message.chat.id, name)
        return

    bot.send_message(message.chat.id, f"✅ Ora sotto «Consigliati» appare: {new_name}")
    render_sponsor_edit(message.chat.id, new_name)


def _sponsor_set_owner(message: types.Message, name: str) -> None:
    if not _sponsor_step_allowed(message) or _sponsor_cancelled(message):
        return

    raw = (message.text or "").strip()
    owner = "" if raw in {"-", ""} else raw.lstrip("@").strip()
    storage.set_sponsor_owner(DB_PATH, name, owner)
    bot.send_message(message.chat.id, _owner_feedback(owner))
    render_sponsor_edit(message.chat.id, name)


def _owner_feedback(owner: str) -> str:
    if not owner:
        return "Nessun proprietario: del rinnovo avviserò solo te."
    if storage.find_user_id_by_username(DB_PATH, owner) is None:
        return f"@{owner} non ha mai scritto al bot: potrò avvisarlo solo dopo che lo avrà fatto."
    return f"Avviserò @{owner} una settimana prima della scadenza."


def _sponsor_ask_owner(message: types.Message) -> None:
    if not _sponsor_step_allowed(message) or _sponsor_cancelled(message):
        return

    name = (message.text or "").strip()
    if not name:
        bot.send_message(message.chat.id, "Nome mancante, riapri il pannello.")
        return
    if len(name) > 64:
        bot.send_message(message.chat.id, "Nome troppo lungo (max 64). Riapri il pannello e riprova.")
        return

    msg = bot.send_message(
        message.chat.id,
        f"Qual è l'username Telegram del proprietario di «{name}»?\n"
        "Serve per avvisarlo del rinnovo (es. @mario). Scrivi - se non ce l'hai.",
        reply_markup=types.ForceReply(),
    )
    bot.register_next_step_handler(msg, _sponsor_save, name)


def _sponsor_save(message: types.Message, name: str) -> None:
    if not _sponsor_step_allowed(message) or _sponsor_cancelled(message):
        return

    raw = (message.text or "").strip()
    owner = "" if raw in {"-", ""} else raw.lstrip("@").strip()
    expires_at = storage.add_sponsor(DB_PATH, name, owner)

    bot.send_message(
        message.chat.id,
        f"✅ «{name}» è tra i Consigliati fino al {_fmt_day(expires_at)}.\n{_owner_feedback(owner)}",
        reply_markup=main_menu_keyboard(ADMIN_USER_ID),
    )
    render_sponsor_panel(message.chat.id)


@bot.message_handler(commands=["sponsor_add"])
def handle_sponsor_add(message: types.Message) -> None:
    if not _is_admin(message):
        return

    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        bot.reply_to(
            message,
            _append_sponsor("Uso: /sponsor_add Nome Sponsor [@username_proprietario]", limit=TEXT_LIMIT, for_chat=message.chat.id),
        )
        return

    args = raw[1].strip().split()
    owner = args.pop().lstrip("@") if len(args) > 1 and args[-1].startswith("@") else ""
    name = " ".join(args)
    if not name or len(name) > 64:
        bot.reply_to(message, _append_sponsor("Nome mancante o troppo lungo (max 64).", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return

    expires_at = storage.add_sponsor(DB_PATH, name, owner)
    bot.reply_to(
        message,
        _append_sponsor(f"Aggiunto sponsor: {name} (fino al {_fmt_day(expires_at)})", limit=TEXT_LIMIT, for_chat=message.chat.id),
    )


@bot.message_handler(commands=["sponsor_remove", "sponsor_del"])
def handle_sponsor_remove(message: types.Message) -> None:
    if not _is_admin(message):
        return

    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        bot.reply_to(
            message,
            _append_sponsor("Uso: /sponsor_remove Nome Sponsor", limit=TEXT_LIMIT, for_chat=message.chat.id),
        )
        return

    name = raw[1].strip()
    removed = storage.remove_sponsor(DB_PATH, name)
    if removed:
        bot.reply_to(message, _append_sponsor(f"Rimosso sponsor: {name}", limit=TEXT_LIMIT, for_chat=message.chat.id))
    else:
        bot.reply_to(message, _append_sponsor(f"Sponsor non trovato: {name}", limit=TEXT_LIMIT, for_chat=message.chat.id))


@bot.message_handler(commands=["sponsor_clear"])
def handle_sponsor_clear(message: types.Message) -> None:
    if not _is_admin(message):
        return

    removed = storage.clear_sponsors(DB_PATH)
    bot.reply_to(message, _append_sponsor(f"Lista sponsor svuotata ({removed}).", limit=TEXT_LIMIT, for_chat=message.chat.id))


def _notify_sponsor_owner(sponsor: dict, text: str) -> bool:
    user_id = storage.find_user_id_by_username(DB_PATH, sponsor.get("owner_username") or "")
    if not user_id:
        return False
    try:
        bot.send_message(user_id, text)
        return True
    except Exception as e:
        print(f"Avviso sponsor a {sponsor.get('owner_username')} fallito: {e}")
        return False


def _renew_keyboard(name: str) -> types.InlineKeyboardMarkup:
    tid = storage.save_token(DB_PATH, name)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(f"🔄 Rinnova {SPONSOR_PERIOD_DAYS} giorni", callback_data=f"sp:renewn:{tid}"))
    return kb


def _sponsor_renewal_hint() -> str:
    return f"\nPer rinnovare scrivi a @{ADMIN_CONTACT}." if ADMIN_CONTACT else ""


def _check_sponsor_deadlines() -> None:
    for s in storage.sponsors_to_warn(DB_PATH, storage.SPONSOR_WARN_S):
        days = max(1, (s["expires_at"] - int(time.time())) // 86400)
        owner = f"@{s['owner_username']}" if s["owner_username"] else "nessun proprietario indicato"
        reached = _notify_sponsor_owner(
            s,
            f"⏰ Il tuo spazio «{s['name']}» tra i Consigliati del bot scade il "
            f"{_fmt_day(s['expires_at'])} (fra {days} giorni).{_sponsor_renewal_hint()}",
        )
        bot.send_message(
            ADMIN_USER_ID,
            f"⏰ Lo sponsor «{s['name']}» scade il {_fmt_day(s['expires_at'])} (fra {days} giorni).\n"
            f"Proprietario: {owner}"
            + ("" if reached or not s["owner_username"] else " — non raggiungibile, non ha mai scritto al bot"),
            reply_markup=_renew_keyboard(s["name"]),
        )
        storage.mark_sponsor_warned(DB_PATH, s["name"])

    for s in storage.sponsors_just_expired(DB_PATH):
        _notify_sponsor_owner(
            s,
            f"🔚 Il tuo spazio «{s['name']}» tra i Consigliati è scaduto il "
            f"{_fmt_day(s['expires_at'])} e non viene più mostrato.{_sponsor_renewal_hint()}",
        )
        bot.send_message(
            ADMIN_USER_ID,
            f"🔚 Lo sponsor «{s['name']}» è scaduto il {_fmt_day(s['expires_at'])}: non compare più tra i Consigliati.",
            reply_markup=_renew_keyboard(s["name"]),
        )
        storage.mark_sponsor_expired_notified(DB_PATH, s["name"])


def watch_sponsor_deadlines() -> None:
    while True:
        try:
            _check_sponsor_deadlines()
        except Exception as e:
            print(f"Errore watcher sponsor: {e}")
        time.sleep(6 * 3600)


def _format_size(fmt: dict) -> Optional[int]:
    size = fmt.get("filesize")
    if isinstance(size, int) and size > 0:
        return size
    size = fmt.get("filesize_approx")
    if isinstance(size, int) and size > 0:
        return size
    return None


def _format_size_or_estimate(fmt: dict, duration_s: Optional[float]) -> Optional[int]:
    size = _format_size(fmt)
    if size is not None:
        return size

    if not duration_s:
        return None

    tbr = fmt.get("tbr")
    if isinstance(tbr, (int, float)) and tbr > 0:
        # tbr è in Kbps circa.
        return int(float(duration_s) * (float(tbr) * 1000.0 / 8.0))

    abr = fmt.get("abr")
    if isinstance(abr, (int, float)) and abr > 0:
        return int(float(duration_s) * (float(abr) * 1000.0 / 8.0))

    return None


def _video_quality_key(fmt: dict) -> tuple:
    height = fmt.get("height") or 0
    fps = fmt.get("fps") or 0
    tbr = fmt.get("tbr") or 0
    vbr = fmt.get("vbr") or 0
    return (height, fps, tbr, vbr)


# Languages offered in ⚙️ Impostazioni; None = the video's original language.
AUDIO_LANGS = {"it": "🇮🇹 Italiano", "en": "🇬🇧 English", "es": "🇪🇸 Español", "fr": "🇫🇷 Français"}


def _track_lang(fmt: dict) -> str:
    return (fmt.get("language") or "").split("-")[0].lower()


def _audio_rank(fmt: dict, audio_lang: Optional[str]) -> tuple:
    wanted = 1 if audio_lang and _track_lang(fmt) == audio_lang else 0
    return (wanted,) + _audio_quality_key(fmt)


def _dub_lang_for(info: dict, audio_lang: Optional[str]) -> Optional[str]:
    """The language to ask yt-dlp for, or None when the original will do.

    Also None when the chosen language already is the original or the video has no such dub:
    that way there is a single cache entry for everyone who gets the same track.
    """
    if not audio_lang:
        return None
    tracks = [
        f for f in info.get("formats") or []
        if f.get("vcodec") == "none" and f.get("acodec") != "none" and _track_lang(f) == audio_lang
    ]
    if not tracks or any((f.get("language_preference") or 0) >= 10 for f in tracks):
        return None
    return audio_lang


def _audio_quality_key(fmt: dict) -> tuple:
    # With YouTube's automatic dubbing a video has several audio tracks, often at the same
    # bitrate: the original language (language_preference 10, dubs -1) must win over quality.
    lang = fmt.get("language_preference") or 0
    abr = fmt.get("abr") or 0
    tbr = fmt.get("tbr") or 0
    asr = fmt.get("asr") or 0
    return (lang, abr, tbr, asr)


def _select_video_format_under_limit(info: dict, limit_bytes: int, audio_lang: Optional[str] = None) -> str:
    formats = info.get("formats") or []
    duration_s = info.get("duration")
    # The download extracts again and YouTube can answer with different ids (e.g. 140-1 instead of 140
    # on videos with several audio languages): the chosen id is always followed by a generic fallback.
    fallback = _fallback_video_format(limit_bytes, audio_lang)

    progressive = [
        f
        for f in formats
        if f.get("vcodec") != "none"
        and f.get("acodec") != "none"
        and (f.get("ext") == "mp4")
    ]
    progressive_ok = [
        f
        for f in progressive
        if (
            _format_size_or_estimate(f, duration_s) is not None
            and _format_size_or_estimate(f, duration_s) <= limit_bytes
        )
    ]
    # Formats with built-in audio carry only the original track.
    if progressive_ok and not audio_lang:
        best = max(progressive_ok, key=_video_quality_key)
        return f"{best['format_id']}/{fallback}"

    video_only = [
        f
        for f in formats
        if f.get("vcodec") != "none"
        and f.get("acodec") == "none"
        and (f.get("ext") == "mp4")
    ]
    audio_only = [
        f
        for f in formats
        if f.get("vcodec") == "none"
        and f.get("acodec") != "none"
        and (f.get("ext") in {"m4a", "mp4"})
    ]

    video_only_sorted = sorted(video_only, key=_video_quality_key, reverse=True)
    audio_only_sorted = sorted(audio_only, key=lambda f: _audio_rank(f, audio_lang), reverse=True)

    for v in video_only_sorted:
        vsize = _format_size_or_estimate(v, duration_s)
        if vsize is None:
            continue
        remaining = limit_bytes - vsize
        if remaining <= 0:
            continue
        best_audio = None
        for a in audio_only_sorted:
            asize = _format_size_or_estimate(a, duration_s)
            if asize is None:
                continue
            if asize <= remaining:
                best_audio = a
                break
        if best_audio:
            return f"{v['format_id']}+{best_audio['format_id']}/{fallback}"

    return fallback


def _fallback_video_format(limit_bytes: int, audio_lang: Optional[str] = None) -> str:
    # Fallback conservativo: limita l'altezza per aumentare le chance di stare sotto il limite.
    limit_mb = limit_bytes / (1024 * 1024)
    if limit_mb <= 60:
        cap = 360
    elif limit_mb <= 120:
        cap = 480
    elif limit_mb <= 300:
        cap = 720
    else:
        cap = 1080

    # Without a language filter yt-dlp's sorting already puts the original audio first.
    dubbed = f"bestvideo[ext=mp4][height<={cap}]+bestaudio[ext=m4a][language^={audio_lang}]/" if audio_lang else ""
    return (
        f"{dubbed}bestvideo[ext=mp4][height<={cap}]+bestaudio[ext=m4a]/"
        f"best[ext=mp4][height<={cap}]/best[height<={cap}]"
    )


def _select_audio_format_under_limit(info: dict, limit_bytes: int, audio_lang: Optional[str] = None) -> str:
    formats = info.get("formats") or []
    duration_s = info.get("duration")
    audio_only = [
        f
        for f in formats
        if f.get("vcodec") == "none"
        and f.get("acodec") != "none"
        and (f.get("ext") in {"m4a", "mp4", "webm"})
    ]
    audio_ok = [
        f
        for f in audio_only
        if (
            _format_size_or_estimate(f, duration_s) is not None
            and _format_size_or_estimate(f, duration_s) <= limit_bytes
        )
    ]
    if audio_ok:
        best = max(audio_ok, key=lambda f: _audio_rank(f, audio_lang))
        return f"{best['format_id']}/{_fallback_audio_format(audio_lang)}"
    return _fallback_audio_format(audio_lang)


def _fallback_audio_format(audio_lang: Optional[str]) -> str:
    return f"bestaudio[language^={audio_lang}]/bestaudio/best" if audio_lang else "bestaudio/best"


def _cleanup_partial_downloads(prefix: str) -> None:
    try:
        for name in os.listdir(DOWNLOAD_DIR):
            if not name.startswith(prefix):
                continue
            if name.endswith(".part") or name.endswith(".ytdl"):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, name))
                except OSError:
                    pass
    except FileNotFoundError:
        return


def _describe_partial_downloads(prefix: str) -> str:
    names = []
    for name in os.listdir(DOWNLOAD_DIR):
        if name.startswith(prefix) and (name.endswith(".part") or name.endswith(".ytdl")):
            names.append(name)
    names.sort()
    if not names:
        return ""
    shown = ", ".join(names[:4])
    if len(names) > 4:
        shown += f" (+{len(names) - 4})"
    return shown


def _wait_for_completed_download(prefix: str, *, timeout_s: float = 2.0) -> Optional[str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        matches = []
        for name in os.listdir(DOWNLOAD_DIR):
            if not name.startswith(prefix):
                continue
            if name.endswith(".part") or name.endswith(".ytdl"):
                continue
            matches.append(os.path.join(DOWNLOAD_DIR, name))
        if matches:
            return max(matches, key=lambda p: os.path.getmtime(p))
        time.sleep(0.2)
    return None


def _latest_downloaded_path(prefix: str) -> str:
    matches = []
    for name in os.listdir(DOWNLOAD_DIR):
        if name.startswith(prefix):
            if name.endswith(".part") or name.endswith(".ytdl"):
                continue
            matches.append(os.path.join(DOWNLOAD_DIR, name))
    if not matches:
        partial = _describe_partial_downloads(prefix)
        if partial:
            if API_BASE_URL.startswith("https://api.telegram.org"):
                lim_mb = int(OFFICIAL_UPLOAD_LIMIT_BYTES / (1024 * 1024))
                raise FileNotFoundError(
                    "Download incompleto (rimasti file .part). "
                    f"Probabile video troppo grande per il limite effettivo (~{lim_mb}MB) sui server ufficiali. "
                    "Per ~2GB configura API_BASE_URL verso un Local Bot API Server."
                )
            raise FileNotFoundError("Download incompleto (rimasti file .part). Riprova.")
        raise FileNotFoundError("Download non trovato.")
    return max(matches, key=lambda p: os.path.getmtime(p))


def _download_video(url: str, yt_id: str, audio_lang: Optional[str] = None) -> tuple[str, dict]:
    ydl_preview_opts = {"quiet": True, "noplaylist": True}
    with _ydl(ydl_preview_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    fmt = _select_video_format_under_limit(info, EFFECTIVE_MAX_UPLOAD_BYTES, audio_lang)
    prefix = f"{yt_id}_video."
    _cleanup_partial_downloads(prefix)
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{yt_id}_video.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "format": fmt,
        "merge_output_format": "mp4",
        "max_filesize": EFFECTIVE_MAX_UPLOAD_BYTES,
    }
    with _ydl(ydl_opts) as ydl:
        ydl.extract_info(url, download=True)
    preferred = os.path.join(DOWNLOAD_DIR, f"{yt_id}_video.mp4")
    if os.path.exists(preferred):
        path = preferred
    else:
        # In alcuni casi yt-dlp può lasciare file intermedi più recenti: preferisci comunque mp4.
        candidates = [
            os.path.join(DOWNLOAD_DIR, n)
            for n in os.listdir(DOWNLOAD_DIR)
            if n.startswith(prefix) and n.endswith(".mp4")
        ]
        if candidates:
            path = max(candidates, key=lambda p: os.path.getmtime(p))
        else:
            waited = _wait_for_completed_download(prefix, timeout_s=2.0)
            path = waited if waited else _latest_downloaded_path(prefix)
    if os.path.getsize(path) <= 0:
        raise ValueError("Il file video scaricato è vuoto.")
    if os.path.getsize(path) > EFFECTIVE_MAX_UPLOAD_BYTES:
        if API_BASE_URL.startswith("https://api.telegram.org"):
            raise ValueError(
                "Il video supera il limite upload dei server ufficiali. "
                "Per inviare file grandi (fino a ~2GB) configura un Local Bot API Server (API_BASE_URL)."
            )
        raise ValueError("Il video scaricato supera 2GB.")
    return path, info


def _download_audio(url: str, yt_id: str, info: dict, audio_lang: Optional[str] = None) -> str:
    fmt = _select_audio_format_under_limit(info, EFFECTIVE_MAX_UPLOAD_BYTES, audio_lang)
    prefix = f"{yt_id}_audio."
    _cleanup_partial_downloads(prefix)
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, f"{yt_id}_audio.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "format": fmt,
        "max_filesize": EFFECTIVE_MAX_UPLOAD_BYTES,
        # Il flusso nativo di YouTube è opus/webm: i lettori dei telefoni lo trattano male
        # e non lo salvano come brano. Ricodifichiamo in mp3, con tag e copertina dentro.
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        "writethumbnail": True,
    }
    try:
        with _ydl(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"Conversione mp3 fallita, ripiego sul flusso originale: {e}")
        ydl_opts.pop("postprocessors", None)
        ydl_opts.pop("writethumbnail", None)
        with _ydl(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

    preferred_exts = (".mp3", ".m4a", ".webm", ".mp4", ".aac", ".opus", ".ogg")
    candidates = [
        os.path.join(DOWNLOAD_DIR, n)
        for n in os.listdir(DOWNLOAD_DIR)
        if n.startswith(prefix) and n.endswith(preferred_exts) and not n.endswith(".part")
    ]
    mp3 = [c for c in candidates if c.lower().endswith(".mp3")]
    if mp3:
        path = max(mp3, key=lambda p: os.path.getmtime(p))
    elif candidates:
        path = max(candidates, key=lambda p: os.path.getmtime(p))
    else:
        waited = _wait_for_completed_download(prefix, timeout_s=2.0)
        path = waited if waited else _latest_downloaded_path(prefix)
    if os.path.getsize(path) <= 0:
        raise ValueError("Il file audio scaricato è vuoto.")
    if os.path.getsize(path) > EFFECTIVE_MAX_UPLOAD_BYTES:
        if API_BASE_URL.startswith("https://api.telegram.org"):
            raise ValueError(
                "L'audio supera il limite upload dei server ufficiali. "
                "Per inviare file grandi (fino a ~2GB) configura un Local Bot API Server (API_BASE_URL)."
            )
        raise ValueError("L'audio scaricato supera 2GB.")
    return path


def _send_with_retry(fn, *args, **kwargs):
    def is_transient(exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
            ),
        ):
            return True
        msg = str(exc)
        return any(
            s in msg
            for s in [
                "HTTPSConnectionPool",
                "SSLEOFError",
                "EOF occurred in violation of protocol",
                "Read timed out",
                "Connection reset",
                "RemoteDisconnected",
                "Bad Gateway",
                "Gateway Timeout",
                "502",
                "504",
            ]
        )

    last_exc: Optional[Exception] = None
    for attempt in range(1, 6):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not is_transient(e) or attempt == 5:
                raise
            last_exc = e
            time.sleep(min(10, attempt * 2))
    if last_exc:
        raise last_exc


def _media_tags(title: str, performer: str, source_url: str) -> dict:
    note = BITCOIN_CHANNEL
    sponsors = storage.list_sponsors(DB_PATH)
    if sponsors:
        note += " — Consigliati: " + ", ".join(sponsors)
    if source_url:
        note += f" — {source_url}"

    tags = {"title": title, "album": BITCOIN_CHANNEL, "comment": note, "publisher": BITCOIN_CHANNEL}
    if performer:
        tags["artist"] = performer
        tags["album_artist"] = BITCOIN_CHANNEL
    return tags


def _tag_media(path: str, *, title: str, performer: str = "", source_url: str = "") -> str:
    """Canale e sponsor finiscono nei tag: restano attaccati al file anche fuori da Telegram."""
    ext = os.path.splitext(path)[1].lower()
    tagged = f"{os.path.splitext(path)[0]}_tagged{ext}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-map", "0", "-c", "copy"]
    if ext in (".mp4", ".m4a"):
        # Sposta l'indice in testa: serve anche a far partire l'anteprima senza scaricare tutto.
        cmd += ["-movflags", "+faststart"]
    for key, value in _media_tags(title, performer, source_url).items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(tagged)

    try:
        subprocess.run(cmd, check=True, timeout=60 * 20)
        if os.path.getsize(tagged) > 0:
            os.replace(tagged, path)
    except Exception as e:
        print(f"Tag non scritti su {os.path.basename(path)}: {e}")
        try:
            os.remove(tagged)
        except OSError:
            pass
    return path


def _prepare_thumbnail(thumb_url: str, yt_id: str) -> Optional[str]:
    """Miniatura per sendVideo/sendAudio: Telegram la vuole jpeg, max 320px e sotto i 200KB."""
    if not thumb_url:
        return None
    raw_path = os.path.join(DOWNLOAD_DIR, f"{yt_id}_thumb.jpg")
    small_path = os.path.join(DOWNLOAD_DIR, f"{yt_id}_thumb_small.jpg")
    try:
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) <= 0:
            r = requests.get(thumb_url, timeout=30)
            r.raise_for_status()
            if not r.content:
                return None
            with open(raw_path, "wb") as f:
                f.write(r.content)

        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path,
             "-vf", "scale=320:-2", "-q:v", "6", small_path],
            check=True, timeout=60,
        )
        if os.path.getsize(small_path) < 200 * 1024:
            return small_path
    except Exception as e:
        print(f"Miniatura non preparata: {e}")
    return None


def _send_video_or_document(
    chat_id: int,
    file_path: str,
    caption: str,
    *,
    title: str = "video",
    thumb_path: Optional[str] = None,
    duration: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    source_url: str = "",
):
    caption = _media_caption(caption, for_chat=chat_id, source_url=source_url)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File non trovato: {file_path}")
    size = os.path.getsize(file_path)
    if size <= 0:
        raise ValueError(f"File video vuoto: {file_path}")

    # Usiamo richieste HTTP dirette per avere controllo completo sull'upload.
    last_exc: Optional[Exception] = None
    for attempt in range(1, 6):
        try:
            return _tg_send_video_file(
                chat_id, file_path, caption,
                title=title, thumb_path=thumb_path,
                duration=duration, width=width, height=height,
            )
        except Exception as e:
            last_exc = e
            # Se Telegram dice che il file è vuoto, come fallback prova come documento.
            if "file must be non-empty" in str(e):
                break
            if attempt == 5:
                break
            time.sleep(min(10, attempt * 2))

    for attempt in range(1, 6):
        try:
            return _tg_send_document_file(chat_id, file_path, caption, title)
        except Exception as e:
            last_exc = e
            if attempt == 5:
                break
            time.sleep(min(10, attempt * 2))

    if last_exc:
        raise last_exc


def _send_audio(
    chat_id: int,
    file_path: str,
    caption: str,
    title: str,
    *,
    thumb_path: Optional[str] = None,
    duration: Optional[int] = None,
    performer: Optional[str] = None,
    source_url: str = "",
):
    caption = _media_caption(caption, for_chat=chat_id, source_url=source_url)
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File non trovato: {file_path}")
    size = os.path.getsize(file_path)
    if size <= 0:
        raise ValueError(f"File audio vuoto: {file_path}")

    last_exc: Optional[Exception] = None
    for attempt in range(1, 6):
        try:
            return _tg_send_audio_file(
                chat_id, file_path, caption, title,
                thumb_path=thumb_path, duration=duration, performer=performer,
            )
        except Exception as e:
            last_exc = e
            if attempt == 5:
                break
            time.sleep(min(10, attempt * 2))
    if last_exc:
        raise last_exc


def _send_thumbnail(chat_id: int, thumb_url: str, caption: str, yt_id: str, title: str = "",
                    source_url: str = ""):
    caption = _media_caption(caption, for_chat=chat_id, source_url=source_url)
    # Prova 1: lascia che Telegram scarichi l'URL (più veloce).
    try:
        return _tg_send_photo(chat_id, thumb_url, caption)
    except Exception as e:
        msg = str(e)
        # Se Telegram non riesce a fetchare l'URL (o riceve 0 byte), fallback: scarichiamo noi.
        if "file must be non-empty" not in msg and "failed to get HTTP URL content" not in msg:
            raise

    r = requests.get(thumb_url, timeout=30)
    r.raise_for_status()
    content = r.content or b""
    if len(content) == 0:
        raise ValueError("Thumbnail scaricata vuota.")
    thumb_path = os.path.join(DOWNLOAD_DIR, f"{yt_id}_thumb.jpg")
    with open(thumb_path, "wb") as f:
        f.write(content)
    last_exc: Optional[Exception] = None
    for attempt in range(1, 6):
        try:
            msg = _tg_send_photo_file(chat_id, thumb_path, caption, title)
            return msg, thumb_path
        except Exception as e:
            last_exc = e
            if attempt == 5:
                break
            time.sleep(min(10, attempt * 2))
    if last_exc:
        raise last_exc


def main_menu_keyboard(user_id: Optional[int] = None):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("➕ Nuova Iscrizione"), types.KeyboardButton("📥 Le mie Iscrizioni"))
    kb.add(types.KeyboardButton("📊 Report Statistiche"), types.KeyboardButton("📣 Broadcast"))
    kb.add(types.KeyboardButton("⚙️ Impostazioni"))
    if user_id == ADMIN_USER_ID:
        kb.add(types.KeyboardButton("💼 Sponsor"))
    elif not _is_ad_free(user_id):
        kb.add(types.KeyboardButton("🚫 Togli la pubblicità"))
    return kb


def _sats(n: int) -> str:
    return f"{n:,}".replace(",", ".")


@bot.message_handler(commands=["adfree"])
def handle_ad_free_offer(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return
    if _is_ad_free(message.chat.id):
        bot.reply_to(message, "Hai già la versione senza pubblicità. Grazie!")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Ho pagato", callback_data="af:paid"))
    bot.send_message(
        message.chat.id,
        f"🚫 Niente più «{SPONSOR_HEADER}» nei messaggi del bot.\n\n"
        f"Costo: {_sats(AD_FREE_PRICE_SATS)} sats una tantum, per sempre.\n"
        f"Paga {AD_FREE_PAYEE} e poi tocca il pulsante qui sotto: "
        "ricevo la tua richiesta e sblocco appena confermo il pagamento.",
        reply_markup=kb,
        reply_to_message_id=_get_msg_id(message),
    )


CHAT_MEDIA_LABELS = {"sticker": "🏷 Sticker", "thumb": "🖼 Anteprima", "video": "🎬 Video", "audio": "🎵 Audio"}


def _settings_view(chat_id: int) -> tuple[str, types.InlineKeyboardMarkup]:
    # One panel for private chats and groups alike: in a group the settings belong to the group.
    current = storage.get_audio_lang(DB_PATH, chat_id)
    show = storage.get_chat_settings(DB_PATH, chat_id)
    kb = types.InlineKeyboardMarkup(row_width=2)
    choices = [("orig", "🌐 Originale")] + list(AUDIO_LANGS.items())
    kb.add(*[
        types.InlineKeyboardButton(
            ("✅ " if (code == "orig" and current is None) or code == current else "") + label,
            callback_data=f"cfg:lang:{code}",
        )
        for code, label in choices
    ])
    kb.add(*[
        types.InlineKeyboardButton(("✅ " if show[key] else "❌ ") + label, callback_data=f"cfg:media:{key}")
        for key, label in CHAT_MEDIA_LABELS.items()
    ])
    current_label = AUDIO_LANGS.get(current, "🌐 Originale") if current else "🌐 Originale"
    is_group = chat_id < 0
    text = (
        f"⚙️ Impostazioni{' del gruppo' if is_group else ''}\n\n"
        f"🎧 Lingua dell'audio: {current_label}\n"
        "Molti video YouTube hanno anche tracce doppiate. Se il video non ha la lingua scelta, "
        "arriva quella originale.\n\n"
        f"📦 Cosa {'invio qui' if is_group else 'ricevi'}: tocca per accendere o spegnere."
        + ("\n\nPossono cambiarle solo gli amministratori." if is_group else "")
    )
    return text, kb


def _is_chat_admin(chat_id: int, user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return True
    try:
        return bot.get_chat_member(chat_id, user_id).status in ("creator", "administrator")
    except Exception:
        return False


@bot.message_handler(commands=["config", "impostazioni"])
@bot.message_handler(func=lambda m: m.text == "⚙️ Impostazioni")
@bot.channel_post_handler(commands=["config", "impostazioni"])
def handle_settings(message: types.Message) -> None:
    if message.chat.type != "private":
        # In a channel, or from an anonymous admin, the chat itself is the sender: only its admins can do that.
        by_chat = message.sender_chat is not None and message.sender_chat.id == message.chat.id
        if by_chat or (message.from_user and _is_chat_admin(message.chat.id, message.from_user.id)):
            text, kb = _settings_view(message.chat.id)
            bot.send_message(message.chat.id, text, reply_markup=kb)
        return
    if not _ensure_authorized(message):
        return
    text, kb = _settings_view(message.chat.id)
    bot.send_message(message.chat.id, text, reply_markup=kb)


# "gcfg:" is what the group panels sent before language and media shared one panel.
@bot.callback_query_handler(func=lambda c: (c.data or "").startswith(("cfg:", "gcfg:")))
def handle_settings_callbacks(call: types.CallbackQuery) -> None:
    parts = (call.data or "").split(":")
    if parts[0] == "gcfg":
        parts = ["cfg", "media"] + parts[1:]
    chat_id = call.message.chat.id
    valid = len(parts) == 3 and (
        (parts[1] == "lang" and (parts[2] == "orig" or parts[2] in AUDIO_LANGS))
        or (parts[1] == "media" and parts[2] in CHAT_MEDIA_LABELS)
    )
    if not valid:
        bot.answer_callback_query(call.id)
        return
    if call.message.chat.type != "private" and not _is_chat_admin(chat_id, call.from_user.id):
        bot.answer_callback_query(call.id, "Solo gli amministratori possono cambiarlo.")
        return
    if parts[1] == "lang":
        storage.set_audio_lang(DB_PATH, chat_id, None if parts[2] == "orig" else parts[2])
    else:
        storage.toggle_chat_setting(DB_PATH, chat_id, parts[2])
    bot.answer_callback_query(call.id, "Salvato")
    text, kb = _settings_view(chat_id)
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=kb)
    except Exception:
        pass  # same choice as before: Telegram rejects an identical message


@bot.message_handler(func=lambda m: m.text == "🚫 Togli la pubblicità")
def handle_btn_ad_free(message: types.Message) -> None:
    handle_ad_free_offer(message)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("af:"))
def handle_ad_free_callbacks(call: types.CallbackQuery) -> None:
    try:
        parts = (call.data or "").split(":")
        action = parts[1]

        if action == "paid":
            user = call.from_user
            bot.answer_callback_query(call.id, "Richiesta inviata, ti avviso appena confermo.")
            try:
                bot.edit_message_text(
                    "⏳ Richiesta inviata. Ti avviso appena il pagamento è confermato.",
                    call.message.chat.id,
                    call.message.message_id,
                )
            except Exception:
                pass

            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                types.InlineKeyboardButton("✅ Confermo", callback_data=f"af:ok:{user.id}"),
                types.InlineKeyboardButton("❌ Non risulta", callback_data=f"af:no:{user.id}"),
            )
            bot.send_message(
                ADMIN_USER_ID,
                f"💸 {_user_label(user)} dice di aver pagato "
                f"{_sats(AD_FREE_PRICE_SATS)} sats per togliere la pubblicità.\nHai ricevuto?",
                reply_markup=kb,
            )
            return

        if call.from_user.id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "Non autorizzato.")
            return

        target_id = int(parts[2])
        if action == "ok":
            username = ""
            try:
                username = bot.get_chat(target_id).username or ""
            except Exception:
                pass
            storage.grant_ad_free(DB_PATH, target_id, username)
            bot.answer_callback_query(call.id, "Sbloccato.")
            _edit_or_ignore(call, f"✅ Pubblicità tolta a {target_id}.")
            try:
                bot.send_message(
                    target_id,
                    "✅ Pagamento confermato: niente più pubblicità nei messaggi del bot. Grazie!",
                    reply_markup=main_menu_keyboard(target_id),
                )
            except Exception:
                pass
            return

        if action == "no":
            bot.answer_callback_query(call.id, "Rifiutato.")
            _edit_or_ignore(call, f"❌ Pagamento non confermato per {target_id}.")
            try:
                bot.send_message(target_id, f"❌ Non risulta il pagamento. Scrivi a {AD_FREE_PAYEE}.")
            except Exception:
                pass
            return

        bot.answer_callback_query(call.id, "Azione non valida.")
    except Exception as e:
        try:
            bot.answer_callback_query(call.id, f"Errore: {e}")
        except Exception:
            pass


def _edit_or_ignore(call: types.CallbackQuery, text: str) -> None:
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id)
    except Exception:
        pass


@bot.message_handler(commands=["adfree_add", "adfree_remove", "adfree_list"])
def handle_ad_free_admin(message: types.Message) -> None:
    if not _is_admin(message):
        return

    parts = (message.text or "").split()
    command = parts[0].split("@")[0].lstrip("/")

    if command == "adfree_list":
        rows = storage.list_ad_free(DB_PATH)
        if not rows:
            bot.reply_to(message, "Nessuno ha ancora tolto la pubblicità.")
            return
        lines = "\n".join(
            f"- {uid}" + (f" (@{uname})" if uname else "") + f" dal {_fmt_day(ts)}"
            for uid, uname, ts in rows
        )
        bot.reply_to(message, f"Senza pubblicità:\n{lines}")
        return

    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        bot.reply_to(message, f"Uso: /{command} USER_ID (lo trovi in /report o nella richiesta)")
        return

    target_id = int(parts[1])
    if command == "adfree_add":
        storage.grant_ad_free(DB_PATH, target_id)
        bot.reply_to(message, f"✅ Pubblicità tolta a {target_id}.")
    else:
        removed = storage.revoke_ad_free(DB_PATH, target_id)
        bot.reply_to(message, f"Pubblicità riattivata per {target_id}." if removed else "Non era sbloccato.")

@bot.message_handler(commands=["start", "help"])
def handle_start(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return
    bot.send_message(
        message.chat.id,
        _append_sponsor(
            "Incolla un link qui (supporto Youtube, TikTok, Instagram, Pinterest, LinkedIn, Twitter/X, Reddit!) e scegli cosa scaricare.\n\n"
            "Usa il menu in basso per gestire le iscrizioni o accedere agli strumenti extra.",
            limit=TEXT_LIMIT,
            for_chat=message.chat.id,
        ),
        reply_markup=main_menu_keyboard(message.from_user.id),
        reply_to_message_id=_get_msg_id(message)
    )

@bot.message_handler(func=lambda m: m.text == "➕ Nuova Iscrizione")
def handle_btn_new_sub(message: types.Message) -> None:
    if not _ensure_authorized(message): return
    msg = bot.reply_to(message, _append_sponsor("Incolla qui il link del canale YouTube a cui vuoi iscriverti\n(Oppure invia /annulla per annullare):", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_markup=types.ForceReply())
    bot.register_next_step_handler(msg, process_new_sub)

def process_new_sub(message: types.Message) -> None:
    if not message.text or message.text.lower() == "/annulla":
        bot.reply_to(message, "Operazione annullata.", reply_markup=main_menu_keyboard(message.from_user.id))
        return
    channel_url = _extract_supported_url(message.text or "") or (message.text or "").strip()
    if not _is_supported_url(channel_url):
         bot.reply_to(message, "Formato URL non valido.", reply_markup=main_menu_keyboard(message.from_user.id))
         return
    message.text = f"/sub {channel_url}"
    handle_sub(message)

@bot.message_handler(func=lambda m: m.text == "📥 Le mie Iscrizioni")
def handle_btn_mysubs(message: types.Message) -> None:
    handle_mysubs(message)

@bot.message_handler(func=lambda m: m.text == "📊 Report Statistiche")
def handle_btn_report(message: types.Message) -> None:
    handle_report(message)
    
@bot.message_handler(func=lambda m: m.text == "📣 Broadcast")
def handle_btn_broadcast_init(message: types.Message) -> None:
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, _append_sponsor("Non sei autorizzato.", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return
    bot.reply_to(message, _append_sponsor("Invia il comando `/broadcast Il_tuo_messaggio_qui` per inoltrarlo a tutti.", limit=TEXT_LIMIT, for_chat=message.chat.id))


REPORT_PERIODS = {"1": "Oggi", "7": "7 giorni", "30": "30 giorni", "0": "Sempre"}
REPORT_PLACES = {"private": "👤 Privato", "group": "👥 Gruppi", "channel": "📢 Canali", "unknown": "❔ Senza dettaglio"}


def _report_view(period: str) -> tuple[str, types.InlineKeyboardMarkup]:
    if period == "1":
        since = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    else:
        since = int(time.time()) - int(period) * 86400 if period != "0" else 0
    st = storage.get_dashboard_stats(DB_PATH, since)
    pct = lambda n, d: f"{round(100 * n / d)}%" if d else "–"

    lines = [
        f"📊 <b>Statistiche · {REPORT_PERIODS[period]}</b>",
        "",
        "<b>Utenti</b>",
        f"Attivi nel periodo: {st['users_active']}",
        f"Conosciuti in totale: {st['users_known']}",
        f"Che pagano: {st['users_paying']}",
        "",
        "<b>Download</b>",
        f"Totali: {st['downloads']} ({st['distinct_videos']} video diversi)",
        f"Dalla cache: {st['from_cache']} ({pct(st['from_cache'], st['downloads'])})",
    ]
    for place, label in REPORT_PLACES.items():
        if place in st["by_place"]:
            downloads, chats = st["by_place"][place]
            where = f" in {chats} chat" if place in ("group", "channel") else ""
            lines.append(f"{label}: {downloads}{where}")
    lines += [
        "",
        "<b>Archivio e iscrizioni</b>",
        f"Video in cache: {st['cached_videos']}",
        f"Iscrizioni: {st['subscriptions']} a {st['subscribed_channels']} canali",
    ]
    if st["top_videos"]:
        lines += ["", "<b>Più scaricati</b>"]
        lines += [f"{n}× {html.escape((title or 'Video')[:60], quote=False)}" for title, n in st["top_videos"]]
    if st["top_requesters"]:
        lines += ["", "<b>Chi scarica di più</b>"]
        for uid, first_name, username, n in st["top_requesters"]:
            who = f"@{username}" if username else (first_name or ("canale " if uid < 0 else "id ") + str(uid))
            lines.append(f"{n}× {html.escape(who, quote=False)}")

    kb = types.InlineKeyboardMarkup(row_width=4)
    kb.add(*[
        types.InlineKeyboardButton(("• " if key == period else "") + label, callback_data=f"rep:{key}")
        for key, label in REPORT_PERIODS.items()
    ])
    return "\n".join(lines), kb


@bot.message_handler(commands=["report"])
def handle_report(message: types.Message) -> None:
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, _append_sponsor("Non autorizzato.", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return
    text, kb = _report_view("7")
    bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("rep:"))
def handle_report_period(call: types.CallbackQuery) -> None:
    period = (call.data or "").split(":", 1)[1]
    if call.from_user.id != ADMIN_USER_ID or period not in REPORT_PERIODS:
        bot.answer_callback_query(call.id)
        return
    bot.answer_callback_query(call.id)
    text, kb = _report_view(period)
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass  # same period as before: Telegram rejects an identical message


@bot.message_handler(commands=["broadcast"])
def handle_broadcast(message: types.Message) -> None:
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, _append_sponsor("Non autorizzato.", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return
        
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        bot.reply_to(message, _append_sponsor("Uso: /broadcast Testo del messaggio", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return
        
    msg_text = raw[1].strip()
    users = storage.get_unique_users(DB_PATH)
    success = 0
    for u in users:
        try:
            bot.send_message(u, f"📣 **Annuncio:**\n\n{msg_text}", parse_mode="Markdown")
            success += 1
        except Exception:
            pass
            
    bot.reply_to(message, _append_sponsor(f"✅ Messaggio inviato con successo a {success}/{len(users)} utenti.", limit=TEXT_LIMIT, for_chat=message.chat.id))

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("fwd:"))
def handle_fwd_choice(call: types.CallbackQuery) -> None:
    try:
        if call.from_user.id != ADMIN_USER_ID:
            bot.answer_callback_query(call.id, "Non autorizzato.")
            return
            
        parts = call.data.split(":")
        if len(parts) != 3:
            return
        action, fwd_id = parts[1], int(parts[2])
        
        fwd_data = storage.get_pending_forward(DB_PATH, fwd_id)
        if not fwd_data:
            bot.answer_callback_query(call.id, "Approvazione già gestita o scaduta.")
            try:
                bot.delete_message(call.message.chat.id, _get_msg_id(call.message))
            except Exception:
                pass
            return
            
        chat_id, msg_ids_str = fwd_data
        msg_ids = [int(m) for m in msg_ids_str.split(",") if m]
        
        if action == "ok":
            bot.answer_callback_query(call.id, f"Pubblicato su {BITCOIN_CHANNEL}!")
            _publish_to_channel(chat_id, msg_ids)
            try:
                bot.edit_message_text(f"✅ Pubblicato su {BITCOIN_CHANNEL}.", call.message.chat.id, call.message.message_id)
            except Exception:
                pass
        else:
            bot.answer_callback_query(call.id, "Rifiutato.")
            try:
                bot.edit_message_text("❌ Rifiutato.", call.message.chat.id, call.message.message_id)
            except Exception:
                pass
                
        storage.delete_pending_forward(DB_PATH, fwd_id)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Errore: {e}")


@bot.message_handler(commands=["sub"])
def handle_sub(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        bot.send_message(message.chat.id, _append_sponsor("Uso: /sub URL_CANALE_YOUTUBE", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))
        return
        
    url = _extract_supported_url(raw[1]) or raw[1].strip()
    if not _is_supported_url(url):
        bot.send_message(message.chat.id, _append_sponsor("Inserisci un URL valido.", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))
        return
        
    # Get channel title
    title = url
    try:
        with _ydl({"quiet": True, "noplaylist": True, "extract_flat": True}) as ydl:
            c_info = ydl.extract_info(url, download=False)
            title = c_info.get("channel") or c_info.get("title") or url
            storage.update_channel_state(DB_PATH, url, channel_title=title)
    except Exception as e:
        print(f"Errore recupero titolo canale: {e}")
        
    if storage.add_subscription(DB_PATH, message.from_user.id, url):
        bot.send_message(message.chat.id, _append_sponsor(f"Iscrizione completata per: {title}\nRiceverai notifiche per i nuovi video.", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))
    else:
        bot.send_message(message.chat.id, _append_sponsor("Sei già iscritto a questo canale.", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))

@bot.message_handler(commands=["unsub"])
def handle_unsub(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        bot.reply_to(message, _append_sponsor("Uso: /unsub URL_CANALE_YOUTUBE", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return
        
    url = raw[1].strip()
    title = storage.get_channel_title(DB_PATH, url) or url
    if storage.remove_subscription(DB_PATH, message.from_user.id, url):
        bot.send_message(message.chat.id, _append_sponsor(f"Iscrizione rimossa per: {title}", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))
    else:
        bot.send_message(message.chat.id, _append_sponsor("Non risulti iscritto a questo canale.", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_to_message_id=_get_msg_id(message))

@bot.message_handler(commands=["mysubs"])
def handle_mysubs(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return
    render_subs_page(message.chat.id, message.from_user.id, 0)

def render_subs_page(chat_id: int, user_id: int, page: int, message_id_to_edit: Optional[int] = None) -> None:
    subs = storage.get_user_subscriptions_with_titles(DB_PATH, user_id)
    if not subs:
        text = "Non sei iscritto a nessun canale. Usa ➕ Nuova Iscrizione per aggiungerne uno."
        if message_id_to_edit:
            bot.edit_message_text(text, chat_id, message_id_to_edit)
        else:
            bot.send_message(chat_id, text)
        return
        
    per_page = 5
    total_pages = (len(subs) + per_page - 1) // per_page
    if page >= total_pages: 
        page = total_pages - 1
    if page < 0: 
        page = 0
        
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_subs = subs[start_idx:end_idx]
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    for url, title in page_subs:
        url_id = storage.save_url_cache(DB_PATH, url)
        display_name = title[:30]
        kb.add(types.InlineKeyboardButton(f"🗑️ Cancella {display_name}", callback_data=f"sub:rm:{url_id}:{page}"))
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("⬅️ Preced. ", callback_data=f"sub:pg:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("Successiva ➡️", callback_data=f"sub:pg:{page+1}"))
        
    if nav_buttons:
        kb.row(*nav_buttons)
        
    text = f"Le tue iscrizioni (Pag {page+1}/{total_pages}):\nTotale canali seguiti: {len(subs)}"
    try:
        if message_id_to_edit:
            bot.edit_message_text(text, chat_id, message_id_to_edit, reply_markup=kb)
        else:
            bot.send_message(chat_id, text, reply_markup=kb)
    except Exception as e:
        pass

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("sub:"))
def handle_sub_callbacks(call: types.CallbackQuery) -> None:
    try:
        parts = call.data.split(":")
        action = parts[1]
        
        if action == "pg":
            page = int(parts[2])
            render_subs_page(call.message.chat.id, call.from_user.id, page, _get_msg_id(call.message))
            bot.answer_callback_query(call.id)
            
        elif action == "rm":
            url_id = int(parts[2])
            page = int(parts[3])
            url = storage.get_url_cache(DB_PATH, url_id)
            if url:
                title = storage.get_channel_title(DB_PATH, url) or url
                storage.remove_subscription(DB_PATH, call.from_user.id, url)
                bot.answer_callback_query(call.id, f"Iscrizione rimossa: {title}")
            else:
                bot.answer_callback_query(call.id, "Errore: non trovata in cache.")
            
            render_subs_page(call.message.chat.id, call.from_user.id, page, _get_msg_id(call.message))
            
    except Exception as e:
        try: bot.answer_callback_query(call.id, "Errore interno.")
        except: pass


class DownloadTask:
    def __init__(self, message: types.Message, url: str, mode: str = "B", auto_publish: bool = False,
                 from_subscription: bool = False):
        self.message = message
        self.url = url
        self.mode = mode
        self.auto_publish = auto_publish
        self.from_subscription = from_subscription
        self.first_seen = time.time()
        self.postponed = False
        self.priority = 0 if _is_paying(message) else 1
        self.seq = next(_task_seq)

    # PriorityQueue orders tasks by this: payers first, then first come first served.
    def __lt__(self, other: "DownloadTask") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


_task_seq = itertools.count()


def _is_paying(message: types.Message) -> bool:
    # Today paying means the ad-free unlock; a future plan only needs to be added here.
    uid, _ = _requester(message)
    return uid == ADMIN_USER_ID or storage.is_ad_free(DB_PATH, uid) or storage.is_ad_free(DB_PATH, message.chat.id)


def _queue_position(task: DownloadTask) -> int:
    with download_queue.mutex:
        return 1 + sum(1 for t in download_queue.queue if t < task)


download_queue: "queue.PriorityQueue[DownloadTask]" = queue.PriorityQueue()

# The channel feed also lists scheduled lives and premieres: they cannot be downloaded yet,
# and a live in progress would record forever. They are retried until they become a normal video.
LIVE_RETRY_S = 3600
LIVE_GIVE_UP_S = 4 * 24 * 3600
_UPCOMING_RE = re.compile(r"live event will begin|Premieres in|is_upcoming|has not started", re.I)
_postponed: list[tuple[float, "DownloadTask"]] = []
_postponed_guard = threading.Lock()


def _postpone(task: "DownloadTask") -> None:
    if time.time() - task.first_seen > LIVE_GIVE_UP_S:
        print(f"[Polling] Rinuncio a {task.url}: ancora non scaricabile dopo {LIVE_GIVE_UP_S // 86400} giorni")
        return
    if not task.postponed:
        task.postponed = True
        try:
            bot.send_message(
                task.message.chat.id,
                _append_sponsor(f"📅 {task.url} è una diretta o premiere non ancora conclusa: la scarico appena possibile.",
                                limit=TEXT_LIMIT, for_chat=task.message.chat.id),
            )
        except Exception as e:
            print(f"Avviso rinvio fallito: {e}")
    with _postponed_guard:
        _postponed.append((time.time() + LIVE_RETRY_S, task))


def _requeue_due_postponed() -> None:
    now = time.time()
    with _postponed_guard:
        due = [t for at, t in _postponed if at <= now]
        _postponed[:] = [(at, t) for at, t in _postponed if at > now]
    for t in due:
        download_queue.put(t)

def queue_worker():
    while True:
        task = download_queue.get()
        if task is None:
            break
        try:
            _process_download(task)
        except Exception as e:
            print(f"Errore del worker: {e}")
        finally:
            download_queue.task_done()

# Avvia 2 worker per gestire la coda di download limitando i paralleli e bilanciando risorse
for _ in range(2):
    t = threading.Thread(target=queue_worker, daemon=True)
    t.start()


def _url_temp_id(url: str) -> Optional[str]:
    # The id comes from the link alone, without calling the site: enough to tell whether it is cached.
    for ie in yt_dlp.extractor.gen_extractor_classes():
        if ie.ie_key() != "Generic" and ie.suitable(url):
            try:
                return ie.get_temp_id(url)
            except Exception:
                return None
    return None


def _has_full_cache(url: str, user_id: int) -> bool:
    yt_id = _url_temp_id(url)
    if not yt_id:
        return False
    # With a chosen language only the dub counts: the original alone might not be what was wanted.
    audio_lang = storage.get_audio_lang(DB_PATH, user_id)
    cached = storage.get_cache(DB_PATH, f"{yt_id}@{audio_lang}" if audio_lang else yt_id)
    return bool(cached and cached.get("video_file_id") and cached.get("audio_file_id"))


def _media_mode(chat_id: int) -> Optional[str]:
    show = storage.get_chat_settings(DB_PATH, chat_id)
    if show["video"] and show["audio"]:
        return "B"
    return "V" if show["video"] else "A" if show["audio"] else None


_telegraph_guard = threading.Lock()


def _telegraph_token() -> str:
    with _telegraph_guard:
        token = storage.get_kv(DB_PATH, "telegraph_token")
        if not token:
            token = articles.create_account("YTDownloader")
            storage.set_kv(DB_PATH, "telegraph_token", token)
        return token


def _is_hub(chat: types.Chat) -> bool:
    username = f"@{chat.username}".lower() if getattr(chat, "username", None) else ""
    return BITCOIN_HUB.lower() in (str(chat.id), username)


def _send_article(message: types.Message, url: str) -> None:
    in_group = message.chat.type != "private"
    source = articles.clean_url(url)
    try:
        known = storage.get_article(DB_PATH, source)
        if known:
            telegraph_url, title, is_bitcoin = known
        else:
            article = articles.fetch_article(url)
            if not article:
                # Not an article: in a group it may still be a video on a site yt-dlp handles generically.
                if in_group:
                    _route_video(message, url)
                else:
                    bot.reply_to(message, "Da questo link non riesco a ricavare né un articolo né un video.")
                return
            telegraph_url = articles.create_page(_telegraph_token(), article, source)
            title = article.title
            is_bitcoin = bool(_BITCOIN_RE.search(f"{article.title}\n{article.text}"))
            storage.save_article(DB_PATH, source, telegraph_url, title, is_bitcoin)

        text = (
            f'<a href="{html.escape(telegraph_url)}">{html.escape(title, quote=False)}</a>\n\n'
            f'via <a href="{html.escape(source)}">{html.escape(articles.site_of(source))}</a>'
        )
        if MOVED_TO:
            text += f"\n\n{html.escape(_moved_notice(), quote=False)}"
        msg_id = _get_msg_id(message)
        bot.send_message(message.chat.id, text, parse_mode="HTML",
                         reply_to_message_id=msg_id if msg_id and msg_id > 0 else None)
        # Bitcoin articles always reach the hub as well, once each, unless they were posted there.
        if is_bitcoin and BITCOIN_HUB and not _is_hub(message.chat) and storage.claim_article_for_hub(DB_PATH, source):
            bot.send_message(BITCOIN_HUB, text, parse_mode="HTML")
    except Exception as e:
        print(f"Articolo non convertito ({url}): {e}")
        if not in_group:
            bot.reply_to(message, "Non riesco a leggere questo articolo.")


@bot.channel_post_handler(
    content_types=["text", "photo", "video", "animation", "document"],
    func=lambda m: _link_for(m) is not None,
)
def handle_channel_download(message: types.Message) -> None:
    _route_link(message, _link_for(message))


@bot.message_handler(
    content_types=["text", "photo", "video", "animation", "document"],
    func=lambda m: _link_for(m) is not None,
)
def handle_download(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return

    _route_link(message, _link_for(message))


def _route_link(message: types.Message, url: str) -> None:
    if _is_supported_url(url) or _url_temp_id(url) is not None:
        _route_video(message, url)
    else:
        threading.Thread(target=_send_article, args=(message, url), daemon=True).start()


def _route_video(message: types.Message, url: str) -> None:
    # In groups everything is downloaded without asking: whoever posts a link wants it there, not in private.
    mode = _media_mode(message.chat.id)
    if message.chat.type != "private":
        if mode:
            download_queue.put(DownloadTask(message, url, mode))
        return
    if not mode:
        bot.reply_to(message, "Hai spento sia il video sia l'audio in ⚙️ Impostazioni: riaccendine almeno uno.")
        return
    # Already cached: send everything right away, without asking the format or waiting in the queue.
    if _has_full_cache(url, message.chat.id):
        threading.Thread(target=_process_download, args=(DownloadTask(message, url, mode),), daemon=True).start()
        return

    if message.from_user.id != ADMIN_USER_ID:
        count = storage.get_downloads_last_hour(DB_PATH, message.from_user.id)
        if count >= 3:
            bot.reply_to(message, _append_sponsor("Hai raggiunto il limite di 3 download all'ora. Riprova più tardi.", limit=TEXT_LIMIT, for_chat=message.chat.id))
            return

    # With only one of video and audio turned on there is nothing to choose.
    if mode != "B":
        task = DownloadTask(message, url, mode)
        bot.reply_to(message, _append_sponsor(f"⏳ Richiesta accodata. Posizione stimata: {_queue_position(task)}...",
                                              limit=TEXT_LIMIT, for_chat=message.chat.id))
        download_queue.put(task)
        return

    # Invia scelte via InlineKeyboardMarkup
    url_id = storage.save_url_cache(DB_PATH, url)
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton("🎵 Solo Audio", callback_data=f"dl:A:{url_id}"),
        types.InlineKeyboardButton("🎬 Solo Video", callback_data=f"dl:V:{url_id}"),
        types.InlineKeyboardButton("📽️ Entrambi", callback_data=f"dl:B:{url_id}")
    )
    bot.send_message(message.chat.id, _append_sponsor("Scegli un formato:", limit=TEXT_LIMIT, for_chat=message.chat.id), reply_markup=kb, reply_to_message_id=_get_msg_id(message))

@bot.callback_query_handler(func=lambda c: (c.data or "").startswith("dl:"))
def handle_dl_choice(call: types.CallbackQuery) -> None:
    try:
        parts = call.data.split(":")
        if len(parts) != 3:
            return
        mode, url_id = parts[1], int(parts[2])
        url = storage.get_url_cache(DB_PATH, url_id)
        if not url:
            bot.answer_callback_query(call.id, "Link scaduto o non trovato.")
            return
            
        bot.answer_callback_query(call.id, "Aggiunto in coda!")
        try:
            bot.delete_message(call.message.chat.id, _get_msg_id(call.message)) # remove keyboard msg
        except Exception:
            pass
            
        original_message = call.message.reply_to_message or call.message
        task = DownloadTask(original_message, url, mode)
        bot.send_message(
            call.message.chat.id, 
            _append_sponsor(f"⏳ Richiesta accodata. Posizione stimata: {_queue_position(task)}...", limit=TEXT_LIMIT, for_chat=call.message.chat.id),
            reply_to_message_id=_get_msg_id(original_message)
        )
        download_queue.put(task)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Errore: {e}")

def _process_download(task: DownloadTask) -> None:
    message = task.message
    url = task.url
    mode = task.mode
    status = None
    yt_id = None
    video_path = None
    audio_path = None
    thumb_path = None
    lock = None
    uid, who = _requester(message)
    # Everyone reads groups and channels: only the content goes there, no service messages.
    in_group = message.chat.type != "private"
    show = storage.get_chat_settings(DB_PATH, message.chat.id)

    try:
        msg_id = _get_msg_id(message)
        if not in_group:
            status = bot.send_message(
                message.chat.id,
                _append_sponsor("⏳ Sto preparando download e invio…", limit=TEXT_LIMIT, for_chat=message.chat.id),
                reply_to_message_id=msg_id if msg_id and msg_id > 0 else None
            )

        preview_opts = {"quiet": True, "noplaylist": True}
        with _ydl(preview_opts) as ydl:
            try:
                info_preview = ydl.extract_info(url, download=False)
            except Exception as e:
                if task.from_subscription and _UPCOMING_RE.search(str(e)):
                    _postpone(task)
                    return
                raise
        if task.from_subscription and info_preview.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            _postpone(task)
            return

        yt_id = info_preview.get("id") or str(int(time.time()))
        title = info_preview.get("title") or "Video"
        # webpage_url is the clean link (watch?v=…), without the sharing parameters.
        source_url = info_preview.get("webpage_url") or url
        lock = _video_lock(yt_id)
        lock.acquire()
        # The same channel can come from two subscriptions (handle and a video link): once is enough.
        if task.from_subscription and storage.has_downloaded(DB_PATH, uid, yt_id):
            return

        # Send the sticker to the user
        sticker_file_id = None
        try:
            sticker_set = bot.get_sticker_set("BitcoinPodcast")
            if sticker_set.stickers:
                sticker_file_id = sticker_set.stickers[0].file_id
                if show["sticker"]:
                    bot.send_sticker(message.chat.id, sticker_file_id)
        except Exception:
            pass

        # The language belongs to the chat: in a group it is the group's, not the poster's.
        dub_lang = _dub_lang_for(info_preview, storage.get_audio_lang(DB_PATH, message.chat.id))
        # A dub is a different file: in the cache it cannot take the original's place.
        cache_key = f"{yt_id}@{dub_lang}" if dub_lang else yt_id
        cached = storage.get_cache(DB_PATH, cache_key)
        
        msg_photo = None
        msg_video = None
        msg_audio = None
        # Without the preview the title would only be in the file name: it goes into the caption.
        video_label = "🎬 Video" if show["thumb"] else f"🎬 {title}"
        audio_label = "🎵 Audio" if show["thumb"] else f"🎵 {title}"
        cache_tag = "" if in_group else " (cache)"

        if cached and cached.get("video_file_id") and cached.get("audio_file_id"):
            if show["thumb"] and cached.get("thumb_file_id"):
                msg_photo = bot.send_photo(
                    message.chat.id,
                    cached["thumb_file_id"],
                    caption=_media_caption(f"📸 {title}", for_chat=message.chat.id, source_url=source_url),
                    parse_mode="HTML",
                )
            if mode in ["V", "B"]:
                try:
                    msg_video = _send_with_retry(
                        bot.send_video,
                        message.chat.id,
                        cached["video_file_id"],
                        caption=_media_caption(video_label + cache_tag, for_chat=message.chat.id, source_url=source_url),
                        parse_mode="HTML",
                    )
                except Exception:
                    msg_video = _send_with_retry(
                        bot.send_document,
                        message.chat.id,
                        cached["video_file_id"],
                        caption=_media_caption(video_label + cache_tag, for_chat=message.chat.id, source_url=source_url),
                        parse_mode="HTML",
                    )
            if mode in ["A", "B"]:
                msg_audio = _send_with_retry(
                    bot.send_audio,
                    message.chat.id,
                    cached["audio_file_id"],
                    caption=_media_caption(audio_label + cache_tag, for_chat=message.chat.id, source_url=source_url),
                    parse_mode="HTML",
                )
            
            storage.log_download(DB_PATH, uid, yt_id, yt_title=title, chat_id=message.chat.id,
                                 chat_type=message.chat.type, from_cache=True)
            if uid != ADMIN_USER_ID:
                bot.send_message(
                    ADMIN_USER_ID,
                    f"📥 L'utente {who} ha scaricato (da cache): {url}"
                )
            
        else:
            video_path = None
            audio_path = None
            if mode in ["V", "B"]:
                video_path, info = _download_video(url, yt_id, dub_lang)
            if mode in ["A", "B"]:
                audio_path = _download_audio(url, yt_id, info if 'info' in locals() else info_preview, dub_lang)
                
            meta = info if 'info' in locals() else info_preview
            thumb_url = meta.get("thumbnail")

            if not thumb_url:
                thumb_url = "https://i.ytimg.com/vi/{}/hqdefault.jpg".format(yt_id)

            small_thumb = _prepare_thumbnail(thumb_url, yt_id)
            duration = int(meta.get("duration") or 0) or None
            performer = meta.get("channel") or meta.get("uploader") or None

            for path in (video_path, audio_path):
                if path:
                    _tag_media(path, title=title, performer=performer or "", source_url=url)

            if show["thumb"]:
                try:
                    thumb_result = _send_thumbnail(message.chat.id, thumb_url, f"📸 {title}", yt_id, title, source_url=source_url)
                    if isinstance(thumb_result, tuple):
                        msg_photo, thumb_path = thumb_result
                    else:
                        msg_photo = thumb_result
                except Exception as e:
                    print(f"Invio thumbnail fallito: {e}")

            if video_path and mode in ["V", "B"]:
                try:
                    msg_video = _send_video_or_document(
                        message.chat.id, video_path, video_label,
                        title=title,
                        thumb_path=small_thumb,
                        duration=duration,
                        width=meta.get("width") or None,
                        height=meta.get("height") or None,
                        source_url=source_url,
                    )
                except Exception as e:
                    vsz = os.path.getsize(video_path) if os.path.exists(video_path) else -1
                    print(f"Invio video fallito (size={vsz}): {e}")

            if audio_path and mode in ["A", "B"]:
                try:
                    msg_audio = _send_audio(
                        message.chat.id, audio_path, audio_label, title,
                        thumb_path=small_thumb,
                        duration=duration,
                        performer=performer,
                        source_url=source_url,
                    )
                except Exception as e:
                    asz = os.path.getsize(audio_path) if os.path.exists(audio_path) else -1
                    print(f"Invio audio fallito (size={asz}): {e}")

            # update cache only if we downloaded BOTH
            if mode == "B" and msg_video and msg_audio:
                storage.upsert_cache(
                    DB_PATH,
                    cache_key,
                    title=title,
                    thumb_file_id=_file_id_from_result(msg_photo, "photo") if msg_photo else "",
                    video_file_id=_file_id_from_result(msg_video, "video") or _file_id_from_result(msg_video, "document"),
                    audio_file_id=_file_id_from_result(msg_audio, "audio"),
                )
            
            storage.log_download(DB_PATH, uid, yt_id, yt_title=title, chat_id=message.chat.id,
                                 chat_type=message.chat.type)
            if uid != ADMIN_USER_ID:
                bot.send_message(
                    ADMIN_USER_ID,
                    f"📥 L'utente {who} ha scaricato un nuovo video: {url}"
                )
                
        sent_ids = []
        if isinstance(msg_photo, dict) and msg_photo.get("message_id"): sent_ids.append(msg_photo["message_id"])
        elif hasattr(msg_photo, "message_id"): sent_ids.append(msg_photo.message_id)

        if isinstance(msg_video, dict) and msg_video.get("message_id"): sent_ids.append(msg_video["message_id"])
        elif hasattr(msg_video, "message_id"): sent_ids.append(msg_video.message_id)

        if isinstance(msg_audio, dict) and msg_audio.get("message_id"): sent_ids.append(msg_audio["message_id"])
        elif hasattr(msg_audio, "message_id"): sent_ids.append(msg_audio.message_id)

        if sent_ids:
            if task.auto_publish:
                _publish_to_channel(message.chat.id, sent_ids, sticker_file_id)
            else:
                # Tutto il resto sul canale ci va solo se lo approvi.
                terms = _bitcoin_terms_in(info if "info" in locals() else info_preview)
                if terms:
                    if uid == ADMIN_USER_ID:
                        # L'admin è chi approverebbe: chiederglielo sarebbe solo un passaggio in più.
                        _publish_to_channel(message.chat.id, sent_ids, sticker_file_id)
                    else:
                        _propose_channel_publication(message, title, terms, sent_ids)

        if status:
            bot.delete_message(message.chat.id, status.message_id)

    except Exception as e:
        msg_id = _get_msg_id(message)
        text = f"Errore: {e}"
        if "confirm you" in str(e) and "not a bot" in str(e):
            _alert_bot_check()
            text = "YouTube sta bloccando i download dal server (verifica anti-bot). L'amministratore è avvisato: riprova più tardi."
        if in_group:
            # Groups also see links that are not videos: failing there is normal, not an error to report.
            print(f"Download in {message.chat.title or message.chat.id} non riuscito: {url}: {e}")
        else:
            bot.send_message(
                message.chat.id,
                _append_sponsor(text, limit=TEXT_LIMIT, for_chat=message.chat.id),
                reply_to_message_id=msg_id if msg_id and msg_id > 0 else None
            )
    finally:
        for p in (video_path, audio_path, thumb_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        if yt_id:
            _cleanup_partial_downloads(f"{yt_id}_video.")
            _cleanup_partial_downloads(f"{yt_id}_audio.")
            leftovers = [f"{yt_id}_thumb.jpg", f"{yt_id}_thumb_small.jpg"]
            # writethumbnail lascia la copertina dell'audio se EmbedThumbnail non la consuma.
            leftovers += [
                n for n in os.listdir(DOWNLOAD_DIR)
                if n.startswith(f"{yt_id}_audio.") and n.endswith((".webp", ".jpg", ".png"))
            ]
            for name in leftovers:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, name))
                except OSError:
                    pass
        if status:
            try:
                bot.delete_message(message.chat.id, status.message_id)
            except Exception:
                pass
        if lock:
            lock.release()


_FEED_NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


def _channel_id_for(url: str) -> Optional[str]:
    m = re.search(r"/channel/(UC[\w-]{22})", url)
    if m:
        return m.group(1)
    stored = storage.get_channel_id(DB_PATH, url)
    if stored:
        return stored
    # A subscription can be an @handle or the link of one of the channel's videos: resolved only once.
    with _ydl({"quiet": True, "noplaylist": True, "extract_flat": True, "playlistend": 1}) as ydl:
        info = ydl.extract_info(url, download=False)
    cid = info.get("channel_id") or (info.get("id") if str(info.get("id", "")).startswith("UC") else None)
    if cid:
        storage.set_channel_id(DB_PATH, url, cid)
    return cid


def _feed_video_ids(channel_id: str) -> tuple[list[str], Optional[str]]:
    # The RSS feed is light and skips the bot check that blocks the channel pages.
    r = requests.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}", timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ids = [e.findtext("yt:videoId", namespaces=_FEED_NS) for e in root.findall("a:entry", _FEED_NS)]
    return [i for i in ids if i], root.findtext("a:title", namespaces=_FEED_NS)


def poll_subscriptions():
    while True:
        try:
            for channel_url in storage.get_distinct_subscribed_channels(DB_PATH):
                try:
                    channel_id = _channel_id_for(channel_url)
                    if not channel_id:
                        continue
                    ids, c_title = _feed_video_ids(channel_id)
                    if not ids:
                        continue
                    last_saved_id = storage.get_channel_state(DB_PATH, channel_url)
                    storage.update_channel_state(DB_PATH, channel_url, ids[0], channel_title=c_title or None)
                    # Without a recognisable last video (first time, or old state) only the starting point is set:
                    # downloading the whole feed would be a flood.
                    if last_saved_id not in ids:
                        continue
                    new_ids = ids[:ids.index(last_saved_id)][:3]
                    for vid in reversed(new_ids):
                        print(f"[Polling] Nuovo video per {c_title or channel_url}: {vid}")
                        notify_subscribers(channel_url, f"https://www.youtube.com/watch?v={vid}", c_title)
                except Exception as e:
                    print(f"Errore polling canale {channel_url}: {e}")
        except Exception as e:
            print(f"Errore generico polling thread: {e}")

        _requeue_due_postponed()
        time.sleep(600)  # Controlla ogni 10 minuti


def notify_subscribers(channel_url: str, video_url: str, channel_title: Optional[str] = None):
    subs = storage.get_all_subscriptions(DB_PATH)
    target_users = [u for u, c in subs if c == channel_url]
    if not target_users:
        return
        
    display_name = channel_title or channel_url
    for user_id in target_users:
        # Whoever already started the new bot gets the video from there: from both it would arrive twice.
        if HANDOFF_DB and storage.is_known_user(HANDOFF_DB, user_id):
            continue
        try:
            text = f"🔔 Nuovo video dal canale {display_name}!\nVerrà scaricato automaticamente."
            if user_id == ADMIN_USER_ID:
                text += f"\nVideo e audio vanno anche su {BITCOIN_CHANNEL}."
            bot.send_message(user_id, _append_sponsor(text, limit=TEXT_LIMIT, for_chat=user_id))
            
            # Create a fake message object for the background download
            # We use message_id=-1 or 0 to indicate a background task
            # de_json: Message's constructor changes signature across pyTelegramBotAPI versions.
            fake_message = types.Message.de_json({
                "message_id": 0,
                "from": {"id": user_id, "is_bot": False, "first_name": "Sub", "username": "sub"},
                "date": int(time.time()),
                "chat": {"id": user_id, "type": "private"},
                "text": video_url,
            })
            mode = _media_mode(user_id)
            if mode:
                download_queue.put(
                    DownloadTask(fake_message, video_url, mode=mode, auto_publish=(user_id == ADMIN_USER_ID),
                                 from_subscription=True)
                )
        except Exception as e:
            print(f"Errore nella notifica all'utente {user_id}: {e}")


poll_thread = threading.Thread(target=poll_subscriptions, daemon=True)
poll_thread.start()

sponsor_thread = threading.Thread(target=watch_sponsor_deadlines, daemon=True)
sponsor_thread.start()

print("Bot partito...")
bot.infinity_polling(skip_pending=True)
