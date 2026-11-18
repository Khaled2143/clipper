import asyncio
import json
import unittest
from unittest.mock import patch

from aiohttp import WSMessage, WSMsgType

from clipmatch import live_events


def envelope(kind, payload, message_id="control"):
    return {"metadata": {"message_type": kind, "message_id": message_id}, "payload": payload}


def notification(kind, channel, message_id):
    return envelope("notification", {
        "subscription": {"type": kind},
        "event": {"broadcaster_user_id": channel, "message": {"text": message_id}},
    }, message_id)


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False

    def send(self, packet):
        value = json.dumps(packet) if isinstance(packet, dict) else packet
        self.frames.put_nowait(WSMessage(WSMsgType.TEXT, value, ""))

    def welcome(self, sid, timeout=10):
        self.send(envelope("session_welcome", {
            "session": {"id": sid, "keepalive_timeout_seconds": timeout}
        }))

    async def receive(self, timeout=None):
        return await asyncio.wait_for(self.frames.get(), timeout)

    async def send_str(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True


class Session:
    def __init__(self):
        self.sockets = asyncio.Queue()
        self.urls = []

    async def ws_connect(self, url, **kwargs):
        self.urls.append(url)
        return await self.sockets.get()


class Rejected(RuntimeError):
    def __init__(self, status):
        self.status = status


class Api:
    def __init__(self):
        self.subscriptions = []
        self.rejections = {}
        self.live = {"1"}
        self.lookup = asyncio.Event()
        self.queries = []
        self.release_lookup = asyncio.Event()
        self.release_lookup.set()
        self.irc_calls = 0

    async def subscribe(self, kind, channel, user_id, sid):
        self.subscriptions.append((kind, channel, user_id, sid))
        if status := self.rejections.get((kind, channel)):
            raise Rejected(status)

    async def live_ids(self, ids):
        self.queries.append(set(ids))
        self.lookup.set()
        await self.release_lookup.wait()
        return self.live.intersection(ids)

    async def irc_credentials(self):
        self.irc_calls += 1
        return "watcher", "access"


class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = Api()
        self.session = Session()
        self.old = self.socket()
        self.old.welcome("first")
        self.states = []
        self.chat = []
        self.gaps = []
        self.errors = []

    def socket(self):
        socket = Socket()
        self.session.sockets.put_nowait(socket)
        return socket

    def start(self, channels=None, **callbacks):
        self.task = asyncio.create_task(live_events.listen(
            self.session, self.api, channels or {"1": "one"}, "viewer",
            lambda cid, live: self.states.append((cid, live)),
            lambda cid, event: self.chat.append((cid, event["message"]["text"])),
            lambda: self.gaps.append(True),
            lambda cid, reason: self.errors.append((cid, reason)),
            **callbacks,
        ))
        self.addAsyncCleanup(self.stop)

    async def stop(self):
        self.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.task
        self.assertTrue(self.old.closed)

    async def until(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                if self.task.done():
                    self.task.result()
                await asyncio.sleep(0.001)

    async def test_already_live_offline_and_duplicate_notifications(self):
        self.start({"1": "one", "2": "two"})
        await self.until(lambda: ("1", True) in self.states and ("2", False) in self.states)
        self.assertEqual(set(self.api.subscriptions), {
            (kind, cid, "viewer", "first") for cid in ("1", "2")
            for kind in ("stream.online", "stream.offline", "channel.chat.message")
        })
        for channel, mid in [("2", "ignored"), ("1", "yes"), ("1", "yes")]:
            self.old.send(notification("channel.chat.message", channel, mid))
        self.old.send(notification("stream.offline", "1", "off"))
        self.old.send(notification("channel.chat.message", "1", "late"))
        self.old.send(notification("stream.online", "2", "on"))
        self.old.send(notification("channel.chat.message", "2", "more"))
        await self.until(lambda: ("2", "more") in self.chat)
        self.assertEqual(self.chat, [("1", "yes"), ("2", "more")])

    async def test_live_transition_received_during_snapshot_wins(self):
        self.api.release_lookup.clear()
        self.start()
        await self.until(self.api.lookup.is_set)
        self.old.send(notification("stream.offline", "1", "off"))
        await self.until(lambda: ("1", False) in self.states)
        self.api.release_lookup.set()
        self.old.send(notification("channel.chat.message", "1", "offline"))
        self.old.send(notification("stream.online", "1", "on"))
        self.old.send(notification("channel.chat.message", "1", "online"))
        await self.until(lambda: ("1", "online") in self.chat)
        self.assertEqual(self.chat, [("1", "online")])
        self.assertEqual(self.states.count(("1", True)), 1)

    async def test_handoff_reads_old_until_new_welcome_and_preserves_subscriptions(self):
        self.start()
        await self.until(lambda: ("1", True) in self.states)
        replacement = self.socket()
        url = "wss://eventsub.wss.twitch.tv/ws?reconnect=abcd"
        self.old.send(envelope("session_reconnect", {"session": {"reconnect_url": url}}))
        await self.until(lambda: url in self.session.urls)
        self.assertFalse(self.old.closed)
        msg = notification("channel.chat.message", "1", "old")
        self.old.send(msg)
        await self.until(lambda: self.chat == [("1", "old")])
        replacement.welcome("inherited")
        replacement.send(msg)
        replacement.send(notification("channel.chat.message", "1", "new"))
        await self.until(lambda: ("1", "new") in self.chat)
        self.assertTrue(self.old.closed)
        self.assertEqual(len(self.api.subscriptions), 3)
        self.assertEqual(self.gaps, [])
        self.assertEqual(self.chat, [("1", "old"), ("1", "new")])

    async def test_silent_socket_resets_baselines_and_reconciles_fresh_subscriptions(self):
        self.old = Socket()
        self.session.sockets = asyncio.Queue()
        self.session.sockets.put_nowait(self.old)
        self.old.welcome("first", timeout=0.08)
        self.start()
        await self.until(lambda: ("1", True) in self.states)
        replacement = self.socket()
        self.api.live.clear()
        replacement.welcome("fresh")
        await self.until(lambda: ("1", False) in self.states)
        self.assertEqual(self.gaps, [True])
        self.assertEqual(len([s for s in self.api.subscriptions if s[3] == "fresh"]), 3)

    async def test_old_socket_close_at_cutover_keeps_transferred_subscriptions(self):
        self.start()
        await self.until(lambda: ("1", True) in self.states)
        replacement = self.socket()
        url = "wss://eventsub.wss.twitch.tv/ws?reconnect=close"
        self.old.send(envelope("session_reconnect", {"session": {"reconnect_url": url}}))
        await self.until(lambda: url in self.session.urls)
        self.old.frames.put_nowait(WSMessage(WSMsgType.CLOSE, 1000, ""))
        await self.until(self.old.frames.empty)
        replacement.welcome("inherited")
        replacement.send(notification("channel.chat.message", "1", "new"))
        await self.until(lambda: self.chat == [("1", "new")])
        self.assertEqual(self.gaps, [])
        self.assertEqual(len(self.api.subscriptions), 3)

    async def test_channel_denial_and_revocation_disable_only_affected_channel(self):
        self.api.rejections[("channel.chat.message", "2")] = 403
        self.api.live.add("2")
        self.start({"1": "one", "2": "two"})
        await self.until(lambda: self.errors and ("1", True) in self.states)
        self.old.send(notification("stream.online", "2", "forbidden"))
        self.old.send(notification("channel.chat.message", "2", "forbidden-chat"))
        self.old.send(notification("channel.chat.message", "1", "accepted"))
        self.old.send(envelope("revocation", {"subscription": {
            "type": "channel.chat.message", "status": "authorization_revoked",
            "condition": {"broadcaster_user_id": "1"},
        }}))
        self.old.send(notification("channel.chat.message", "1", "revoked"))
        await self.until(lambda: len(self.errors) == 2 and self.old.frames.empty())
        self.assertEqual({cid for cid, _ in self.errors}, {"1", "2"})
        self.assertEqual(self.chat, [("1", "accepted")])
        self.assertEqual(self.states[-1], ("1", False))
        self.assertEqual(self.api.irc_calls, 0)

    async def test_authenticated_irc_recovers_chat_service_outage_and_answers_ping(self):
        self.api.rejections[("channel.chat.message", "1")] = 503
        irc = self.socket()
        self.start()
        await self.until(lambda: any("NICK watcher" in cmd for cmd in irc.sent))
        self.assertTrue(any("PASS oauth:access" in cmd for cmd in irc.sent))
        irc.send(":tmi.twitch.tv 001 watcher :Welcome\r\n")
        await self.until(lambda: any("JOIN #one" in cmd for cmd in irc.sent))
        irc.send(":watcher!watcher@watcher.tmi.twitch.tv JOIN #one\r\n")
        irc.send("@msg-id=emote_only_on :tmi.twitch.tv NOTICE #one :Mode changed\r\n")
        message = "Pog\u2028:fan!fan@example PRIVMSG #one :another line"
        irc.send("PING :tmi.twitch.tv\r\n"
                 f"@id=42;room-id=1 :fan!fan@fan.tmi.twitch.tv PRIVMSG #one :{message}\r\n"
                 f"@id=42;room-id=1 :fan!fan@fan.tmi.twitch.tv PRIVMSG #one :{message}\r\n")
        await self.until(lambda: self.chat and any("PONG :tmi.twitch.tv" in cmd for cmd in irc.sent))
        self.assertEqual(self.chat, [("1", message)])
        irc.send(":watcher!watcher@watcher.tmi.twitch.tv PART #one\r\n"
                 ":fan!fan@example PRIVMSG #one :after removal\r\n")
        await self.until(lambda: irc.closed)
        self.assertEqual(self.chat, [("1", message)])

    async def test_rate_limit_retries_with_eventsub_and_no_irc(self):
        self.api.rejections[("channel.chat.message", "1")] = 429
        self.start()
        await self.until(lambda: any(s[0] == "channel.chat.message" for s in self.api.subscriptions))
        self.api.rejections.clear()
        await self.until(lambda: ("1", True) in self.states)
        self.assertEqual(self.api.irc_calls, 0)

    async def test_paused_intake_preserves_last_confirmed_stream_state(self):
        pauses = []
        self.start(on_pause=pauses.append)
        await self.until(lambda: self.states == [("1", True)])
        self.old.send(envelope("revocation", {"subscription": {
            "type": "channel.chat.message", "status": "authorization_revoked",
            "condition": {"broadcaster_user_id": "1"},
        }}))
        await self.until(lambda: pauses == ["1"])
        self.assertEqual(self.states, [("1", True)])

    async def test_revoked_access_does_not_switch_to_irc_after_a_hard_drop(self):
        self.start()
        await self.until(lambda: ("1", True) in self.states)
        self.old.send(envelope("revocation", {"subscription": {
            "type": "channel.chat.message", "status": "authorization_revoked",
            "condition": {"broadcaster_user_id": "1"},
        }}))
        await self.until(lambda: bool(self.errors))
        self.api.rejections[("channel.chat.message", "1")] = 503
        self.old.frames.put_nowait(WSMessage(WSMsgType.CLOSED, None, ""))
        self.socket().welcome("fresh")
        await self.until(lambda: ("channel.chat.message", "1", "viewer", "fresh") in self.api.subscriptions)
        self.assertEqual(self.api.irc_calls, 0)

    def test_deduplication_history_keeps_recently_repeated_ids_within_budget(self):
        recent = live_events._Recent(limit=2)
        for key, expected in [("a", False), ("b", False), ("a", True), ("c", False),
                              ("a", True), ("b", False)]:
            self.assertEqual(recent.duplicate(key), expected)

    async def test_large_watchlist_uses_batch_polling_with_chat_for_every_channel(self):
        channels = {str(index): f"login{index}" for index in range(1, 9)}
        self.enterContext(patch.object(live_events, "POLL_INTERVAL", 0.05, create=True))
        self.start(channels)
        await self.until(lambda: len(self.states) == 8)
        self.assertEqual(self.api.queries[0], set(channels))
        self.assertEqual(self.api.subscriptions, [
            ("channel.chat.message", cid, "viewer", "first") for cid in channels
        ])
        self.api.live = {"8"}
        await self.until(lambda: ("1", False) in self.states and ("8", True) in self.states)
        self.old.send(notification("channel.chat.message", "1", "ignored"))
        self.old.send(notification("channel.chat.message", "8", "counted"))
        await self.until(lambda: self.chat == [("8", "counted")])

    async def test_exhausted_stream_subscription_budget_falls_back_to_live_poll(self):
        self.api.rejections[("stream.online", "1")] = 429
        self.enterContext(patch.object(live_events, "POLL_INTERVAL", 0.05, create=True))
        self.start()
        await self.until(lambda: ("1", True) in self.states)
        self.assertEqual(self.api.subscriptions[0], ("channel.chat.message", "1", "viewer", "first"))
        self.api.live = set()
        await self.until(lambda: ("1", False) in self.states)
        self.assertEqual([s[0] for s in self.api.subscriptions].count("stream.online"), 1)
        self.assertEqual(self.api.irc_calls, 0)
