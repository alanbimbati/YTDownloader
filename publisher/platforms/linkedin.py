import os
import re
import time
from urllib.parse import quote

import requests

from .. import config

API = "https://api.linkedin.com/rest"


def available() -> bool:
    return bool(config.LINKEDIN_ACCESS_TOKEN)


def days_left() -> float:
    if not config.LINKEDIN_TOKEN_EXPIRES_AT:
        return float("inf")
    return (config.LINKEDIN_TOKEN_EXPIRES_AT - time.time()) / 86400


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.LINKEDIN_ACCESS_TOKEN}",
        "LinkedIn-Version": config.LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }


def _check(r: requests.Response, what: str) -> requests.Response:
    if r.status_code >= 400:
        raise RuntimeError(f"LinkedIn {what}: HTTP {r.status_code} {r.text[:300]}")
    return r


def _person_urn() -> str:
    if config.LINKEDIN_PERSON_URN:
        return config.LINKEDIN_PERSON_URN
    r = _check(requests.get("https://api.linkedin.com/v2/userinfo", headers=_headers(), timeout=30), "userinfo")
    return f"urn:li:person:{r.json()['sub']}"


def escape(text: str) -> str:
    # Posts are "little text": these characters are markup and LinkedIn drops what follows an unescaped one.
    # A hashtag stays a hashtag only through its own template.
    parts = re.split(r"#(\w+)", text)
    return "".join(
        "{hashtag|\\#|" + part + "}" if i % 2 else re.sub(r"[\\|{}@\[\]()<>#*_~]", lambda m: "\\" + m.group(0), part)
        for i, part in enumerate(parts)
    )


def upload(path: str, text: str, title: str) -> str:
    owner = _person_urn()
    size = os.path.getsize(path)
    init = _check(requests.post(f"{API}/videos?action=initializeUpload", headers=_headers(), timeout=60, json={
        "initializeUploadRequest": {"owner": owner, "fileSizeBytes": size, "uploadCaptions": False, "uploadThumbnail": False},
    }), "initializeUpload").json()["value"]

    etags = []
    with open(path, "rb") as f:
        for part in init["uploadInstructions"]:
            f.seek(part["firstByte"])
            chunk = f.read(part["lastByte"] - part["firstByte"] + 1)
            r = _check(requests.put(part["uploadUrl"], data=chunk, timeout=600,
                                    headers={"Content-Type": "application/octet-stream"}), "part upload")
            etags.append(r.headers["etag"])
    video = init["video"]
    _check(requests.post(f"{API}/videos?action=finalizeUpload", headers=_headers(), timeout=60, json={
        "finalizeUploadRequest": {"video": video, "uploadToken": init.get("uploadToken", ""), "uploadedPartIds": etags},
    }), "finalizeUpload")

    for _ in range(120):
        status = _check(requests.get(f"{API}/videos/{quote(video, safe='')}", headers=_headers(), timeout=30), "video status").json()
        if status.get("status") == "AVAILABLE":
            break
        if status.get("status") == "PROCESSING_FAILED":
            raise RuntimeError(f"LinkedIn could not process the video: {status.get('processingFailureReason')}")
        time.sleep(10)
    else:
        raise RuntimeError("LinkedIn still processing the video after 20 minutes")

    r = _check(requests.post(f"{API}/posts", headers=_headers(), timeout=60, json={
        "author": owner,
        "commentary": escape(text),
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "content": {"media": {"title": title, "id": video}},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }), "post")
    return f"https://www.linkedin.com/feed/update/{r.headers.get('x-restli-id', '')}"
