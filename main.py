import os
import re
import time
import mimetypes
import subprocess
from typing import Any, Optional
import queue
import threading

import requests
import telebot
import yt_dlp
from telebot import apihelper, types

import storage
from config import (
    AD_FREE_PAYEE,
    AD_FREE_PRICE_SATS,
    ADMIN_CONTACT,
    ADMIN_USER_ID,
    API_BASE_URL,
    BITCOIN_CHANNEL,
    BOT_TOKEN,
    DB_PATH,
    MAX_UPLOAD_BYTES,
)


DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

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


def _append_sponsor(text: str, *, limit: int, for_chat: Optional[int] = None) -> str:
    base = (text or "").strip()
    if SPONSOR_HEADER.lower() in base.lower() or _is_ad_free(for_chat):
        return base[:limit]

    suffix = _sponsor_block(min(SPONSOR_BLOCK_MAX, limit))
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
    data = {"chat_id": str(chat_id), "caption": caption, "supports_streaming": "true"}
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
            {"chat_id": str(chat_id), "caption": caption},
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
    data = {"chat_id": str(chat_id), "caption": caption, "title": title}
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
        {"chat_id": str(chat_id), "photo": photo, "caption": caption},
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
            {"chat_id": str(chat_id), "caption": caption},
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
        f"Richiesto da: {_user_label(message.from_user)}\n\n"
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


def _message_url(message: types.Message) -> Optional[str]:
    text = message.text or message.caption or ""
    if text.startswith("/"):
        return None

    found = _extract_supported_url(text)
    if found:
        return found

    # Link nascosto dietro un testo formattato (tipico dei messaggi inoltrati).
    for ent in list(message.entities or []) + list(message.caption_entities or []):
        url = getattr(ent, "url", "") or ""
        if url and _is_supported_url(url):
            return url
    return None


def _user_label(user: Any) -> str:
    username = getattr(user, "username", "") or ""
    first_name = getattr(user, "first_name", "") or ""
    if username:
        return f"@{username} (id {user.id})"
    if first_name:
        return f"{first_name} (id {user.id})"
    return f"id {user.id}"


def _ensure_authorized(message: types.Message) -> bool:
    if message.from_user:
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


def _audio_quality_key(fmt: dict) -> tuple:
    abr = fmt.get("abr") or 0
    tbr = fmt.get("tbr") or 0
    asr = fmt.get("asr") or 0
    return (abr, tbr, asr)


def _select_video_format_under_limit(info: dict, limit_bytes: int) -> str:
    formats = info.get("formats") or []
    duration_s = info.get("duration")

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
    if progressive_ok:
        best = max(progressive_ok, key=_video_quality_key)
        return str(best["format_id"])

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
    audio_only_sorted = sorted(audio_only, key=_audio_quality_key, reverse=True)

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
            return f"{v['format_id']}+{best_audio['format_id']}"

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

    return (
        f"bestvideo[ext=mp4][height<={cap}]+bestaudio[ext=m4a]/"
        f"best[ext=mp4][height<={cap}]/best[height<={cap}]"
    )


def _select_audio_format_under_limit(info: dict, limit_bytes: int) -> str:
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
        best = max(audio_ok, key=_audio_quality_key)
        return str(best["format_id"])
    return "bestaudio/best"


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


def _download_video(url: str, yt_id: str) -> tuple[str, dict]:
    ydl_preview_opts = {"quiet": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(ydl_preview_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    fmt = _select_video_format_under_limit(info, EFFECTIVE_MAX_UPLOAD_BYTES)
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
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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


def _download_audio(url: str, yt_id: str, info: dict) -> str:
    fmt = _select_audio_format_under_limit(info, EFFECTIVE_MAX_UPLOAD_BYTES)
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"Conversione mp3 fallita, ripiego sul flusso originale: {e}")
        ydl_opts.pop("postprocessors", None)
        ydl_opts.pop("writethumbnail", None)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
):
    caption = _append_sponsor(caption, limit=CAPTION_LIMIT, for_chat=chat_id)
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
):
    caption = _append_sponsor(caption, limit=CAPTION_LIMIT, for_chat=chat_id)
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


