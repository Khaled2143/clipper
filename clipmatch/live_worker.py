"""Run per-channel spike detection and deliver confirmed Twitch clips."""
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from .live_api import ApiError, discord_webhook_url, send_discord
from .render_queue import RenderQueue
from .spikes import Bucket, Detector, SpikeSettings


def log(event, **fields):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(json.dumps({"time": timestamp, "event": event, **fields}, allow_nan=False), flush=True)


def required(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} before starting the worker.")
    return value


def state_directory(env):
    return Path(env.get("CLIPBOT_STATE_DIR") or env.get("RAILWAY_VOLUME_MOUNT_PATH") or ".clipbot")


@dataclass(frozen=True)
class WorkerConfig:
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    webhook_url: str = field(repr=False)
    channels: tuple[str, ...]
    state_dir: Path
    settings: SpikeSettings
    finish_clips: bool = False
    discord_delivery: bool = True
    deliver_finished: bool = True
    public_posting: bool = False
    discord_watch_alerts: bool | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]):
        channels = tuple(dict.fromkeys(s.strip().lower() for s in required(env, "CHANNELS").split(",")))
        if any(not re.fullmatch(r"[a-z0-9_]{1,25}", name) for name in channels):
            raise ValueError("CHANNELS must be a comma-separated list of Twitch logins.")
        discord = env.get("DISCORD_CLIP_DELIVERY", "1").strip().lower() not in {"0", "false", "no"}
        alerts_setting = env.get("DISCORD_WATCH_ALERTS")
        alerts = None if alerts_setting is None else alerts_setting.strip().lower() in {"1", "true", "yes"}
        posting = env.get("PUBLIC_POSTING_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        webhook = (discord_webhook_url(required(env, "DISCORD_WEBHOOK_URL"), "DISCORD_WEBHOOK_URL")
                   if discord or alerts else "")
        finished = env.get("DISCORD_FINISHED_WEBHOOK_URL", "").strip() if discord else ""
        if finished:
            discord_webhook_url(finished, "DISCORD_FINISHED_WEBHOOK_URL")
        return cls(required(env, "TWITCH_CLIENT_ID"), required(env, "TWITCH_CLIENT_SECRET"),
                   env.get("TWITCH_REFRESH_TOKEN", "").strip(), webhook, channels,
                   state_directory(env), SpikeSettings.from_env(env), finish_clips=bool(finished) or posting,
                   discord_delivery=discord, deliver_finished=bool(finished), public_posting=posting,
                   discord_watch_alerts=alerts)


