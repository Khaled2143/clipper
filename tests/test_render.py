import tempfile
import unittest
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

from clipmatch.spec import ClipSpec, LayoutSegment, Settings, parse_job
from clipmatch.media import MediaInfo, SourceWindow
from clipmatch.render import gameplay_rect, layout_filter, validate_crop, render_clip, render_key
from clipmatch.transcribe import Word
from clipmatch.captions import overlay_events


class RenderTests(unittest.TestCase):
    def test_agent00_gameplay_uses_the_tall_section_left_of_the_camera(self):
        # Fitting the 1920x620 strip above the camera gives 349 rows of gameplay.
        # Fitting the 1056x1080 section to its left gives 1104 rows.
        self.assertEqual(gameplay_rect((1056, 620, 768, 460), 1920, 1080),
                         (0, 0, 1056, 1080))

    def adaptive_job(self, directory, segments):
        return parse_job({
            'version': 1, 'settings': {'layout': 'adaptive', 'fps': 8, 'preset': 'ultrafast'},
            'clips': [dict(id='switch', source='source.mp4', start=1, end=3,
                           label='Caster', layout_segments=segments)],
        }, directory)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg suite required")
    def test_adaptive_panels_fill_edges_and_center_the_action_across_switches(self):
        import array
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            settings, (clip,) = self.adaptive_job(directory, [
                dict(start=0, facecam_rect=[0, 0, 48, 180]),
                dict(start=.375, facecam_rect=None),
                dict(start=1.375, facecam_rect=[272, 0, 48, 180]),
            ])
            source = directory / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=green:s=320x180:r=8,drawbox=x=0:y=0:w=48:h=180:c=red:t=fill,'
                            'drawbox=x=272:y=0:w=48:h=180:c=blue:t=fill,'
                            'drawbox=x=154:y=0:w=12:h=180:c=yellow:t=fill',
                            '-f', 'lavfi', '-i', 'sine=frequency=440', '-t', '3', '-c:v', 'libx264',
                            '-c:a', 'aac', str(source)], check=True)
            output = directory / 'master.mp4'
            render_clip(clip, SourceWindow(source, 0, 1, 2, 'source'), [], settings, output)
            frames = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-vf',
                                              'scale=54:96:flags=neighbor', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
            def pixel(frame, x, y):
                offset = ((frame * 96 + y) * 54 + x) * 3
                return tuple(frames[offset:offset + 3])
            self.assertEqual(len(frames), 16 * 54 * 96 * 3)
            for frame, camera_channel in ((0, 0), (2, 0), (11, 2), (15, 2)):
                for x in (0, 27, 53):
                    for y in (0, 35):
                        self.assertGreater(pixel(frame, x, y)[camera_channel], 200, (frame, x, y))
                for x in (0, 53):
                    for y in (36, 95):
                        self.assertGreater(pixel(frame, x, y)[1], 85, (frame, x, y))
                # The source-center marker stays centered even when the disjoint game region moves.
                self.assertGreater(min(pixel(frame, 27, 65)[:2]), 200)
            for frame in range(3, 11):
                for x in (0, 53):
                    for y in (0, 48, 95):
                        self.assertGreater(pixel(frame, x, y)[1], 85, (frame, x, y))
                self.assertGreater(min(pixel(frame, 27, 48)[:2]), 200)
            # White lettering sits over the lower left of the upper picture; no reserved band.
            self.assertGreater(max(min(pixel(0, x, y)) for x in range(1, 13) for y in range(32, 35)), 120)
            pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-vn',
                                           '-ac', '1', '-ar', '8000', '-f', 's16le', '-'])
            samples = array.array('h', pcm)
            for begin in range(0, 16000, 400):
                block = samples[begin:begin + 400]
                crossings = sum(a < 0 <= b for a, b in zip(block, block[1:]))
                self.assertAlmostEqual(crossings, 22, delta=2)

    def test_cache_misses_when_only_an_adaptive_cut_time_or_crop_changes(self):
        def key(start, x):
            settings, (clip,) = self.adaptive_job(Path.cwd(), [
                dict(start=0, facecam_rect=None), dict(start=start, facecam_rect=[x, 0, 60, 60])])
            window = SourceWindow(Path('unused'), 0, 1, 2, 'source')
            return render_key(clip, window, [], settings, False, {})
        with patch('clipmatch.render.tool_versions', return_value={}):
            # An existing padded master with identical inputs must be rendered again.
            self.assertNotEqual(key(.5, 0), '64558aee6dc52954f5f184e568ea968ce2289dbe5d590d0608aa28e16b4d3e1b')
            self.assertNotEqual(key(.5, 0), key(.625, 0))
            self.assertNotEqual(key(.5, 0), key(.5, 10))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg needed to inspect crop geometry")
    def test_single_adaptive_crop_preserves_anamorphic_display_geometry(self):
        # A ten-pixel stripe in a 2:1 SAR frame is twenty display pixels wide.
        # The 180px-tall portrait window spans ~101 display pixels, so the
        # stripe occupies ~21 of the 108 output columns measured below.
        graph = layout_filter(None, 1, "adaptive", segments=(LayoutSegment(0, None),),
                              info=MediaInfo(320, 180, 1, True))
        frame = subprocess.check_output([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=black:s=320x180:r=1,drawbox=x=155:y=0:w=10:h=180:c=white:t=fill,setsar=2",
            "-filter_complex", graph + ";[base]scale=108:192:flags=neighbor[out]", "-map", "[out]",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ])
        for y in (0, 96, 191):
            stripe = [x for x in range(108) if frame[y * 108 + x] > 170]
            self.assertTrue(19 <= len(stripe) <= 23, (y, stripe))
            self.assertAlmostEqual((stripe[0] + stripe[-1]) / 2, 53.5, delta=1)

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

    def test_stacked_layout_places_the_streamer_label_and_optional_words(self):
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
