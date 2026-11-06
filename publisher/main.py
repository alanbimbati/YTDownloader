"""Publishes a horizontal + vertical video pair dropped in a Syncthing folder, after the author approves Claude's texts."""
import os
import shutil
import time
import traceback

from . import config, media, post_file, texts
from .media import Video
from .platforms import linkedin, nostr, telegram, youtube

ORDER = ["youtube", "shorts", "linkedin", "telegram", "nostr"]
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
             "linkedin": lambda: linkedin.available(), "telegram": lambda: telegram.available(),
             "nostr": lambda: nostr.available()}


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
    written = texts.write_posts(transcript, language, post_file.read_notes(folder))
    platforms = _applicable(horizontal, vertical)
    post_file.write_post(folder, platforms, written)
    post_file.write_state(folder, {"fase": "in_approvazione", "lingua": language, "piattaforme": {}})
    found = ", ".join(f"{'verticale' if v.vertical else 'orizzontale'} {v.dim} {v.duration:.0f}s"
                      for v in (horizontal, vertical) if v)
    telegram.notify(f"📝 {name}: testi pronti ({found}).\nControlla {name}/post.yaml e metti `pubblica: si`.\n"
                    f"Piattaforme: {', '.join(f'{p}' + ('' if AVAILABLE[p]() else ' (senza credenziali)') for p in platforms)}\n\n"
                    f"YouTube: {written['youtube']['title']}")


def _publish_one(platform: str, folder: str, post: dict, language: str,
                 horizontal: Video | None, vertical: Video | None) -> str:
    if not AVAILABLE[platform]():
        raise RuntimeError("credenziali mancanti nel .env del Pi")
    work = _workdir(folder)
    main = horizontal or vertical
    if platform == "youtube":
        p = post["youtube"]
        return youtube.upload(horizontal.path, p["title"], p["description"], p.get("tags") or [], language)
    if platform == "shorts":
        p = post["shorts"]
        description = p["description"] if "#shorts" in p["description"].lower() else p["description"] + "\n#Shorts"
        return youtube.upload(vertical.path, p["title"], description, [], language)
    if platform == "linkedin":
        path = media.shrink(main, os.path.join(work, "linkedin.mp4"), config.LINKEDIN_MAX_BYTES)
        return linkedin.upload(path, post["linkedin"]["text"], post["youtube"]["title"])
    if platform == "telegram":
        path = media.shrink(main, os.path.join(work, "telegram.mp4"), config.TELEGRAM_MAX_BYTES)
        return telegram.post_video(media.probe(path) if path != main.path else main, post["telegram"]["text"])
    if platform == "nostr":
        thumb = media.thumbnail(main, os.path.join(work, "thumb.jpg"))
        upload_path = lambda v: media.shrink(v, os.path.join(work, f"nostr-{'v' if v.vertical else 'h'}.mp4"), config.BLOSSOM_MAX_BYTES)
        return nostr.post(horizontal, vertical, post["nostr"], thumb, upload_path)
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
            url = _publish_one(platform, folder, post, state.get("lingua", "it"), horizontal, vertical)
            done[platform] = {"url": url, "quando": time.strftime("%Y-%m-%d %H:%M")}
        except Exception as e:
            traceback.print_exc()
            done[platform] = {"errore": str(e)[:500], "quando": time.strftime("%Y-%m-%d %H:%M")}
        post_file.write_state(folder, state)

    failed = [p for p in pending if "errore" in done[p]]
    lines = [f"{'✅' if 'url' in done[p] else '❌'} {p}: {done[p].get('url') or done[p].get('errore')}"
             for p in post.get("piattaforme") or [] if p in done]
    if failed:
        state["fase"] = "errori"
        post_file.write_state(folder, state)
        telegram.notify(f"⚠️ {name}: pubblicazione parziale\n" + "\n".join(lines)
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
    telegram.notify(f"🚀 {name}: pubblicato\n" + "\n".join(lines))


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
        telegram.notify(f"🔑 Il token LinkedIn scade tra {max(linkedin.days_left(), 0):.0f} giorni: "
                        "rifallo con `python -m publisher.auth linkedin` e aggiorna il .env sul Pi.")


def main() -> None:
    os.makedirs(config.WORK_DIR, exist_ok=True)
    print(f"Watching {config.WATCH_DIR}; platforms: youtube={youtube.available()} linkedin={linkedin.available()} "
          f"telegram={telegram.available()} nostr={nostr.available()}")
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
                telegram.notify(f"❌ {entry}: {e}\n{retry}")
        time.sleep(config.POLL_SECONDS)


if __name__ == "__main__":
    main()
