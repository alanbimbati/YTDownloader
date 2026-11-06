"""Publishes a horizontal + vertical video pair dropped in a Syncthing folder, after the author approves Claude's texts."""
import os
import shutil
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, media, post_file, texts
from .media import Video
from .notify import notify
from .platforms import linkedin, nostr, youtube

ORDER = ["youtube", "shorts", "linkedin", "nostr"]
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_sizes: dict[str, tuple[int, float]] = {}
_linkedin_warned_day = ""


def _videos(folder: str) -> list[str] | None:
    """The folder's videos once they have stopped growing; None while Syncthing is still writing."""
    names = os.listdir(folder)
    if any(".syncthing." in n or n.endswith(".tmp") for n in names if not n.startswith(".post") and not n.startswith(".stato")):
        return None
    paths = [os.path.join(folder, n) for n in names
             if not n.startswith(".") and os.path.splitext(n)[1].lower() in media.VIDEO_EXTS]
    now = time.time()
    ready = bool(paths)
    for p in paths:
        size = os.path.getsize(p)
        last = _sizes.get(p)
        if not last or last[0] != size:
            _sizes[p] = (size, now)
            ready = False
        elif now - last[1] < config.STABLE_SECONDS:
            ready = False
    return paths if ready else None


def _pick(paths: list[str]) -> tuple[Video | None, Video | None]:
    probed = [media.probe(p) for p in paths]
    horizontal = max((v for v in probed if not v.vertical), key=lambda v: v.size, default=None)
    vertical = max((v for v in probed if v.vertical), key=lambda v: v.size, default=None)
    return horizontal, vertical


def _workdir(folder: str) -> str:
    path = os.path.join(config.WORK_DIR, os.path.basename(folder))
    os.makedirs(path, exist_ok=True)
    return path


AVAILABLE = {"youtube": lambda: youtube.available(), "shorts": lambda: youtube.available(),
             "linkedin": lambda: linkedin.available(), "nostr": lambda: nostr.available()}


def _applicable(horizontal: Video | None, vertical: Video | None) -> list[str]:
    # Listed even without credentials: the author sees what is missing, and a retry picks it up once added.
    wanted = {
        "youtube": horizontal is not None,
        "shorts": vertical is not None and vertical.duration <= config.SHORTS_MAX_SECONDS,
    }
    return [p for p in ORDER if wanted.get(p, True)]


def prepare(folder: str, paths: list[str]) -> None:
    name = os.path.basename(folder)
    post_file.write_state(folder, {"fase": "trascrizione"})
    horizontal, vertical = _pick(paths)
    source = horizontal or vertical
    transcript, language = post_file.read_subtitles(folder)
    if not transcript:
        audio = media.extract_audio(source.path, os.path.join(_workdir(folder), "audio.wav"), config.WHISPER_MAX_SECONDS)
        from .transcribe import transcribe  # heavy import, only when there is something to transcribe
        transcript, language = transcribe(audio)
    post_file.write_transcript(folder, transcript)
    post_file.write_state(folder, {"fase": "testi", "lingua": language})
    durations = {("vertical" if v.vertical else "horizontal"): v.duration for v in (horizontal, vertical) if v}
    written = texts.write_posts(transcript, language, post_file.read_notes(folder), durations)
    platforms = _applicable(horizontal, vertical)
    post_file.write_post(folder, platforms, written)
    post_file.write_state(folder, {"fase": "in_approvazione", "lingua": language, "piattaforme": {}})
    found = ", ".join(f"{'verticale' if v.vertical else 'orizzontale'} {v.dim} {v.duration:.0f}s"
                      for v in (horizontal, vertical) if v)
    notify(f"📝 {name}: testi pronti ({found}).\nControlla {name}/post.yaml e metti `pubblica: si`.\n"
                    f"Piattaforme: {', '.join(f'{p}' + ('' if AVAILABLE[p]() else ' (senza credenziali)') for p in platforms)}\n\n"
                    f"YouTube: {written['youtube']['title']}")


def _publish_at(post: dict) -> str:
    when = str(post.get("programma") or "").strip()
    if not when:
        return ""
    local = datetime.strptime(when, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(config.TIMEZONE))
    if local.timestamp() <= time.time() + 600:
        return ""
    return local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _thumbnail_image(folder: str) -> str | None:
    images = [n for n in sorted(os.listdir(folder)) if n.lower().endswith(IMAGE_EXTS) and not n.startswith(".")]
    return media.jpeg(os.path.join(folder, images[0]), os.path.join(_workdir(folder), "miniatura.jpg")) if images else None


def _shorts_description(description: str, long_url: str) -> str:
    lines = [l.replace("{video_lungo}", long_url) for l in description.splitlines()
             if long_url or "{video_lungo}" not in l]
    return "\n".join(lines)


