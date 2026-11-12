import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipmatch.spec import ClipSpec, Settings
from clipmatch.media import MediaInfo, SourceWindow, acquire, extract_audio, section_bounds, run, probe


class MediaTests(unittest.TestCase):
    def clip(self, source='https://example.com/vod', start=100, end=110):
        return ClipSpec('one', source, start, end, 'A', 'Hi')

    def test_buffered_bounds_and_beginning_clamp(self):
        self.assertEqual(section_bounds(self.clip(), 2, 500), (98, 112))
        self.assertEqual(section_bounds(self.clip(start=1, end=8), 2, 9), (0, 9))
        with self.assertRaisesRegex(ValueError, 'duration'):
            section_bounds(self.clip(), 2, 105)

    def test_local_window_preserves_source_time_and_checks_audio(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / "it's a source.mp4"
            source.write_bytes(b'fake video')
            with patch('clipmatch.media.probe', return_value=MediaInfo(1920,1080,20,True)):
                window = acquire(self.clip(str(source), 2, 8), Settings(), Path(d))
                self.assertEqual((window.source_start, window.trim_start, window.duration), (0,2,6))
            with patch('clipmatch.media.probe', return_value=MediaInfo(1920,1080,20,False)):
                with self.assertRaisesRegex(ValueError, 'audio'):
                    acquire(self.clip(str(source), 2, 8), Settings(), Path(d))
            with patch('clipmatch.media.probe', return_value=MediaInfo(1920,1080,5,True)):
                with self.assertRaisesRegex(ValueError, 'duration'):
                    acquire(self.clip(str(source), 2, 8), Settings(), Path(d))

    def test_run_passes_paths_without_shell_and_sanitizes_errors(self):
        import subprocess
        with patch('clipmatch.media.subprocess.run') as child:
            child.return_value = subprocess.CompletedProcess([], 0, 'ok', '')
            self.assertEqual(run(['ffmpeg','-i',"C:\\my videos\\it's.mp4"]), 'ok')
            self.assertFalse(child.call_args.kwargs.get('shell', False))
        with patch('clipmatch.media.subprocess.run', side_effect=subprocess.CalledProcessError(
                1, ['tool'], stderr='secret https://user:password@example.com/vod')):
            with self.assertRaises(ValueError) as error:
                run(['tool'])
            self.assertNotIn('password', str(error.exception))

    def test_failed_remote_download_has_no_completion_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            def fake(argv, **kwargs):
                if '--dump-single-json' in argv:
                    return json.dumps({'duration':500, 'id':'123'})
                raise ValueError('download unavailable')
            with patch('clipmatch.media.run', side_effect=fake), patch('clipmatch.media.tool_versions', return_value={}):
                with self.assertRaises(ValueError):
                    acquire(self.clip(), Settings(), Path(d))
            self.assertEqual(list(Path(d).rglob('*.manifest.json')), [])

    def test_extract_trims_exact_window(self):
        with tempfile.TemporaryDirectory() as d:
            window = SourceWindow(Path(d)/'buffer.mp4',98,2,10,'abc')
            def fake(argv, **kwargs):
                Path(argv[-1]).write_bytes(b'wav')
                return ''
            with patch('clipmatch.media.run', side_effect=fake) as child:
                extract_audio(window, Path(d)/'audio.wav')
                args = child.call_args.args[0]
                self.assertEqual(args[args.index('-ss')+1], '2')
                self.assertEqual(args[args.index('-t')+1], '10')
                self.assertEqual(args[args.index('-ar')+1], '16000')
