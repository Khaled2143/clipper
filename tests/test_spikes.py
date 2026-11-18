import unittest
from math import nextafter

from clipmatch.spikes import Detector, SpikeSettings


def add_messages(detector: Detector, count: int, text: str = "wow") -> None:
    for _ in range(count):
        detector.add({"message": {"text": text}})


class SpikeSettingsTests(unittest.TestCase):
    def test_defaults_and_environment_overrides(self):
        self.assertEqual(SpikeSettings.from_env({}), SpikeSettings(10, 10, 3, 20, 5, 90))
        self.assertEqual(
            SpikeSettings.from_env({
                "BUCKET_SECONDS": "5",
                "BASELINE_MINUTES": "2.5",
                "SPIKE_MULTIPLIER": "3.5",
                "MIN_MESSAGES": "15",
                "WARMUP_MINUTES": "0.5",
                "COOLDOWN_SECONDS": "30",
            }),
            SpikeSettings(5, 2.5, 3.5, 15, 0.5, 30),
        )

    def test_invalid_environment_values_report_the_setting(self):
        for key, value in [
            ("BUCKET_SECONDS", "0"),
            ("BUCKET_SECONDS", "2.5"),
            ("BASELINE_MINUTES", "0.1"),
            ("BASELINE_MINUTES", "inf"),
            ("SPIKE_MULTIPLIER", "NaN"),
            ("SPIKE_MULTIPLIER", "0"),
            ("MIN_MESSAGES", "abc"),
            ("MIN_MESSAGES", "0"),
            ("WARMUP_MINUTES", "-1"),
            ("COOLDOWN_SECONDS", "-1"),
        ]:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(ValueError, key):
                    SpikeSettings.from_env({key: value})


class DetectorTests(unittest.TestCase):
    def test_fractional_start_closes_at_the_computed_boundary(self):
        start = 1015.1
        detector = Detector(SpikeSettings(), start=start)
        boundary = start + 10
        add_messages(detector, 1)
        self.assertEqual(detector.advance(nextafter(boundary, start)), [])
        [bucket] = detector.advance(boundary)
        self.assertEqual(bucket.count, 1)

    def test_exact_count_and_ratio_thresholds_use_earlier_buckets(self):
        settings = SpikeSettings(min_messages=21, warmup_minutes=0)
        cases = [(7, 21, True), (7, 20, False), (8, 21, False)]
        for earlier_count, count, expect_spike in cases:
            with self.subTest(earlier_count=earlier_count, count=count):
                detector = Detector(settings, start=100)
                add_messages(detector, earlier_count)
                detector.advance(110)
                add_messages(detector, count)
                self.assertEqual(detector.advance(119.9), [])
                [bucket] = detector.advance(120)
                self.assertEqual((bucket.count, bucket.baseline), (count, earlier_count))
                if expect_spike:
                    self.assertEqual(bucket.ratio, 3.0)
                self.assertEqual(bucket.spike, expect_spike)

    def test_default_warmup_ends_at_five_minutes(self):
        detector = Detector(SpikeSettings(), start=0)
        detector.advance(290)
        add_messages(detector, 20)
        self.assertFalse(detector.advance(300)[0].spike)
        add_messages(detector, 20)
        self.assertTrue(detector.advance(310)[0].spike)

    def test_zero_baseline_needs_the_minimum_and_has_no_ratio(self):
        detector = Detector(SpikeSettings(warmup_minutes=0), start=0)
        detector.advance(10)
        add_messages(detector, 20)
        [bucket] = detector.advance(20)
        self.assertEqual(bucket.baseline, 0.0)
        self.assertIsNone(bucket.ratio)
        self.assertTrue(bucket.spike)

    def test_silent_windows_contribute_to_the_average(self):
        detector = Detector(SpikeSettings(warmup_minutes=0), start=100)
        add_messages(detector, 12)
        buckets = detector.advance(130)
        self.assertEqual([bucket.count for bucket in buckets], [12, 0, 0])
        self.assertEqual([bucket.baseline for bucket in buckets], [0.0, 12.0, 6.0])
        self.assertFalse(any(bucket.spike for bucket in buckets))
        add_messages(detector, 20)
        [bucket] = detector.advance(140)
        self.assertEqual((bucket.baseline, bucket.ratio), (4.0, 5.0))
        self.assertTrue(bucket.spike)

    def test_baseline_rolls_over_at_the_configured_duration(self):
        detector = Detector(SpikeSettings(baseline_minutes=1, warmup_minutes=0), start=0)
        add_messages(detector, 120)
        detector.advance(70)
        add_messages(detector, 20)
        [bucket] = detector.advance(80)
        self.assertEqual(bucket.baseline, 0.0)
        self.assertTrue(bucket.spike)

    def test_a_long_gap_discards_stale_buckets_and_preserves_recent_silence(self):
        detector = Detector(SpikeSettings(baseline_minutes=1, warmup_minutes=0), start=0)
        add_messages(detector, 100)
        buckets = detector.advance(86400)
        self.assertEqual([bucket.count for bucket in buckets], [0] * 6)
        add_messages(detector, 20)
        [bucket] = detector.advance(86410)
        self.assertEqual(bucket.baseline, 0.0)
        self.assertTrue(bucket.spike)

    def test_channels_have_independent_counts_and_baselines(self):
        settings = SpikeSettings(warmup_minutes=0)
        first = Detector(settings, start=0)
        second = Detector(settings, start=0)
        add_messages(first, 20)
        first.advance(10)
        second.advance(10)
        add_messages(first, 20)
        add_messages(second, 20)
        self.assertFalse(first.advance(20)[0].spike)
        self.assertTrue(second.advance(20)[0].spike)

    def test_top_three_combines_normalized_words_with_emote_names(self):
        detector = Detector(SpikeSettings(), start=0)
        detector.add({
            "message": {
                "text": "WOW! wow Kappa Kappa Kappa gg once",
                "fragments": [
                    {"type": "text", "text": "WOW! wow "},
                    {"type": "emote", "text": "Kappa", "emote": {"id": "25"}},
                    {"type": "text", "text": " "},
                    {"type": "emote", "text": "Kappa", "emote": {"id": "25"}},
                    {"type": "text", "text": " "},
                    {"type": "emote", "text": "Kappa", "emote": {"id": "25"}},
                    {"type": "text", "text": " gg once"},
                ],
            }
        })
        detector.add({"message": {"text": "gg, WOW!"}})
        [bucket] = detector.advance(10)
        self.assertEqual(bucket.count, 2)
        self.assertEqual(bucket.top, ("WOW", "Kappa", "gg"))
