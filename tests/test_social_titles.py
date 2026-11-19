import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from clipmatch import social_titles


class SocialTitleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.url = "https://clips.twitch.tv/Box"

    def prepare(self, slug, candidates):
        master = self.root / "jynxzi" / slug / "renders" / "clip.mp4"
        master.parent.mkdir(parents=True, exist_ok=True)
        master.touch()
        data = dict(label="jynxzi", source=dict(twitch_url=self.url + slug), output=dict(duration=18))
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

    def test_unavailable_evidence_or_generic_title_preserves_delivery(self):
        for slug, candidates in [("empty", []), ("generic", [dict(title="Jynxzi Plays GTA 6", description="Gameplay.")])]:
            content, _, _ = self.prepare(slug, candidates)
            self.assertIn("Title needs review", content)
            self.assertEqual(content.splitlines()[-1], f"<{self.url}{slug}>")

    def test_service_outage_retains_review_notice_and_source(self):
        master = self.root / "channel" / "job" / "renders" / "sample.mp4"
        master.parent.mkdir(parents=True)
        data = dict(label="jynxzi", source=dict(twitch_url=self.url))
        with patch.object(social_titles, "generate", side_effect=ConnectionRefusedError):
            self.assertEqual(social_titles.post_copy(master, "abc", data, "local-model"),
                             f"Title needs review\n<{self.url}>")
        self.assertEqual(json.loads(master.with_suffix(".social.json").read_text())["status"], "needs_review")
