import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from yt_dlp.extractor.twitch import TwitchClipsIE

from clipmatch import finish_clip
from clipmatch.finished import FinishedDeliveryUncertain
from clipmatch.highlights import Highlight
from clipmatch.live_worker import ClipState
from clipmatch.media import MediaInfo, run
from clipmatch.spec import load_job


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe")
class FinishedEditMediaTests(unittest.TestCase):
    def test_local_master_keeps_setup_and_payoff_from_the_original(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with closing(ClipState(state)) as capture:
                capture.remember("Example", "agent00", "test spike", chat_lead=6)
            footage = state / "footage.mp4"
            run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                 "color=gray:s=160x90:r=8:d=48[still];testsrc2=s=160x90:r=8:d=48[play];"
                 "[still][play]overlay=enable='between(t,32,36)'",
                 "-f", "lavfi", "-i", "aevalsrc='if(between(t,32,38),0.4*sin(440*PI*t),0)':s=8000:d=48",
                 "-c:v", "libx264", "-threads", "2", "-c:a", "aac", str(footage)])
            def local_download(destination, url):
                shutil.copyfile(footage, destination)
                return {"display_id": "Example", "channel": "Agent00", "title": "Example"}
            with patch.object(finish_clip, "_download", side_effect=local_download), \
                 patch("clipmatch.cli.transcribe", side_effect=AssertionError("unexpected transcription")):
                status = finish_clip.main(["--state-dir", temp, "--channel", "agent00",
                                           "--clip-id", "Example", "--url", "https://clips.twitch.tv/Example",
                                           "--render-only"])
            self.assertEqual(status, 0)
            directory = finish_clip.clip_directory(state, "agent00", "Example")
            master = directory / "renders" / f"{directory.name}.mp4"
            sidecar = json.loads(master.with_suffix(".json").read_text(encoding="utf-8"))
            start, end = sidecar["source"]["start"], sidecar["source"]["end"]
            self.assertTrue(25 <= start < 32, start)
            self.assertTrue(38 <= end < 48, end)
            self.assertAlmostEqual(finish_clip.probe(master).duration, end - start, delta=.15)
            self.assertEqual((directory / "source.mp4").read_bytes(), footage.read_bytes())
            self.assertEqual(sidecar["edit"]["source_duration"], 48)
            self.assertEqual(sidecar["edit"]["chat_lead_seconds"], 6)
            self.assertEqual(sidecar["edit"]["chat_estimate"], 32)
            self.assertFalse(sidecar["captions_enabled"])
            self.assertFalse(master.with_suffix(".srt").exists())

    def test_failed_delivery_keeps_the_current_edit_with_the_verified_master(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            url = "https://clips.twitch.tv/Example"
            args = ["--state-dir", temp, "--channel", "agent00", "--clip-id", "Example", "--url", url]
            directory = finish_clip.clip_directory(state, "agent00", "Example")
            master = directory / "renders" / f"{directory.name}.mp4"
            sidecar_path = master.with_suffix(".json")
            at_upload = []

            def local_download(destination, url):
                run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=160x90:r=8:d=2",
                     "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac",
                     str(destination)])
                return {"display_id": "Example", "channel": "agent00", "title": "Example"}

            async def lose_confirmation(session, webhook, path, filename, content=""):
                at_upload.append(json.loads(path.with_suffix(".json").read_text(encoding="utf-8")))
                raise FinishedDeliveryUncertain("Mock upload unconfirmed")

            webhook = "https://discord.com/api/webhooks/1/token"
            with patch.object(finish_clip, "_download", side_effect=local_download), \
                 patch.dict(os.environ, {"DISCORD_FINISHED_WEBHOOK_URL": webhook, "SOCIAL_VISION_MODEL": ""}), \
                 patch("clipmatch.finished._upload", new=lose_confirmation), \
                 redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(finish_clip.main([*args, "--render-only"]), 0)
                verified_master = master.read_bytes()
                with closing(ClipState(state)) as capture:
                    capture.remember("Example", "agent00", "test spike", chat_lead=1)
                self.assertEqual(finish_clip.main(args), 1)
                retained = json.loads(sidecar_path.read_text(encoding="utf-8"))
                self.assertEqual(at_upload, [retained])
                self.assertEqual(retained["source"].get("twitch_url"), url)
                edit = retained["edit"]
                self.assertEqual((edit["start"], edit["end"], edit["source_duration"]), (0, 2, 2))
                self.assertEqual((edit["cue"], edit["chat_lead_seconds"]), ("short_source", 1))

                # A failed recut must leave the previous master's times and provenance together.
                with patch.object(finish_clip, "choose_edit", return_value=Highlight(.5, 1.5, None, "test", None)), \
                     patch("clipmatch.cli.render_clip", side_effect=ValueError("Mock encode failed")):
                    self.assertEqual(finish_clip.main(args), 1)
                self.assertEqual(master.read_bytes(), verified_master)
                self.assertEqual(json.loads(sidecar_path.read_text(encoding="utf-8")), retained)
                self.assertEqual(at_upload, [retained])