def _send_thumbnail(chat_id: int, thumb_url: str, caption: str, yt_id: str, title: str = ""):
    caption = _append_sponsor(caption, limit=CAPTION_LIMIT, for_chat=chat_id)
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


@bot.message_handler(commands=["report"])
def handle_report(message: types.Message) -> None:
    if message.from_user.id != ADMIN_USER_ID:
        bot.reply_to(message, _append_sponsor("Non autorizzato.", limit=TEXT_LIMIT, for_chat=message.chat.id))
        return

    stats = storage.get_report_stats(DB_PATH)
    
    text = "📊 **Report Statistiche YTDownloader** 📊\n"
    text += f"Totale Download: {stats['total_downloads']}\n"
    text += f"Utenti Unici: {stats['unique_users']}\n"
    text += f"Iscrizioni Totali: {stats['total_subscriptions']}\n\n"
    text += "🕒 **Ultimi 10 download:**\n"
    
    for u_id, y_id, title, ts, first_name, username in stats['recent_downloads']:
        dt = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
        user_label = first_name or str(u_id)
        if username:
            user_label += f" (@{username})"
        
        # Markdown Link: [Title](URL)
        video_url = f"https://www.youtube.com/watch?v={y_id}" if len(y_id) == 11 else f"https://youtu.be/{y_id}"
        # For non-YT videos, yt_id might be different, but ytdlp IDs are generally good enough for links if we assume YT.
        # However, to be safe, we just link to the ID if it looks like YT, otherwise just show title.
        link_md = f"[{title}]({video_url})" if title else f"`{y_id}`"
        
        text += f"- `[{dt}]` {user_label} -> {link_md}\n"
        
    bot.send_message(message.chat.id, _append_sponsor(text, limit=TEXT_LIMIT, for_chat=message.chat.id), parse_mode="Markdown", reply_to_message_id=_get_msg_id(message))

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
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "extract_flat": True}) as ydl:
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
    def __init__(self, message: types.Message, url: str, mode: str = "B", auto_publish: bool = False):
        self.message = message
        self.url = url
        self.mode = mode
        self.auto_publish = auto_publish

download_queue = queue.Queue()

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


