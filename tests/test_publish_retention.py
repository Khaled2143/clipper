import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from clipmatch.cache import atomic_json, complete, file_hash
from clipmatch.finish_clip import clip_directory
from clipmatch.publishing import Publications
from clipmatch.render_queue import RenderQueue


class RetentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.db = sqlite3.connect(self.root / "clips.sqlite3")
        self.addCleanup(self.db.close)
        self.renders = RenderQueue(self.db, self.root)
        self.queue = Publications(self.db, self.root)
        self.old = time.time() - 90000

    async def clean(self):
        from clipmatch.publish_retention import cleanup_published
        await cleanup_published(self.queue, lambda *args, **kwargs: None)

    def files(self, slug, content=None, channel="agent00"):
        url = f"https://clips.twitch.tv/{slug}"
        self.renders.enqueue(slug, channel, url)
        folder = clip_directory(self.root, channel, slug)
        master = folder / "renders" / (folder.name + ".mp4")
        source = folder / "source.mp4"
        master.parent.mkdir(parents=True)
        (folder / "lock.sqlite3").touch()
        master.write_bytes(content or slug.encode())
        source.write_bytes(b"original " + slug.encode())
        complete(master, master.with_suffix(".manifest.json"), "render-key", "render", {})
        complete(source, folder / "source.manifest.json", "source-key", "twitch_clip", {})
        digest = file_hash(master)
        pin = self.queue.path(digest)
        pin.parent.mkdir(exist_ok=True)
        if not pin.exists():
            os.link(master, pin)
        atomic_json(master.with_suffix(".json"), {
            "clip_id": folder.name, "label": channel, "output": {"path": master.name},
            "source": {"identity": file_hash(source), "url_or_path": str(source), "twitch_url": url}})
        atomic_json(folder / "source.json", {"channel": channel, "clip_id": slug, "url": url, "key": "source-key"})
        with self.db:
            self.db.execute("UPDATE render_jobs SET status='done' WHERE id=?", (slug,))
            self.db.execute("INSERT INTO publish_clips(id,channel,source_url,digest) VALUES(?,?,?,?)",
                            (slug, channel, url, digest))
            self.db.executemany("INSERT INTO publish_jobs(clip_id,platform,phase,state) VALUES(?,?,'published',?)",
                                [(slug, platform, json.dumps({"published_at": self.old})) for platform in ("youtube", "tiktok")])
        return source, master, pin

    def receipt(self, slug, platform, phase, stamp):
        with self.db:
            self.db.execute("UPDATE publish_jobs SET phase=?,state=? WHERE clip_id=? AND platform=?",
                            (phase, json.dumps({"published_at": stamp}), slug, platform))

    async def test_live_render_url_can_alias_receipt_only_for_the_same_channel_and_clip(self):
        await self.clean()
        valid = self.files("SmellyPeppyGrouseNotLikeThis", channel="jynxzi")
        other_channel = self.files("OtherChannel", channel="jynxzi")
        other_clip = self.files("OtherClip", channel="jynxzi")
        invalid_receipt = self.files("WrongReceipt", channel="jynxzi")
        with self.db:
            self.db.executemany("UPDATE render_jobs SET url=? WHERE id=?", [
                ("https://www.twitch.tv/jynxzi/clip/SmellyPeppyGrouseNotLikeThis", "SmellyPeppyGrouseNotLikeThis"),
                ("https://www.twitch.tv/agent00/clip/OtherChannel", "OtherChannel"),
                ("https://www.twitch.tv/jynxzi/clip/DifferentSlug", "OtherClip")])
            self.db.execute("UPDATE publish_clips SET source_url='https://clips.twitch.tv/DifferentSlug' WHERE id='WrongReceipt'")
        await self.clean()
        self.assertFalse(any(p.exists() for p in valid))
        self.assertTrue(all(p.exists() for p in (*other_channel, *other_clip, *invalid_receipt)))

    async def test_retains_old_jobs_and_waits_24_hours_after_later_confirmation(self):
        earlier = self.files("Earlier")
        await self.clean()
        source, master, pin = self.files("New")
        # Preserve metadata and unrelated files, including other MP4 exports.
        extra = master.with_suffix(".clean.mp4")
        extra.write_bytes(b"other export")
        self.receipt("New", "tiktok", "pending", self.old)
        await self.clean()
        self.receipt("New", "tiktok", "published", time.time() - 86300)
        await self.clean()
        self.assertTrue(all(p.exists() for p in (*earlier, source, master, pin)))
        self.receipt("New", "tiktok", "published", self.old)
        await self.clean()
        self.assertFalse(any(p.exists() for p in (source, master, pin)))
        self.assertTrue(all(p.exists() for p in (*earlier, extra, master.with_suffix(".json"),
                                                  source.with_suffix(".manifest.json"))))
        source.write_bytes(b"restored later")
        await self.clean()
        self.assertEqual(source.read_bytes(), b"restored later")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM publish_jobs").fetchone(), (4,))

    async def test_shared_pin_waits_for_every_clip_and_a_partial_deletion_resumes(self):
        await self.clean()
        s1, m1, pin = self.files("First", b"shared")
        s2, m2, same_pin = self.files("Second", b"shared")
        self.receipt("Second", "youtube", "review", self.old)
        s1.unlink()
        await self.clean()
        self.assertFalse(m1.exists())
        self.assertTrue(pin.exists() and s2.exists() and m2.exists())
        self.receipt("Second", "youtube", "published", self.old)
        await self.clean()
        self.assertFalse(any(p.exists() for p in (same_pin, s2, m2)))

    async def test_rerender_lock_damaged_hash_changed_master_and_missing_timestamp_preserve_files(self):
        await self.clean()
        busy = self.files("Busy")
        changed = self.files("Changed")
        newer = self.files("NewMaster")
        bad = self.files("MissingStamp")
        changed[0].write_bytes(b"wrong source")
        newer[1].unlink()
        newer[1].write_bytes(b"updated")
        complete(newer[1], newer[1].with_suffix(".manifest.json"), "new-key", "render", {})
        self.receipt("MissingStamp", "tiktok", "published", None)
        lock = sqlite3.connect(busy[0].parent / "lock.sqlite3")
        try:
            lock.execute("BEGIN IMMEDIATE")
            await self.clean()
            self.assertTrue(all(p.exists() for p in (*busy, *changed, *newer, *bad)))
        finally:
            lock.close()
        await self.clean()
        self.assertFalse(any(p.exists() for p in busy))
        self.assertTrue(all(p.exists() for p in (*changed, *newer, *bad)))

    async def test_symlinked_render_directory_leaves_both_clips_intact(self):
        await self.clean()
        source, master, pin = self.files("Link")
        alternate = self.root / "other-job"
        master.parent.rename(alternate)
        try:
            master.parent.symlink_to(alternate, target_is_directory=True)
        except OSError:
            if sys.platform != "win32":
                raise
            import _winapi
            _winapi.CreateJunction(str(alternate), str(master.parent))
        await self.clean()
        self.assertTrue(source.exists() and pin.exists() and (alternate / master.name).exists())

    async def test_changed_pin_and_missing_source_digest_remain_untouched(self):
        await self.clean()
        source, master, pin = self.files("ChangedPin")
        pin.unlink()
        pin.write_bytes(b"replacement pin")
        missing = self.files("NoSourceHash")
        manifest = missing[0].with_suffix(".manifest.json")
        record = json.loads(manifest.read_text())
        del record["sha256"]
        atomic_json(manifest, record)
        await self.clean()
        self.assertFalse(source.exists() or master.exists())
        self.assertEqual(pin.read_bytes(), b"replacement pin")
        self.assertTrue(all(p.exists() for p in missing))

    async def test_new_digest_reference_during_pin_verification_keeps_the_pin(self):
        from clipmatch import publish_retention
        await self.clean()
        _, _, pin = self.files("First", b"shared")
        started, release = threading.Event(), threading.Event()
        verify = publish_retention._verified
        def paused(path, *args):
            if path == pin:
                started.set()
                release.wait(3)
            return verify(path, *args)
        with patch.object(publish_retention, "_verified", side_effect=paused):
            task = asyncio.create_task(self.clean())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                self.files("Second", b"shared")
                self.receipt("Second", "tiktok", "pending", self.old)
            finally:
                release.set()
                await task
        self.assertTrue(pin.exists())
