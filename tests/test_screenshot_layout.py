import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipmatch.layout_judge import review_layout


class ScreenshotLayoutTests(unittest.TestCase):
    cameras = [[0, 56, 80, 68]]

    @unittest.skipUnless(shutil.which('ffmpeg'), 'Requires FFmpeg')
    def test_jynxzi_street_driving_midpoint_keeps_his_forehead_in_the_verified_camera(self):
        # SplendidIcySoymilkPeoplesChamp at 41.25s. The saved hint starts six
        # source pixels below the detected forehead when the host leans forward.
        from clipmatch.media import run
        from clipmatch.screenshot_layout import review_screenshot
        fixture = Path(__file__).with_name('fixtures') / 'jynxzi-driving-midpoint.jpg'
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'midpoint.mp4'
            run(['ffmpeg', '-v', 'error', '-loop', '1', '-i', str(fixture), '-t', '1',
                 '-c:v', 'libx264', '-crf', '14', str(source)])
            result = review_screenshot(source, 0, 1, [[1344, 648, 576, 432]], 1920, 1080)
        box = result['segments'][0]['facecam_rect']
        self.assertIsNotNone(box)
        x, y, w, h = box
        self.assertEqual((x, x + w, y + h), (1344, 1920, 1080))
        self.assertLessEqual(y, 642)
        self.assertGreater(y, 600)

    def test_avatar_well_beyond_the_crop_edge_does_not_expand_the_camera(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        frame = np.random.default_rng(13).integers(0, 255, (360, 640, 3), dtype=np.uint8)
        faces = [(529, 206, 66, 89, .93)]
        camera, _ = screenshot.choose_camera(frame, faces, [[1344, 648, 576, 432]], 1920, 1080)
        self.assertIsNone(camera)

    def test_midpoint_can_switch_corners_without_a_model_service(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        # A textured game's right side has an inset where the saved left camera would mirror.
        rng = np.random.default_rng(42)
        frame = rng.integers(0, 255, (192, 320, 3), dtype=np.uint8)
        with patch.object(screenshot, '_read_frame', return_value=frame) as read, \
             patch.object(screenshot, '_faces', return_value=[(273, 84, 15, 24, .95)]) as detect, \
             patch('clipmatch.layout_judge.ask_model', side_effect=AssertionError('unexpected API request')):
            review = review_layout(Path('source.mp4'), 20, 28, self.cameras, 320, 192, '', mode='screenshot')
        self.assertEqual(review['segments'], [dict(start=0, facecam_rect=[240, 56, 80, 68])])
        self.assertEqual(review['status'], 'judged')
        self.assertEqual(review['sample_source_time'], 24)
        read.assert_called_once()
        detect.assert_called_once()

    def test_nearby_samples_recover_inset_when_streamer_covers_face(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        frame = np.random.default_rng(42).integers(0, 255, (192, 320, 3), dtype=np.uint8)
        face = (273, 84, 15, 24, .95)
        with patch.object(screenshot, '_read_frame', return_value=frame), \
             patch.object(screenshot, '_faces', side_effect=[[], [face], [face], [], []]):
            review = review_layout(Path('source.mp4'), 20, 28, self.cameras, 320, 192, '', mode='screenshot')
        self.assertEqual(review['segments'], [dict(start=0, facecam_rect=[240, 56, 80, 68])])

    def test_nearby_samples_with_disagreeing_or_isolated_faces_are_inconclusive(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        frame = np.random.default_rng(42).integers(0, 255, (192, 320, 3), dtype=np.uint8)
        left = (31, 84, 15, 24, .95)
        right = (273, 84, 15, 24, .95)
        for observations in ([[], [right], [], [], []], [[], [left], [right], [], [right]]):
            with patch.object(screenshot, '_read_frame', return_value=frame), \
                 patch.object(screenshot, '_faces', side_effect=observations):
                review = review_layout(Path('source.mp4'), 20, 28, self.cameras, 320, 192, '', mode='screenshot')
            self.assertEqual(review['segments'], [dict(start=0, facecam_rect=None)])

    def test_avatar_outside_hint_large_camera_and_duplicate_view_use_single(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        rng = np.random.default_rng(42)
        frame = rng.integers(0, 255, (192, 320, 3), dtype=np.uint8)
        for faces in ([(139, 84, 20, 32, .99)], [(2, 53, 74, 92, .98)], [],
                      [(31, 80, 16, 27, .94), (187, 80, 16, 27, .95)]):
            duplicate = frame.copy()
            duplicate[:, 156:] = frame[:, :164]
            rect, _ = screenshot.choose_camera(duplicate, faces, self.cameras, 320, 192)
            self.assertIsNone(rect, faces)

    def test_blank_game_region_and_ambiguous_corners_use_single(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        face = (31, 80, 16, 25, .95)
        frame = np.zeros((192, 320, 3), dtype=np.uint8)
        rect, _ = screenshot.choose_camera(frame, [face], self.cameras, 320, 192)
        self.assertIsNone(rect)
        frame = np.random.default_rng(42).integers(0, 255, frame.shape, dtype=np.uint8)
        cameras = [*self.cameras, [240, 56, 80, 68]]
        rect, _ = screenshot.choose_camera(frame, [face, (273, 84, 15, 24, .95)], cameras, 320, 192)
        self.assertIsNone(rect)

    def test_unconfigured_top_corner_needs_a_visible_inset_to_accept_a_face(self):
        from clipmatch import screenshot_layout as screenshot
        import numpy as np
        frame = np.random.default_rng(22).integers(0, 90, (180, 320, 3), dtype=np.uint8)
        face = (253, 22, 19, 24, .95)
        cams = [[230, 112, 84, 68]]
        rect, _ = screenshot.choose_camera(frame, [face], cams, 320, 180)
        self.assertIsNone(rect)
        frame[:68, 230:314] += 150
        rect, _ = screenshot.choose_camera(frame, [face], cams, 320, 180)
        self.assertEqual(rect, [230, 0, 84, 68])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'Requires FFmpeg')
    def test_real_packaged_detector_finds_no_face_on_color_bars(self):
        from clipmatch.media import run
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'bars.mp4'
            run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'smptebars=s=320x180:r=8:d=1',
                 '-c:v', 'libx264', str(source)])
            review = review_layout(source, 0, 1, self.cameras, 320, 180, '', mode='screenshot')
        self.assertEqual(review['status'], 'judged')
        self.assertEqual(review['segments'], [dict(start=0, facecam_rect=None)])
        self.assertEqual(review['sample_source_time'], .5)

    def test_transient_runtime_failure_is_distinct_from_a_verified_single_view(self):
        with patch('clipmatch.screenshot_layout._read_frame', side_effect=ValueError('decode failed')):
            review = review_layout(Path('source.mp4'), 0, 4, self.cameras, 320, 192, '', mode='screenshot')
        self.assertEqual(review['status'], 'unavailable')
        self.assertEqual(review['segments'], [dict(start=0, facecam_rect=None)])
