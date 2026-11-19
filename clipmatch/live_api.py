"""Authenticated HTTP calls for the live chat worker."""

import asyncio
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2"
REQUIRED_SCOPES = {"user:read:chat", "clips:edit"}


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, *, code: int | None = None):
        self.status = status
        self.code = code
        super().__init__(message)


def save_refresh_token(token_path: Path, token: str) -> None:
    if not token or any(char.isspace() for char in token):
        raise ValueError("Invalid refresh token")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=token_path.parent, delete=False
    ) as temporary:
        name = Path(temporary.name)
        try:
            temporary.write(token)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.close()
            os.replace(name, token_path)
        finally:
            temporary.close()
            name.unlink(missing_ok=True)


def _retry_delay(headers, body):
    try:
        if "Retry-After" in headers:
            delay = float(headers["Retry-After"])
        elif "retry_after" in body:
            delay = float(body["retry_after"])
        elif "Ratelimit-Reset" in headers:
            delay = float(headers["Ratelimit-Reset"]) - time.time()
        else:
            delay = 1.0
        return max(0.0, delay) if math.isfinite(delay) else 1.0
    except (TypeError, ValueError):
        return 1.0


class _HelixBucket:
    def __init__(self):
        self.reset = 0.0

    def observe(self, status, headers, body):
        if status == 429 or headers.get("Ratelimit-Remaining") == "0":
            self.reset = max(self.reset, time.time() + _retry_delay(headers, body))


def _clip_error(status, body):
    if status == 404:
        return "Twitch clip creation returned HTTP 404 (broadcaster offline or unavailable)"
    if status != 403:
        return f"Twitch clip creation returned HTTP {status}"
    message = str(body.get("message", "")).lower()
    if "disabled" in message or "not enabled" in message:
        reason = "clips disabled on this channel"
    elif "follow" in message or "subscrib" in message:
        reason = "clips restricted to followers or subscribers"
    elif "banned" in message or "timed out" in message:
        reason = "account banned or timed out"
    else:
        reason = "check channel clip settings and account restrictions"
    return f"Twitch clip creation returned HTTP 403 ({reason})"


async def _request(session, service, method, url, *, attempts=3, bucket=None, timeout_seconds=15,
                   on_attempt: Callable[[], None] | None = None, **kwargs):
    # Replay only explicit throttling responses. A lost POST response can mean
    # the service already created the clip, subscription, or Discord message.
    creating_clip = method == "POST" and url == f"{HELIX}/clips"
    wait_budget = 10.0 if creating_clip else 60.0
    for attempt in range(attempts):
        if bucket:
            delay = max(0.0, bucket.reset - time.time())
            if delay > wait_budget:
                raise ApiError(429, f"{service} rate limit exceeds the request wait budget")
            if delay:
                await asyncio.sleep(delay)
                wait_budget -= delay
        if on_attempt is not None:
            on_attempt()
        try:
            async with session.request(
                method, url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout_seconds), **kwargs
            ) as response:
                status = response.status
                headers = response.headers
                try:
                    body = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    body = None
        except (aiohttp.ClientError, TimeoutError):
            raise ApiError(0, f"{service} connection failed") from None

        error_body = body if isinstance(body, dict) else {}
        if bucket:
            bucket.observe(status, headers, error_body)
        if 200 <= status < 300:
            if isinstance(body, dict) or status == 204:
                return body or {}
            raise ApiError(502, f"{service} returned an invalid response")
        if status == 429 and attempt + 1 < attempts:
            if bucket:
                continue
            delay = _retry_delay(headers, error_body)
            if delay <= wait_budget:
                await asyncio.sleep(delay)
                wait_budget -= delay
                continue
        if creating_clip:
            raise ApiError(status, _clip_error(status, error_body))
        code = error_body.get("code")
        raise ApiError(status, f"{service} returned HTTP {status}", code=code if type(code) is int else None)


def _rows(body):
    rows = body.get("data")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ApiError(502, "Twitch returned invalid data")
    return rows


def _field(body, key):
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ApiError(502, f"Twitch response is missing {key}")
    return value


def _batches(values):
    unique = list(dict.fromkeys(values))
    for start in range(0, len(unique), 100):
        yield unique[start:start + 100]


