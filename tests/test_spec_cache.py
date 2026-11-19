import json
import tempfile
import unittest
from pathlib import Path

from clipmatch.spec import load_job, parse_job
from clipmatch.cache import atomic_json, cache_valid, fingerprint, complete


class SpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.clip = dict(id='one', source='source.mp4', start='00:00:02', end=8,
                         label='KAI', hook='Wait: what?!')

    def parse(self, clips=None, **extra):
        return parse_job(dict(version=1, clips=clips or [self.clip], **extra), self.root)

    def test_times_and_relative_paths(self):
        settings, clips = self.parse()
        self.assertEqual((clips[0].start, clips[0].end), (2.0, 8.0))
        self.assertEqual(clips[0].source, str(self.root / 'source.mp4'))
        self.assertEqual(settings.output_dir, self.root / 'renders')

    def test_rejects_bad_ranges_ids_and_crops(self):
        for change in [dict(end=2), dict(start=float('nan')), dict(id='../bad'),
                       dict(id='CON'), dict(facecam_rect=[0, 0, 0, 20]),
                       dict(facecam_rect=[0, 0, True, 20]), dict(start=True),
                       dict(source='ftp://example.com/vod')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.parse([{**self.clip, **change}])
        with self.assertRaises(ValueError):
            self.parse([self.clip, self.clip])

    def test_unknown_fields_and_invalid_settings(self):
        with self.assertRaisesRegex(ValueError, 'hok'):
            self.parse([{**self.clip, 'hok': 'typo'}])
        for settings in [dict(crf=100), dict(fps=0), dict(buffer_seconds=-1),
                         dict(device='magic'), dict(font='a\nb')]:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.parse(settings=settings)

    def test_stacked_job_has_a_facecam_and_omits_the_manual_hook(self):
        clip = {**self.clip, "facecam_rect": [0, 0, 100, 100]}
        del clip["hook"]
        _, (stacked,) = self.parse([clip], settings={"layout": "stacked"})
        self.assertEqual(stacked.hook, "")
        with self.assertRaisesRegex(ValueError, "hook"):
            self.parse([clip])
        with self.assertRaisesRegex(ValueError, "facecam_rect"):
            self.parse([{**clip, "facecam_rect": None}], settings={"layout": "stacked"})

    def test_yaml_loading_and_explicit_config_paths(self):
        spec = self.root / 'job.yaml'
        spec.write_text('version: 1\nclips:\n  - id: first\n    source: a.mp4\n    start: 0\n    end: 2\n    label: A\n    hook: Hi\n')
        config = self.root / 'config' / 'settings.yaml'
        config.parent.mkdir()
        config.write_text('version: 1\nsettings:\n  crf: 22\n')
        settings, clips = load_job(spec, config)
        self.assertEqual(settings.crf, 22)
        self.assertEqual(settings.output_dir, config.parent / 'renders')
        self.assertEqual(clips[0].source, str(self.root / 'a.mp4'))

    def test_adaptive_segments_use_edit_relative_seconds_and_verified_crops(self):
        clip = {k: v for k, v in self.clip.items() if k != 'hook'}
        segments = [dict(start=0, facecam_rect=None), dict(start=1.25, facecam_rect=[2, 4, 32, 24])]
        _, (parsed,) = self.parse([{**clip, 'layout_segments': segments}], settings={'layout': 'adaptive'})
        self.assertEqual(parsed.hook, '')
        self.assertEqual([(s.start, s.facecam_rect) for s in parsed.layout_segments],
                         [(0, None), (1.25, (2, 4, 32, 24))])
        for invalid in ([], [{'start': 0}], [dict(start=1, facecam_rect=None)],
                        [dict(start=False, facecam_rect=None)], [dict(start='0', facecam_rect=None)],
                        [segments[0], dict(start=6, facecam_rect=None)],
                        [segments[0], dict(start=float('nan'), facecam_rect=None)],
                        [segments[0], segments[1], segments[1]],
                        [dict(start=0, facecam_rect=[0, 0, -2, 2])]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.parse([{**clip, 'layout_segments': invalid}], settings={'layout': 'adaptive'})
        for invalid in (dict(hook='Hello'), dict(facecam_rect=[0, 0, 8, 8])):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.parse([{**clip, 'layout_segments': segments, **invalid}], settings={'layout': 'adaptive'})
        with self.assertRaises(ValueError):
            self.parse([{**self.clip, 'layout_segments': segments}])


class CacheTests(unittest.TestCase):
    def test_only_completed_intact_artifacts_hit(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'audio.wav'
            manifest = out.with_suffix('.manifest.json')
            out.write_bytes(b'valid')
            complete(out, manifest, 'abc', 'audio', {'ffmpeg': 'test'})
            self.assertTrue(cache_valid(out, manifest, 'abc'))
            self.assertFalse(cache_valid(out, manifest, 'changed'))
            out.write_bytes(b'wrong')  # same size corruption must miss
            self.assertFalse(cache_valid(out, manifest, 'abc'))
            out.write_bytes(b'')
            self.assertFalse(cache_valid(out, manifest, 'abc'))
            out.unlink()
            self.assertFalse(cache_valid(out, manifest, 'abc'))

    def test_atomic_json_and_canonical_keys(self):
        self.assertEqual(fingerprint({'a': 1, 'b': 2}), fingerprint({'b': 2, 'a': 1}))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'nested' / 'data.json'
            atomic_json(path, {'hello': '世界'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'hello': '世界'})
            self.assertEqual(list(path.parent.glob('*.tmp')), [])
