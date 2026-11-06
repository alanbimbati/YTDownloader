"""One-off OAuth logins, run on the laptop: python -m publisher.auth youtube|linkedin. Prints the .env lines."""
import http.server
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser

import requests

REDIRECT = "http://localhost:8765/callback"


def _wait_for_code(url: str, state: str) -> str:
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write("Fatto, puoi chiudere questa scheda.".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", 8765), Handler)
    print(f"Apri questo link se il browser non parte:\n{url}\n")
    webbrowser.open(url)
    while "code" not in result and "error" not in result:
        server.handle_request()
    if result.get("state") != state or "code" not in result:
        sys.exit(f"Login fallito: {result}")
    return result["code"]


def _need(name: str) -> str:
    value = os.getenv(name) or input(f"{name}: ").strip()
    if not value:
        sys.exit(f"{name} mancante")
    return value


def youtube() -> None:
    client_id, secret = _need("YOUTUBE_CLIENT_ID"), _need("YOUTUBE_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "state": state,
        # force-ssl on top of upload: captions, thumbnails, playlists and the channel's own handle.
        "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl",
        "access_type": "offline", "prompt": "consent",
    })
    code = _wait_for_code(url, state)
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    })
    r.raise_for_status()
    print(f"\nYOUTUBE_CLIENT_ID={client_id}\nYOUTUBE_CLIENT_SECRET={secret}\nYOUTUBE_REFRESH_TOKEN={r.json()['refresh_token']}")


def linkedin() -> None:
    client_id, secret = _need("LINKEDIN_CLIENT_ID"), _need("LINKEDIN_CLIENT_SECRET")
    state = secrets.token_urlsafe(16)
    url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": state,
        "scope": "openid profile w_member_social",
    })
    code = _wait_for_code(url, state)
    r = requests.post("https://www.linkedin.com/oauth/v2/accessToken", timeout=30, data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "client_secret": secret,
    })
    r.raise_for_status()
    token = r.json()
    me = requests.get("https://api.linkedin.com/v2/userinfo", timeout=30,
                      headers={"Authorization": f"Bearer {token['access_token']}"})
    me.raise_for_status()
    print(f"\nLINKEDIN_ACCESS_TOKEN={token['access_token']}\n"
          f"LINKEDIN_TOKEN_EXPIRES_AT={int(time.time()) + int(token['expires_in'])}\n"
          f"LINKEDIN_PERSON_URN=urn:li:person:{me.json()['sub']}")


if __name__ == "__main__":
    {"youtube": youtube, "linkedin": linkedin}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
