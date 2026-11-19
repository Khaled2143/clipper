import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from yt_dlp.extractor.twitch import TwitchClipsIE

from clipmatch import finish_clip
from clipmatch.media import MediaInfo, run
from clipmatch.spec import load_job


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

    def render(self, spec, config, *, send_finished):
        settings, (clip,) = load_job(spec, config)
        self.assertEqual(clip.facecam_rect, (1440, 54, 480, 216))
        self.assertEqual((clip.start, clip.end, clip.label, clip.hook), (0, 9, "agent00", ""))
        self.assertEqual(settings.layout, "stacked")
        self.assertEqual(Path(clip.source).read_bytes(), b"complete Twitch MP4")
        self.assertEqual(settings.model, "/opt/model")
        settings.output_dir.mkdir(exist_ok=True)
        (settings.output_dir / f"{clip.id}.json").write_text(json.dumps({
            "source": {"url_or_path": clip.source, "start": clip.start, "end": clip.end}
        }))
        if send_finished:
            self.deliveries.append(clip.id)
        return 0

    def test_render_only_then_delivery_reuses_source_and_preserves_twitch_provenance(self):
        with patch.object(finish_clip, "run", side_effect=self.download), \
             patch.object(finish_clip, "probe", return_value=self.info), \
             patch.object(finish_clip, "render_job", side_effect=self.render), \
             patch.dict("os.environ", {"CLIPMATCH_MODEL": "/opt/model"}):
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

    def test_high_bitrate_short_clip_remains_eligible(self):
        source = self.state / "high-bitrate.mp4"
        with source.open("wb") as output:
            output.truncate(129 * 1024 * 1024)
        with patch.object(finish_clip, "probe", return_value=self.info):
            self.assertEqual(finish_clip._inspect_source(source), self.info)

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
             patch.object(finish_clip, "render_job", side_effect=self.render), \
             patch.dict("os.environ", {"CLIPMATCH_MODEL": "/opt/model"}):
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
             patch.object(finish_clip, "render_job", side_effect=self.render), \
             patch.dict("os.environ", {"CLIPMATCH_MODEL": "/opt/model"}):
            self.assertEqual(self.invoke(), 0, self.errors.getvalue())
        self.assertEqual(len(self.deliveries), 1)
