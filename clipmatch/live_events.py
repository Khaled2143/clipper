"""Receive Twitch stream and chat events over EventSub WebSockets."""

import asyncio
import logging
from collections import OrderedDict

import aiohttp


EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
POLL_INTERVAL = 30
TRANSPORT_ERRORS = (aiohttp.ClientError, OSError, TimeoutError)
API_ERRORS = (RuntimeError, *TRANSPORT_ERRORS)
LOG = logging.getLogger(__name__)


class _SocketClosed(ConnectionError):
    pass


class _Recent:
    """Twitch may deliver a notification more than once, including across handoffs."""

    def __init__(self, limit=16384):
        self.ids = OrderedDict()
        self.limit = limit

    def duplicate(self, key):
        if key in self.ids:
            self.ids.move_to_end(key)
            return True
        self.ids[key] = None
        if len(self.ids) > self.limit:
            self.ids.popitem(last=False)
        return False


async def _packet(socket, timeout):
    frame = await socket.receive(timeout=timeout)
    if frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
        raise _SocketClosed("EventSub socket closed")
    if frame.type != aiohttp.WSMsgType.TEXT:
        raise ConnectionError("EventSub socket failed")
    try:
        packet = frame.json()
        if isinstance(packet["metadata"]["message_type"], str) and isinstance(packet["payload"], dict):
            return packet
    except (KeyError, TypeError, ValueError):
        pass
    raise ConnectionError("Invalid EventSub envelope")


async def _connect(session, url):
    async with asyncio.timeout(15):
        socket = await session.ws_connect(url)
        try:
            packet = await _packet(socket, 10)
            welcome = packet["payload"].get("session", {})
            sid = welcome.get("id")
            timeout = welcome.get("keepalive_timeout_seconds")
            if (packet["metadata"]["message_type"] != "session_welcome" or not sid
                    or not isinstance(timeout, (int, float)) or timeout <= 0):
                raise ConnectionError("Invalid EventSub welcome")
            return socket, sid, timeout
        except BaseException:
            await socket.close()
            raise


