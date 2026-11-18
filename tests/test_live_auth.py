import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from aiohttp import ClientSession

from clipmatch import live_auth


class LiveAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_matching_state_can_seed_the_worker(self):
        opened = []
        exchange = AsyncMock(return_value="new-refresh")
        with tempfile.TemporaryDirectory() as temp, patch.object(live_auth, "CALLBACK_PORT", 0), \
                patch.object(live_auth.webbrowser, "open", side_effect=opened.append), \
                patch.object(live_auth, "exchange_code", exchange):
            env = {"TWITCH_CLIENT_ID": "app", "TWITCH_CLIENT_SECRET": "secret", "CLIPBOT_STATE_DIR": temp}
            async with asyncio.timeout(5):
                setup = asyncio.create_task(live_auth.authorize(env))
                try:
                    while not opened:
                        await asyncio.sleep(.01)
                    query = parse_qs(urlsplit(opened[0]).query)
                    self.assertEqual(set(query["scope"][0].split()), {"clips:edit", "user:read:chat"})
                    callback = query["redirect_uri"][0].replace("localhost", "127.0.0.1")
                    async with ClientSession() as session:
                        async with session.get(callback, params={"code": "bogus", "state": "wrong"}) as reply:
                            self.assertEqual(reply.status, 400)
                        self.assertFalse(setup.done())
                        async with session.get(callback, params={"code": "valid", "state": query["state"][0]}) as reply:
                            self.assertEqual(reply.status, 200)
                    await setup
                finally:
                    setup.cancel()
                    await asyncio.gather(setup, return_exceptions=True)
            exchange.assert_awaited_once()
            self.assertIn("valid", exchange.await_args.args)
            self.assertEqual((Path(temp) / "refresh-token").read_text(), "new-refresh")
