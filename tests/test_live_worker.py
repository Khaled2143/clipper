import asyncio
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from clipmatch.live_api import ApiError
from clipmatch.live_worker import ClipState, Worker, WorkerConfig
from clipmatch.spikes import SpikeSettings


class SimulatedTwitch:
    def __init__(self):
        self.created = []
        self.visible = asyncio.Event()

    async def create_clip(self, channel):
        self.created.append(channel)
        return "clip-" + channel

    async def confirm_clip(self, clip):
        await self.visible.wait()
        return "https://clips.twitch.tv/" + clip


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_notice_on_startup_and_after_offline_but_only_once_per_transition(self):
        with tempfile.TemporaryDirectory() as temp, closing(ClipState(Path(temp))) as state:
            worker = Worker(None, None, "", {"1": "first"}, SpikeSettings(), state)
            with patch("clipmatch.live_worker.send_discord") as webhook:
                worker.live("1", True)
                worker.gap()
                worker.live("1", True)
                worker.live("1", False)
                worker.live("1", False)
                worker.live("1", True)
                await worker.drain()
            messages = [call.args[2] for call in webhook.await_args_list]
            self.assertEqual(messages, [
                "[first] live — watching chat for clips.",
                "[first] offline — chat monitoring paused.",
                "[first] live — watching chat for clips.",
            ])

    async def test_spike_sends_one_confirmed_link_and_restart_preserves_cooldown(self):
        received = []

        async def webhook(request):
            received.append(await request.json())
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/hook", webhook)
        api = SimulatedTwitch()
        settings = SpikeSettings.from_env({"BUCKET_SECONDS": "10", "WARMUP_MINUTES": "0"})
        now = [0]
        with tempfile.TemporaryDirectory() as temp:
            async with TestServer(app) as server, ClientSession() as session:
                with closing(ClipState(Path(temp))) as state:
                    worker = Worker(api, session, str(server.make_url("/hook")), {"1": "first", "2": "other"},
                                    settings, state, clock=lambda: now[0], wall=lambda: 1000 + now[0])
                    worker.live("1", True)
                    await worker.drain()
                    chat = {"message": {"text": "W", "fragments": [{"type": "text", "text": "W"}]}}
                    for _ in range(5):
                        worker.chat("1", chat)
                    now[0] = 10
                    for _ in range(21):
                        worker.chat("1", chat)
                        worker.chat("2", chat)
                    now[0] = 20
                    worker.tick()
                    await asyncio.sleep(0)
                    self.assertEqual(api.created, ["1"])
                    self.assertEqual([post["content"] for post in received], ["[first] live — watching chat for clips."])
                    self.assertEqual(len(state.pending()), 1)
                    for _ in range(70):
                        worker.chat("1", chat)
                    now[0] = 30
                    worker.tick()
                    worker.gap()
                    api.visible.set()
                    await worker.drain()
                    self.assertEqual(api.created, ["1"])
                    self.assertEqual(len(received), 2)
                    clip = received[1]
                    self.assertIn("4.2", clip["content"])
                    self.assertIn("W", clip["content"])
                    self.assertIn("https://clips.twitch.tv/clip-1", clip["content"])
                    self.assertEqual(clip["allowed_mentions"], {"parse": []})
                    self.assertEqual(list(state.pending()), [])
                with closing(ClipState(Path(temp))) as reopened:
                    self.assertFalse(reopened.reserve("1", 1109, 90))
                    self.assertTrue(reopened.reserve("1", 1110, 90))
                    self.assertTrue(reopened.reserve("2", 1110, 90))

    async def test_restart_confirms_saved_id_without_recreating_clip(self):
        api = SimulatedTwitch()
        api.visible.set()
        messages = []

        async def webhook(_session, _url, message):
            messages.append(message)

        with tempfile.TemporaryDirectory() as temp, closing(ClipState(Path(temp))) as state:
            state.remember("saved", "channel", "spike stats")
            worker = Worker(api, None, "", {"1": "channel"}, SpikeSettings.from_env({}), state)
            with patch("clipmatch.live_worker.send_discord", webhook):
                worker.resume()
                await worker.drain()
            self.assertEqual(api.created, [])
            self.assertIn("https://clips.twitch.tv/saved", messages[0])
            self.assertEqual(list(state.pending()), [])

    async def test_restriction_posts_error_and_other_channel_still_delivers(self):
        class RestrictedTwitch(SimulatedTwitch):
            async def create_clip(self, channel):
                if channel == "1":
                    raise ApiError(403, "clips disabled on this channel")
                return await super().create_clip(channel)

        posts = []

        async def webhook(_session, _url, message):
            posts.append(message)

        api = RestrictedTwitch()
        api.visible.set()
        now = [0]
        rules = SpikeSettings(min_messages=1, warmup_minutes=0)
        with tempfile.TemporaryDirectory() as temp, closing(ClipState(Path(temp))) as state:
            worker = Worker(api, None, "", {"1": "one", "2": "two"}, rules, state, clock=lambda: now[0])
            with patch("clipmatch.live_worker.send_discord", webhook):
                for channel in ("1", "2"):
                    worker.live(channel, True)
                    worker.chat(channel, {"message": {"text": "wow"}})
                now[0] = 10
                worker.tick()
                await worker.drain()
            self.assertIn("[one] clip failed: clips disabled on this channel", posts)
            self.assertTrue(any("https://clips.twitch.tv/clip-2" in post for post in posts))

    async def test_offline_transition_before_queued_clip_skips_create(self):
        api = SimulatedTwitch()
        api.visible.set()
        now = [0]
        with tempfile.TemporaryDirectory() as temp, closing(ClipState(Path(temp))) as state:
            worker = Worker(api, None, "", {"1": "channel"}, SpikeSettings(min_messages=1, warmup_minutes=0),
                            state, clock=lambda: now[0])
            with patch("clipmatch.live_worker.send_discord"):
                worker.live("1", True)
                worker.chat("1", {"message": {"text": "wow"}})
                now[0] = 10
                worker.tick()
                worker.live("1", False)
                await worker.drain()
            self.assertEqual(api.created, [])

    def test_config_normalizes_channels_and_uses_volume(self):
        config = WorkerConfig.from_env({
            "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret",
            "CHANNELS": " Alpha, BETA,alpha ", "RAILWAY_VOLUME_MOUNT_PATH": "/data",
            "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/token",
        })
        self.assertEqual(config.channels, ("alpha", "beta"))
        self.assertEqual(config.state_dir, Path("/data"))
        self.assertNotIn("token", repr(config))

    async def test_bucket_log_includes_bounded_reactions(self):
        with tempfile.TemporaryDirectory() as temp, closing(ClipState(Path(temp))) as state:
            now = [0]
            worker = Worker(None, None, "", {"1": "channel"}, SpikeSettings(), state, clock=lambda: now[0])
            with patch("clipmatch.live_worker.log") as output, patch("clipmatch.live_worker.send_discord"):
                worker.live("1", True)
                await worker.drain()
                worker.chat("1", {"message": {"text": "hello " + "W" * 200}})
                now[0] = 10
                worker.tick()
            self.assertEqual(output.call_args.kwargs["top"], ["hello", "W" * 80])
