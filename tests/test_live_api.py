import asyncio
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

from clipmatch.live_api import ApiError, LiveAPI, exchange_code, save_refresh_token, send_discord


class Response:
    def __init__(self, status, data, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        # Let simultaneous callers overlap while the upstream response is pending.
        ready = asyncio.Event()
        asyncio.get_running_loop().call_soon(ready.set)
        await ready.wait()
        return self.data


class Transport:
    def __init__(self):
        self.responses = deque()
        self.calls = []

    def expect(self, status, data, headers=None):
        self.responses.append(Response(status, data, headers))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class LiveApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Transport()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "auth" / "refresh"
        self.auth = {"client_id": "app", "user_id": "reader",
                     "scopes": ["user:read:chat", "clips:edit"]}
        self.api = LiveAPI(self.session, "app", "secret", "seed", self.path)
        self.grant("access", "rotated")

    def grant(self, access, refresh):
        self.session.expect(200, {"access_token": access, "refresh_token": refresh})

    async def test_concurrent_validation_and_restart_use_latest_refresh_token(self):
        self.session.expect(200, self.auth)
        self.session.expect(200, self.auth)
        ids = await asyncio.gather(self.api.validate(), self.api.validate())
        self.assertEqual(ids, ["reader", "reader"])
        self.assertEqual(self.session.calls[0][2]["data"]["refresh_token"], "seed")
        self.assertEqual(len(self.session.calls), 3)
        restarted = LiveAPI(self.session, "app", "secret", "seed", self.path)
        self.grant("next", "twice")
        self.session.expect(200, self.auth)
        await restarted.validate()
        self.assertEqual(self.session.calls[3][2]["data"]["refresh_token"], "rotated")
        self.assertEqual(self.path.read_text().strip(), "twice")

    async def test_bootstrap_atomically_replaces_old_token(self):
        save_refresh_token(self.path, "old")
        with patch("os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                save_refresh_token(self.path, "new")
        self.assertEqual(self.path.read_text().strip(), "old")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    async def test_authorization_code_exchange_returns_only_refresh_token(self):
        callback = "http://localhost:3000/callback"
        session = Transport()
        session.expect(200, {"access_token": "access", "refresh_token": "refresh"})
        session.expect(400, {"message": "leaked-secret"})
        args = (session, "app", "leaked-secret", "one-time-code", callback)
        self.assertEqual(await exchange_code(*args), "refresh")
        self.assertEqual(session.calls[0][2]["data"], {
            "grant_type": "authorization_code", "client_id": "app", "client_secret": "leaked-secret",
            "code": "one-time-code", "redirect_uri": callback,
        })
        with self.assertRaises(ApiError) as raised:
            await exchange_code(*args)
        self.assertNotIn("leaked-secret", str(raised.exception))

    async def test_rejects_wrong_client_scopes_or_user(self):
        for override in [{"scopes": ["clips:edit"]}, {"client_id": "other"}, {"user_id": ""}]:
            self.session.expect(200, {**self.auth, **override})
            with self.assertRaises(ApiError):
                await self.api.validate()

    async def test_irc_credentials_require_opt_in_scope(self):
        self.session.expect(200, {**self.auth, "login": "viewer"})
        with self.assertRaises(ApiError):
            await self.api.irc_credentials()
        self.session.expect(200, {**self.auth, "login": "viewer",
                                  "scopes": [*self.auth["scopes"], "chat:read"]})
        self.assertEqual(await self.api.irc_credentials(), ("viewer", "access"))

    async def test_rejected_token_refreshes_then_replays_once(self):
        self.session.expect(401, {})
        self.grant("renewed", "newrefresh")
        self.session.expect(200, {"data": [{"id": "7", "login": "alpha"}]})
        self.assertEqual(await self.api.resolve_channels(["alpha"]), {"7": "alpha"})
        request = self.session.calls[-1]
        self.assertEqual(request[2]["headers"]["Authorization"], "Bearer renewed")
        self.assertEqual(request[2]["headers"]["Client-Id"], "app")
        self.assertEqual(request[:2], ("GET", "https://api.twitch.tv/helix/users"))

    async def test_subscriptions_and_stream_snapshot_use_broadcaster_ids(self):
        self.session.expect(200, {"data": [{"user_id": "7"}]})
        self.assertEqual(await self.api.live_ids(["7", "8"]), {"7"})
        self.assertIn(("first", "100"), self.session.calls[-1][2]["params"])
        for kind in ["channel.chat.message", "stream.online", "stream.offline"]:
            self.session.expect(202, {})
            await self.api.subscribe(kind, "7", "reader", "socket")
            self.assertEqual(self.session.calls[-1][:2],
                             ("POST", "https://api.twitch.tv/helix/eventsub/subscriptions"))
            payload = self.session.calls[-1][2]["json"]
            condition = {"broadcaster_user_id": "7"}
            if kind == "channel.chat.message":
                condition["user_id"] = "reader"
            self.assertEqual(payload, {"type": kind, "version": "1", "condition": condition,
                                       "transport": {"method": "websocket", "session_id": "socket"}})

    async def test_create_and_poll_returns_confirmed_public_url(self):
        self.session.expect(202, {"data": [{"id": "slug", "edit_url": "private"}]})
        self.assertEqual(await self.api.create_clip("7"), "slug")
        self.assertEqual(self.session.calls[-1][2]["params"], {"broadcaster_id": "7", "duration": "60"})
        self.session.expect(200, {"data": []})
        public = "https://clips.twitch.tv/slug"
        self.session.expect(200, {"data": [{"id": "slug", "url": public}]})
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.api.confirm_clip("slug"), public)
        sleep.assert_awaited_once_with(3)

    async def test_ambiguous_subscription_reconciles_pages_on_current_session(self):
        kind = "channel.chat.message"
        sub = {"type": kind, "version": "1", "condition": {"broadcaster_user_id": "7", "user_id": "reader"},
               "transport": {"method": "websocket", "session_id": "ours", "connected_at": "today"},
               "status": "enabled"}
        for status in (409, 503, 0):
            if status:
                self.session.expect(status, {})
            else:
                self.session.responses.append(aiohttp.ClientConnectionError())
            self.session.expect(200, {"data": [], "pagination": {"cursor": "next"}})
            self.session.expect(200, {"data": [sub]})
            await self.api.subscribe(kind, "7", "reader", "ours")
            calls = self.session.calls[-3:]
            self.assertEqual([call[0] for call in calls], ["POST", "GET", "GET"])
            self.assertEqual(calls[1][2]["params"], {"type": kind})
            self.assertEqual(calls[2][2]["params"], {"type": kind, "after": "next"})

    async def test_conflicting_subscription_must_match_session_condition_and_status(self):
        kind = "stream.online"
        sub = {"type": kind, "version": "1", "condition": {"broadcaster_user_id": "7"},
               "transport": {"method": "websocket", "session_id": "ours"}, "status": "enabled"}
        for different in [
            {"transport": {"method": "websocket", "session_id": "theirs"}},
            {"transport": {"method": "webhook", "session_id": "ours"}},
            {"condition": {"broadcaster_user_id": "9"}},
            {"type": "stream.offline"},
            {"status": "websocket_disconnected"},
        ]:
            self.session.expect(409, {})
            self.session.expect(200, {"data": [{**sub, **different}], "pagination": {}})
            with self.assertRaises(ApiError) as raised:
                await self.api.subscribe(kind, "7", "reader", "ours")
            self.assertEqual(raised.exception.status, 409)
            self.assertEqual(self.session.calls[-1][0], "GET")

    async def test_confirmation_deadline_includes_http_and_rate_limit_waits(self):
        self.session.expect(429, {}, {"Retry-After": "55"})
        short_timeout = asyncio.timeout
        with patch("asyncio.timeout", side_effect=lambda _: short_timeout(0.05)) as timeout:
            with self.assertRaises(ApiError) as raised:
                await self.api.confirm_clip("slug")
        timeout.assert_called_once_with(60)
        self.assertEqual(raised.exception.status, 504)

    async def test_429_honors_reset_and_ambiguous_clip_failure_is_final(self):
        self.session.expect(429, {}, {"Ratelimit-Reset": "110"})
        self.session.expect(503, {"message": "secret"})
        with patch("time.time", return_value=100), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ApiError) as raised:
                await self.api.create_clip("7")
        sleep.assert_awaited_once_with(10)
        self.assertEqual(raised.exception.status, 503)
        self.assertNotIn("secret", str(raised.exception))

    async def test_discord_limits_retries_and_disables_mentions(self):
        session = Transport()
        for _ in range(3):
            session.expect(429, {"retry_after": 0.5})
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(ApiError) as raised:
                await send_discord(session, "https://discord.example/secret", "@everyone url")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(sleep.await_count, 1)
        self.assertEqual(session.calls[-1][2]["json"], {"content": "@everyone url",
                                                       "allowed_mentions": {"parse": []}})
        self.assertEqual(session.calls[-1][2]["params"]["wait"], "true")

    async def test_exhausted_bucket_delays_next_helix_request(self):
        self.session.expect(200, {"data": []}, {"Ratelimit-Remaining": "0", "Ratelimit-Reset": "108"})
        self.session.expect(200, {"data": []})
        with patch("time.time", return_value=100), patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            await self.api.live_ids(["7"])
            await self.api.resolve_channels(["alpha"])
        sleep.assert_awaited_once_with(8)

    async def test_long_throttle_skips_stale_clip_request(self):
        self.session.expect(200, {"data": []}, {"Ratelimit-Remaining": "0", "Ratelimit-Reset": "130"})
        with patch("time.time", return_value=100):
            await self.api.live_ids(["7"])
            with self.assertRaises(ApiError) as raised:
                await self.api.create_clip("7")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(self.session.calls), 2)

    async def test_clip_denials_explain_known_reasons_and_redact_server_text(self):
        for message, reason in [("Clips are disabled for this channel secret", "disabled"),
                                ("Only followers can create clips secret", "followers"),
                                ("The user is banned or timed out secret", "banned")]:
            self.session.expect(403, {"message": message})
            with self.assertRaises(ApiError) as raised:
                await self.api.create_clip("7")
            self.assertIn(reason, str(raised.exception))
            self.assertNotIn("secret", str(raised.exception))

    async def test_discord_transport_errors_redact_webhook(self):
        session = Transport()
        session.responses.append(aiohttp.ClientConnectionError("url/secret"))
        with self.assertRaises(ApiError) as raised:
            await send_discord(session, "https://discord.example/secret", "message")
        self.assertEqual(raised.exception.status, 0)
        self.assertNotIn("secret", str(raised.exception))
