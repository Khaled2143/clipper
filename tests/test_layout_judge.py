import unittest
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from clipmatch import layout_judge


class LayoutJudgeTests(unittest.TestCase):
    cameras = [[8, 48, 68, 50], [248, 50, 72, 48]]

    @unittest.skipUnless(shutil.which("ffmpeg"), "Requires FFmpeg")
    def test_detects_a_brief_camera_toggle_even_when_the_game_stays_still(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "small-cam.mp4"
            layout_judge.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=gray:s=320x180:r=30:d=5",
                              "-vf", "drawbox=x=240:y=132:w=64:h=40:c=white:t=fill:enable='between(t,3.4,4.19)'",
                              "-c:v", "libx264", str(source)])
            starts = layout_judge._shot_starts(source, 0, 5, [[240, 132, 64, 40]])
        self.assertTrue(any(abs(start - 3.4) < .04 for start in starts), starts)
        self.assertTrue(any(abs(start - 4.2) < .04 for start in starts), starts)

    def review(self, answers):
        with patch.object(layout_judge, "_shot_starts", return_value=[0.0, 2.0, 3.0]), \
             patch.object(layout_judge, "_preview", return_value="images"), \
             patch.object(layout_judge, "ask_model", side_effect=answers):
            return layout_judge.review_layout(Path("source.mp4"), 45, 50, self.cameras, 320, 100, "vision")

    def test_moved_camera_and_one_second_full_camera_cutaway(self):
        right = {"camera_box": "blue", "evidence": "The quiet streamer is in the bottom right."}
        source = {"webcam": "bottom_right"}
        decision = self.review([source, right, {"webcam": "fullscreen"}, source, right])
        self.assertEqual(decision["segments"], [
            {"start": 0, "facecam_rect": self.cameras[1]}, {"start": 2, "facecam_rect": None},
            {"start": 3, "facecam_rect": self.cameras[1]}])

    def test_only_a_distinct_verified_camera_can_split(self):
        decision = self.review([{"webcam": "none"}, {"webcam": "bottom_left"},
                                {"camera_box": "blue", "evidence": "Possibly the player."},
                                {"webcam": "bottom_right"}, {"camera_box": "surprise", "evidence": "Unknown position."}])
        self.assertEqual(decision["segments"], [{"start": 0, "facecam_rect": None}])

    def test_outage_and_missing_model_deliver_one_complete_view(self):
        for decision in (self.review([ConnectionRefusedError()]),
                         layout_judge.review_layout(Path("source.mp4"), 4, 6, self.cameras, 320, 100, "")):
            self.assertEqual(decision["segments"], [{"start": 0, "facecam_rect": None}])
            self.assertNotEqual(decision["status"], "judged")

    def test_outage_later_in_clip_retains_the_already_verified_camera(self):
        review = self.review([{"webcam": "bottom_right"}, {"camera_box": "blue", "evidence": "Overlay is visible."},
                              ConnectionRefusedError()])
        self.assertEqual(review["segments"], [{"start": 0, "facecam_rect": self.cameras[1]},
                                              {"start": 2, "facecam_rect": None}])

    def test_fast_game_cut_with_continuous_camera_keeps_the_same_layout(self):
        answers = [{"webcam": "bottom_right"}, {"camera_box": "blue", "evidence": "Face is visible."}]
        with patch.object(layout_judge, "_shot_starts", return_value=[0, 1.0, 1.034]), \
             patch.object(layout_judge, "_preview", return_value="frames"), \
             patch.object(layout_judge, "ask_model", side_effect=answers * 3):
            review = layout_judge.review_layout(Path("source.mp4"), 0, 2, self.cameras, 320, 100, "vision")
        self.assertEqual(review["segments"], [{"start": 0, "facecam_rect": self.cameras[1]}])

    def test_many_game_cuts_do_not_disable_a_continuous_webcam(self):
        answers = [{"webcam": "bottom_right"}, {"camera_box": "blue", "evidence": "Face is visible."}]
        with patch.object(layout_judge, "_shot_starts", return_value=list(range(21))), \
             patch.object(layout_judge, "_preview", return_value="frames"), \
             patch.object(layout_judge, "ask_model", side_effect=answers * 21):
            review = layout_judge.review_layout(Path("source.mp4"), 0, 21, self.cameras, 320, 100, "vision")
        self.assertEqual(review["segments"], [{"start": 0, "facecam_rect": self.cameras[1]}])
