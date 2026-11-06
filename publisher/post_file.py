import os
import time

import yaml

POST = "post.yaml"
STATE = "stato.yaml"
TRANSCRIPT = "trascrizione.txt"
NOTES = ("note.txt", "note.md", "appunti.txt")

HEADER = """\
# Testi scritti da Claude a partire dalla trascrizione. Correggili qui, togli da `piattaforme`
# quelle da saltare, poi metti `pubblica: si` e salva: parte entro un minuto.
# L'esito di ogni piattaforma finisce in stato.yaml. Se una fallisce, salvare di nuovo questo
# file riprova solo quelle non ancora pubblicate.
"""


OPTIONAL = """\
# Facoltativi per YouTube: uscita programmata (ora italiana, es. 2026-11-07 18:00; vuoto = subito)
# e id della playlist in cui aggiungere il video lungo (quello dopo list= nel link della playlist).
programma:
playlist:
"""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(
    str, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|" if "\n" in s else None)
)


def _dump(data: dict) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)


def _write(path: str, text: str) -> None:
    # Syncthing must never pick up a half-written file.
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write_post(folder: str, platforms: list[str], texts: dict) -> None:
    _write(os.path.join(folder, POST), HEADER + "pubblica: no\n" + OPTIONAL + _dump({"piattaforme": platforms, **texts}))


def read_post(folder: str) -> dict | None:
    path = os.path.join(folder, POST)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def post_mtime(folder: str) -> float:
    return os.path.getmtime(os.path.join(folder, POST))


def approved(post: dict) -> bool:
    return post.get("pubblica") is True or str(post.get("pubblica", "")).strip().lower() in {"si", "sì", "yes", "true"}


def read_state(folder: str) -> dict:
    path = os.path.join(folder, STATE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_state(folder: str, state: dict) -> None:
    state["aggiornato"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(os.path.join(folder, STATE), _dump(state))


def write_transcript(folder: str, text: str) -> None:
    _write(os.path.join(folder, TRANSCRIPT), text + "\n")


def _seconds(stamp: str) -> float:
    h, m, rest = stamp.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def parse_srt(path: str) -> list[tuple[float, float, str]]:
    with open(path, encoding="utf-8-sig") as f:
        blocks = f.read().replace("\r", "").strip().split("\n\n")
    cues = []
    for block in blocks:
        lines = [l for l in block.split("\n") if l.strip()]
        times = next((l for l in lines if "-->" in l), None)
        if times:
            start, end = times.split("-->")
            cues.append((_seconds(start), _seconds(end), " ".join(lines[lines.index(times) + 1:])))
    return cues


def timed_lines(parts: list[tuple[float, str]], every: float = 10.0) -> str:
    """Transcript as [m:ss] lines, so Claude can place YouTube chapters on real times."""
    lines, current, mark = [], [], None
    for start, text in parts:
        if mark is None or start - mark >= every:
            if current:
                lines.append(f"[{int(mark // 60)}:{int(mark % 60):02d}] " + " ".join(current))
            current, mark = [], start
        current.append(text)
    if current:
        lines.append(f"[{int(mark // 60)}:{int(mark % 60):02d}] " + " ".join(current))
    return "\n".join(lines)


def srt_files(folder: str) -> list[str]:
    return [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n.lower().endswith(".srt")]


def srt_for(folder: str, duration: float) -> str | None:
    """The .srt that belongs to a video: the one ending where the video ends."""
    for path in srt_files(folder):
        cues = parse_srt(path)
        if cues and abs(cues[-1][1] - duration) <= 3:
            return path
    return None


def read_subtitles(folder: str) -> tuple[str, str]:
    """The editor's .srt, when there is one: no need to transcribe again."""
    files = srt_files(folder)
    if not files:
        return "", ""
    return timed_lines([(start, text) for start, _, text in parse_srt(files[0])]), os.getenv("PUBLISH_SRT_LANGUAGE", "it")


def read_notes(folder: str) -> str:
    for name in NOTES:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    return ""
