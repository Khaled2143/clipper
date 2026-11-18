import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from clipmatch import live_api, live_events
from clipmatch.live import run_worker
from clipmatch.live_worker import WorkerConfig
from clipmatch.spikes import SpikeSettings


class LiveFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_live_chat_posts_only_the_confirmed_clip_link(self):
        public_url = "https://clips.twitch.tv/ConfirmedClip"
        private_url = "https://clips.twitch.tv/ConfirmedClip/edit"
        clip_id = "ConfirmedClip"
        subscriptions = []
        delivery_order = []
        discord_messages = []
        snapshot_sent = asyncio.Event()
        clip_created = asyncio.Event()
        webhook_received = asyncio.Event()

        async def token(request):
            form = await request.post()
            if dict(form) != {
                "grant_type": "refresh_token",
                "refresh_token": "seed",
                "client_id": "app",
                "client_secret": "secret",
            }:
                raise web.HTTPUnauthorized()
            return web.json_response({"access_token": "access", "refresh_token": "rotated"})

        async def validate(_request):
            return web.json_response({
                "client_id": "app", "user_id": "10", "login": "reader",
                "scopes": ["user:read:chat", "clips:edit"],
            })

        async def users(request):
            if request.query.get("login") != "channel":
                raise web.HTTPBadRequest()
            return web.json_response({"data": [{"id": "20", "login": "channel"}]})

        async def streams(request):
            if request.query.get("user_id") != "20":
                raise web.HTTPBadRequest()
            response = web.json_response({"data": [{"user_id": "20"}]})
            await response.prepare(request)
            await response.write_eof()
            snapshot_sent.set()
            return response

        async def subscribe(request):
            subscriptions.append(await request.json())
            return web.json_response({"data": []}, status=202)

        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({
                "metadata": {"message_type": "session_welcome", "message_id": "welcome"},
                "payload": {"session": {"id": "session", "keepalive_timeout_seconds": 10}},
            })
            try:
                await asyncio.wait_for(snapshot_sent.wait(), 5)
            except TimeoutError:
                return ws
            # Chat starts after Twitch sends its already-live snapshot. Repeat
            # briefly so the HTTP response can be applied before a complete burst.
            for batch in range(20):
                for index in range(3):
                    message_id = f"chat-{batch}-{index}"
                    await ws.send_json({
                        "metadata": {"message_type": "notification", "message_id": message_id},
                        "payload": {
                            "subscription": {"type": "channel.chat.message"},
                            "event": {
                                "broadcaster_user_id": "20",
                                "message_id": message_id,
                                "message": {
                                    "text": "W!",
                                    "fragments": [{"type": "text", "text": "W!"}],
                                },
                            },
                        },
                    })
                with suppress(TimeoutError):
                    await asyncio.wait_for(clip_created.wait(), 0.1)
                    break
            async for _ in ws:
                pass
            return ws

        async def create_clip(request):
            if dict(request.query) != {"broadcaster_id": "20", "duration": "60"}:
                raise web.HTTPBadRequest()
            delivery_order.append("create")
            clip_created.set()
            return web.json_response({"data": [{"id": clip_id, "edit_url": private_url}]}, status=202)

        async def get_clip(request):
            if request.query.get("id") != clip_id or not clip_created.is_set():
                raise web.HTTPBadRequest()
            if "pending" not in delivery_order:
                delivery_order.append("pending")
                return web.json_response({"data": []})
            delivery_order.append("confirmed")
            return web.json_response({"data": [{"id": clip_id, "url": public_url}]})

        async def webhook(request):
            delivery_order.append("discord")
            discord_messages.append(await request.json())
            webhook_received.set()
            return web.json_response({"id": "discord-message"})

        @web.middleware
        async def require_access_token(request, handler):
            if request.path.startswith("/helix/") or request.path == "/oauth/validate":
                if request.headers.get("Authorization") != "Bearer access":
                    raise web.HTTPUnauthorized()
            return await handler(request)

        app = web.Application(middlewares=[require_access_token])
        app.add_routes([
            web.post("/oauth/token", token),
            web.get("/oauth/validate", validate),
            web.get("/helix/users", users),
            web.get("/helix/streams", streams),
            web.post("/helix/eventsub/subscriptions", subscribe),
            web.get("/eventsub", socket),
            web.post("/helix/clips", create_clip),
            web.get("/helix/clips", get_clip),
            web.post("/discord", webhook),
        ])
        with tempfile.TemporaryDirectory() as directory:
            async with TestServer(app) as server:
                config = WorkerConfig(
                    client_id="app", client_secret="secret", refresh_token="seed",
                    webhook_url=str(server.make_url("/discord")), channels=("channel",),
                    state_dir=Path(directory), settings=SpikeSettings(
                        bucket_seconds=1, warmup_minutes=0, min_messages=2,
                    ),
                )
                with (
                    patch.object(live_api, "OAUTH", str(server.make_url("/oauth"))),
                    patch.object(live_api, "HELIX", str(server.make_url("/helix"))),
                    patch.object(live_events, "EVENTSUB_URL", str(server.make_url("/eventsub").with_scheme("ws"))),
                ):
                    worker = asyncio.create_task(run_worker(config))
                    delivery = asyncio.create_task(webhook_received.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {worker, delivery}, timeout=12, return_when=asyncio.FIRST_COMPLETED,
                        )
                        if worker in done:
                            worker.result()
                            self.fail("Worker exited before delivering the clip")
                        self.assertIn(delivery, done, f"No Discord post. Requests so far: {delivery_order}")
                    finally:
                        worker.cancel()
                        delivery.cancel()
                        await asyncio.gather(worker, delivery, return_exceptions=True)

        self.assertCountEqual([sub["type"] for sub in subscriptions], [
            "stream.online", "stream.offline", "channel.chat.message",
        ])
        for sub in subscriptions:
            self.assertEqual(sub["transport"], {"method": "websocket", "session_id": "session"})
        chat_sub = next(sub for sub in subscriptions if sub["type"] == "channel.chat.message")
        self.assertEqual(chat_sub["condition"], {"broadcaster_user_id": "20", "user_id": "10"})
        self.assertEqual(delivery_order, ["create", "pending", "confirmed", "discord"])
        [message] = discord_messages
        self.assertIn("[channel]", message["content"])
        self.assertIn("Top chat: W", message["content"])
        self.assertTrue(message["content"].endswith(public_url))
        self.assertNotIn(private_url, message["content"])
        self.assertEqual(message["allowed_mentions"], {"parse": []})
