import base64
import hashlib
import json
import os
import time

import requests
from coincurve import PrivateKey
from websockets.sync.client import connect

from .. import config
from ..media import Video

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def available() -> bool:
    return bool(config.NOSTR_NSEC)


def _bech32_decode(value: str) -> bytes:
    data = [_CHARSET.index(c) for c in value.lower().rsplit("1", 1)[1][:-6]]
    acc, bits, out = 0, 0, bytearray()
    for v in data:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def _polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_encode(hrp: str, data: bytes) -> str:
    acc, bits, words = 0, 0, []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            words.append((acc >> bits) & 31)
    if bits:
        words.append((acc << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(expanded + words + [0] * 6) ^ 1
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[w] for w in words + checksum)


def _key() -> PrivateKey:
    nsec = config.NOSTR_NSEC
    return PrivateKey(_bech32_decode(nsec) if nsec.startswith("nsec1") else bytes.fromhex(nsec))


def sign(kind: int, content: str, tags: list[list[str]], key: PrivateKey) -> dict:
    pubkey = key.public_key_xonly.format().hex()
    created_at = int(time.time())
    payload = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    event_id = hashlib.sha256(payload.encode()).digest()
    return {
        "id": event_id.hex(), "pubkey": pubkey, "created_at": created_at, "kind": kind,
        "tags": tags, "content": content, "sig": key.sign_schnorr(event_id, os.urandom(32)).hex(),
    }


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def blossom_upload(path: str, mime: str, key: PrivateKey) -> dict:
    digest = _sha256(path)
    auth = sign(24242, f"Upload {os.path.basename(path)}",
                [["t", "upload"], ["x", digest], ["expiration", str(int(time.time()) + 3600)]], key)
    with open(path, "rb") as f:
        r = requests.put(f"{config.BLOSSOM_SERVER}/upload", data=f, timeout=3600, headers={
            "Authorization": "Nostr " + base64.b64encode(json.dumps(auth).encode()).decode(),
            "Content-Type": mime,
            "Content-Length": str(os.path.getsize(path)),
            "X-SHA-256": digest,
        })
    if r.status_code >= 400:
        raise RuntimeError(f"Blossom upload: HTTP {r.status_code} {r.headers.get('X-Reason', r.text[:200])}")
    blob = r.json()
    blob.setdefault("sha256", digest)
    return blob


def _imeta(video: Video, blob: dict, image: str = "") -> list[str]:
    tag = ["imeta", f"url {blob['url']}", "m video/mp4", f"x {blob['sha256']}", f"dim {video.dim}",
           f"duration {video.duration:.1f}"]
    if image:
        tag.append(f"image {image}")
    return tag


def publish(events: list[dict]) -> list[str]:
    """Sends every event to every relay; returns the relays that accepted all of them."""
    accepted = []
    for relay in config.NOSTR_RELAYS:
        try:
            with connect(relay, open_timeout=15, close_timeout=5) as ws:
                ok = True
                for ev in events:
                    ws.send(json.dumps(["EVENT", ev]))
                    while True:
                        msg = json.loads(ws.recv(timeout=30))
                        if msg[0] == "OK" and msg[1] == ev["id"]:
                            ok = ok and bool(msg[2])
                            break
                if ok:
                    accepted.append(relay)
        except Exception as e:
            print(f"Relay {relay}: {e}")
    if not accepted:
        raise RuntimeError("no relay accepted the events")
    return accepted


def post(horizontal: Video | None, vertical: Video | None, texts: dict, thumb: str | None, upload_path) -> str:
    """upload_path(video) gives the file to upload (a smaller copy when the original exceeds the server's limit)."""
    key = _key()
    hashtags = [h.lstrip("#").lower() for h in texts.get("hashtags", [])]
    title, text = texts["title"], texts["text"]
    image = blossom_upload(thumb, "image/jpeg", key)["url"] if thumb else ""
    published_at = str(int(time.time()))
    events = []
    main = None
    for video, kind in ((horizontal, 21), (vertical, 22)):
        if not video:
            continue
        blob = blossom_upload(upload_path(video), "video/mp4", key)
        main = main or (video, blob)
        tags = [["title", title], ["published_at", published_at], ["alt", title], _imeta(video, blob, image)]
        events.append(sign(kind, text, tags + [["t", h] for h in hashtags], key))
    # Most clients only show kind 1 notes: the same video as a plain note reaches them too.
    note = f"{text}\n\n{main[1]['url']}" + ("\n\n" + " ".join(f"#{h}" for h in hashtags) if hashtags else "")
    events.append(sign(1, note, [_imeta(*main, image)] + [["t", h] for h in hashtags], key))
    relays = publish(events)
    return f"https://njump.me/{bech32_encode('note', bytes.fromhex(events[-1]['id']))} ({len(relays)} relay)"
