"""Publish a verified master to the configured creator's YouTube Shorts channel."""
import argparse
import asyncio
import base64
from collections.abc import Callable
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlencode, urlsplit
import webbrowser

import aiohttp
from aiohttp import web

from .cache import atomic_json


CHANNEL_ID = "UCqd66XANzi22AxRvjeoOz8A"
API_URL = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly")
DEFAULT_GRANT = Path(".clipbot/youtube-grant.json")


class PublishError(ValueError):
    """A safe-to-log service failure, with a delay when retryable."""

    def __init__(self, message, delay=0):
        super().__init__(message)
        self.delay = delay


def _retry_after(headers, default):
    raw = headers.get("Retry-After")
    if raw is None:
        return default
    if raw.isascii() and raw.isdigit() and len(raw) <= 10:
        return max(1, int(raw))
    try:
        return max(1, int(parsedate_to_datetime(raw).timestamp() - time.time()) + 1)
    except (TypeError, ValueError, OverflowError):
        return default


async def _request(session, method, url, *, expected=(200,), timeout=30, **options):
    async with session.request(method, url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=timeout),
                               **options) as response:
        try:
            body = json.loads(await response.read())
        except (ValueError, UnicodeError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        status, headers = response.status, response.headers
        if status in expected:
            return status, headers, body
        error = body.get("error")
        reasons = set()
        if isinstance(error, dict):
            reasons = {item["reason"] for item in (error.get("errors") or [])
                       if isinstance(item, dict) and isinstance(item.get("reason"), str)}
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"}:
            raise PublishError("YouTube upload quota reached", _retry_after(headers, 3600))
        if (status in (408, 425, 429) or status >= 500
                or reasons & {"userRateLimitExceeded", "rateLimitExceeded", "backendError"}):
            raise PublishError("YouTube is temporarily unavailable or rate limited", _retry_after(headers, 60))
        if status in (401, 403) or (isinstance(error, str) and error in {"invalid_grant", "invalid_client"}):
            raise PublishError("Check the YouTube account grant and API permissions")
        raise PublishError(f"YouTube request failed (HTTP {status})")


async def _verify_channel(session, token):
    if not isinstance(token, str) or not token:
        raise PublishError("Google did not issue an access token")
    headers = {"Authorization": f"Bearer {token}"}
    _, _, body = await _request(session, "GET", API_URL + "/channels", headers=headers,
                               params={"part": "id", "mine": "true"})
    channels = body.get("items")
    if (not isinstance(channels, list) or len(channels) != 1 or not isinstance(channels[0], dict)
            or channels[0].get("id") != CHANNEL_ID):
        raise PublishError("The authorized Google account does not select the expected YouTube channel")
    return headers


def _result(state, phase, reason, delay=0, **fields):
    return dict(state, phase=phase, reason=reason, delay=delay, **fields)


def _session_url(value):
    # A session URI receives an OAuth bearer token and the clip. Use only the
    # Google upload endpoint to which the initial metadata request was sent.
    if isinstance(value, str):
        target, upload = urlsplit(value), urlsplit(UPLOAD_URL)
        if (target.scheme, target.netloc, target.path) == (upload.scheme, upload.netloc, upload.path) and target.query and not target.fragment:
            return value
    raise PublishError("YouTube returned an unexpected resumable upload address")


async def _confirm(session, headers, state):
    remote_id = state["remote_id"]
    _, _, body = await _request(session, "GET", API_URL + "/videos", headers=headers,
                               params={"part": "snippet,status,processingDetails", "id": remote_id})
    video = next((item for item in (body.get("items") or [])
                  if isinstance(item, dict) and item.get("id") == remote_id), None)
    if video:
        if (video.get("snippet") or {}).get("channelId") != CHANNEL_ID:
            raise PublishError("YouTube returned a video from a different channel")
        status = video.get("status") or {}
        upload = status.get("uploadStatus")
        processing = (video.get("processingDetails") or {}).get("processingStatus")
        if upload in {"deleted", "failed", "rejected"} or processing == "failed":
            raise PublishError("YouTube removed, rejected, or failed to process this upload")
        if processing == "succeeded" or (upload == "processed" and processing != "processing"):
            if status.get("privacyStatus") != "public":
                raise PublishError("YouTube processed the upload with nonpublic visibility")
            return _result(state, "published", "YouTube confirmed the public video",
                           url=f"https://www.youtube.com/shorts/{remote_id}")
    since = state.get("confirm_since", time.time())
    if time.time() - since > 86400:
        raise PublishError("YouTube has not confirmed this video after 24 hours. Check YouTube Studio")
    return _result(state, "pending", "Waiting for YouTube to confirm the public video", 60, confirm_since=since)


def _next_byte(headers, size):
    if "Range" not in headers:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", headers["Range"])
    if not match or not 0 < (offset := int(match[1]) + 1) <= size:
        raise PublishError("YouTube returned an invalid resumable upload range")
    return offset


def _accepted(state, body):
    remote_id = body.get("id")
    if not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", remote_id):
        raise PublishError("YouTube accepted the transfer without a valid video ID. Check YouTube Studio")
    return _result(state, "pending", "YouTube accepted the video for processing", 15,
                   remote_id=remote_id, confirm_since=time.time())


async def _transfer(session, headers, state, master, size, checkpoint):
    url = _session_url(state["session_url"])
    offset = state.pop("resume_offset", None)
    if offset is None or state.get("final_attempted"):
        status, reply, body = await _request(session, "PUT", url, data=b"", expected=(200, 201, 308, 404, 410),
                                             headers={**headers, "Content-Range": f"bytes */{size}"})
        if status in (404, 410):
            if state.get("final_attempted", True):
                raise PublishError("Upload session expired after a possible final transfer. Check YouTube Studio")
            return _result(state, "pending", "Starting a new session for the expired incomplete upload", session_url=None)
        if status != 308:
            return _accepted(state, body)
        offset = _next_byte(reply, size)
        if offset == size:
            return _result(state, "pending", "Waiting for YouTube's upload receipt",
                           _retry_after(reply, 60), final_attempted=True)
        if "Retry-After" in reply:
            return _result(state, "pending", "YouTube requested a transfer delay", _retry_after(reply, 60),
                           final_attempted=False, resume_offset=offset)
    if type(offset) is not int or not 0 <= offset < size:
        raise PublishError("The saved upload offset is invalid")
    with master.open("rb") as stream:
        stream.seek(offset)
        state["final_attempted"] = True
        checkpoint(dict(state))
        status, reply, body = await _request(
            session, "PUT", url, expected=(200, 201, 308), timeout=600, data=stream,
            headers={**headers, "Content-Range": f"bytes {offset}-{size-1}/{size}",
                     "Content-Length": str(size-offset), "Content-Type": "video/mp4"})
    if status == 308:
        offset = _next_byte(reply, size)
        return _result(state, "pending", "Checking the remaining YouTube upload", _retry_after(reply, 1),
                       final_attempted=offset == size, resume_offset=offset if offset < size else None)
    return _accepted(state, body)


async def youtube_step(session, grant: dict, master: Path, title: str, description: str, state: dict,
                       checkpoint: Callable[[dict], None]) -> dict:
    """Advance one upload. The caller persists the returned state and each pre-transfer checkpoint."""
    state = dict(state)
    if state.get("phase") in ("published", "review"):
        return state
    try:
        if (not isinstance(grant, dict) or grant.get("channel_id") != CHANNEL_ID or
                not all(isinstance(grant.get(k), str) and grant[k] for k in ("client_id", "client_secret", "refresh_token"))):
            raise PublishError("Configure a YouTube OAuth grant for the expected channel")
        _, _, tokens = await _request(session, "POST", TOKEN_URL, data={
            "grant_type": "refresh_token", **{k: grant[k] for k in ("client_id", "client_secret", "refresh_token")}})
        headers = await _verify_channel(session, tokens.get("access_token"))
        if state.get("remote_id"):
            return await _confirm(session, headers, state)
        size = master.stat().st_size
        if not size or size != state.get("source_size", size):
            raise PublishError("The local video no longer matches the upload session")
        if state.get("session_url"):
            return await _transfer(session, headers, state, master, size, checkpoint)
        if state.get("final_attempted"):
            raise PublishError("Missing upload session after a possible final transfer. Check YouTube Studio")
        if (not title.strip() or len(title) > 100 or len(description.encode("utf-8")) > 5000
                or any(c in title + description for c in "<>")):
            raise PublishError("The title or description does not meet YouTube upload limits")
        _, reply, _ = await _request(
            session, "POST", UPLOAD_URL, expected=(200, 201),
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={**headers, "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
            json={"snippet": {"title": title, "description": description, "categoryId": "24"},
                  "status": {"privacyStatus": "public"}})
        return _result(state, "pending", "YouTube upload session created",
                       session_url=_session_url(reply.get("Location")), source_size=size, final_attempted=False)
    except PublishError as exc:
        return _result(state, "pending" if exc.delay else "review", str(exc), exc.delay)
    except (aiohttp.ClientError, TimeoutError):
        return _result(state, "pending", "YouTube connection interrupted", 60)
    except OSError:
        return _result(state, "review", "The local upload file or checkpoint could not be accessed")


async def _authorization_code(client_id, verifier):
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    result = asyncio.get_running_loop().create_future()

    async def callback(request):
        if not secrets.compare_digest(request.query.get("state", "").encode(), state.encode()):
            raise web.HTTPBadRequest(text="Unrecognized authorization request.")
        if not result.done():
            result.set_result(request.query.get("code"))
        return web.Response(text="You can close this tab and return to Clipmatch.")

    app = web.Application()
    app.router.add_get("/", callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        redirect = f"http://127.0.0.1:{runner.addresses[0][1]}/"
        query = urlencode(dict(client_id=client_id, redirect_uri=redirect, response_type="code",
                               scope=" ".join(SCOPES), state=state, code_challenge=challenge,
                               code_challenge_method="S256", access_type="offline", prompt="select_account consent"))
        if not await asyncio.to_thread(webbrowser.open, AUTH_URL + "?" + query):
            raise PublishError("Run authorization on a desktop with an available browser")
        code = await asyncio.wait_for(result, 300)
        if not code:
            raise PublishError("YouTube authorization was declined")
        return redirect, code
    finally:
        await runner.cleanup()


async def authorize(client_secrets: Path, destination: Path):
    try:
        installed = json.loads(client_secrets.read_text(encoding="utf-8"))["installed"]
        client = {k: installed[k] for k in ("client_id", "client_secret")}
        if not all(isinstance(v, str) and v for v in client.values()):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError):
        raise PublishError("Select the Google Cloud credentials JSON for a Desktop app") from None
    verifier = secrets.token_urlsafe(64)
    redirect, code = await _authorization_code(client["client_id"], verifier)
    async with aiohttp.ClientSession() as session:
        _, _, tokens = await _request(session, "POST", TOKEN_URL, data={
            **client, "grant_type": "authorization_code", "redirect_uri": redirect, "code": code, "code_verifier": verifier})
        if not tokens.get("refresh_token") or ("scope" in tokens and not set(SCOPES) <= set(tokens["scope"].split())):
            raise PublishError("Grant both YouTube permissions and offline access")
        await _verify_channel(session, tokens.get("access_token"))
    atomic_json(destination, dict(client, refresh_token=tokens["refresh_token"], channel_id=CHANNEL_ID))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.add_subparsers(dest="command", required=True).add_parser("authorize")
    args.add_argument("--client-secrets", type=Path, required=True)
    args.add_argument("--output", type=Path, default=DEFAULT_GRANT)
    options = parser.parse_args(argv)
    print(f"Select YouTube channel {CHANNEL_ID} in the Google consent window.", flush=True)
    try:
        asyncio.run(authorize(options.client_secrets, options.output))
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, aiohttp.ClientError, TimeoutError):
        print("YouTube authorization could not finish. Check the connection and local output path.", file=sys.stderr)
        return 1
    print(f"Saved the verified YouTube grant to {options.output}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
