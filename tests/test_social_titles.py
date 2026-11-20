import base64
from contextlib import closing
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from clipmatch import social_titles


class SocialTitleTests(unittest.TestCase):
    def test_future_model_drafts_receive_reviewed_examples_as_style_only(self):
        from clipmatch.title_feedback import Feedback

        finished = self.root / "finished"
        master = finished / "jynxzi" / "new" / "renders" / "clip.mp4"
        master.parent.mkdir(parents=True)
        feedback = Feedback(finished)
        feedback.save(feedback.register("jynxzi", self.url + "old", "old", "Jynxzi chases a ball"),
                      "down", "Jynxzi goes back for his own rebound")
        feedback.save(feedback.register("jynxzi", self.url + "new", "new", "An older draft for this clip"),
                      "up", "")
        draft = dict(title="Jynxzi finds a wrench in the drawer", description="He opens the drawer.")
        examples = [{"original": "Jynxzi chases a ball", "vote": "down",
                     "preferred": "Jynxzi goes back for his own rebound"}]

        def inference(model, frames, context, prompt, schema):
            if "candidates" in schema["properties"]:
                self.assertEqual(context["title_preferences"], examples)
                return dict(candidates=[draft])
            self.assertNotIn("title_preferences", context)
            if "reviews" in schema["properties"]:
                return dict(reviews=[dict(decision="publish")])
            return {key: "He opens a drawer." for key in schema["properties"]}

        data = dict(label="jynxzi", source=dict(twitch_url=self.url + "new"))
        with patch.object(social_titles, "selected_sheet", return_value="pixels"), \
                patch.object(social_titles, "transcript", return_value=""), \
                patch.object(social_titles, "ask_model", side_effect=inference):
            content = social_titles.post_copy(master, "new", data, "model")
        self.assertEqual(content.splitlines()[0], draft["title"])

    def test_finished_clip_review_link_and_feedback_reach_the_local_editor(self):
        from clipmatch.title_feedback import Feedback

        finished = self.root / "finished"
        master = finished / "jynxzi" / "new" / "renders" / "clip.mp4"
        master.parent.mkdir(parents=True)
        model = self.root / "model.gguf"
        model.touch()
        feedback = Feedback(finished)
        feedback.save(feedback.register("agent00", self.url + "old", "old", "Agent00 opens the box"),
                      "up", "Agent00 finds the spare key")
        prompts = []
        draft = "Jynxzi keeps a spare putter in the mailbox"

        class Model:
            def __init__(self, **options):
                pass

            def __call__(self, prompt, **options):
                prompts.append(prompt)
                return {"choices": [{"text": draft if len(prompts) == 1 else "YES"}]}

            def close(self):
                pass

        master.with_suffix(".json").write_text(json.dumps(dict(
            label="jynxzi", source=dict(twitch_url=self.url + "new"))))
        beats = ((1, 2, "The spare putter is in the mailbox"),)
        with patch.dict(os.environ, PUBLIC_POSTING_ENABLED="1", PUBLISH_MEDIA_ORIGIN="https://clips.example",
                        SOCIAL_VISION_MODEL="", SOCIAL_TITLE_MODEL=str(model)), \
                patch.dict(sys.modules, llama_cpp=SimpleNamespace(Llama=Model)), \
                patch.object(social_titles, "speech_evidence", return_value=("", (), beats)), \
                patch("clipmatch.title_screen.screen_outcome", return_value=None):
            content = social_titles.social_content(master, "new", include_review=True)
            self.assertEqual(social_titles.social_content(master, "new", include_review=True), content)
        self.assertEqual(content.splitlines()[0], draft)
        self.assertRegex(content.splitlines()[-1], r"^\[Review title\]\(https://clips\.example/title-review/[\w-]{43}\)$")
        self.assertEqual(len(prompts), 2)
        self.assertIn("Agent00 finds the spare key", prompts[0])
        self.assertNotIn("Agent00", prompts[1])

    def test_titles_strip_whole_emoji_sequences_before_selection_and_deduplication(self):
        clean = "Jynxzi’s GTA 6 café getaway?"
        dirty = "Jynxzi’s 👨🏾‍🚀 GTA 6 café🇺🇸getaway1️⃣⁉️?"
        item = {"title": dirty, "description": "The car pulls away."}
        self.assertEqual(social_titles.choose([item], [], "jynxzi"), (clean, item["description"]))
        self.assertIsNone(social_titles.choose([item], [clean], "jynxzi"))
        self.assertIsNone(social_titles.choose([dict(item, title="❤️‍🔥")], [], "jynxzi"))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.url = "https://clips.twitch.tv/Box"

    def test_title_request_reaches_configured_ollama_service(self):
        requests = []

        class Ollama(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.path, json.loads(body)["model"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"message":{"content":"{}"}}')

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Ollama) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                address = f"http://127.0.0.1:{server.server_port}/"
                with patch.dict(os.environ, SOCIAL_OLLAMA_URL=address):
                    social_titles.ask_model("fixture-model", "", {}, "test", {})
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual(requests, [("/api/chat", "fixture-model")])

    def prepare(self, slug, candidates, channel="jynxzi"):
        master = self.root / channel / slug / "renders" / "clip.mp4"
        master.parent.mkdir(parents=True, exist_ok=True)
        master.touch()
        data = dict(label=channel, source=dict(twitch_url=self.url + slug), output=dict(duration=18))
        with patch.object(social_titles, "generate", return_value=candidates) as generate:
            content = social_titles.post_copy(master, slug, data, "local-model")
        return content, generate.call_count, master

    def test_similar_title_uses_another_detail_and_retries_reuse_saved_copy(self):
        first = dict(title="Jynxzi Opens the Moving Box", description="He lifts the cardboard flap.")
        alternate = dict(title="Jynxzi Finds a Skateboard Inside the Box", description="A skateboard comes into view.")
        self.prepare("one", [first])
        content, _, master = self.prepare("two", [dict(first, title=first["title"] + "!!"), alternate])
        self.assertEqual(content.splitlines(), [alternate["title"], alternate["description"], f"<{self.url}two>"])
        self.assertEqual(json.loads(master.with_suffix(".social.json").read_text())["title"], alternate["title"])
        self.assertEqual(self.prepare("two", [])[0:2], (content, 0))

    def test_unavailable_evidence_or_generic_vision_title_preserves_public_copy(self):
        for slug, candidates in [("empty", []), ("generic", [dict(title="Jynxzi Plays GTA 6", description="Gameplay.")])]:
            content, _, _ = self.prepare(slug, candidates)
            self.assertIn("Jynxzi", content.splitlines()[0])
            self.assertNotIn("needs review", content.lower())
            self.assertEqual(content.splitlines()[-1], f"<{self.url}{slug}>")

    def test_template_drafts_yield_to_a_concrete_alternative(self):
        concrete = "The getaway car stalls in YourRAGE’s driveway"
        for placeholder in ("YourRAGE | In-game event", "A crazy in-game moment (YourRAGE)", "YourRAGE reacts to a wild moment",
                            "YourRAGE | [Describe the specific action]"):
            with self.subTest(placeholder=placeholder):
                drafts = [dict(title=title, description="The car stops halfway down the driveway.")
                          for title in (placeholder, concrete)]
                self.assertEqual(social_titles.choose(drafts, [], "yourragegaming")[0], concrete)

    def test_stored_placeholder_is_regenerated_once(self):
        _, _, master = self.prepare("old", [], "yourragegaming")
        with closing(sqlite3.connect(master.parents[2] / "social-titles.sqlite3")) as db, db:
            db.execute("INSERT OR REPLACE INTO titles (url,digest,title,description) VALUES (?, ?, ?, ?)",
                       (self.url + "old", "old", "YourRAGE | In-game event", "The car stalls."))
        good = dict(title="The getaway car stalls in YourRAGE’s driveway", description="The car stalls.")
        content, calls, _ = self.prepare("old", [good], "yourragegaming")
        self.assertEqual((content.splitlines()[0], calls), (good["title"], 1))
        self.assertEqual(self.prepare("old", [], "yourragegaming")[:2], (content, 0))

    def test_long_scene_description_yields_to_a_short_hook(self):
        description = "The ball rolls back toward the player."
        alt_text = "Man in white tank top releases basketball toward hoop on green stilt house (YourRAGE)"
        short = "The rebound is a longer run than the shot (YourRAGE)"
        drafts = [dict(title=title, description=description) for title in (alt_text, short)]
        self.assertEqual(social_titles.choose(drafts, [], "yourragegaming"), (short, description))

    def test_fact_checker_can_ground_a_spoken_topic_and_game_name(self):
        speech = "How much would the hotel charge if we stayed here?"
        draft = dict(title="What would a hotel room cost in GTA 6?", description="The conversation turns to room rates.")
        data = dict(label="jynxzi", source=dict(category="Grand Theft Auto VI"), output=dict(duration=5))

        def inference(model, frames, context, prompt, schema):
            if "candidates" in schema["properties"]:
                return dict(candidates=[draft])
            if "reviews" in schema["properties"]:
                grounded = context.get("transcript") == speech and context.get("category") == "Grand Theft Auto VI"
                return dict(reviews=[dict(decision="publish" if grounded else "revise", reason="Speaking about rates",
                                          confirmed_action="The conversation turns to room rates.")])
            return {key: "A hotel room" for key in schema["properties"]}

        with patch.object(social_titles, "selected_sheet", return_value="pixels"), \
                patch.object(social_titles, "transcript", return_value=speech), \
                patch.object(social_titles, "ask_model", side_effect=inference):
            approved = social_titles.generate(self.root / "master.mp4", data, "local", [])
        self.assertEqual(approved, [dict(draft, title=draft["title"] + " (Jynxzi)")])

    def test_unverified_payoff_is_dropped_before_publication(self):
        drafts = [dict(title="The shot banks off the backboard", description="The shot goes in."),
                  dict(title="The stilt house doubles as a basketball court", description="A player lines up a jumper.")]
        observations = dict(setup="A basketball player raises the ball.", payoff="")
        reviews = [dict(reason="The ball going in is not shown.", decision="revise", confirmed_action="Takes a shot"),
                   dict(reason="The hoop is visible.", decision="publish", confirmed_action="Takes a shot")]
        data = dict(label="yourragegaming", source={}, output=dict(duration=5))
        with patch.object(social_titles, "selected_sheet", return_value="pixels"), \
                patch.object(social_titles, "transcript", return_value=""), \
                patch.object(social_titles, "ask_model",
                             side_effect=[observations, dict(candidates=drafts), dict(reviews=reviews)]):
            expected = dict(title="The stilt house doubles as a basketball court (YourRAGE)",
                            description="A player lines up a jumper.")
            self.assertEqual(social_titles.generate(self.root / "master.mp4", data, "local", []), [expected])

    def test_a_rejected_draft_can_be_corrected_and_checked_again(self):
        original = dict(title="Jynxzi wins the match on the rebound", description="The shot wins the match.")
        revised = dict(title="Jynxzi goes back for his own rebound", description="He collects the loose ball.")
        rejected = dict(decision="revise", reason="The score and result aren't visible.", confirmed_action="Retrieves ball")
        accepted = dict(decision="publish", reason="Ball retrieval is visible.", confirmed_action="Retrieves ball")
        replies = [dict(beginning="A shot.", middle="The ball rolls away.", ending="The player retrieves it."),
                   dict(candidates=[original]), dict(reviews=[rejected]), dict(candidates=[revised]),
                   dict(reviews=[accepted])]
        with patch.object(social_titles, "selected_sheet", return_value="pixels"), \
                patch.object(social_titles, "transcript", return_value=""), \
                patch.object(social_titles, "ask_model", side_effect=replies):
            drafts = social_titles.generate(self.root / "master.mp4", dict(label="jynxzi", source={}), "local", [])
        self.assertEqual(drafts, [revised])

    @unittest.skipUnless(shutil.which("ffmpeg"), "Requires FFmpeg for contact-sheet pixels")
    def test_source_window_and_master_fallback_keep_the_whole_frame(self):
        master = self.root / "edit.mp4"
        source = self.root / "source.mp4"
        # Source is black outside the edit. Inside it, the upper and lower halves differ.
        for path, video in [
            (source, "color=black:s=160x90:r=12:d=4,"
                     "drawbox=y=0:w=iw:h=ih/2:c=red:t=fill:enable='gte(t,1)*lt(t,3)',"
                     "drawbox=y=ih/2:w=iw:h=ih/2:c=blue:t=fill:enable='gte(t,1)*lt(t,3)'"),
            (master, "color=yellow:s=90x160:r=12:d=2,drawbox=y=ih/2:w=iw:h=ih/2:c=magenta:t=fill"),
        ]:
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", video, str(path)], check=True)
        corrupt = self.root / "unreadable.mp4"
        corrupt.write_bytes(b"incomplete")
        for provenance, top, bottom in [
            (dict(url_or_path=str(source), start=1, end=3), (1, 0, 0), (0, 0, 1)),
            ({}, (1, 1, 0), (1, 0, 1)),
            (dict(url_or_path=str(corrupt), start=1, end=3), (1, 1, 0), (1, 0, 1)),
        ]:
            with self.subTest(provenance=provenance):
                sheets = set()

                def read_model_request(model, encoded, context, prompt, schema):
                    if encoded:
                        sheets.add(encoded)
                    if "candidates" in schema["properties"]:
                        return dict(candidates=[dict(title="A wheel catches on the pavement",
                                                     description="The front wheel catches.")])
                    return {key: "A wheel catches" for key in schema["properties"]}

                data = dict(label="jynxzi", source=provenance, output=dict(duration=2))
                with patch.object(social_titles, "ask_model", side_effect=read_model_request), \
                        patch.object(social_titles, "transcript", return_value="speech") as speech:
                    social_titles.generate(master, data, "local", [])
                speech.assert_called_once_with(master)
                self.assertTrue(sheets)
                for sheet in sheets:
                    pixels = subprocess.run(
                        ["ffmpeg", "-v", "error", "-i", "-", "-vf", "scale=3:12:flags=neighbor",
                         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                        input=base64.b64decode(sheet), capture_output=True, check=True).stdout
                    colors = [tuple(int(c > 128) for c in pixels[i:i + 3]) for i in range(0, len(pixels), 3)]
                    for offset in (0, 9, 18, 27):
                        self.assertEqual(colors[offset:offset + 3], [top] * 3)
                        self.assertEqual(colors[offset + 6:offset + 9], [bottom] * 3)

    def test_service_outage_retains_public_source_copy(self):
        master = self.root / "channel" / "job" / "renders" / "sample.mp4"
        master.parent.mkdir(parents=True)
        data = dict(label="jynxzi", source=dict(twitch_url=self.url, twitch_title="The upside-down trophy"))
        with patch.object(social_titles, "generate", side_effect=ConnectionRefusedError):
            content = social_titles.post_copy(master, "abc", data, "local-model")
        self.assertEqual(content.splitlines()[0], "The upside-down trophy on Jynxzi’s stream")
        self.assertEqual(content.splitlines()[-1], f"<{self.url}>")
        record = json.loads(master.with_suffix(".social.json").read_text())
        self.assertEqual((record["status"], record["copy_source"]), ("ready", "twitch_title"))

    def test_without_vision_uses_confident_speech_then_reuses_copy_without_transcribing(self):
        master = self.root / "jynxzi" / "job" / "renders" / "edit.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"selected master")
        data = dict(label="jynxzi", output=dict(duration=18), source=dict(twitch_url=self.url, category="Golf"))
        master.with_suffix(".json").write_text(json.dumps(data))
        models = []

        class Whisper:
            def __init__(self, model, **options):
                models.append(options)

            def transcribe(self, path, **options):
                def seg(line, probability):
                    words = [SimpleNamespace(word=" " + part, probability=probability) for part in line.split()]
                    return SimpleNamespace(text=line, words=words, avg_logprob=-0.32, no_speech_prob=.04)
                return iter([seg("Subscribe to my channel.", .98), seg("Find your dream trophy, fuckers.", .99),
                             seg("The dinosaur broke the trophy.", .2),
                             seg("Where did the spare putter go?", .96)]), None

        with patch.dict(os.environ, SOCIAL_VISION_MODEL="", SOCIAL_SPEECH_MODEL="cached-tiny"), \
                patch.dict(sys.modules, faster_whisper=SimpleNamespace(WhisperModel=Whisper)):
            first = social_titles.social_content(master, "abc")
            second = social_titles.social_content(master, "abc")
        self.assertEqual(first, second)
        self.assertEqual(first.splitlines()[0], "Where did the spare putter go on Jynxzi’s stream?")
        self.assertEqual(first.splitlines()[-1], f"<{self.url}>")
        self.assertEqual(len(models), 1)
        self.assertTrue(models[0]["local_files_only"])
        record = json.loads(master.with_suffix(".social.json").read_text())
        self.assertEqual((record["status"], record["copy_source"]), ("ready", "speech"))

    def test_local_fallback_rejects_unrelated_legacy_origin_and_survives_missing_model(self):
        master = self.root / "jynxzi" / "job" / "renders" / "edit.mp4"
        master.parent.mkdir(parents=True)
        master.touch()
        data = dict(label="jynxzi", source=dict(twitch_url=self.url, category="Golf"), output=dict(duration=16))
        master.with_suffix(".json").write_text(json.dumps(data))
        (master.parent.parent / "source.json").write_text(json.dumps(
            dict(url="https://clips.twitch.tv/Other", title="Trophy falls from the roof", channel="jynxzi")))
        with patch.dict(os.environ, SOCIAL_VISION_MODEL=""), patch.dict(sys.modules, faster_whisper=None):
            content = social_titles.social_content(master, "abc")
        self.assertIn("Golf", content.splitlines()[0])
        self.assertIn("Jynxzi", content.splitlines()[0])
        self.assertNotIn("Trophy", content)
        record = json.loads(master.with_suffix(".social.json").read_text())
        self.assertEqual((record["status"], record["copy_source"]), ("ready", "metadata"))

    def test_quiet_clips_never_use_runtime_or_broadcast_promo_to_make_titles_distinct(self):
        history = []
        for slug in ("quiet-one", "quiet-two", "quiet-three"):
            master = self.root / "yourragegaming" / slug / "renders" / "edit.mp4"
            master.parent.mkdir(parents=True)
            data = dict(label="yourragegaming", output=dict(duration=17), source=dict(
                twitch_url=self.url + slug, twitch_title="PLAYING GTA 6 FOR THE FIRST TIME",
                category="Grand Theft Auto VI"))
            text = social_titles.post_copy(master, slug, data, "")
            history.append(text.splitlines()[0])
        for title in history:
            self.assertIn("YourRAGE", title)
            self.assertIn("GTA 6", title)
            self.assertNotRegex(title.lower(), r"second|first time|highlight")

    def test_new_editor_titles_reach_both_platforms_copy_and_retries_reuse_the_record(self):
        master = self.root / "jynxzi" / "test" / "renders" / "clip.mp4"
        master.parent.mkdir(parents=True)
        master.write_bytes(b"edited clip")
        master.with_suffix(".json").write_text(json.dumps(dict(label="jynxzi",
            source=dict(twitch_url=self.url, category="Grand Theft Auto VI"), output=dict(duration=15))))
        title = "A Valhalla goodbye sends Jynxzi straight to the Wasted screen"
        beats = ((0.5, 3.5, "I'll see you in Valhalla"),)
        with patch.dict(os.environ, SOCIAL_VISION_MODEL="", SOCIAL_TITLE_MODEL="local.gguf"), \
                patch.object(social_titles, "speech_evidence", return_value=("", (), beats)), \
                patch("clipmatch.title_screen.screen_outcome", return_value=("wasted", 8.5)), \
                patch("clipmatch.title_editor.write_titles", return_value=[title]) as writer:
            first = social_titles.social_content(master, "digest")
            second = social_titles.social_content(master, "digest")
        self.assertEqual(first.splitlines()[0], title)
        self.assertEqual(first, second)
        self.assertEqual(writer.call_count, 1)
        record = json.loads(master.with_suffix(".social.json").read_text())
        self.assertEqual((record["title"], record["copy_source"]), (title, "editor"))
