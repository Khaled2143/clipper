import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from clipmatch import live
from clipmatch.live_worker import ClipState, Worker, WorkerConfig
from clipmatch.render_queue import read_chat_lead
from clipmatch.spikes import SpikeSettings


def queued_rows(state):
    return state.db.execute(
        "SELECT id, status, attempts FROM render_jobs ORDER BY rowid"
    ).fetchall()


class ChatLeadReaderTests(unittest.TestCase):
    def test_missing_state_and_legacy_or_manual_clips_need_no_writes(self):
        with tempfile.TemporaryDirectory(prefix="clip state #") as temp:
            directory = Path(temp)
            self.assertIsNone(read_chat_lead(directory / "absent", "Manual"))
            self.assertIsNone(read_chat_lead(directory, "Manual"))
            self.assertEqual(list(directory.iterdir()), [])

            database = directory / "clips.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE pending (id TEXT PRIMARY KEY, login TEXT, summary TEXT)")
                db.execute("INSERT INTO pending VALUES ('Old', 'streamer', 'spike')")
            original = database.read_bytes()
            self.assertIsNone(read_chat_lead(directory, "Old"))
            self.assertEqual(database.read_bytes(), original)

            with closing(ClipState(directory)) as state:
                self.assertEqual(state.pending(), [("Old", "streamer", "spike")])
                state.renders.enqueue("Manual", "streamer", "https://clips.twitch.tv/Manual")
            self.assertIsNone(read_chat_lead(directory, "Old"))
            self.assertIsNone(read_chat_lead(directory, "Manual"))


class RenderQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.state = self.enterContext(closing(ClipState(self.directory)))

    def test_pending_confirmation_and_chat_lead_commit_together(self):
        def reject_timing(action, table, *_args):
            if action == sqlite3.SQLITE_INSERT and table == "clip_chat_timing":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.state.db.set_authorizer(reject_timing)
        with self.assertRaises(sqlite3.DatabaseError):
            self.state.remember("Interrupted", "streamer", "Spike", chat_lead=2.5)
        self.state.db.set_authorizer(None)
        self.assertEqual(self.state.pending(), [])
        self.assertIsNone(read_chat_lead(self.directory, "Interrupted"))

    def test_only_finite_nonnegative_leads_are_saved(self):
        for clip_id, lead in (("Instant", 0), ("Future", -1), ("Unknown", float("nan")), ("Infinite", float("inf"))):
            self.state.remember(clip_id, "streamer", "Spike", chat_lead=lead)
        self.assertEqual(len(self.state.pending()), 4)
        self.assertEqual(self.state.db.execute("SELECT id, lead_seconds FROM clip_chat_timing").fetchall(),
                         [("Instant", 0.0)])
        self.assertEqual(read_chat_lead(self.directory, "Instant"), 0.0)

    async def test_confirm_enqueues_before_link_delivery_and_duplicate_confirmation_is_harmless(self):
        url = "https://clips.twitch.tv/Confirmed"
        api = AsyncMock()
        api.confirm_clip.return_value = url
        seen = []

        async def receive(_session, _hook, text):
            seen.append((text, queued_rows(self.state)))

        worker = Worker(api, None, "", {}, SpikeSettings(), self.state,
                        render_queue=self.state.renders)
        with patch("clipmatch.live_worker.send_discord", side_effect=receive):
            for _ in range(2):
                self.state.remember("Confirmed", "streamer", "Spike")
                worker.resume()
                await worker.drain()
        self.assertEqual(seen, [(f"Spike\n{url}", [("Confirmed", "queued", 0)])] * 2)
        with closing(ClipState(self.directory)) as reopened:
            self.assertEqual(queued_rows(reopened), [("Confirmed", "queued", 0)])
            self.assertEqual(reopened.pending(), [])

    async def test_link_only_watcher_does_not_enqueue(self):
        api = AsyncMock()
        api.confirm_clip.return_value = "https://clips.twitch.tv/Confirmed"
        worker = Worker(api, None, "", {}, SpikeSettings(), self.state)
        self.state.remember("Confirmed", "streamer", "Spike")
        with patch("clipmatch.live_worker.send_discord"):
            worker.resume()
            await worker.drain()
        self.assertEqual(queued_rows(self.state), [])

    async def test_watch_entry_point_sends_link_while_configured_finisher_is_busy(self):
        config = WorkerConfig("client", "secret", "", "link-hook", ("streamer",), self.directory,
                              SpikeSettings(), finish_clips=True)
        self.state.remember("Live", "streamer", "Spike")
        started = asyncio.Event()
        received = asyncio.Event()
        release = asyncio.Event()
        api = AsyncMock()
        api.validate.return_value = "10"
        api.resolve_channels.return_value = {"20": "streamer"}
        api.confirm_clip.return_value = "https://clips.twitch.tv/Live"

        async def listen(*_args, **_kwargs):
            await asyncio.Future()

        async def post(_session, hook, message):
            self.assertEqual((hook, message), ("link-hook", "Spike\nhttps://clips.twitch.tv/Live"))
            received.set()

        async def child_wait():
            started.set()
            await release.wait()
            return 0

        child = AsyncMock()
        child.wait.side_effect = child_wait
        with (patch.object(live, "LiveAPI", return_value=api), patch.object(live, "listen", listen),
              patch("clipmatch.live_worker.send_discord", post),
              patch("asyncio.create_subprocess_exec", return_value=child)):
            watcher = asyncio.create_task(live.run_worker(config))
            try:
                async with asyncio.timeout(3):
                    await asyncio.gather(started.wait(), received.wait())
                self.assertEqual(queued_rows(self.state), [("Live", "running", 1)])
            finally:
                watcher.cancel()
                release.set()
                await asyncio.gather(watcher, return_exceptions=True)
        self.assertEqual(queued_rows(self.state), [("Live", "done", 1)])

    async def test_consumer_runs_serial_children_and_finishes_active_child_on_shutdown(self):
        queue = self.state.renders
        for clip_id in ("First", "Second"):
            queue.enqueue(clip_id, "streamer", f"https://clips.twitch.tv/{clip_id}")
        started = asyncio.Event()
        release = asyncio.Event()
        commands = []

        async def spawn(*argv, **_options):
            commands.append(argv)
            started.set()
            child = AsyncMock()
            async def wait():
                await release.wait()
                return 0
            child.wait.side_effect = wait
            return child

        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            active = asyncio.create_task(queue.run(lambda *_args, **_kw: None))
            async with asyncio.timeout(2):
                await started.wait()
            with closing(ClipState(self.directory)) as other:
                competitor = asyncio.create_task(other.renders.run(lambda *_args, **_kw: None))
                await asyncio.sleep(0)
                competitor.cancel()
                await asyncio.gather(competitor, return_exceptions=True)
            self.assertEqual(queued_rows(self.state), [("First", "running", 1), ("Second", "queued", 0)])
            active.cancel()
            await asyncio.sleep(0)
            self.assertFalse(active.done())
            release.set()
            await asyncio.gather(active, return_exceptions=True)
        self.assertEqual(queued_rows(self.state), [("First", "done", 1), ("Second", "queued", 0)])
        self.assertEqual(commands, [(sys.executable, "-m", "clipmatch.finish_clip", "--state-dir",
                                    str(self.directory), "--channel", "streamer", "--clip-id", "First",
                                    "--url", "https://clips.twitch.tv/First")])

    async def test_only_explicitly_safe_child_exit_retries_with_a_bounded_backoff(self):
        queue = self.state.renders
        self.state.remember("Retry", "streamer", "Spike", chat_lead=4.5)
        self.state.forget("Retry")
        queue.enqueue("Retry", "streamer", "https://clips.twitch.tv/Retry")
        queue.enqueue("Unknown", "streamer", "https://clips.twitch.tv/Unknown")
        child = AsyncMock()
        child.wait.side_effect = [75, 1, 75, 75]
        with patch("asyncio.create_subprocess_exec", return_value=child):
            for now in [0, 0, 0, 3600, 7200, 10800]:
                with patch("time.time", return_value=now):
                    await queue.run_one(lambda *_args, **_kw: None)
                if now == 0:
                    self.assertEqual(queued_rows(self.state)[0], ("Retry", "queued", 1))
        self.assertEqual(queued_rows(self.state), [("Retry", "review", 3), ("Unknown", "review", 1)])
        self.assertTrue(queue.retry("Unknown"))
        self.assertFalse(queue.retry("Unknown"))
        self.assertEqual(queued_rows(self.state)[1], ("Unknown", "queued", 0))
        self.assertEqual(read_chat_lead(self.directory, "Retry"), 4.5)

    async def test_interrupted_running_job_waits_for_manual_retry_after_restart(self):
        self.state.renders.enqueue("Lost", "streamer", "https://clips.twitch.tv/Lost")
        with self.state.db:
            self.state.db.execute("UPDATE render_jobs SET status='running', attempts=1")
        with closing(ClipState(self.directory)) as reopened:
            consumer = asyncio.create_task(reopened.renders.run(lambda *_args, **_kw: None))
            await asyncio.sleep(0)
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self.assertEqual(queued_rows(reopened), [("Lost", "review", 1)])

    def test_finished_webhook_is_optional_and_validated_without_exposing_credential(self):
        env = {"TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret", "CHANNELS": "streamer",
               "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/raw"}
        self.assertFalse(WorkerConfig.from_env(env).finish_clips)
        env["DISCORD_FINISHED_WEBHOOK_URL"] = "https://discord.com/api/webhooks/456/finished-token"
        config = WorkerConfig.from_env(env)
        self.assertTrue(config.finish_clips)
        self.assertNotIn("finished-token", repr(config))
        env["DISCORD_FINISHED_WEBHOOK_URL"] = "invalid-token"
        with self.assertRaisesRegex(ValueError, "DISCORD_FINISHED_WEBHOOK_URL"):
            WorkerConfig.from_env(env)


class BackfillTests(unittest.TestCase):
    def test_cli_backfill_creates_one_job_and_does_not_retry_completed_jobs(self):
        with tempfile.TemporaryDirectory() as directory, patch("clipmatch.live.load_dotenv"):
            args = ["enqueue-render", "--state-dir", directory, "--channel", "streamer",
                    "--clip-id", "Backfill", "--url", "https://www.twitch.tv/streamer/clip/Backfill"]
            self.assertEqual(live.main(args), 0)
            self.assertEqual(live.main(args), 0)
            with closing(ClipState(Path(directory))) as state:
                self.assertEqual(queued_rows(state), [("Backfill", "queued", 0)])
                with state.db:
                    state.db.execute("UPDATE render_jobs SET status='done' WHERE id='Backfill'")
            self.assertEqual(live.main(["retry-render", "--state-dir", directory, "--clip-id", "Backfill"]), 1)
