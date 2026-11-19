"""Measure known changes on real, losslessly generated video and audio."""
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from clipmatch.highlight_signals import measure


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for media measurements")
class HighlightSignalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.source = Path(directory.name) / "clip with delayed audio.mkv"
        cls.face = (96, 16, 32, 32)
        # Half the frame changes just after 0.5s. Only the facecam changes at 1s.
        video = (
            "color=black:size=128x64:rate=20:duration=2,"
            "drawbox=x=0:y=0:w=64:h=64:color=white:t=fill:enable='gte(t,0.55)',"
            "drawbox=x=96:y=16:w=32:h=32:color=white:t=fill:enable='gte(t,1)'"
        )
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", video,
            "-itsoffset", "0.75", "-f", "lavfi", "-i",
            "aevalsrc=0.5*sin(2*PI*400*t):s=16000:d=0.375",
            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(cls.source),
        ], check=True)
        cls.short_silent_source = Path(directory.name) / "two static frames.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=black:size=128x64:rate=20:duration=0.1",
            "-c:v", "ffv1", str(cls.short_silent_source),
        ], check=True)

    def test_full_frame_and_face_motion_follow_their_own_pixels(self):
        samples = measure(self.source, 2, self.face)
        self.assertEqual([s.time for s in samples], [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75])
        for sample in (samples[0], samples[1], samples[2], *samples[5:]):
            self.assertEqual((sample.game_motion, sample.face_motion), (0, 0))
        self.assertGreater(samples[3].game_motion, 100)
        self.assertLess(samples[3].game_motion, 130)
        self.assertEqual(samples[3].face_motion, 0)
        self.assertGreater(samples[4].face_motion, 210)
        self.assertAlmostEqual(samples[4].game_motion, samples[4].face_motion / 8, delta=1)

    def test_rms_uses_clip_time_and_counts_silence_after_audio_ends(self):
        samples = measure(self.source, 2, self.face)
        self.assertEqual(len(samples), 8)
        for sample in (*samples[:3], *samples[5:]):
            self.assertTrue(math.isfinite(sample.rms_db))
            self.assertLess(sample.rms_db, -70)
        # A half-scale sine is -9.03 dBFS. The next bin is half silence.
        self.assertAlmostEqual(samples[3].rms_db, -9.03, delta=0.2)
        self.assertAlmostEqual(samples[4].rms_db, -12.04, delta=0.2)

    def test_audio_window_stops_at_the_requested_clip_end(self):
        samples = measure(self.source, 1.125, self.face)
        self.assertEqual([s.time for s in samples], [0, 0.25, 0.5, 0.75, 1])
        self.assertAlmostEqual(samples[-1].rms_db, -9.03, delta=0.2)

    def test_mono_rms_preserves_a_gap_between_audio_packets(self):
        source = self.source.with_name("stereo with a packet gap.mkv")
        # Opposite phases cancel in mono until 0.25s. The next 0.05s is missing.
        stereo = (
            "aevalsrc=0.5*sin(2*PI*400*t)|0.5*sgn(t-0.25)*sin(2*PI*400*t):s=16000:d=0.75,"
            "asetnsamples=n=400:p=0,aselect='lt(t,0.25)+gte(t,0.3)'"
        )
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=black:size=128x64:rate=20:duration=0.75", "-f", "lavfi", "-i", stereo,
            "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source),
        ], check=True)
        samples = measure(source, 0.75, self.face)
        self.assertLess(samples[0].rms_db, -70)
        # A gap occupying one fifth of the bin reduces RMS by 10 * log10(0.8) dB.
        self.assertAlmostEqual(samples[1].rms_db - samples[2].rms_db, -0.97, delta=0.1)

    def test_quarter_contains_the_peak_scene_change_at_and_between_boundaries(self):
        source = self.source.with_name("two scene cuts.mkv")
        video = (
            "color=black:size=128x64:rate=24:duration=1.5,"
            "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='gte(t,0.625)*lt(t,1)'"
        )
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", video, "-c:v", "ffv1", str(source),
        ], check=True)
        samples = measure(source, 1.5, self.face)
        self.assertEqual([s.time for s in samples if s.scene_change > 75], [0.5, 1.0])
        self.assertTrue(all(s.scene_change == 0 for s in samples if s.time not in (0.5, 1.0)))

    def test_sub_quarter_second_static_clip_without_audio_has_one_sample(self):
        samples = measure(self.short_silent_source, 0.1, self.face)
        self.assertEqual(len(samples), 1)
        self.assertEqual((samples[0].time, samples[0].game_motion, samples[0].face_motion), (0, 0, 0))
        self.assertTrue(math.isfinite(samples[0].rms_db))
        self.assertLess(samples[0].rms_db, -70)
