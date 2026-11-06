import requests

from .. import config
from ..media import Video


def available() -> bool:
    return bool(config.BOT_TOKEN and config.TELEGRAM_CHANNEL)


def _call(method: str, data: dict, timeout: int = 60) -> dict:
    r = requests.post(f"{config.API_BASE_URL}/bot{config.BOT_TOKEN}/{method}", data=data, timeout=timeout)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method}: {body.get('description', r.text[:300])}")
    return body["result"]


def notify(text: str) -> None:
    if not (config.BOT_TOKEN and config.ADMIN_USER_ID):
        print(text)
        return
    try:
        _call("sendMessage", {"chat_id": config.ADMIN_USER_ID, "text": text[:4000], "disable_web_page_preview": "true"})
    except Exception as e:
        print(f"Notify failed ({e}): {text}")


def post_video(video: Video, caption: str) -> str:
    # The local Bot API server reads the file from the shared volume: nothing big passes through memory.
    result = _call("sendVideo", {
        "chat_id": config.TELEGRAM_CHANNEL,
        "video": f"file://{video.path}",
        "caption": caption[:1024],
        "supports_streaming": "true",
        "width": video.width,
        "height": video.height,
        "duration": int(video.duration),
    }, timeout=3600)
    chat = result["chat"]
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{result['message_id']}"
    return f"https://t.me/c/{str(chat['id']).removeprefix('-100')}/{result['message_id']}"
