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
    _write(os.path.join(folder, POST), HEADER + "pubblica: no\n" + _dump({"piattaforme": platforms, **texts}))


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


def read_subtitles(folder: str) -> tuple[str, str]:
    """The text of an .srt next to the videos, when the editor already made one: no need to transcribe again."""
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(".srt"):
            with open(os.path.join(folder, name), encoding="utf-8-sig") as f:
                lines = [l.strip() for l in f if l.strip() and not l.strip().isdigit() and "-->" not in l]
            return " ".join(lines), os.getenv("PUBLISH_SRT_LANGUAGE", "it")
    return "", ""


def read_notes(folder: str) -> str:
    for name in NOTES:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
    return ""