class _Channel:
    def __init__(self, channel_id, login, feed):
        self.id = channel_id
        self.login = login
        self.feed = feed
        self.live = None
        self.ready = False
        self.published = None
        self.transitions = 0
        self.disabled = False
        self.state_available = asyncio.Event()

    def publish(self):
        if self.disabled or self.live is None or (self.live and not self.ready):
            return
        if self.live != self.published:
            self.published = self.live
            self.feed.on_live(self.id, self.live)

    def pause(self):
        self.ready = False
        self.published = None
        self.feed.on_pause(self.id)

    def chat(self, event):
        if self.published and not self.disabled:
            mid = event.get("message_id")
            if not mid or not self.feed.recent.duplicate(("chat", self.id, mid)):
                self.feed.on_chat(self.id, event)

    def disable(self, reason):
        self.disabled = True
        self.feed.irc_denied.add(self.id)
        self.pause()
        self.feed.on_error(self.id, reason)
        for job in self.feed.jobs[self.id]:
            if job is not asyncio.current_task():
                job.cancel()

    def apply_snapshot(self, version, current):
        if version == self.transitions:
            self.live = self.id in current
        self.publish()
        self.state_available.set()

    async def watch(self, operations):
        for kind in operations:
            delay = 1
            while not self.disabled:
                try:
                    if kind == "snapshot":
                        version = self.transitions
                        current = await self.feed.api.live_ids([self.id])
                        self.apply_snapshot(version, current)
                    else:
                        await self.feed.api.subscribe(kind, self.id, self.feed.user_id, self.feed.sid)
                        if kind == "channel.chat.message":
                            self.feed.irc_denied.discard(self.id)
                            self.ready = True
                            self.publish()
                    break
                except API_ERRORS as exc:
                    status = getattr(exc, "status", None)
                    if status and 400 <= status < 500 and status not in (408, 409, 429):
                        self.disable(f"{kind} rejected (HTTP {status})")
                        return
                    self.feed.on_error(self.id, f"{kind} temporarily unavailable ({status or 'network'})")
                    if status == 429 and kind in ("stream.online", "stream.offline"):
                        self.feed.polling.add(self.id)
                        self.feed.poll_now.set()
                        return
                    if (kind == "channel.chat.message" and self.id not in self.feed.irc_denied
                            and status and 500 <= status < 600):
                        await self.state_available.wait()
                        await self.irc_attempt()
                    await asyncio.sleep(delay)
                    delay = min(60, 2 * delay)

    async def irc_attempt(self):
        # IRC requires the same authenticated account and an additional read-only scope.
        # A channel denial, rate limit or revocation never routes to this transport.
        try:
            credentials = await self.feed.api.irc_credentials()
            if not credentials:
                return
            nick, token = credentials
            async with asyncio.timeout(15):
                socket = await self.feed.session.ws_connect(IRC_URL)
            try:
                await socket.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
                await socket.send_str(f"PASS oauth:{token}")
                await socket.send_str(f"NICK {nick}")
                await self.read_irc(socket, nick)
            finally:
                if not self.disabled:
                    self.pause()
                await socket.close()
        except API_ERRORS:
            self.feed.on_error(self.id, "IRC unavailable; retrying EventSub")

    async def read_irc(self, socket, nick):
        room = "#" + self.login.lower()
        while not self.disabled:
            frame = await socket.receive(timeout=360 if self.ready else 15)
            if frame.type != aiohttp.WSMsgType.TEXT:
                return
            for line in frame.data.split("\r\n"):
                tags = {}
                if line.startswith("@"):
                    taglist, _, line = line.partition(" ")
                    tags = dict(tag.partition("=")[::2] for tag in taglist[1:].split(";"))
                prefix = ""
                if line.startswith(":"):
                    prefix, _, line = line[1:].partition(" ")
                command_and_args, _, trailing = line.partition(" :")
                parts = command_and_args.split()
                if not parts:
                    continue
                command = parts[0]
                target = parts[1].lower() if len(parts) > 1 else ""
                if command == "PING":
                    await socket.send_str(f"PONG :{trailing}")
                elif command == "001":
                    await socket.send_str(f"JOIN {room}")
                elif command == "RECONNECT":
                    return
                elif command == "NOTICE":
                    denial = tags.get("msg-id") in ("msg_banned", "msg_channel_blocked", "msg_channel_suspended")
                    if target == "*" or (target == room and (denial or not self.ready)):
                        self.disable("IRC channel or authentication rejection")
                        return
                elif target == room:
                    own_membership = prefix.split("!")[0].lower() == nick.lower()
                    if command == "PART" and own_membership:
                        self.disable("IRC removed the account from this channel")
                        return
                    if command == "ROOMSTATE" or (command == "JOIN" and own_membership):
                        self.ready = True
                        self.publish()
                    elif command == "PRIVMSG":
                        self.chat({"broadcaster_user_id": self.id, "message_id": tags.get("id"),
                                   "chatter_user_login": prefix.split("!")[0],
                                   "message": {"text": trailing}})