async def exchange_code(session, client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    body = await _request(session, "Twitch OAuth", "POST", f"{OAUTH}/token", data={
        "grant_type": "authorization_code", "client_id": client_id,
        "client_secret": client_secret, "code": code, "redirect_uri": redirect_uri,
    })
    return _field(body, "refresh_token")


class LiveAPI:
    def __init__(self, session, client_id: str, client_secret: str, refresh_token: str, token_path: Path):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        try:
            self._refresh_token = token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self._refresh_token = refresh_token
        self._access_token = None
        self._refresh_lock = asyncio.Lock()
        self._bucket = _HelixBucket()

    async def _refresh(self, rejected_token):
        async with self._refresh_lock:
            if self._access_token != rejected_token:
                return
            if not self._refresh_token:
                raise ApiError(401, "A Twitch refresh token is required")
            body = await _request(self.session, "Twitch OAuth", "POST", f"{OAUTH}/token", data={
                "grant_type": "refresh_token", "client_id": self.client_id,
                "client_secret": self.client_secret, "refresh_token": self._refresh_token,
            })
            access = _field(body, "access_token")
            self._refresh_token = _field(body, "refresh_token")
            save_refresh_token(self.token_path, self._refresh_token)
            self._access_token = access

    async def _authenticated(self, method, url, **kwargs):
        await self._refresh(None)
        for attempt in range(2):
            token = self._access_token
            try:
                bucket = self._bucket if url.startswith(HELIX + "/") else None
                body = await _request(self.session, "Twitch", method, url, bucket=bucket, headers={
                    "Authorization": f"Bearer {token}", "Client-Id": self.client_id
                }, **kwargs)
                return body, token
            except ApiError as exc:
                if exc.status != 401 or attempt:
                    raise
                await self._refresh(token)

    async def _validated(self):
        body, token = await self._authenticated("GET", f"{OAUTH}/validate")
        scopes = body.get("scopes")
        if body.get("client_id") != self.client_id:
            raise ApiError(401, "Twitch token belongs to another application")
        if not isinstance(scopes, list) or not REQUIRED_SCOPES.issubset(scopes):
            raise ApiError(403, "Twitch token requires user:read:chat and clips:edit")
        _field(body, "user_id")
        return body, token

    async def validate(self) -> str:
        body, _ = await self._validated()
        return body["user_id"]

    async def irc_credentials(self) -> tuple[str, str]:
        body, token = await self._validated()
        if "chat:read" not in body["scopes"]:
            raise ApiError(403, "Twitch IRC requires chat:read")
        return _field(body, "login"), token

    async def resolve_channels(self, logins: Iterable[str]) -> dict[str, str]:
        channels = {}
        for batch in _batches(logins):
            body, _ = await self._authenticated(
                "GET", f"{HELIX}/users", params=[("login", login) for login in batch]
            )
            channels.update((_field(user, "id"), _field(user, "login")) for user in _rows(body))
        return channels

    async def live_ids(self, ids: Iterable[str]) -> set[str]:
        live = set()
        for batch in _batches(ids):
            params = [("user_id", user_id) for user_id in batch] + [("first", "100")]
            body, _ = await self._authenticated("GET", f"{HELIX}/streams", params=params)
            live.update(_field(stream, "user_id") for stream in _rows(body))
        return live

    async def subscribe(self, kind: str, broadcaster_id: str, user_id: str, session_id: str):
        condition = {"broadcaster_user_id": broadcaster_id}
        if kind == "channel.chat.message":
            condition["user_id"] = user_id
        url = f"{HELIX}/eventsub/subscriptions"
        subscription = {
            "type": kind, "version": "1", "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id},
        }
        try:
            await self._authenticated("POST", url, json=subscription)
        except ApiError as exc:
            if exc.status not in (0, 408, 409) and exc.status < 500:
                raise
            if not await self._has_subscription(url, subscription):
                raise

    async def _has_subscription(self, url, subscription):
        params = {"type": subscription["type"]}
        seen = set()
        while True:
            body, _ = await self._authenticated("GET", url, params=params)
            for row in _rows(body):
                transport = row.get("transport") or {}
                if (row.get("status") == "enabled"
                        and all(row.get(key) == subscription[key] for key in ("type", "version", "condition"))
                        and all(transport.get(key) == value for key, value in subscription["transport"].items())):
                    return True
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor or cursor in seen:
                return False
            seen.add(cursor)
            params = {"type": subscription["type"], "after": cursor}

    async def create_clip(self, broadcaster_id: str, *, on_attempt: Callable[[], None] | None = None) -> str:
        body, _ = await self._authenticated("POST", f"{HELIX}/clips", params={
            "broadcaster_id": broadcaster_id, "duration": "60",
        }, on_attempt=on_attempt)
        rows = _rows(body)
        if not rows:
            raise ApiError(502, "Twitch returned no clip ID")
        return _field(rows[0], "id")

    async def confirm_clip(self, clip_id: str) -> str:
        try:
            async with asyncio.timeout(60):
                while True:
                    try:
                        body, _ = await self._authenticated("GET", f"{HELIX}/clips", params={"id": clip_id})
                    except ApiError as exc:
                        if exc.status != 0 and exc.status < 500:
                            raise
                    else:
                        for clip in _rows(body):
                            if clip.get("id") == clip_id:
                                return _field(clip, "url")
                    await asyncio.sleep(3)
        except TimeoutError:
            raise ApiError(504, "Twitch did not confirm the clip within 60 seconds") from None


def discord_webhook_url(value: str, setting: str) -> str:
    value = value.strip()
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc not in {"discord.com", "canary.discord.com",
            "ptb.discord.com", "discordapp.com"} or url.fragment or url.query
            or not re.fullmatch(r"/api/(?:v[0-9]+/)?webhooks/[0-9]+/[\w.-]+", url.path)):
        raise ValueError(f"{setting} must be a Discord webhook URL.")
    return value


async def send_discord(session, webhook_url: str, message: str) -> None:
    await _request(session, "Discord", "POST", webhook_url, attempts=2, params={"wait": "true"},
                   json={"content": message, "allowed_mentions": {"parse": []}})