@bot.message_handler(
    content_types=["text", "photo", "video", "animation", "document"],
    func=lambda m: _message_url(m) is not None,
)
def handle_download(message: types.Message) -> None:
    if not _ensure_authorized(message):
        return

    if message.from_user.id != ADMIN_USER_ID:
        count = storage.get_downloads_last_hour(DB_PATH, message.from_user.id)
        if count >= 3:
            bot.reply_to(message, _append_sponsor("Hai raggiunto il limite di 3 download all'ora. Riprova più tardi.", limit=TEXT_LIMIT, for_chat=message.chat.id))
            return

    url = _message_url(message)
    if not url:
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
            
        qsize = download_queue.qsize()
        original_message = call.message.reply_to_message or call.message
        bot.send_message(
            call.message.chat.id, 
            _append_sponsor(f"⏳ Richiesta accodata. Posizione stimata: {qsize + 1}...", limit=TEXT_LIMIT, for_chat=call.message.chat.id),
            reply_to_message_id=_get_msg_id(original_message)
        )
        download_queue.put(DownloadTask(original_message, url, mode))
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

    try:
        msg_id = _get_msg_id(message)
        status = bot.send_message(
            message.chat.id, 
            _append_sponsor("⏳ Sto preparando download e invio…", limit=TEXT_LIMIT, for_chat=message.chat.id),
            reply_to_message_id=msg_id if msg_id and msg_id > 0 else None
        )

        preview_opts = {"quiet": True, "noplaylist": True}
        with yt_dlp.YoutubeDL(preview_opts) as ydl:
            info_preview = ydl.extract_info(url, download=False)

        yt_id = info_preview.get("id") or str(int(time.time()))
        title = info_preview.get("title") or "Video"
        
        # Send the sticker to the user
        sticker_file_id = None
        try:
            sticker_set = bot.get_sticker_set("BitcoinPodcast")
            if sticker_set.stickers:
                sticker_file_id = sticker_set.stickers[0].file_id
                bot.send_sticker(message.chat.id, sticker_file_id)
        except Exception:
            pass

        cached = storage.get_cache(DB_PATH, yt_id)
        
        msg_photo = None
        msg_video = None
        msg_audio = None

        if cached and cached.get("thumb_file_id") and cached.get("video_file_id") and cached.get("audio_file_id"):
            msg_photo = bot.send_photo(
                message.chat.id,
                cached["thumb_file_id"],
                caption=_append_sponsor(f"📸 {title}", limit=CAPTION_LIMIT, for_chat=message.chat.id),
            )
            if mode in ["V", "B"]:
                try:
                    msg_video = _send_with_retry(
                        bot.send_video,
                        message.chat.id,
                        cached["video_file_id"],
                        caption=_append_sponsor("🎬 Video (cache)", limit=CAPTION_LIMIT, for_chat=message.chat.id),
                    )
                except Exception:
                    msg_video = _send_with_retry(
                        bot.send_document,
                        message.chat.id,
                        cached["video_file_id"],
                        caption=_append_sponsor("🎬 Video (cache)", limit=CAPTION_LIMIT, for_chat=message.chat.id),
                    )
            if mode in ["A", "B"]:
                msg_audio = _send_with_retry(
                    bot.send_audio,
                    message.chat.id,
                    cached["audio_file_id"],
                    caption=_append_sponsor("🎵 Audio (cache)", limit=CAPTION_LIMIT, for_chat=message.chat.id),
                )
            
            storage.log_download(DB_PATH, message.from_user.id, yt_id, yt_title=title)
            if message.from_user.id != ADMIN_USER_ID:
                bot.send_message(
                    ADMIN_USER_ID,
                    f"📥 L'utente {_user_label(message.from_user)} ha scaricato (da cache): {url}"
                )
            
        else:
            video_path = None
            audio_path = None
            if mode in ["V", "B"]:
                video_path, info = _download_video(url, yt_id)
            if mode in ["A", "B"]:
                audio_path = _download_audio(url, yt_id, info if 'info' in locals() else info_preview)
                
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

            try:
                thumb_result = _send_thumbnail(message.chat.id, thumb_url, f"📸 {title}", yt_id, title)
                if isinstance(thumb_result, tuple):
                    msg_photo, thumb_path = thumb_result
                else:
                    msg_photo = thumb_result
            except Exception as e:
                print(f"Invio thumbnail fallito: {e}")

            if video_path and mode in ["V", "B"]:
                try:
                    msg_video = _send_video_or_document(
                        message.chat.id, video_path, "🎬 Video",
                        title=title,
                        thumb_path=small_thumb,
                        duration=duration,
                        width=meta.get("width") or None,
                        height=meta.get("height") or None,
                    )
                except Exception as e:
                    vsz = os.path.getsize(video_path) if os.path.exists(video_path) else -1
                    print(f"Invio video fallito (size={vsz}): {e}")

            if audio_path and mode in ["A", "B"]:
                try:
                    msg_audio = _send_audio(
                        message.chat.id, audio_path, "🎵 Audio", title,
                        thumb_path=small_thumb,
                        duration=duration,
                        performer=performer,
                    )
                except Exception as e:
                    asz = os.path.getsize(audio_path) if os.path.exists(audio_path) else -1
                    print(f"Invio audio fallito (size={asz}): {e}")

            # update cache only if we downloaded BOTH
            if mode == "B" and msg_photo and msg_video and msg_audio:
                storage.upsert_cache(
                    DB_PATH,
                    yt_id,
                    title=title,
                    thumb_file_id=_file_id_from_result(msg_photo, "photo"),
                    video_file_id=_file_id_from_result(msg_video, "video") or _file_id_from_result(msg_video, "document"),
                    audio_file_id=_file_id_from_result(msg_audio, "audio"),
                )
            
            storage.log_download(DB_PATH, message.from_user.id, yt_id, yt_title=title)
            if message.from_user.id != ADMIN_USER_ID:
                bot.send_message(
                    ADMIN_USER_ID,
                    f"📥 L'utente {_user_label(message.from_user)} ha scaricato un nuovo video: {url}"
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
                    _propose_channel_publication(message, title, terms, sent_ids)

        if status:
            bot.delete_message(message.chat.id, status.message_id)

    except Exception as e:
        msg_id = _get_msg_id(message)
        bot.send_message(
            message.chat.id,
            _append_sponsor(f"Errore: {str(e)}", limit=TEXT_LIMIT, for_chat=message.chat.id),
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


def poll_subscriptions():
    while True:
        try:
            channels = storage.get_distinct_subscribed_channels(DB_PATH)
            for channel_url in channels:
                ydl_opts = {
                    "quiet": True,
                    "noplaylist": True,
                    "extract_flat": "in_playlist",
                    "playlistend": 5
                }
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(channel_url, download=False)
                        entries = info.get("entries", [])
                        if not entries:
                            continue
                        latest_video = entries[0]
                        latest_id = latest_video.get("id")
                        latest_url = latest_video.get("url") or f"https://www.youtube.com/watch?v={latest_id}"
                        
                        last_saved_id = storage.get_channel_state(DB_PATH, channel_url)
                        # Store channel title if it's the first time
                        c_title = info.get("channel") or info.get("title")
                        
                        if latest_id and latest_id != last_saved_id:
                            print(f"[Polling] Nuovo video trovato per {c_title or channel_url}: {latest_id}")
                            storage.update_channel_state(DB_PATH, channel_url, latest_id, channel_title=c_title)
                            # Only notify if it's not the first time checking
                            if last_saved_id is not None:
                                notify_subscribers(channel_url, latest_url, c_title)
                        elif c_title:
                             storage.update_channel_state(DB_PATH, channel_url, channel_title=c_title)
                except Exception as e:
                    print(f"Errore polling canale {channel_url}: {e}")
        except Exception as e:
            print(f"Errore generico polling thread: {e}")
            
        time.sleep(600)  # Controlla ogni 10 minuti


def notify_subscribers(channel_url: str, video_url: str, channel_title: Optional[str] = None):
    subs = storage.get_all_subscriptions(DB_PATH)
    target_users = [u for u, c in subs if c == channel_url]
    if not target_users:
        return
        
    display_name = channel_title or channel_url
    for user_id in target_users:
        try:
            text = f"🔔 Nuovo video dal canale {display_name}!\nVerrà scaricato automaticamente."
            if user_id == ADMIN_USER_ID:
                text += f"\nVideo e audio vanno anche su {BITCOIN_CHANNEL}."
            bot.send_message(user_id, _append_sponsor(text, limit=TEXT_LIMIT, for_chat=user_id))
            
            # Create a fake message object for the background download
            # We use message_id=-1 or 0 to indicate a background task
            fake_message = types.Message(
                message_id=0,
                from_user=types.User(id=user_id, is_bot=False, first_name="Sub", username="sub"),
                date=int(time.time()),
                chat=types.Chat(id=user_id, type="private"),
                content_type="text",
                options={}
            )
            download_queue.put(
                DownloadTask(fake_message, video_url, mode="B", auto_publish=(user_id == ADMIN_USER_ID))
            )
        except Exception as e:
            print(f"Errore nella notifica all'utente {user_id}: {e}")


poll_thread = threading.Thread(target=poll_subscriptions, daemon=True)
poll_thread.start()

sponsor_thread = threading.Thread(target=watch_sponsor_deadlines, daemon=True)
sponsor_thread.start()

print("Bot partito...")
bot.infinity_polling(skip_pending=True)
