"""Penumbra Account (accounts.eclip5e.com) sign-in for the bot: device sign-in, refresh, sign out.

Standard library only. Every call has a short timeout and raises PenumbraError; nothing here retries or sleeps,
the caller (the remote thread) decides.
"""
import json
import os
import pathlib
import platform
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

ACCOUNTS = "https://accounts.eclip5e.com"
CLIENT_ID = "000015"
TIMEOUT = (5, 10)  # connect, read (urllib takes one value: the larger is used per call)
REMOTE_FILE = pathlib.Path(os.environ.get("LOCALAPPDATA", ".")) / "pgbot" / "remote.json"
SIGNED_OUT = {"invalid_grant", "invalid_token", "access_denied", "expired_token", "http_401"}


class PenumbraError(Exception):
    def __init__(self, code, status=0):
        super().__init__(code)
        self.code = code
        self.status = status

    @property
    def signed_out(self):
        """The account no longer accepts this installation: link again (password changed, app disconnected)."""
        return self.code in SIGNED_OUT


def request(url, data=None, method=None, token=None, timeout=TIMEOUT[1], body=None, content_type=None):
    """One HTTP call; returns (status, parsed JSON or {}). data: dict sent as a form; body: raw bytes."""
    headers = {"Accept": "application/json", "User-Agent": "pgbot-remote"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Penumbra-Token"] = token
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        content_type = "application/x-www-form-urlencoded"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status, raw = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(1 << 20) if error.fp else b""
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise PenumbraError(f"network: {getattr(error, 'reason', error)}") from None
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        parsed = {"error": f"http_{status}"} if status >= 400 else {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


def _checked(status, parsed):
    if status >= 400 or "error" in parsed:
        raise PenumbraError(str(parsed.get("error") or f"http_{status}"), status)
    return parsed


def start_device_login():
    """Step 1: {device_code, user_code, verification_uri, verification_uri_complete, interval, expires_in}."""
    reply = _checked(*request(f"{ACCOUNTS}/api/device.php", {"client_id": CLIENT_ID}))
    if not reply.get("device_code") or not reply.get("user_code"):
        raise PenumbraError("bad_reply")
    return reply


def poll_device_login(device_code):
    """Step 2, once per interval: tokens when approved, None while pending; 'slow_down' raises."""
    status, reply = request(f"{ACCOUNTS}/api/token.php", {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": CLIENT_ID,
        "device_code": device_code})
    if reply.get("error") == "authorization_pending":
        return None
    reply = _checked(status, reply)
    if not reply.get("access_token") or not reply.get("refresh_token"):
        raise PenumbraError("bad_reply")
    return reply


def refresh(refresh_token):
    """New tokens; the old refresh token is dead after this call (save the result at once)."""
    reply = _checked(*request(f"{ACCOUNTS}/api/token.php", {
        "grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token}))
    if not reply.get("access_token") or not reply.get("refresh_token"):
        raise PenumbraError("bad_reply")
    return reply


def sign_out(access_token):
    """Revokes the token and its refresh token; errors ignored (the local link is removed anyway)."""
    try:
        request(f"{ACCOUNTS}/api/userinfo.php", method="DELETE", token=access_token, timeout=5)
    except PenumbraError:
        pass


class Link:
    """The saved link (remote.json): tokens, user, bot id/name and the Allow control switch."""

    def __init__(self, path=REMOTE_FILE):
        self.path = pathlib.Path(path)
        self.data = {}
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}  # missing or broken: simply not linked
        if not self.data.get("bot_id"):
            self.data["bot_id"] = secrets.token_hex(6)
        self.data.setdefault("bot_name", platform.node() or "pgbot")
        self.data.setdefault("allow_control", False)
        self.data.setdefault("enabled", True)

    def save(self):
        """Written to a temp file and renamed: a crash mid-write never leaves a broken file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        os.replace(temp, self.path)

    @property
    def linked(self):
        return bool(self.data.get("refresh_token"))

    @property
    def user_name(self):
        return (self.data.get("user") or {}).get("username") or "?"

    def store_tokens(self, reply):
        self.data["access_token"] = reply["access_token"]
        self.data["refresh_token"] = reply["refresh_token"]
        self.data["expires_at"] = time.time() + float(reply.get("expires_in") or 86400) - 120
        if isinstance(reply.get("user"), dict):
            self.data["user"] = reply["user"]
        self.save()

    def access_token(self):
        """A valid access token, refreshing (and saving) when it is about to expire."""
        if not self.linked:
            raise PenumbraError("not_linked")
        if self.data.get("access_token") and time.time() < float(self.data.get("expires_at") or 0):
            return self.data["access_token"]
        self.store_tokens(refresh(self.data["refresh_token"]))
        return self.data["access_token"]

    def force_refresh(self):
        self.data["expires_at"] = 0
        return self.access_token()

    def clear(self):
        for key in ("access_token", "refresh_token", "expires_at", "user"):
            self.data.pop(key, None)
        self.save()
