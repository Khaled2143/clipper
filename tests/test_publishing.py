from contextlib import asynccontextmanager
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from clipmatch.cache import atomic_json, complete
from clipmatch.finish_clip import clip_directory
from clipmatch.render_queue import RenderQueue
from clipmatch.publishing import Publications, Publisher, media_app
from clipmatch.tiktok_publisher import BufferFailure, confirm_public, create_tiktok, post_result
from tests.test_live_api import Transport


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(self.root / "clips.sqlite3")
        self.addCleanup(self.db.close)
        self.renders = RenderQueue(self.db, self.root)

    async def test_public_tiktok_confirmation_canonicalizes_buffer_url(self):
        canonical = "https://www.tiktok.com/@owner/video/123"
        response = AsyncMock()
        response.status = 200
        response.json.return_value = {"provider_name": "TikTok"}

        class Embed:
            @asynccontextmanager
            async def get(self, endpoint, *, params, **kwargs):
                self.url = params["url"]
                yield response

        embed = Embed()
        receipt = await confirm_public(embed, {"url": "https://tiktok.com/@owner/video/123?source=app"})
        self.assertEqual(embed.url, canonical)
        self.assertEqual(receipt["url"], canonical)
        self.assertEqual(receipt["phase"], "published")

    def render(self, slug, content=b"finished master", render_status="done"):
        url = f"https://clips.twitch.tv/{slug}"
        self.renders.enqueue(slug, "yourragegaming", url)
        with self.db:
            self.db.execute("UPDATE render_jobs SET status=? WHERE id=?", (render_status, slug))
        directory = clip_directory(self.root, "yourragegaming", slug)
        master = directory / "renders" / (directory.name + ".mp4")
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(content)
        complete(master, master.with_suffix(".manifest.json"), "key", "render", {})
        atomic_json(master.with_suffix(".json"), {"label": "yourragegaming", "source": {"twitch_url": url}})
        atomic_json(master.with_suffix(".social.json"), {
            "source_url": url, "status": "ready", "master_sha256": hashlib.sha256(content).hexdigest(),
            "title": "YourRAGE chases the loose ball", "description": "A missed shot turns into a footrace."})
        return master

    async def test_each_new_master_once_for_both_and_independent_of_discord_receipt(self):
        self.render("Earlier")
        queue = Publications(self.db, self.root)
        self.render("NewClip", render_status="review")
        with self.db:
            self.db.execute("UPDATE render_jobs SET url='https://www.twitch.tv/yourragegaming/clip/NewClip' WHERE id='NewClip'")
        await queue.collect()
        await Publications(self.db, self.root).collect()
        self.assertEqual(self.db.execute(
            "SELECT clip_id, platform FROM publish_jobs ORDER BY platform").fetchall(),
            [("NewClip", "tiktok"), ("NewClip", "youtube")])

    async def test_missing_title_after_a_render_child_failure_can_recover_without_loading_the_editor(self):
        import os
        queue = Publications(self.db, self.root)
        master = self.render("RecoverCopy", render_status="review")
        master.with_suffix(".social.json").unlink()
        with patch.dict(os.environ, SOCIAL_TITLE_MODEL="recover.gguf", SOCIAL_VISION_MODEL=""), \
                patch("clipmatch.social_titles.speech_evidence", return_value=("", (), ())), \
                patch("clipmatch.title_editor.write_titles", side_effect=AssertionError("loaded heavy editor")):
            await queue.collect()
        title, = self.db.execute("SELECT title FROM publish_clips WHERE id='RecoverCopy'").fetchone()
        self.assertIn("YourRAGE", title)
        self.assertEqual(self.db.execute("SELECT count(*) FROM publish_jobs").fetchone(), (2,))

    async def test_emoji_in_old_sidecar_and_queue_titles_are_removed_for_both_platforms_and_discord(self):
        queue = Publications(self.db, self.root)
        master = self.render("CleanTitle")
        sidecar = master.with_suffix(".social.json")
        copy = json.loads(sidecar.read_text())
        clean = copy["title"]
        copy["title"] += " 👀"
        atomic_json(sidecar, copy)
        await queue.collect()
        self.assertEqual(self.db.execute("SELECT title FROM publish_clips").fetchone(), (clean,))
        with self.db:
            self.db.execute("UPDATE publish_clips SET title=?", (copy["title"],))
        for platform in ("youtube", "tiktok"):
            self.assertEqual(queue.due(platform)[0][2], clean)
            queue.save("CleanTitle", platform, {"phase": "published", "url": "https://example.com/test"},
                       lambda *a, **kw: None, discord_receipt=True)
        self.assertEqual(self.db.execute("SELECT title FROM publish_discord_receipts").fetchall(), [(clean,), (clean,)])

    async def test_only_pinned_master_can_be_served_and_supports_range(self):
        queue = Publications(self.db, self.root)
        master = self.render("NewClip")
        await queue.collect()
        token, = self.db.execute("SELECT token FROM publish_clips WHERE id='NewClip'").fetchone()
        replacement = master.with_suffix(".partial")
        replacement.write_bytes(b"new revision")
        replacement.replace(master)
        async with TestClient(TestServer(media_app(queue))) as client:
            self.assertEqual((await client.get("/media/nope.mp4")).status, 404)
            self.assertEqual((await client.get("/clips.sqlite3")).status, 404)
            response = await client.get(f"/media/{token}.mp4", headers={"Range": "bytes=0-7"})
            self.assertEqual(response.status, 206)
            self.assertEqual(await response.read(), b"finished")

    async def test_unconfirmed_and_mismatched_delivery_are_never_published(self):
        self.assertEqual(post_result({"id": "1", "channelId": "wrong", "status": "sent"}, "right")["phase"], "review")
        self.assertEqual(post_result({"id": "1", "channelId": "right", "status": "sending"}, "right")["phase"], "pending")

        class LostResponse:
            async def request(inner, *args, **kwargs):
                from clipmatch.tiktok_publisher import BufferFailure
                raise BufferFailure("unconfirmed")
        saved = []
        result = await create_tiktok(LostResponse(), "right", "caption", "https://worker/media/test.mp4", saved.append)
        self.assertTrue(saved[0]["creating"])
        self.assertEqual(result["phase"], "review")

    def publisher(self, queue):
        return Publisher(queue, None, {"BUFFER_API_KEY": "example", "PUBLISH_MEDIA_ORIGIN": "https://clips.example",
                                      "YOUTUBE_GRANT_JSON": '{"channel_id":"test"}'}, lambda *a, **k: None)

    def receipt_worker(self, transport, **settings):
        db = sqlite3.connect(self.root / "clips.sqlite3")
        self.addCleanup(db.close)
        env = {"DISCORD_CLIP_DELIVERY": "0", "DISCORD_WATCH_ALERTS": "1",
               "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/11/watch-token",
               "DISCORD_FINISHED_WEBHOOK_URL": "https://discord.com/api/webhooks/22/finished-token",
               "YOUTUBE_GRANT_JSON": '{"channel_id":"test"}', "PUBLISH_INTERVAL_MINUTES": "0",
               "PUBLISH_MEDIA_ORIGIN": "https://clips.example", "BUFFER_API_KEY": "example"}
        return Publisher(Publications(db, self.root), transport, {**env, **settings}, Mock())

    async def receipt_cycle(self, publisher):
        async def once(name, operation, seconds=10):
            if name == "discord_receipts":
                await operation()
        with patch.object(publisher, "repeat", once):
            await publisher.run()

    async def public_short(self, publisher, slug):
        self.render(slug)
        await publisher.queue.collect()
        state = {"phase": "published", "remote_id": "testVideo01", "url": "https://www.youtube.com/shorts/testVideo01"}
        with patch("clipmatch.youtube_publisher.youtube_step", new=AsyncMock(return_value=state)):
            await publisher.youtube()

    async def test_only_future_public_confirmations_send_one_finished_receipt_per_platform(self):
        http = Transport()
        previous = self.receipt_worker(http, DISCORD_FINISHED_WEBHOOK_URL="")
        await self.public_short(previous, "Earlier")
        await self.receipt_cycle(previous)
        # Simulate a TikTok publication already recorded by the earlier worker.
        with self.db:
            self.db.execute("UPDATE publish_jobs SET phase='published' WHERE clip_id='Earlier'")
        publisher = self.receipt_worker(http)
        self.render("New")
        await publisher.queue.collect()
        youtube_url = "https://www.youtube.com/shorts/testVideo01"
        tiktok_url = "https://www.tiktok.com/@creator/video/235"
        publisher.queue.save("New", "youtube", {"phase": "pending", "remote_id": "testVideo01"}, publisher.log)
        sent = post_result({"id": "buffer-id", "status": "sent", "channelId": "channel",
                            "externalLink": tiktok_url}, "channel")
        publisher.queue.save("New", "tiktok", sent, publisher.log)
        await self.receipt_cycle(publisher)
        self.assertEqual(http.calls, [])

        with patch("clipmatch.youtube_publisher.youtube_step", new=AsyncMock(return_value={
                "phase": "published", "url": youtube_url, "remote_id": "testVideo01"})):
            await publisher.youtube()
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id": "channel", "serviceId": "creator"}
        with patch("clipmatch.publishing.confirm_public", new=AsyncMock(return_value={**sent, "phase": "published"})):
            await publisher.tiktok()
        # The copy on each receipt is the title originally submitted.
        with self.db:
            self.db.execute("UPDATE publish_clips SET title='Edited after publication' WHERE id='New'")
        for msg_id in ("101", "102"):
            http.expect(200, {"id": msg_id})
        resumed = self.receipt_worker(http)
        for _ in range(3):
            await self.receipt_cycle(resumed)
        await self.receipt_cycle(self.receipt_worker(http))
        self.assertEqual(len(http.calls), 2)
        for (method, destination, options), (label, url) in zip(http.calls, [("YouTube", youtube_url), ("TikTok", tiktok_url)]):
            self.assertEqual((method, destination), ("POST", "https://discord.com/api/webhooks/22/finished-token"))
            self.assertEqual(options["params"], {"wait": "true"})
            self.assertEqual(options["json"]["allowed_mentions"], {"parse": []})
            content = options["json"]["content"]
            for part in (label, "YourRAGE chases the loose ball", url):
                self.assertIn(part, content)
            self.assertNotIn("twitch.tv", content)
            self.assertNotIn("data", options)
        self.assertEqual(self.db.execute("SELECT message_id FROM publish_discord_receipts ORDER BY rowid").fetchall(),
                         [("101",), ("102",)])
        self.assertEqual(publisher.api.posts.await_count, 0)
        self.assertEqual(publisher.api.request.await_count, 0)

    async def test_discord_rate_limit_survives_restart_without_republishing(self):
        http = Transport()
        http.expect(429, {"retry_after": 1800})
        http.expect(429, {"retry_after": 2})
        http.expect(200, {"id": "103"})
        publisher = self.receipt_worker(http)
        with patch("time.time", return_value=10000):
            await self.public_short(publisher, "RateLimited")
            await self.receipt_cycle(publisher)
        self.assertEqual(len(http.calls), 1)
        resumed = self.receipt_worker(http)
        with patch("time.time", return_value=11799):
            await self.receipt_cycle(resumed)
        self.assertEqual(len(http.calls), 1)
        with patch("time.time", return_value=11801), patch("asyncio.sleep", new_callable=AsyncMock):
            await self.receipt_cycle(resumed)
            await self.receipt_cycle(resumed)
        self.assertEqual(len(http.calls), 2)
        with patch("time.time", return_value=11812):
            await self.receipt_cycle(self.receipt_worker(http))
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(self.db.execute("SELECT phase FROM publish_jobs WHERE platform='youtube'").fetchone(), ("published",))
        self.assertEqual(self.db.execute("SELECT phase,message_id FROM publish_discord_receipts").fetchone(), ("sent", "103"))

    async def test_ambiguous_or_interrupted_discord_delivery_stays_in_review_after_restart(self):
        http = Transport()
        publisher = self.receipt_worker(http)
        for slug in ("LostResponse", "MissingMessageID", "Stopped"):
            await self.public_short(publisher, slug)
        http.responses.append(aiohttp.ServerDisconnectedError("private-provider-text"))
        http.expect(200, {})
        http.expect(200, {})
        http.responses[-1].json = AsyncMock(side_effect=asyncio.CancelledError)
        for _ in range(3):
            await self.receipt_cycle(publisher)
        await self.receipt_cycle(self.receipt_worker(http))
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(self.db.execute(
            "SELECT clip_id,phase,message_id FROM publish_discord_receipts ORDER BY rowid").fetchall(),
            [("LostResponse", "review", None), ("MissingMessageID", "review", None), ("Stopped", "review", None)])
        self.assertNotIn("private-provider-text", str(publisher.log.call_args_list))
        self.assertNotIn("finished-token", str(publisher.log.call_args_list))

    async def test_discord_connection_failure_before_sending_retries(self):
        http = Transport()
        http.responses.append(aiohttp.ClientConnectorError(Mock(), OSError("private-provider-text")))
        http.expect(200, {"id": "104"})
        publisher = self.receipt_worker(http)
        with patch("time.time", return_value=10000):
            await self.public_short(publisher, "WaitingForConnection")
            await self.receipt_cycle(publisher)
            await self.receipt_cycle(publisher)
        self.assertEqual(len(http.calls), 1)
        with patch("time.time", return_value=10061):
            await self.receipt_cycle(self.receipt_worker(http))
        self.assertEqual(len(http.calls), 2)
        self.assertNotIn("private-provider-text", str(publisher.log.call_args_list))

    async def test_rate_capped_tiktok_waits_and_youtube_still_advances(self):
        queue = Publications(self.db, self.root)
        self.render("ClipA")
        self.render("ClipB")
        await queue.collect()
        await queue.collect()
        publisher = self.publisher(queue)
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id": "tiktok-channel", "serviceId": "creator"}
        publisher.api.request.return_value = {"createPost": {
            "__typename": "LimitReachedError", "message": "Daily posting limit reached"}}
        await publisher.tiktok()
        await publisher.tiktok()
        self.assertEqual(publisher.api.request.await_count, 1)
        async def advance(session, grant, master, title, description, state, checkpoint):
            self.assertEqual(master.read_bytes(), b"finished master")
            self.assertIn("https://clips.twitch.tv/ClipA", description)
            return {"phase":"published", "remote_id":"test", "url":"https://www.youtube.com/shorts/test"}
        with patch("clipmatch.youtube_publisher.youtube_step", advance):
            await publisher.youtube()
        self.assertEqual(self.db.execute("SELECT phase FROM publish_jobs WHERE clip_id='ClipA' ORDER BY platform").fetchall(),
                         [("pending",), ("published",)])

    async def test_interrupted_create_and_changed_tiktok_account_cannot_duplicate(self):
        queue = Publications(self.db, self.root)
        self.render("ClipA")
        await queue.collect()
        queue.save("ClipA", "tiktok", {"phase":"pending", "creating":True}, lambda *a, **k: None)
        publisher = self.publisher(queue)
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id":"abc", "serviceId":"1"}
        await publisher.tiktok()
        self.assertFalse(publisher.api.request.called)
        self.assertEqual(self.db.execute("SELECT phase FROM publish_jobs WHERE platform='tiktok'").fetchone(), ("review",))
        self.render("ClipB")
        await queue.collect()
        publisher.recheck_channel = 0
        publisher.api.destination.return_value = {"id":"abc", "serviceId":"another"}
        with self.assertRaises(BufferFailure):
            await publisher.tiktok()
        self.assertFalse(publisher.api.request.called)

    async def test_batch_receipts_do_not_recreate_posts_after_restart(self):
        queue = Publications(self.db, self.root)
        for slug in ("ClipA","ClipB"):
            self.render(slug)
            await queue.collect()
            queue.save(slug, "tiktok", {"phase":"pending", "remote_id":slug}, lambda *a, **k: None)
        publisher = self.publisher(Publications(self.db, self.root))
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id":"abc", "serviceId":"creator"}
        publisher.api.posts.return_value = [
            {"channelId":"abc", "id":slug, "status":"sent", "externalLink":f"https://www.tiktok.com/@owner/video/{i}"}
            for i,slug in enumerate(("ClipA","ClipB"), start=20)]
        await publisher.tiktok()
        self.assertEqual(publisher.api.posts.await_count, 1)
        self.assertEqual(publisher.api.request.await_count, 0)
        async def confirm(session,state):
            return {**state, "phase":"published"}
        with patch("clipmatch.publishing.confirm_public", confirm):
            await publisher.tiktok()
            await publisher.tiktok()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM publish_jobs WHERE phase='published'").fetchone(), (2,))
        self.assertEqual(publisher.api.request.await_count, 0)

    async def test_new_tiktok_gets_its_first_buffer_status_check_after_one_idle_minute(self):
        queue = Publications(self.db, self.root)
        self.render("New")
        await queue.collect()
        queue.set("buffer_poll", 9000)
        publisher = self.publisher(queue)
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id": "channel", "serviceId": "creator"}
        post = {"id": "new-id", "channelId": "channel", "status": "sending"}
        publisher.api.request.return_value = {"createPost": {"post": post}}
        link = "https://www.tiktok.com/@creator/video/123"
        publisher.api.posts.return_value = [{**post, "status": "sent", "externalLink": link}]
        for now in (10000, 10059):
            with patch("time.time", return_value=now):
                await publisher.tiktok()
        publisher.api.posts.assert_not_awaited()
        with patch("time.time", return_value=10060):
            await publisher.tiktok()
        publisher.api.posts.assert_awaited_once_with(["new-id"])
        phase, url = self.db.execute(
            "SELECT phase,json_extract(state,'$.url') FROM publish_jobs WHERE platform='tiktok'").fetchone()
        self.assertEqual((phase, url), ("pending", link))
        self.assertEqual(publisher.api.request.await_count, 1)

    async def test_new_scheduled_post_joins_the_next_shared_three_hour_batch_after_restart(self):
        queue = Publications(self.db, self.root)
        self.render("Earlier")
        await queue.collect()
        publisher = self.publisher(queue)
        api = publisher.api = AsyncMock()
        api.destination.return_value = {"id": "channel", "serviceId": "creator"}
        api.posts.side_effect = lambda ids: [{"id": id_, "channelId": "channel", "status": "sending"} for id_ in ids]
        api.request.return_value = {"createPost": {"post": {"id": "new-id", "channelId": "channel", "status": "scheduled"}}}
        with patch("time.time", return_value=10000):
            queue.save("Earlier", "tiktok", {"phase": "pending", "remote_id": "earlier-id"}, publisher.log)
            await publisher.tiktok()
        api.posts.assert_awaited_once_with(["earlier-id"])
        self.render("New")
        await queue.collect()
        with patch("time.time", return_value=10030):
            await publisher.tiktok()
        db = sqlite3.connect(self.root / "clips.sqlite3")
        self.addCleanup(db.close)
        resumed = self.publisher(Publications(db, self.root))
        resumed.api = api
        for now in (10090, 20799):
            with patch("time.time", return_value=now):
                await resumed.tiktok()
        self.assertEqual(api.posts.await_count, 1)
        with patch("time.time", return_value=20800):
            await resumed.tiktok()
        self.assertEqual(api.posts.await_count, 2)
        self.assertCountEqual(api.posts.await_args.args[0], ["earlier-id", "new-id"])
        self.assertEqual(api.request.await_count, 1)

    async def test_create_keeps_buffer_error_delays_and_immediate_public_link_verification(self):
        api = AsyncMock()
        link = "https://www.tiktok.com/@creator/video/123"
        for values, expected in (
                ({"status": "sending", "error": {"message": "under review"}}, ("pending", 10800, None)),
                ({"status": "error", "error": {"message": "Daily posting limit reached"}}, ("pending", 21600, None)),
                ({"status": "sent", "externalLink": link}, ("pending", 0, link))):
            with self.subTest(values=values):
                api.request.return_value = {"createPost": {"post": {"id": "post", "channelId": "channel", **values}}}
                state = await create_tiktok(api, "channel", "title", "https://clips.example/master.mp4", Mock())
                self.assertEqual((state["phase"], state["delay"], state.get("url")), expected)

    async def test_deleted_or_unavailable_receipt_does_not_block_the_next_creation(self):
        queue = Publications(self.db, self.root)
        for slug in ("Old","New"):
            self.render(slug)
            await queue.collect()
        queue.save("Old", "tiktok", {"phase":"pending", "remote_id":"old-id"}, lambda *a, **k: None)
        publisher = self.publisher(queue)
        publisher.api = AsyncMock()
        publisher.api.destination.return_value = {"id":"abc", "serviceId":"creator"}
        publisher.api.posts.side_effect = BufferFailure("unconfirmed")
        publisher.api.request.return_value = {"createPost":{"post":{"id":"new-id", "channelId":"abc", "status":"sending"}}}
        await publisher.tiktok()
        self.assertEqual(publisher.api.request.await_count, 1)
        posted = publisher.api.request.await_args.args[1]["input"]["text"]
        self.assertEqual(posted, "YourRAGE chases the loose ball")
        saved = self.db.execute("SELECT state FROM publish_jobs WHERE platform='tiktok' AND clip_id='New'").fetchone()[0]
        self.assertEqual(json.loads(saved)["remote_id"], "new-id")

    async def test_recoverable_account_error_preserves_youtube_queue(self):
        queue = Publications(self.db, self.root)
        self.render("One")
        await queue.collect()
        pub = self.publisher(queue)
        error = {"phase":"review","reason":"Check the YouTube account grant and API permissions"}
        with patch("clipmatch.youtube_publisher.youtube_step", new=AsyncMock(return_value=error)):
            await pub.youtube()
        self.assertFalse(pub.can_create("youtube"))
        self.assertEqual(self.db.execute("SELECT phase FROM publish_jobs WHERE platform='youtube'").fetchone(), ("pending",))

    def test_caption_and_size_errors_only_hold_the_affected_post(self):
        for message in ("Video exceeds the file size limit", "Caption character limit exceeded"):
            state = post_result({"id":"r", "channelId":"c", "status":"error","error":{"message":message}}, "c")
            self.assertEqual((state["phase"], state["remote_id"]), ("review","r"))


if __name__ == "__main__":
    unittest.main()