class FinishClipTests(unittest.TestCase):
    slug = "CovertDoubtfulBillKAPOW--bw1uFkCIa6BJc-d"
    url = f"https://www.twitch.tv/agent00/clip/{slug}"
    canonical = f"https://clips.twitch.tv/{slug}"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        layouts = self.state / "layouts.json"
        layouts.write_text('{"agent00": [0.75, 0.05, 0.25, 0.20]}')
        self.enterContext(patch.object(finish_clip, "LAYOUTS", layouts))
        self.enterContext(redirect_stdout(StringIO()))
        self.errors = self.enterContext(redirect_stderr(StringIO()))
        self.info = MediaInfo(1920, 1080, 9.07, True, "h264", "aac", 9, 9.04)
        self.downloads = []
        self.deliveries = []

    def invoke(self, *flags, channel="agent00"):
        return finish_clip.main(["--state-dir", str(self.state), "--channel", channel,
                                 "--clip-id", self.slug, "--url", self.url, *flags])

    def download(self, argv, **kwargs):
        self.assertEqual(argv[-2:], ["--", self.canonical])
        self.assertIn("--no-playlist", argv)
        self.assertIn("--ignore-config", argv)
        self.assertNotIn("--max-filesize", argv)
        self.assertEqual(argv[argv.index("--use-extractors") + 1], TwitchClipsIE.IE_NAME)
        self.downloads.append(self.canonical)
        source = self.state / "landscape.mp4"
        portrait = self.state / "portrait.mp4"
        source.write_bytes(b"complete Twitch MP4")
        portrait.write_bytes(b"reframed portrait footage")
        formats = [dict(format_id=name, url=path.as_uri(), ext="mp4", height=h,
                        vcodec="h264", acodec="aac")
                   for name, path, h in [("720", source, 720), ("portrait-1080", portrait, 1080)]]
        fixture = self.state / "twitch-metadata.json"
        fixture.write_text(json.dumps(dict(id="123", display_id=self.slug, channel="Agent00",
            title="Last lap", duration=9, webpage_url=self.canonical, formats=formats,
            extractor="twitch:clips", extractor_key="TwitchClips")))
        return run([*argv[:-2], "--load-info-json", str(fixture), "--enable-file-urls"], **kwargs)

    def render(self, spec, config, *, send_finished, no_captions=False, add_provenance):
        settings, (clip,) = load_job(spec, config)
        self.assertTrue(no_captions, "Automatic masters must skip generated captions")
        self.assertEqual(clip.facecam_rect, (1440, 54, 480, 216))
        self.assertEqual((clip.start, clip.end, clip.label, clip.hook), (0, 9, "agent00", ""))
        self.assertEqual(settings.layout, "stacked")
        self.assertEqual(Path(clip.source).read_bytes(), b"complete Twitch MP4")
        settings.output_dir.mkdir(exist_ok=True)
        data = {"source": {"url_or_path": clip.source, "start": clip.start, "end": clip.end}}
        add_provenance(data)
        (settings.output_dir / f"{clip.id}.json").write_text(json.dumps(data))
        if send_finished:
            self.deliveries.append(clip.id)
        return 0

    def test_render_only_then_delivery_reuses_source_and_preserves_twitch_provenance(self):
        with patch.object(finish_clip, "run", side_effect=self.download), \
             patch.object(finish_clip, "probe", return_value=self.info), \
             patch.object(finish_clip, "render_job", side_effect=self.render):
            self.assertEqual(self.invoke("--render-only"), 0, self.errors.getvalue())
            self.assertEqual(self.deliveries, [])
            self.assertEqual(self.invoke(), 0, self.errors.getvalue())
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.downloads, [self.canonical])
        sidecar, = self.state.glob("finished/agent00/*/renders/*.json")
        self.assertEqual(json.loads(sidecar.read_text())["source"]["twitch_url"], self.canonical)

    def test_invalid_and_mismatched_urls_never_create_work(self):
        for channel, slug, url in [
            ("../agent00", self.slug, self.url), ("agent00", "../outside", self.url),
            ("agent00", self.slug, self.canonical + "/edit"),
            ("agent00", self.slug, self.canonical + "Other"),
            ("agent00", self.slug, self.url.replace("/agent00/", "/other/")),
            ("agent00", self.slug, self.url.replace("www.twitch.tv", "twitch.tv.example")),
            ("agent00", self.slug, self.url.replace("www.twitch.tv", "user@www.twitch.tv")),
            ("agent00", self.slug, self.url.replace("https://", "http://")),
        ]:
            with self.subTest(url=url, slug=slug), self.assertRaises(ValueError):
                finish_clip.validate_clip(channel, slug, url)
        self.assertEqual(finish_clip.validate_clip("agent00", self.slug, self.url + "?t=4"), self.canonical)
        self.assertFalse((self.state / "finished").exists())

    def test_missing_layout_fails_before_downloading(self):
        self.url = self.canonical
        with patch.object(finish_clip, "run", side_effect=AssertionError("unexpected download")):
            self.assertEqual(self.invoke(channel="other"), 1)
        self.assertIn("layouts.json", self.errors.getvalue())

    def test_chat_timing_read_retries_only_for_database_contention(self):
        for code, status in ((sqlite3.SQLITE_BUSY, 75), (sqlite3.SQLITE_LOCKED, 75),
                             (sqlite3.SQLITE_LOCKED_SHAREDCACHE, 75), (sqlite3.SQLITE_ERROR, 1)):
            failure = sqlite3.OperationalError("Chat timing read failed")
            failure.sqlite_errorcode = code
            with self.subTest(code=code), patch.object(finish_clip, "_source", return_value=self.info), \
                 patch.object(finish_clip, "read_chat_lead", side_effect=failure), \
                 patch.object(finish_clip, "render_job", side_effect=AssertionError("unexpected render")):
                self.assertEqual(self.invoke(), status)

    def test_high_bitrate_short_clip_remains_eligible(self):
        source = self.state / "high-bitrate.mp4"
        with source.open("wb") as output:
            output.truncate(129 * 1024 * 1024)
        with patch.object(finish_clip, "probe", return_value=self.info):
            self.assertEqual(finish_clip._inspect_source(source), self.info)

    def test_not_yet_available_twitch_source_requests_a_queue_retry(self):
        for message, expected in (
            ("unable to download video data: HTTP Error 404: Not Found", 75),
            ("This clip is no longer available", 75),
            ("Clip is unavailable", 75),
            ("Requested format is not available", 75),
            ("This clip is private", 1),
            ("HTTP Error 403: Forbidden", 1),
            ("unable to open output file: Permission denied", 1),
        ):
            failure = subprocess.CalledProcessError(1, "yt-dlp", stderr=f"ERROR: {message}")
            with self.subTest(message=message), patch("subprocess.run", side_effect=failure):
                self.assertEqual(self.invoke(), expected)
        self.assertFalse(list(self.state.rglob("source.mp4")))
        self.assertFalse(list(self.state.rglob("finished-discord.json")))

    def test_interrupted_source_is_never_published_and_can_be_retried(self):
        def interrupted(argv, **kwargs):
            Path(argv[argv.index("--output") + 1]).write_bytes(b"partial")
            try:
                raise subprocess.TimeoutExpired("yt-dlp", 180)
            except subprocess.TimeoutExpired as cause:
                raise ValueError("download timed out") from cause

        with patch.object(finish_clip, "run", side_effect=interrupted):
            self.assertEqual(self.invoke(), 75)
        self.assertFalse(list(self.state.rglob("source.mp4")))
        with patch.object(finish_clip, "run", side_effect=self.download), \
             patch.object(finish_clip, "probe", return_value=self.info), \
             patch.object(finish_clip, "render_job", side_effect=self.render):
            self.assertEqual(self.invoke(), 0, self.errors.getvalue())
            source, = self.state.rglob("source.mp4")
            source.write_bytes(b"damaged cache entry")
            self.assertEqual(self.invoke(), 0, self.errors.getvalue())
        self.assertEqual(len(self.downloads), 2)

    def test_mismatched_downloader_identity_never_reaches_render_or_cache(self):
        def wrong_channel(argv, **kwargs):
            metadata = json.loads(self.download(argv, **kwargs))
            metadata["channel"] = "SomeOtherStreamer"
            return json.dumps(metadata)

        with patch.object(finish_clip, "run", side_effect=wrong_channel), \
             patch.object(finish_clip, "render_job", side_effect=AssertionError("unexpected render")):
            self.assertEqual(self.invoke(), 1)
        self.assertFalse(list(self.state.rglob("source.mp4")))

    def test_duplicate_process_cannot_start_a_second_download_or_upload(self):
        def concurrent_download(argv, **kwargs):
            with patch.object(finish_clip, "run", side_effect=AssertionError("concurrent download")):
                self.assertEqual(self.invoke(), 75)
            return self.download(argv, **kwargs)

        with patch.object(finish_clip, "run", side_effect=concurrent_download), \
             patch.object(finish_clip, "probe", return_value=self.info), \
             patch.object(finish_clip, "render_job", side_effect=self.render):
            self.assertEqual(self.invoke(), 0, self.errors.getvalue())
        self.assertEqual(len(self.deliveries), 1)
