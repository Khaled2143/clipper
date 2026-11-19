import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from clipmatch import finished
from clipmatch.media import probe


class FinishedDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.output = self.root / "sample.mp4"
        self.output.write_bytes(b"rendered master")
        self.receipt = self.root / "receipt.json"
        self.uploads = []
        self.responses = []
        self.payloads = []
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_post("/api/webhooks/{id}/token", self.receive)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)

    async def receive(self, request):
        self.assertEqual(request.query.get("wait"), "true")
        fields = await request.post()
        video = fields["files[0]"]
        with video.file:
            self.uploads.append((request.path, video.filename, video.file.read()))
        payload = json.loads(fields["payload_json"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.payloads.append(payload)
        if self.responses:
            status, body = self.responses.pop(0)
            if status == 0:
                request.transport.close()
            else:
                return web.json_response(body, status=status)
        return web.json_response({"id": "123", "attachments": [{"filename": video.filename}]})

    async def deliver(self, webhook_id=42):
        url = str(self.server.make_url(f"/api/webhooks/{webhook_id}/token"))
        return await finished.deliver_finished(self.output, self.receipt, url)

    async def test_uploads_master_to_finished_channel_and_remembers_confirmed_delivery(self):
        self.assertTrue(await self.deliver())
        self.assertFalse(await self.deliver())
        self.assertEqual(self.uploads, [("/api/webhooks/42/token", "sample.mp4", b"rendered master")])
        self.assertNotIn("token", self.receipt.read_text())
        self.output.write_bytes(b"revised render")
        self.assertTrue(await self.deliver())
        self.assertTrue(await self.deliver(webhook_id=99))

    async def test_caption_follows_both_attachment_sizes_and_receipt_skips_generation(self):
        self.responses.append((413, {}))
        copy = "Agent00 Spots the Vice City Signs\nHe looks up as the signs pass.\nhttps://clips.twitch.tv/Signs"
        with patch.object(finished, "social_content", return_value=copy) as generate, \
             patch.object(finished, "_make_attachment", side_effect=lambda src, dst: dst.write_bytes(b"small")):
            self.assertTrue(await self.deliver())
            self.assertFalse(await self.deliver())
        self.assertEqual([item.get("content") for item in self.payloads], [copy, copy])
        self.assertEqual(generate.call_count, 1)

    async def test_only_definite_size_rejections_trigger_a_smaller_upload(self):
        def compress(source, destination):
            self.assertEqual(source.read_bytes(), b"rendered master")
            destination.write_bytes(b"small MP4")

        for status, code in ((413, None), (400, 40005)):
            with self.subTest(status=status):
                self.responses.append((status, {"code": code, "message": "private"}))
                with patch.object(finished, "_make_attachment", compress):
                    self.assertTrue(await self.deliver())
                    self.assertFalse(await self.deliver())
                self.assertEqual([row[2] for row in self.uploads], [b"rendered master", b"small MP4"])
                self.assertEqual(self.uploads[1][1], "sample.mp4")
                self.assertEqual(self.output.read_bytes(), b"rendered master")
                self.assertEqual(json.loads(self.receipt.read_text()), {
                    "42": hashlib.sha256(b"rendered master").hexdigest(),
                })
                self.receipt.unlink()
                self.uploads.clear()

    async def test_reports_an_unrelated_rejection_without_replaying_or_exposing_response(self):
        self.responses.append((400, {"code": 50035, "message": "private"}))
        with self.assertRaisesRegex(ValueError, "HTTP 400") as caught:
            await self.deliver()
        self.assertNotIn("private", str(caught.exception))
        self.assertNotIn("token", str(caught.exception))
        self.assertEqual(len(self.uploads), 1)
        self.assertFalse(self.receipt.exists())

    async def test_connection_loss_and_unconfirmed_messages_require_review_without_replay(self):
        for status, body in ((0, {}), (200, {"id": "42"}), (408, {}), (503, {})):
            with self.subTest(status=status):
                self.responses.append((status, body))
                with self.assertRaisesRegex(ValueError, "Check the finished channel") as caught:
                    await self.deliver()
                self.assertIsInstance(caught.exception, finished.FinishedDeliveryUncertain)
                self.assertEqual(len(self.uploads), 1)
                self.assertFalse(self.receipt.exists())
                self.uploads.clear()

    async def test_confirmed_upload_with_unwritable_receipt_requires_review(self):
        with patch.object(finished, "atomic_json", side_effect=OSError):
            with self.assertRaisesRegex(finished.FinishedDeliveryUncertain, "receipt could not be saved"):
                await self.deliver()
        self.assertEqual(len(self.uploads), 1)
        self.assertFalse(self.receipt.exists())

    async def test_rejected_compressed_attachment_keeps_master_and_has_no_receipt(self):
        self.responses.extend([(413, {}), (413, {})])

        def compress(source, destination):
            destination.write_bytes(b"small MP4")

        with patch.object(finished, "_make_attachment", compress):
            with self.assertRaisesRegex(ValueError, "too large"):
                await self.deliver()
        self.assertEqual(len(self.uploads), 2)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.output.read_bytes(), b"rendered master")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe")
    async def test_smaller_attachment_keeps_full_length_portrait_video_and_audio(self):
        await asyncio.to_thread(subprocess.run, [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=s=1080x1920:r=12:d=2", "-f", "lavfi", "-i", "sine=d=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-c:a", "aac", str(self.output),
        ], check=True)
        master = self.output.read_bytes()
        cap = 250_000
        self.assertGreater(len(master), cap)
        self.responses.append((413, {}))
        with patch.object(finished, "_ATTACHMENT_LIMIT_BYTES", cap):
            self.assertTrue(await self.deliver())
        attachment = self.root / "received.mp4"
        attachment.write_bytes(self.uploads[1][2])
        self.assertLessEqual(attachment.stat().st_size, cap)
        self.assertEqual(self.output.read_bytes(), master)
        info = probe(attachment)
        self.assertEqual((info.width, info.height, info.video_codec, info.audio_codec),
                         (1080, 1920, "h264", "aac"))
        self.assertAlmostEqual(info.video_duration, 2, delta=0.1)
        self.assertAlmostEqual(info.audio_duration, 2, delta=0.1)