class ClipState:
    """Keep cooldowns, pending confirmations, and render jobs on the worker volume."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "clips.sqlite3")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS cooldowns (channel TEXT PRIMARY KEY, fired REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, login TEXT, summary TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS clip_chat_timing "
                            "(id TEXT PRIMARY KEY, lead_seconds REAL NOT NULL)")
        self.renders = RenderQueue(self.db, directory)

    def reserve(self, channel, now, cooldown):
        with self.db:
            changed = self.db.execute(
                "INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(channel) DO UPDATE SET fired=excluded.fired "
                "WHERE cooldowns.fired <= excluded.fired - ?", (channel, now, cooldown))
            return changed.rowcount == 1

    def remember(self, clip_id, login, summary, chat_lead: float | None = None):
        with self.db:
            self.db.execute("INSERT INTO pending(id, login, summary) VALUES (?, ?, ?)", (clip_id, login, summary))
            if chat_lead is not None and 0 <= chat_lead < math.inf:
                self.db.execute("INSERT INTO clip_chat_timing(id, lead_seconds) VALUES (?, ?) "
                                "ON CONFLICT(id) DO NOTHING", (clip_id, chat_lead))

    def pending(self):
        return self.db.execute("SELECT id, login, summary FROM pending").fetchall()

    def forget(self, clip_id):
        with self.db:
            self.db.execute("DELETE FROM pending WHERE id=?", (clip_id,))

    def close(self):
        self.db.close()


class Worker:
    def __init__(self, api, session, webhook, channels, settings, state, *,
                 clock=time.monotonic, wall=time.time, render_queue=None, discord_delivery=True,
                 discord_watch_alerts=None):
        self.api = api
        self.session = session
        self.webhook = webhook
        self.discord_delivery = discord_delivery
        self.discord_watch_alerts = discord_delivery if discord_watch_alerts is None else discord_watch_alerts
        self.channels = channels
        self.settings = settings
        self.state = state
        self.render_queue = render_queue
        self.clock = clock
        self.wall = wall
        self.detectors = {}
        self.status = {}
        self.tasks = set()
        self.last_alert = {}
        self.failure = asyncio.get_running_loop().create_future()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) and not self.failure.done():
            self.failure.set_result(error)

    async def drain(self):
        if self.tasks:
            await asyncio.gather(*list(self.tasks))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    def gap(self):
        self.detectors.clear()
        log("chat_disconnected")

    def pause(self, channel):
        self.detectors.pop(channel, None)

    def announce_watchlist(self):
        async def announce():
            channels = list(self.channels.values())
            delivered = True
            for start in range(0, len(channels), 60):
                names = ", ".join(channels[start:start + 60])
                sent = await self._post("watcher", f"Tracking these Twitch channels: {names}", watch_status=True)
                delivered = delivered and sent
            log("discord_watchlist", channels=channels, delivered=delivered)

        self._spawn(announce())

    def live(self, channel, online):
        login = self.channels[channel]
        previous = self.status.get(channel)
        self.status[channel] = online
        if online and channel not in self.detectors:
            self.detectors[channel] = Detector(self.settings, self.clock())
        if not online:
            self.detectors.pop(channel, None)
        if previous != online:
            log("online" if online else "offline", channel=login)
            notice = "live — watching chat for clips." if online else "offline — chat monitoring paused."
            self._spawn(self._post(login, f"[{login}] {notice}", watch_status=True))

    def channel_error(self, channel, reason):
        login = self.channels.get(channel, channel)
        log("channel_error", channel=login, reason=reason)
        now = self.clock()
        if now - self.last_alert.get((channel, reason), float("-inf")) >= 300:
            self.last_alert[channel, reason] = now
            self._spawn(self._post(login, f"[{login}] {reason}", watch_status=True))

    def chat(self, channel, event):
        detector = self.detectors.get(channel)
        if detector is not None:
            self._advance(channel, detector, self.clock())
            detector.add(event)

    def tick(self):
        now = self.clock()
        for channel, detector in self.detectors.items():
            self._advance(channel, detector, now)

    def _advance(self, channel, detector, now):
        buckets = detector.advance(now)
        for index, bucket in enumerate(buckets):
            login = self.channels[channel]
            log("bucket", channel=login, count=bucket.count, baseline=bucket.baseline,
                ratio=bucket.ratio, top=[word[:80] for word in bucket.top])
            if bucket.spike and index == len(buckets) - 1:
                if self.state.reserve(channel, self.wall(), self.settings.cooldown_seconds):
                    log("spike", channel=login, count=bucket.count)
                    self._spawn(self._clip(channel, detector, bucket))

    def _summary(self, login, bucket: Bucket):
        change = f"{bucket.ratio:.1f}× spike" if bucket.ratio is not None else "spike from silence"
        text = (f"[{login}] {change}: {bucket.count} msgs in {self.settings.bucket_seconds}s "
                f"(baseline {bucket.baseline:.1f})")
        if bucket.top:
            text += "\nTop chat: " + ", ".join(word[:80] for word in bucket.top)
        return text

    async def _post(self, login, message, *, watch_status=False):
        enabled = self.discord_watch_alerts if watch_status else self.discord_delivery
        if not enabled:
            return False
        try:
            await send_discord(self.session, self.webhook, message)
        except ApiError as exc:
            log("discord_failed", channel=login, reason=str(exc))
            return False
        return True

    async def _clip(self, channel, detector, bucket: Bucket):
        login = self.channels[channel]
        if self.detectors.get(channel) is not detector:
            log("clip_skipped", channel=login, reason="live chat monitoring ended before request")
            return
        chat_lead = None

        def mark_attempt():
            nonlocal chat_lead
            if bucket.rise_at is not None:
                chat_lead = self.clock() - bucket.rise_at

        try:
            clip_id = await self.api.create_clip(channel, on_attempt=mark_attempt)
        except ApiError as exc:
            await self._clip_failed(login, exc)
            return
        summary = self._summary(login, bucket)
        self.state.remember(clip_id, login, summary, chat_lead)
        log("clip_requested", channel=login, clip_id=clip_id)
        await self._confirm(clip_id, login, summary)

    async def _clip_failed(self, login, error):
        log("clip_failed", channel=login, reason=str(error))
        await self._post(login, f"[{login}] clip failed: {error}")

    async def _confirm(self, clip_id, login, summary):
        try:
            url = await self.api.confirm_clip(clip_id)
        except ApiError as exc:
            await self._clip_failed(login, exc)
        else:
            if self.render_queue is not None and self.render_queue.enqueue(clip_id, login, url):
                log("render_queued", channel=login, clip_id=clip_id)
            delivered = await self._post(login, f"{summary}\n{url}")
            log("clip_result", channel=login, url=url, delivered=delivered)
        self.state.forget(clip_id)

    def resume(self):
        for clip in self.state.pending():
            self._spawn(self._confirm(*clip))
