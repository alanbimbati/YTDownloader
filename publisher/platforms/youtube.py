import json
import os
import uuid

import requests

from .. import config

CHUNK = 16 * 1024 * 1024
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
_subscribe_url = ""


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


def _check(r: requests.Response, what: str) -> requests.Response:
    if r.status_code >= 400:
        raise RuntimeError(f"{what}: HTTP {r.status_code} {r.text[:300]}")
    return r


def subscribe_url(token: str) -> str:
    """The channel link that opens the subscribe confirmation: one click from the description to a subscriber."""
    global _subscribe_url
    if not _subscribe_url:
        r = _check(requests.get(f"{API}/channels?part=snippet&mine=true", timeout=30,
                                headers={"Authorization": f"Bearer {token}"}), "channel")
        snippet = r.json()["items"][0]["snippet"]
        handle = snippet.get("customUrl") or f"channel/{r.json()['items'][0]['id']}"
        _subscribe_url = f"https://www.youtube.com/{handle}?sub_confirmation=1"
    return _subscribe_url


def _upload(token: str, path: str, meta: dict) -> str:
    size = os.path.getsize(path)
    r = _check(requests.post(
        f"{UPLOAD}/videos?uploadType=resumable&part=snippet,status",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/*",
        },
        data=json.dumps(meta), timeout=60,
    ), "YouTube upload start")
    session = r.headers["Location"]
    sent = 0
    with open(path, "rb") as f:
        while True:
            f.seek(sent)
            chunk = f.read(CHUNK)
            r = requests.put(session, data=chunk, timeout=600, headers={
                "Authorization": f"Bearer {token}",
                "Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}",
            })
            if r.status_code in (200, 201):
                return r.json()["id"]
            if r.status_code != 308:
                raise RuntimeError(f"YouTube upload failed: HTTP {r.status_code} {r.text[:300]}")
            # The server says how far it got: resume from there rather than trusting what was sent.
            rng = r.headers.get("Range")
            sent = int(rng.split("-")[1]) + 1 if rng else 0


def _captions(token: str, video_id: str, srt: str, language: str) -> None:
    boundary = uuid.uuid4().hex
    meta = json.dumps({"snippet": {"videoId": video_id, "language": language, "name": "", "isDraft": False}})
    with open(srt, "rb") as f:
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + f.read() + f"\r\n--{boundary}--".encode()
    _check(requests.post(f"{UPLOAD}/captions?part=snippet&uploadType=multipart", data=body, timeout=120, headers={
        "Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}",
    }), "sottotitoli")


def _thumbnail(token: str, video_id: str, image: str) -> None:
    with open(image, "rb") as f:
        _check(requests.post(f"{UPLOAD}/thumbnails/set?videoId={video_id}", data=f.read(), timeout=120, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "image/jpeg",
        }), "miniatura (serve il canale verificato col telefono)")


def _playlist(token: str, video_id: str, playlist: str) -> None:
    _check(requests.post(f"{API}/playlistItems?part=snippet", timeout=30, headers={"Authorization": f"Bearer {token}"},
                         json={"snippet": {"playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}),
           "playlist")


def publish(path: str, *, title: str, description: str, tags: list[str], category: str, language: str,
            publish_at: str = "", srt: str | None = None, thumbnail: str | None = None, playlist: str = "") -> tuple[str, list[str]]:
    """Uploads the video, then the extras; an extra that fails is a warning, never a failed upload."""
    token = _access_token()
    warnings = []
    try:
        description = description.rstrip() + f"\n\n🔔 Iscriviti: {subscribe_url(token)}"
    except Exception as e:
        warnings.append(f"link iscrizione: {e}")
    status = {"privacyStatus": config.YOUTUBE_PRIVACY, "selfDeclaredMadeForKids": False, "embeddable": True}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)
    video_id = _upload(token, path, {
        "snippet": {
            "title": title[:100],
            "description": description.replace("<", "‹").replace(">", "›")[:4900],
            "tags": tags,
            "categoryId": category or "22",
            "defaultLanguage": language,
            "defaultAudioLanguage": language,
        },
        "status": status,
    })
    for what, run in (("sottotitoli", srt and (lambda: _captions(token, video_id, srt, language))),
                      ("miniatura", thumbnail and (lambda: _thumbnail(token, video_id, thumbnail))),
                      ("playlist", playlist and (lambda: _playlist(token, video_id, playlist)))):
        if run:
            try:
                run()
            except Exception as e:
                warnings.append(f"{what}: {e}")
    return f"https://youtu.be/{video_id}", warnings
