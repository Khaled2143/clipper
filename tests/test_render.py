import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from clipmatch.spec import ClipSpec, Settings
from clipmatch.media import MediaInfo, SourceWindow
from clipmatch.render import layout_filter, validate_crop, render_clip, render_key
from clipmatch.transcribe import Word
from clipmatch.captions import overlay_events


class RenderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg needed to inspect layout pixels")
    def test_stacked_frames_fill_both_panels_with_facecam_and_center_gameplay(self):
        # Source has a red facecam on the left and blue gameplay across the center.
        argv = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=blue:size=320x180,drawbox=x=0:y=0:w=64:h=50:color=red:t=fill",
                "-filter_complex", layout_filter((0, 0, 64, 50), 30, "stacked"),
                "-map", "[base]", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        frame = subprocess.check_output(argv)
        self.assertEqual(len(frame), 1080 * 1920 * 3)
        for row, channel in [(0, 0), (719, 0), (720, 2), (1919, 2)]:
            for x in (0, 540, 1079):
                start = (row * 1080 + x) * 3
                self.assertGreater(frame[start + channel], 220, f"pixel at {x},{row}")

    def test_automatic_overlays_show_only_the_streamer_and_words(self):
        from clipmatch.captions import caption_events
        label, = overlay_events("", "agent00", 5, "stacked")
        self.assertIn("agent00", label)
        self.assertIn(",Label,", label)
        self.assertNotIn(",Hook,", label)
        words = [Word(0, 1, "Hello"), Word(1, 2, "there")]
        events = caption_events(words, 2, "stacked")
        self.assertEqual(len(events), 2)
        self.assertIn(r"\pos(510,836)", events[0])
        self.assertIn("Hello", events[0])

    def test_stack_rerenders_while_legacy_manual_cache_keys_still_match(self):
        clip = ClipSpec("one", "source.mp4", 2, 8, "A", "Hi", (0, 0, 320, 180))
        window = SourceWindow(Path("source.mp4"), 0, 2, 6, "abc")
        words = [Word(0, 1, "hi")]
        with patch("clipmatch.render.tool_versions", return_value={}):
            legacy_key = render_key(clip, window, words, Settings(), False, {})
            self.assertEqual(legacy_key, "2ddd2b6fe736fd5ef2492079f92032f0b75f5f35e61155d15d0bdfd82658d6ba")
            self.assertNotEqual(render_key(clip, window, words, Settings(layout="stacked"), False, {}), legacy_key)

    def test_crop_bounds(self):
        info = MediaInfo(1920,1080,5,True)
        validate_crop((1500,0,420,300),info)
        with self.assertRaisesRegex(ValueError,'bounds'):
            validate_crop((1500,0,500,300),info)

    def test_fit_geometry_for_portrait_and_landscape(self):
        blur = layout_filter(None,30)
        self.assertIn('force_original_aspect_ratio=decrease',blur)
        self.assertIn('force_original_aspect_ratio=increase',blur)
        self.assertIn('setsar=1',blur)
        face = layout_filter((0,0,320,180),30)
        self.assertIn('crop=320:180:0:0',face)
        self.assertIn('pad=1080:600',face)
        self.assertIn('pad=1080:800',face)

    def test_render_key_depends_on_text_and_not_transcription_device(self):
        clip = ClipSpec('one','source.mp4',2,8,'A','Hi')
        window = SourceWindow(Path('source.mp4'),0,2,6,'abc')
        from dataclasses import replace
        with patch('clipmatch.render.tool_versions', return_value={}), patch('clipmatch.render.resolve_fonts', return_value={}):
            key = render_key(clip,window,[],Settings(),False)
            self.assertNotEqual(key, render_key(replace(clip,hook='Changed'),window,[],Settings(),False))
            self.assertEqual(key, render_key(clip,window,[],Settings(device='cuda'),False))
            self.assertNotEqual(key,render_key(clip,window,[Word(0,1,'hi')],Settings(),False))

    def test_user_hook_is_literal_and_visible_from_start(self):
        events = overlay_events("That's: {\\pos(5,5)} 世界",'X',6)
        self.assertIn('0:00:00.00',events[0])
        self.assertNotIn('{\\pos',events[0])
        with self.assertRaises(ValueError):
            overlay_events('too long '*100,'A',6)

    def test_failed_render_preserves_previous_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            output=root/'final.mp4'
            output.write_bytes(b'previous valid video')
            clip=ClipSpec('one','source.mp4',2,8,'A','Hi')
            window=SourceWindow(root/'source.mp4',0,2,6,'abc')
            def failed(argv,**kwargs):
                raise ValueError('encoding failed')
            with patch('clipmatch.render.probe',return_value=MediaInfo(1920,1080,10,True)), patch('clipmatch.render.run',side_effect=failed):
                with self.assertRaises(ValueError):
                    render_clip(clip,window,[],Settings(),output)
            self.assertEqual(output.read_bytes(),b'previous valid video')

class CoverageTests(unittest.TestCase):
    def test_anamorphic_facecam_normalizes_pixel_aspect_before_fitting(self):
        graph=layout_filter((0,0,320,180),30)
        self.assertIn('crop=320:180:0:0,scale=trunc(iw*sar/2)*2:ih,setsar=1,scale=1080:600',graph)

    def test_audio_duration_cannot_mask_truncated_video(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            output=root/'final.mp4'
            output.write_bytes(b'previous')
            clip=ClipSpec('one','source.mp4',0,3,'A','Hi')
            window=SourceWindow(root/'source.mp4',0,0,3,'a')
            from types import SimpleNamespace as NS
            results=[MediaInfo(640,360,4,True),NS(width=1080,height=1920,duration=3,has_audio=True,
                      video_codec='h264',audio_codec='aac',video_duration=2,audio_duration=3)]
            def fake(argv,**kwargs):
                Path(argv[-1]).write_bytes(b'bad truncated video')
                return ''
            with patch('clipmatch.render.probe',side_effect=results),patch('clipmatch.render.run',side_effect=fake):
                with self.assertRaisesRegex(ValueError,'verification'):
                    render_clip(clip,window,[],Settings(),output)
            self.assertEqual(output.read_bytes(),b'previous')

class FontTests(unittest.TestCase):
    def test_font_identity_changes_when_selected_font_file_changes(self):
        from clipmatch.render import resolve_fonts
        with tempfile.TemporaryDirectory() as d:
            font=Path(d)/'fallback.ttf';font.write_bytes(b'font version one')
            def fake(argv,**kwargs):
                kwargs['diagnostics'].append(f'fontselect: (Arial, 700, 0) -> {font}, 0, Fallback')
                return ''
            with patch('clipmatch.render.run',side_effect=fake):
                first=resolve_fonts('Arial','Hello')
                font.write_bytes(b'font version two')
                second=resolve_fonts('Arial','Hello')
            self.assertNotEqual(first,second)
            self.assertIn('fallback.ttf',first['selected'])

    def test_missing_glyphs_are_reported_as_failure(self):
        from clipmatch.render import resolve_fonts
        def fake(argv,**kwargs):
            kwargs['diagnostics'].append('fontselect: failed to find any fallback with glyph 0x754C')
            return ''
        with patch('clipmatch.render.run',side_effect=fake):
            with self.assertRaisesRegex(ValueError,'glyph'):
                resolve_fonts('Arial','界')
