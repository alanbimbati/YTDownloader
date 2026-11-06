import json
import os

import requests

from .. import config

CHUNK = 16 * 1024 * 1024


def available() -> bool:
    return bool(config.YOUTUBE_CLIENT_ID and config.YOUTUBE_CLIENT_SECRET and config.YOUTUBE_REFRESH_TOKEN)


def _access_token() -> str:
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config.YOUTUBE_CLIENT_ID,
        "client_secret": config.YOUTUBE_CLIENT_SECRET,
        "refresh_token": config.YOUTUBE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def upload(path: str, title: str, description: str, tags: list[str], language: str) -> str:
    token = _access_token()
    size = os.path.getsize(path)
    meta = {
        "snippet": {
            "title": title[:100],
            "description": description.replace("<", "‹").replace(">", "›")[:4900],
            "tags": tags,
            "categoryId": config.YOUTUBE_CATEGORY,
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": {"privacyStatus": config.YOUTUBE_PRIVACY, "selfDeclaredMadeForKids": False},
    }
    r = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/*",
        },
        data=json.dumps(meta), timeout=60,
    )
    r.raise_for_status()
    session = r.headers["Location"]

    sent = 0
    with open(path, "rb") as f:
        while True:
            f.seek(sent)
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            r = requests.put(session, data=chunk, timeout=600, headers={
                "Authorization": f"Bearer {token}",
                "Content-Range": f"bytes {sent}-{end}/{size}",
            })
            if r.status_code in (200, 201):
                return f"https://youtu.be/{r.json()['id']}"
            if r.status_code != 308:
                raise RuntimeError(f"YouTube upload failed: HTTP {r.status_code} {r.text[:300]}")
            # The server says how far it got: resume from there rather than trusting what was sent.
            rng = r.headers.get("Range")
            sent = int(rng.split("-")[1]) + 1 if rng else 0