def _publish_one(platform: str, folder: str, post: dict, language: str, done: dict,
                 horizontal: Video | None, vertical: Video | None) -> tuple[str, list[str]]:
    if not AVAILABLE[platform]():
        raise RuntimeError("credenziali mancanti nel .env del Pi")
    work = _workdir(folder)
    main = horizontal or vertical
    if platform == "youtube":
        p = post["youtube"]
        return youtube.publish(
            horizontal.path, title=p["title"], description=p["description"], tags=p.get("tags") or [],
            category=str(p.get("category") or "22"), language=language, publish_at=_publish_at(post),
            srt=post_file.srt_for(folder, horizontal.duration), thumbnail=_thumbnail_image(folder),
            playlist=str(post.get("playlist") or "").strip(),
        )
    if platform == "shorts":
        p = post["shorts"]
        title = p["title"] if "#shorts" in p["title"].lower() else f"{p['title']} #Shorts"
        description = _shorts_description(p["description"], (done.get("youtube") or {}).get("url", ""))
        return youtube.publish(
            vertical.path, title=title, description=description, tags=post["youtube"].get("tags") or [],
            category=str(post["youtube"].get("category") or "22"), language=language,
            publish_at=_publish_at(post), srt=post_file.srt_for(folder, vertical.duration),
        )
    if platform == "linkedin":
        path = media.shrink(main, os.path.join(work, "linkedin.mp4"), config.LINKEDIN_MAX_BYTES)
        return linkedin.upload(path, post["linkedin"]["text"], post["youtube"]["title"]), []
    if platform == "nostr":
        thumb = media.thumbnail(main, os.path.join(work, "thumb.jpg"))
        upload_path = lambda v: media.shrink(v, os.path.join(work, f"nostr-{'v' if v.vertical else 'h'}.mp4"), config.BLOSSOM_MAX_BYTES)
        return nostr.post(horizontal, vertical, post["nostr"], thumb, upload_path), []
    raise ValueError(f"piattaforma sconosciuta: {platform}")


def publish(folder: str, post: dict, state: dict, paths: list[str]) -> None:
    name = os.path.basename(folder)
    done = state.setdefault("piattaforme", {})
    pending = [p for p in post.get("piattaforme") or [] if not (done.get(p) or {}).get("url")]
    horizontal, vertical = _pick(paths)
    state["fase"] = "pubblicazione"
    state["tentativo_su_post_salvato_il"] = post_file.post_mtime(folder)
    post_file.write_state(folder, state)
    for platform in pending:
        try:
            url, warnings = _publish_one(platform, folder, post, state.get("lingua", "it"), done, horizontal, vertical)
            done[platform] = {"url": url, "quando": time.strftime("%Y-%m-%d %H:%M")}
            if warnings:
                done[platform]["avvisi"] = warnings
        except Exception as e:
            traceback.print_exc()
            done[platform] = {"errore": str(e)[:500], "quando": time.strftime("%Y-%m-%d %H:%M")}
        post_file.write_state(folder, state)

    failed = [p for p in pending if "errore" in done[p]]
    lines = [f"{'✅' if 'url' in done[p] else '❌'} {p}: {done[p].get('url') or done[p].get('errore')}"
             + "".join(f"\n   ⚠️ {w}" for w in done[p].get("avvisi", []))
             for p in post.get("piattaforme") or [] if p in done]
    if failed:
        state["fase"] = "errori"
        post_file.write_state(folder, state)
        notify(f"⚠️ {name}: pubblicazione parziale\n" + "\n".join(lines)
                        + "\n\nSalva di nuovo post.yaml per riprovare solo quelle fallite.")
        return
    state["fase"] = "pubblicato"
    post_file.write_state(folder, state)
    os.makedirs(config.DONE_DIR, exist_ok=True)
    target = os.path.join(config.DONE_DIR, name)
    if os.path.exists(target):
        target += time.strftime("-%Y%m%d-%H%M%S")
    shutil.move(folder, target)
    shutil.rmtree(_workdir(folder), ignore_errors=True)
    notify(f"🚀 {name}: pubblicato\n" + "\n".join(lines))


def handle(folder: str) -> None:
    paths = _videos(folder)
    if paths is None:
        return
    post = post_file.read_post(folder)
    state = post_file.read_state(folder)
    if post is None:
        if state.get("fase") != "errore":
            prepare(folder, paths)
        return
    if not post_file.approved(post):
        return
    # A failure is retried only when the author saves post.yaml again, never in a loop.
    if state.get("fase") in ("errori", "errore", "pubblicazione") and state.get("tentativo_su_post_salvato_il", 0) >= post_file.post_mtime(folder):
        return
    publish(folder, post, state, paths)


def _check_linkedin_token() -> None:
    global _linkedin_warned_day
    today = time.strftime("%Y-%m-%d")
    if linkedin.available() and linkedin.days_left() < 7 and _linkedin_warned_day != today:
        _linkedin_warned_day = today
        notify(f"🔑 Il token LinkedIn scade tra {max(linkedin.days_left(), 0):.0f} giorni: "
                        "rifallo con `python -m publisher.auth linkedin` e aggiorna il .env sul Pi.")


def main() -> None:
    os.makedirs(config.WORK_DIR, exist_ok=True)
    print(f"Watching {config.WATCH_DIR}; platforms: youtube={youtube.available()} linkedin={linkedin.available()} "
          f"nostr={nostr.available()}")
    while True:
        _check_linkedin_token()
        for entry in sorted(os.listdir(config.WATCH_DIR)):
            folder = os.path.join(config.WATCH_DIR, entry)
            if entry.startswith(("_", ".")) or not os.path.isdir(folder):
                continue
            try:
                handle(folder)
            except Exception as e:
                traceback.print_exc()
                retry = f"Cancella {entry}/stato.yaml per riprovare."
                if os.path.isdir(folder):
                    # Merged, not replaced: the links of what is already published must survive.
                    state = post_file.read_state(folder)
                    state.update(fase="errore", errore=str(e)[:1000])
                    if post_file.read_post(folder) is not None:
                        state["tentativo_su_post_salvato_il"] = post_file.post_mtime(folder)
                        retry = f"Salva di nuovo {entry}/post.yaml per riprovare."
                    post_file.write_state(folder, state)
                notify(f"❌ {entry}: {e}\n{retry}")
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