class _Feed:
    def __init__(self, session, api, channels, user_id, recent, irc_denied,
                 on_live, on_chat, on_error, on_pause):
        self.session = session
        self.api = api
        self.user_id = user_id
        self.recent = recent
        self.irc_denied = irc_denied
        self.on_live = on_live
        self.on_chat = on_chat
        self.on_error = on_error
        self.on_pause = on_pause
        self.channels = {cid: _Channel(cid, login, self) for cid, login in channels.items()}
        self.jobs = {}
        # Two third-party stream subscriptions per channel consume the ten-point
        # WebSocket cost budget. Chat subscriptions use the authorized user's grant.
        self.polling = set(channels) if len(channels) > 5 else set()
        self.poll_now = asyncio.Event()

    async def poll(self):
        while True:
            self.poll_now.clear()
            versions = {cid: self.channels[cid].transitions for cid in self.polling
                        if not self.channels[cid].disabled}
            if not versions:
                await self.poll_now.wait()
                continue
            try:
                current = await self.api.live_ids(list(versions))
            except API_ERRORS:
                for cid in versions:
                    channel = self.channels[cid]
                    if channel.published:
                        channel.published = None
                        self.on_pause(cid)
                    channel.live = None
                    self.on_error(cid, "Live status unavailable; polling again")
            else:
                for cid, version in versions.items():
                    self.channels[cid].apply_snapshot(version, current)
            try:
                await asyncio.wait_for(self.poll_now.wait(), POLL_INTERVAL)
            except TimeoutError:
                pass

    def dispatch(self, packet):
        metadata = packet["metadata"]
        payload = packet["payload"]
        message_type = metadata["message_type"]
        if message_type not in ("notification", "revocation"):
            return
        mid = metadata.get("message_id")
        if mid and self.recent.duplicate(("eventsub", mid)):
            return
        subscription = payload.get("subscription", {})
        event = payload.get("event", {})
        source = subscription.get("condition", {}) if message_type == "revocation" else event
        channel = self.channels.get(source.get("broadcaster_user_id"))
        if not channel or channel.disabled:
            return
        kind = subscription.get("type")
        if message_type == "revocation":
            channel.disable(f"{kind} revoked ({subscription.get('status', 'unknown')})")
        elif kind in ("stream.online", "stream.offline"):
            channel.transitions += 1
            channel.live = kind == "stream.online"
            channel.publish()
        elif kind == "channel.chat.message":
            channel.chat(event)

    async def pump(self, socket, timeout, *, migrating=False):
        while True:
            packet = await _packet(socket, timeout)
            if packet["metadata"]["message_type"] == "session_reconnect":
                if not migrating:
                    url = packet["payload"].get("session", {}).get("reconnect_url")
                    if not isinstance(url, str) or not url.startswith("wss://"):
                        raise ConnectionError("Invalid EventSub reconnect URL")
                    return url
            else:
                self.dispatch(packet)

    async def handoff(self, url):
        old = self.socket
        drain = asyncio.create_task(self.pump(old, self.timeout, migrating=True))
        try:
            new, sid, timeout = await _connect(self.session, url)
            if drain.done():
                try:
                    drain.result()
                except _SocketClosed:
                    # The old close and the replacement welcome can arrive together.
                    pass
                except BaseException:
                    await new.close()
                    raise
            self.socket, self.sid, self.timeout = new, sid, timeout
        finally:
            drain.cancel()
            await asyncio.gather(drain, return_exceptions=True)
            if self.socket is not old:
                await old.close()

    async def run(self):
        self.socket, self.sid, self.timeout = await _connect(self.session, EVENTSUB_URL)
        try:
            async with asyncio.TaskGroup() as group:
                self.jobs = {cid: [group.create_task(channel.watch(["channel.chat.message"]))]
                             for cid, channel in self.channels.items()}
                for cid, channel in self.channels.items():
                    if cid not in self.polling:
                        operations = ["stream.online", "stream.offline", "snapshot"]
                        self.jobs[cid].append(group.create_task(channel.watch(operations)))
                poller = group.create_task(self.poll())
                try:
                    while True:
                        url = await self.pump(self.socket, self.timeout)
                        await self.handoff(url)
                except TRANSPORT_ERRORS:
                    pass
                finally:
                    poller.cancel()
                    for jobs in self.jobs.values():
                        for job in jobs:
                            job.cancel()
        finally:
            await self.socket.close()


async def listen(session, api, channels, user_id, on_live, on_chat, on_gap, on_error, *, on_pause=None):
    """Receive live chat until cancelled. A hard disconnect invokes on_gap.

    on_pause signals unavailable chat separately from a confirmed offline stream.
    Each broadcaster resumes counting through on_live after chat becomes available.
    """
    recent = _Recent()
    irc_denied = set()
    if on_pause is None:
        def on_pause(channel_id):
            on_live(channel_id, False)
    delay = 1
    loop = asyncio.get_running_loop()
    while True:
        feed = _Feed(session, api, channels, user_id, recent, irc_denied,
                     on_live, on_chat, on_error, on_pause)
        started = loop.time()
        try:
            await feed.run()
        except TRANSPORT_ERRORS:
            pass
        on_gap()
        if loop.time() - started >= 60:
            delay = 1
        LOG.warning("EventSub disconnected; reconnecting in %s seconds", delay)
        await asyncio.sleep(delay)
        delay = min(60, 2 * delay)
