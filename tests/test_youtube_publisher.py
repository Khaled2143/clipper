import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from clipmatch import youtube_publisher as youtube


CHANNEL = "UCqd66XANzi22AxRvjeoOz8A"
VIDEO = "testVideo01"


class YouTubeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.master = Path(self.directory.name) / "clip.mp4"
        self.master.write_bytes(b"sample video")
        self.grant = dict(client_id="client", client_secret="secret", refresh_token="refresh", channel_id=CHANNEL)
        self.channel = CHANNEL
        self.visibility = "public"
        self.processing = "processing"
        self.media = []
        self.sessions = []
        self.saved = []
        self.probe_status = 308
        self.received = 0
        self.rate = False
        self.prior_upload = False
        self.probe_delay = False
        self.transfer_status = 201
        self.exchange = {}
        self.scope = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
        app = web.Application()
        app.router.add_post("/token", self.token)
        app.router.add_get("/channels", self.channels)
        app.router.add_get("/videos", self.videos)
        app.router.add_post("/upload", self.start_upload)
        app.router.add_put("/upload", self.transfer)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        base = str(self.server.make_url("")).rstrip("/")
        self.enterContext(patch.multiple(youtube, TOKEN_URL=base+"/token", API_URL=base, UPLOAD_URL=base+"/upload"))
        self.upload_url = base + "/upload?upload_id=opaque"

    async def token(self, request):
        self.exchange = dict(await request.post())
        return web.json_response(dict(access_token="access", refresh_token="refresh", scope=self.scope))

    async def channels(self, request):
        self.assertEqual(request.query["mine"], "true")
        self.assertEqual(request.headers["Authorization"], "Bearer access")
        return web.json_response(dict(items=[dict(id=self.channel)]))

    async def videos(self, request):
        self.assertEqual(request.query["id"], VIDEO)
        return web.json_response(dict(items=[dict(id=VIDEO, snippet=dict(channelId=CHANNEL),
            status=dict(privacyStatus=self.visibility, uploadStatus="processed" if self.processing == "succeeded" else "uploaded"),
            processingDetails=dict(processingStatus=self.processing))]))

    async def start_upload(self, request):
        self.assertEqual(request.query["uploadType"], "resumable")
        self.sessions.append(await request.json())
        if self.rate:
            return web.json_response(dict(error=dict(errors=[dict(reason="quotaExceeded")])), status=403,
                                     headers={"Retry-After": "7200"})
        return web.Response(headers={"Location": self.upload_url})

    async def transfer(self, request):
        if request.headers["Content-Range"].startswith("bytes */"):
            if self.prior_upload:
                return web.json_response(dict(id=VIDEO), status=201)
            fields = {"Range": f"bytes=0-{self.received - 1}"} if self.received else {}
            if self.probe_delay:
                fields["Retry-After"] = "120"
            return web.Response(status=self.probe_status, headers=fields)
        self.assertTrue(self.saved[-1]["final_attempted"])
        self.assertEqual(self.saved[-1]["session_url"], self.upload_url)
        self.media.append((request.headers["Content-Range"], await request.read()))
        if self.transfer_status != 201:
            return web.Response(status=self.transfer_status, headers={"Retry-After": "90"})
        return web.json_response(dict(id=VIDEO), status=201)

    async def step(self, state=None):
        return await youtube.youtube_step(self.session, self.grant, self.master, "A precise title",
                                          "A short description", state or {}, self.saved.append)

    async def test_resume_from_acknowledged_byte_and_confirm_public_channel(self):
        state = await self.step()
        self.assertEqual(state["phase"], "pending")
        self.assertEqual(state["session_url"], self.upload_url)
        self.assertEqual(self.sessions[0]["status"]["privacyStatus"], "public")
        self.assertEqual(self.sessions[0]["snippet"]["title"], "A precise title")
        self.received = 4
        state = await self.step(state)
        self.assertEqual(self.media, [("bytes 4-11/12", b"le video")])
        self.assertEqual(state["remote_id"], VIDEO)
        state = await self.step(state)
        self.assertEqual(state["phase"], "pending")
        self.processing = "succeeded"
        state = await self.step(state)
        self.assertEqual(state["phase"], "published")
        self.assertEqual(state["url"], "https://www.youtube.com/shorts/" + VIDEO)

    async def test_wrong_identity_stops_session_creation_and_transfer(self):
        self.channel = "different"
        self.assertEqual((await self.step())["phase"], "review")
        state = dict(session_url=self.upload_url, source_size=12)
        self.assertEqual((await self.step(state))["phase"], "review")
        self.assertFalse(self.sessions or self.media)

    async def test_lost_final_response_recovers_video_or_holds_expired_session(self):
        state = dict(session_url=self.upload_url, source_size=12, final_attempted=True)
        self.prior_upload = True
        resumed = await self.step(state)
        self.assertEqual(resumed["remote_id"], VIDEO)
        self.prior_upload = False
        self.probe_status = 404
        held = await self.step(state)
        self.assertEqual((held["phase"], held["session_url"]), ("review", self.upload_url))
        self.assertFalse(self.sessions or self.media)

    async def test_expired_unused_session_can_restart_and_daily_quota_waits(self):
        state = dict(session_url=self.upload_url, source_size=12, final_attempted=False)
        self.probe_status = 404
        state = await self.step(state)
        self.assertEqual(state["phase"], "pending")
        self.assertFalse(state.get("session_url"))
        self.rate = True
        state = await self.step(state)
        self.assertEqual(state["phase"], "pending")
        self.assertGreaterEqual(state["delay"], 7200)
        self.assertFalse(self.media)

    async def test_completed_private_video_is_never_reported_published(self):
        self.visibility, self.processing = "private", "succeeded"
        result = await self.step(dict(remote_id=VIDEO))
        self.assertEqual((result["phase"], result["remote_id"]), ("review", VIDEO))
        self.assertFalse(self.sessions)

    async def test_retry_after_incomplete_status_resumes_after_wait_without_waiting_forever(self):
        self.received, self.probe_delay = 4, True
        state = await self.step()
        state = await self.step(state)
        self.assertEqual((state["phase"], state["delay"]), ("pending", 120))
        self.assertFalse(self.media)
        state = await self.step(state)
        self.assertEqual(state.get("remote_id"), VIDEO)
        self.assertEqual(self.media, [("bytes 4-11/12", b"le video")])

    async def test_throttled_final_put_retains_checkpoint_and_probes_before_resending(self):
        self.transfer_status = 429
        state = await self.step()
        state = await self.step(state)
        self.assertEqual((state["phase"], state["delay"], state["session_url"]), ("pending", 90, self.upload_url))
        self.assertTrue(state["final_attempted"])
        self.prior_upload = True
        result = await self.step(state)
        self.assertEqual(result["remote_id"], VIDEO)
        self.assertEqual(len(self.media), 1)

    async def test_stale_processing_job_eventually_exposes_manual_review_and_retains_video(self):
        result = await self.step(dict(remote_id=VIDEO, confirm_since=1))
        self.assertEqual((result["phase"], result["remote_id"]), ("review", VIDEO))

    async def test_session_outside_upload_endpoint_never_receives_credentials_or_media(self):
        result = await self.step(dict(session_url="https://invalid.example/file", source_size=12))
        self.assertEqual(result["phase"], "review")
        self.assertFalse(self.media)

    async def test_desktop_pkce_grant_is_saved_only_for_expected_channel(self):
        source = Path(self.directory.name) / "client.json"
        source.write_text(json.dumps(dict(installed=dict(client_id="client", client_secret="secret"))))
        destination = Path(self.directory.name) / "grant.json"
        def browser(url):
            fields = parse_qs(urlsplit(url).query)
            callback = fields["redirect_uri"][0]
            self.assertEqual(urlsplit(callback).hostname, "127.0.0.1")
            self.assertEqual(set(fields["scope"][0].split()), set(self.scope.split()))
            self.challenge = fields["code_challenge"][0]
            with urlopen(callback + "?" + urlencode(dict(code="code", state=fields["state"][0])), timeout=4) as response:
                self.assertEqual(response.status, 200)
            return True
        with patch.object(youtube.webbrowser, "open", side_effect=browser):
            self.channel = "different"
            with self.assertRaises(ValueError):
                await youtube.authorize(source, destination)
            self.assertFalse(destination.exists())
            self.channel = CHANNEL
            await youtube.authorize(source, destination)
        self.assertEqual(json.loads(destination.read_text()), self.grant)
        verifier = self.exchange["code_verifier"]
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(digest, self.challenge)
