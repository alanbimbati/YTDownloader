import requests

from . import config


def notify(text: str) -> None:
    if not (config.BOT_TOKEN and config.ADMIN_USER_ID):
        print(text)
        return
    try:
        r = requests.post(f"{config.API_BASE_URL}/bot{config.BOT_TOKEN}/sendMessage", timeout=60, data={
            "chat_id": config.ADMIN_USER_ID, "text": text[:4000], "disable_web_page_preview": "true",
        })
        if not r.json().get("ok"):
            print(f"Notify failed ({r.text[:200]}): {text}")
    except Exception as e:
        print(f"Notify failed ({e}): {text}")
