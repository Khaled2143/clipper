import unittest

from clipmatch import local_copy


class TitleEvidenceTests(unittest.TestCase):
    def test_a_pickup_instruction_keeps_the_item_and_its_location_in_their_original_roles(self):
        data = {"source": {"twitch_url": "https://clips.twitch.tv/A", "category": "Golf",
                           "twitch_title": "Just a stream"}, "output": {}}
        from pathlib import Path
        choices = list(local_copy.local_candidates(Path("clip.mp4"), data, "Fanum",
                       ["Collect rewards from tournaments from your locker", "See who's at the door"]))
        titles = [item["title"] for method, item in choices if method == "speech"]
        self.assertIn("Fanum can collect rewards from tournaments at the locker", titles)
        self.assertIn("Fanum, who's at the door?", titles)

    def test_only_supported_candidates_can_link_a_spoken_setup_and_screen_result(self):
        from clipmatch.title_editor import supported_draft

        speech = ((0.5, 3.5, "See you in Valhalla"),)
        result = ("wasted", 8.5)
        args = ("Jynxzi", "GTA 6", speech, result)
        self.assertTrue(supported_draft("From Valhalla to the Wasted screen on Jynxzi’s stream", *args))
        for invented in ("Jynxzi crashes a stolen plane into Valhalla",
                         "Jynxzi loses everything in the Wasted screen",
                         "Jynxzi screams Valhalla before the Wasted screen",
                         "Jynxzi gets 50 kills before the Valhalla Wasted screen"):
            with self.subTest(invented=invented):
                self.assertFalse(supported_draft(invented, *args))

    def test_metadata_or_streamer_name_alone_cannot_validate_an_editor_title(self):
        from clipmatch.title_editor import supported_draft

        speech = ((2, 4, "The spare putter is in the mailbox"),)
        for title in ("Jynxzi gets the win everyone wanted in GTA 6",
                      "The spare putter is in the mailbox (Jynxzi)",
                      "Jynxzi finds the spare putter at https://example.com",
                      "Jynxzi sees an insane moment in GTA 6"):
            with self.subTest(title=title):
                self.assertFalse(supported_draft(title, "Jynxzi", "GTA 6", speech, None))

    def test_a_quiet_scene_can_ground_a_title_with_a_visible_action(self):
        from clipmatch.title_editor import supported_draft

        observed = "In the game a loose basketball rolls down a driveway."
        self.assertTrue(supported_draft("The basketball rolls down the driveway on YourRAGE’s stream",
                                        "YourRAGE", "GTA 6", (), None, observed))
        self.assertFalse(supported_draft("YourRAGE wins the whole tournament in GTA 6",
                                         "YourRAGE", "GTA 6", (), None, observed))

    def test_only_a_spoken_setup_before_a_nearby_result_can_be_a_timing_joke(self):
        from clipmatch.title_editor import payoff_setup

        for line, end in (("There was no car crash", 22), ("What if there is a car crash?", 22),
                          ("There wasn't a car crash", 22), ("See you in Valhalla", 10),
                          ("See you in Valhalla", 40)):
            self.assertIsNone(payoff_setup([(1, end, line)], ("wasted", 25)))

    def test_a_question_cannot_supply_an_unseen_action_or_location_to_the_picture(self):
        from clipmatch.title_editor import supported_draft

        beats = ((1, 3, "Did the bicycle fall down those stairs?"),)
        observed = "A man wearing a purple shirt sits in front of a game menu."
        self.assertFalse(supported_draft("Purple shirt man on stairs on YourRAGE’s stream", "YourRAGE",
                                         "GTA 6", beats, None, observed))
        self.assertTrue(supported_draft("Did the bicycle fall down the stairs on YourRAGE’s stream?",
                                        "YourRAGE", "GTA 6", beats, None, ""))

    def test_a_verified_payoff_can_land_without_naming_the_screen(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from types import SimpleNamespace
        import sys
        import tempfile
        from clipmatch import title_editor

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, llama_cpp=SimpleNamespace(Llama=MagicMock())), \
                patch.object(title_editor, "_answer", side_effect=["Jynxzi made it to Valhalla real quick", "YES"]):
            model = Path(temp) / "model.gguf"
            model.touch()
            titles = title_editor.write_titles(model, "Jynxzi", "GTA 6",
                [(1, 3, "I'll see you in Valhalla")], ("wasted", 8), [])
        self.assertEqual(titles, ["Jynxzi made it to Valhalla real quick"])

    def test_an_unverified_joke_yields_to_the_spoken_detail(self):
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        from types import SimpleNamespace
        import sys
        import tempfile
        from clipmatch import title_editor

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, llama_cpp=SimpleNamespace(Llama=MagicMock())), \
                patch.object(title_editor, "_answer", side_effect=[
                    "Kai stole the spare key from a cop", "NO", "There goes Kai’s spare key", "YES"]):
            model = Path(temp) / "model.gguf"
            model.touch()
            titles = title_editor.write_titles(model, "Kai", "GTA 6",
                [(1, 3, "There goes the spare key")], ("busted", 8), [])
        self.assertEqual(titles, ["There goes Kai’s spare key"])

    def test_four_frames_cannot_become_four_players_or_a_wardrobe_headline(self):
        from clipmatch.title_editor import supported_draft

        mistaken = "The four views show four players in blue headsets."
        for title in ("YourRAGE has four players on screen", "Blue headsets take over YourRAGE’s stream"):
            self.assertFalse(supported_draft(title, "YourRAGE", "GTA 6", (), None, mistaken))

    def test_confident_spoken_beats_keep_their_order_and_times_in_the_selected_edit(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from pathlib import Path
        import sys
        import tempfile

        def segment(first, line, score=.97):
            words = [SimpleNamespace(word=" " + word, start=first + i/3, end=first + (i+1)/3,
                                     probability=score) for i, word in enumerate(line.split())]
            return SimpleNamespace(text=line, no_speech_prob=.03, avg_logprob=-.2, words=words)

        class Whisper:
            def __init__(self, *args, **options):
                pass

            def transcribe(self, *args, **options):
                return iter([segment(1, "Save the spare key."),
                             segment(3, "The submarine explodes.", .24),
                             segment(5, "The locked gate is open.")]), None

        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, faster_whisper=SimpleNamespace(WhisperModel=Whisper)):
            master = Path(temp) / "clip.mp4"
            master.write_bytes(b"selected cut")
            _, _, beats = local_copy.speech_evidence(master, "model")
        self.assertEqual([b[2] for b in beats], ["Save the spare key", "The locked gate is open"])
        self.assertEqual([b[0] for b in beats], [1, 5])
        self.assertLess(beats[0][1], beats[1][0])
