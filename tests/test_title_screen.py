import shutil
import tempfile
import unittest
from pathlib import Path

from clipmatch.media import run


TOOLS = shutil.which("ffmpeg") and shutil.which("tesseract")


def make_video(directory, size, labels):
    width, height = size
    # Draw at exact source coordinates with the same subtitle renderer the worker ships.
    header = f"""[Script Info]
PlayResX: {width}
PlayResY: {height}
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Game,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,5,0,0,0,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [f"Dialogue: 0,0:00:{start:05.2f},0:00:{end:05.2f},Game,,0,0,0,,"
              rf"{{\pos({x},{y})\fs{font}}}{label}" for start, end, x, y, font, label in labels]
    (directory / "labels.ass").write_text(header + "\n".join(events), encoding="utf-8")
    source = directory / "source.mp4"
    run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
         f"color=c=0x15192d:s={width}x{height}:r=6:d=12", "-vf", "ass=labels.ass",
         "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", str(source)], cwd=directory)
    return source


class TitleScreenTests(unittest.TestCase):
    def test_category_alone_yields_no_result(self):
        from clipmatch.title_screen import screen_outcome
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "missing.mp4"
            data = {"source": {"category": "Grand Theft Auto VI"}, "output": {"duration": 12}}
            self.assertIsNone(screen_outcome(missing, data))

    @unittest.skipUnless(TOOLS, "Requires FFmpeg and English Tesseract")
    def test_only_a_repeated_result_in_the_source_edit_supplies_a_timestamp(self):
        from clipmatch.title_screen import screen_outcome
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            make_video(directory, (640, 360), [
                (0, 2, 320, 178, 44, "GAME OVER"),
                (5, 8, 320, 178, 44, "YOU DIED"),
                (10, 12, 320, 178, 44, "GAME OVER"),
                (0, 12, 85, 315, 24, "VICTORY"),
            ])
            data = {"source": {"url_or_path": "source.mp4", "start": 2, "end": 10},
                    "output": {"duration": 8}}
            result = screen_outcome(directory / "renders" / "edit.mp4", data)
        self.assertIsNotNone(result)
        title, seconds = result
        self.assertEqual(title, "you died")
        self.assertTrue(3 <= seconds < 6, seconds)

    @unittest.skipUnless(TOOLS, "Requires FFmpeg and English Tesseract")
    def test_stacked_master_without_original_reads_the_game_panel(self):
        from clipmatch.title_screen import screen_outcome
        with tempfile.TemporaryDirectory() as root:
            master = make_video(Path(root), (360, 640), [
                (3, 9, 180, 124, 40, "YOU DIED"),
                (3, 9, 219, 442, 35, "VICTORY ROYALE"),
            ])
            data = {"source": {"url_or_path": "lost.mp4", "start": 42, "end": 54},
                    "output": {"duration": 12},
                    "layout_segments": [{"start": 0, "facecam_rect": [0, 0, 120, 80]}]}
            result = screen_outcome(master, data)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "victory royale")
        self.assertTrue(3 <= result[1] < 9)

    @unittest.skipUnless(TOOLS, "Requires FFmpeg and English Tesseract")
    def test_source_with_a_full_height_camera_uses_the_adjacent_game(self):
        from clipmatch.title_screen import screen_outcome
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            make_video(directory, (640, 360), [
                (2, 8, 144, 180, 42, "VICTORY"), (2, 8, 464, 180, 40, "DEFEAT"),
            ])
            data = {"source": {"url_or_path": "source.mp4", "start": 0, "end": 12},
                    "layout_segments": [{"start": 0, "facecam_rect": [0, 0, 288, 360]}]}
            result = screen_outcome(directory / "renders" / "edit.mp4", data)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "defeat")

    @unittest.skipUnless(TOOLS, "Requires FFmpeg and English Tesseract")
    def test_persistent_banner_chat_promo_and_single_flash_are_not_results(self):
        from clipmatch.title_screen import screen_outcome
        cases = [
            [(0, 12, 320, 180, 48, "VICTORY")],
            [(2, 9, 320, 180, 28, "USE CODE VICTORY"), (2, 9, 82, 108, 18, "GAME OVER")],
            [(4.9, 5.6, 320, 180, 42, "DEFEAT")],
        ]
        for labels in cases:
            with self.subTest(labels=labels), tempfile.TemporaryDirectory() as root:
                master = make_video(Path(root), (640, 360), labels)
                data = {"source": {"category": "Grand Theft Auto VI"}, "output": {"duration": 12}}
                self.assertIsNone(screen_outcome(master, data))
