"""Durable per-platform delivery of full-quality, finished Twitch highlights."""
import argparse
import asyncio
from contextlib import closing
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
from urllib.parse import urlsplit

from aiohttp import web

from .cache import file_hash
from .finish_clip import clip_directory, validate_clip
from .live_api import ApiError, discord_webhook_url, send_discord
from .local_copy import plain_title
from .tiktok_publisher import Buffer, BufferFailure, confirm_public, create_tiktok, post_result


def _prepare(root, channel, clip_id, source_url):
    directory = clip_directory(root, channel, clip_id)
    master = directory / "renders" / (directory.name + ".mp4")
    record = json.loads(master.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    digest = record["sha256"]
    if (record.get("status") != "complete" or record.get("stage") != "render"
            or record.get("artifact") != master.name or record.get("size") != master.stat().st_size
            or master.stat().st_size <= 0 or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ValueError("incomplete master")
    provenance = json.loads(master.with_suffix(".json").read_text(encoding="utf-8"))
    if provenance.get("label") != channel or provenance["source"].get("twitch_url") != source_url:
        raise ValueError("source mismatch")
    social_file = master.with_suffix(".social.json")

    def copy_for_master():
        if social_file.is_file():
            copy = json.loads(social_file.read_text(encoding="utf-8"))
            if (copy.get("master_sha256") == digest and copy.get("status") == "ready"
                    and copy.get("source_url") == source_url and isinstance(copy.get("description"), str)
                    and copy["description"].strip()
                    and isinstance(copy.get("title"), str) and 0 < len(copy["title"]) <= 100):
                title = plain_title(copy["title"])
                if title:
                    return title, copy["description"]
        return None

    copy = copy_for_master()
    if copy is None:
        from .social_titles import social_content
        # The render child normally runs the editor. Repair a missing record without
        # retrying a potentially interrupted native model inside the long-lived watcher.
        social_content(master, digest, lightweight=True)
        copy = copy_for_master()
    if copy is None:
        raise ValueError("copy unavailable")
    folder = root / "publish-media"
    folder.mkdir(exist_ok=True)
    pinned = folder / (digest + ".mp4")
    if not pinned.exists():
        # Renders are atomically replaced. A hard link pins the exact submitted bytes.
        # An exclusive temporary copy supports volumes without hard links.
        try:
            os.link(master, pinned)
        except FileExistsError:
            pass
        except OSError:
            temp = folder / (secrets.token_hex(16) + ".partial")
            try:
                shutil.copyfile(master, temp)
                if file_hash(temp) != digest:
                    raise ValueError("master changed before publication")
                temp.replace(pinned)
            finally:
                temp.unlink(missing_ok=True)
    if file_hash(pinned) != digest:
        # This file is worker-created staging, never the source or original master.
        pinned.unlink(missing_ok=True)
        raise ValueError("master changed before publication")
    return digest, *copy


class Publications:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()
        # Older queued clips can have titles saved before emoji filtering.
        db.create_function("posting_title", 2, lambda title, channel:
                           plain_title(title or "") or f"From {channel} on Twitch", deterministic=True)
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS publish_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("""INSERT OR IGNORE INTO publish_settings SELECT 'first_row', COALESCE(MAX(rowid),0)
                          FROM render_jobs""")
            db.execute("""CREATE TABLE IF NOT EXISTS publish_clips (
                id TEXT PRIMARY KEY, channel TEXT, source_url TEXT, digest TEXT, title TEXT,
                description TEXT, token TEXT UNIQUE, next_check REAL NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS publish_jobs (
                clip_id TEXT, platform TEXT, phase TEXT NOT NULL DEFAULT 'pending',
                state TEXT NOT NULL DEFAULT '{}', ready_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(clip_id,platform))""")
            db.execute("CREATE INDEX IF NOT EXISTS publish_due ON publish_jobs(platform,phase,ready_at)")
            db.execute("""CREATE TABLE IF NOT EXISTS publish_discord_receipts (
                clip_id TEXT, platform TEXT, title TEXT NOT NULL, url TEXT NOT NULL,
                phase TEXT NOT NULL DEFAULT 'pending', ready_at REAL NOT NULL DEFAULT 0,
                message_id TEXT, reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(clip_id,platform))""")
            db.execute("""CREATE INDEX IF NOT EXISTS publish_discord_due ON publish_discord_receipts(ready_at)
                WHERE phase='pending'""")

    def setting(self, name, default=""):
        row = self.db.execute("SELECT value FROM publish_settings WHERE name=?", (name,)).fetchone()
        return row[0] if row else default

    def set(self, name, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO publish_settings VALUES (?,?)", (name, str(value)))

    async def collect(self, log=lambda *args, **kwargs: None):
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO publish_clips(id,channel,source_url)
                SELECT id,channel,url FROM render_jobs WHERE rowid>? AND status IN ('done','review')""",
                            (int(self.setting("first_row")),))
        row = self.db.execute("""SELECT id,channel,source_url FROM publish_clips
            WHERE digest IS NULL AND next_check<=? ORDER BY rowid LIMIT 1""", (time.time(),)).fetchone()
        if not row:
            return
        clip_id, channel, url = row
        try:
            url = validate_clip(channel, clip_id, url)
            digest, title, description = await asyncio.to_thread(_prepare, self.root, channel, clip_id, url)
        except (OSError, ValueError, TypeError, KeyError):
            with self.db:
                self.db.execute("UPDATE publish_clips SET next_check=? WHERE id=?", (time.time()+3600, clip_id))
            log("publish_awaiting_master", clip_id=clip_id)
            return
        with self.db:
            self.db.execute("""UPDATE publish_clips SET digest=?,title=?,description=?,token=?,source_url=? WHERE id=?""",
                            (digest, title, description, secrets.token_urlsafe(32), url, clip_id))
            self.db.executemany("INSERT OR IGNORE INTO publish_jobs(clip_id,platform) VALUES (?,?)",
                                [(clip_id, platform) for platform in ("youtube", "tiktok")])
        log("publish_queued", clip_id=clip_id)

    def due(self, platform):
        return self.db.execute("""SELECT c.id, c.digest, posting_title(c.title,c.channel), c.description, c.source_url,
                                         c.token, j.state
            FROM publish_jobs j JOIN publish_clips c ON j.clip_id=c.id
            WHERE j.phase='pending' AND j.platform=? AND j.ready_at<=?
            ORDER BY CASE WHEN json_extract(j.state,'$.remote_id') IS NOT NULL THEN j.ready_at ELSE 0 END,
                     c.rowid""",
                               (platform, time.time())).fetchall()

    def save(self, clip_id, platform, state, log, *, discord_receipt=False):
        phase = state["phase"]
        if phase == "published" and "published_at" not in state:
            state = {**state, "published_at": time.time()}
        with self.db:
            if discord_receipt and phase == "published":
                # Snapshot the submitted title on the transition. Existing public history stays outside the outbox.
                self.db.execute("""INSERT OR IGNORE INTO publish_discord_receipts(clip_id,platform,title,url)
                    SELECT j.clip_id,j.platform,posting_title(c.title,c.channel),? FROM publish_jobs j JOIN publish_clips c ON c.id=j.clip_id
                    WHERE j.clip_id=? AND j.platform=? AND j.phase!='published'""", (state["url"], clip_id, platform))
            self.db.execute("""UPDATE publish_jobs SET phase=?, state=?, ready_at=?
                WHERE clip_id=? AND platform=?""",
                (phase, json.dumps(state), time.time()+max(0, state.get("delay", 0)), clip_id, platform))
        if phase != "pending" or state.get("reason"):
            report = {"platform": platform, "clip_id": clip_id, "reason": state.get("reason", "")}
            if phase == "published":
                report["url"] = state.get("url")
            log("publish_" + phase, **report)

    def claim_discord_receipt(self):
        with self.db:
            row = self.db.execute("""SELECT clip_id,platform,title,url FROM publish_discord_receipts
                WHERE phase='pending' AND ready_at<=? ORDER BY ready_at,rowid LIMIT 1""", (time.time(),)).fetchone()
            if row:
                # A crash during POST leaves the delivery in review. Discord cannot look up a webhook
                # message without its ID, so replaying it on restart could create a second receipt.
                claimed = self.db.execute("""UPDATE publish_discord_receipts SET phase='review',
                    reason='Delivery started without a saved Discord acknowledgment'
                    WHERE clip_id=? AND platform=? AND phase='pending'""", row[:2])
                if claimed.rowcount:
                    return row

    def finish_discord_receipt(self, clip_id, platform, phase, message_id, reason, ready_at):
        with self.db:
            self.db.execute("""UPDATE publish_discord_receipts SET phase=?,message_id=?,reason=?,ready_at=?
                WHERE clip_id=? AND platform=?""", (phase, message_id, reason, ready_at, clip_id, platform))

    def path(self, digest):
        return self.root / "publish-media" / (digest + ".mp4")


def media_app(queue):
    app = web.Application()

    async def health(request):
        return web.json_response({"status": "ok"})

    async def media(request):
        row = queue.db.execute("""SELECT digest FROM publish_clips c WHERE token=? AND EXISTS
            (SELECT 1 FROM publish_jobs j WHERE j.clip_id=c.id AND j.platform='tiktok'
             AND j.phase IN ('pending','review'))""", (request.match_info["token"],)).fetchone()
        if not row or not queue.path(row[0]).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(queue.path(row[0]), headers={"Content-Type": "video/mp4",
                                "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    app.router.add_get("/healthz", health)
    app.router.add_get("/media/{token:[A-Za-z0-9_-]{43}}.mp4", media)
    return app


def media_origin(env):
    domain = env.get("RAILWAY_PUBLIC_DOMAIN", "")
    origin = env.get("PUBLISH_MEDIA_ORIGIN") or (f"https://{domain}" if domain else "")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or (
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("PUBLISH_MEDIA_ORIGIN must be an HTTPS origin.")
    return origin.rstrip("/")


class Publisher:
    def __init__(self, queue, session, env, log):
        self.queue, self.session, self.env, self.log = queue, session, env, log
        self.interval = max(0, float(env.get("PUBLISH_INTERVAL_MINUTES", "60"))) * 60
        self.api = Buffer(session, env["BUFFER_API_KEY"]) if env.get("BUFFER_API_KEY") else None
        self.channel = None
        self.recheck_channel = 0
        self.origin = media_origin(env)
        self.media_ready = False
        self.finished_webhook = ""
        finished = env.get("DISCORD_FINISHED_WEBHOOK_URL", "").strip()
        if finished:
            try:
                self.finished_webhook = discord_webhook_url(finished, "DISCORD_FINISHED_WEBHOOK_URL")
            except ValueError:
                self.log("publish_receipt_config", reason="Invalid DISCORD_FINISHED_WEBHOOK_URL")
        self.grant = None
        self.grant_error = False
        if env.get("YOUTUBE_GRANT_JSON"):
            try:
                self.grant = json.loads(env["YOUTUBE_GRANT_JSON"])
                if not isinstance(self.grant, dict):
                    raise ValueError
            except ValueError:
                self.grant_error = True
                self.grant = None

    def can_create(self, platform):
        return time.time() >= float(self.queue.setting(platform + "_next", "0"))

    def defer(self, platform, seconds):
        self.queue.set(platform + "_next", max(float(self.queue.setting(platform+"_next", "0")), time.time()+seconds))

    def save(self, clip_id, platform, state):
        self.queue.save(clip_id, platform, state, self.log, discord_receipt=bool(self.finished_webhook))

    async def discord_receipts(self):
        if not self.finished_webhook or not self.can_create("discord_receipts"):
            return
        row = self.queue.claim_discord_receipt()
        if not row:
            return
        clip_id, platform, title, url = row
        label = {"youtube": "YouTube Short", "tiktok": "TikTok"}[platform]
        message_id, delay = None, 0
        try:
            message_id = await send_discord(self.session, self.finished_webhook, f"{label} published\n{title}\n<{url}>",
                                            attempts=1)
        except ApiError as exc:
            reason = str(exc)
            if exc.retry_after is None:
                phase = "review"
            else:
                phase, delay = "pending", max(10, exc.retry_after)
        else:
            phase, reason = ("sent", "") if message_id else ("review", "Discord did not return a message ID")
        self.queue.finish_discord_receipt(clip_id, platform, phase, message_id, reason, time.time() + delay)
        if delay:
            self.defer("discord_receipts", delay)
        self.log("publish_receipt_" + phase, clip_id=clip_id, platform=platform, message_id=message_id,
                 reason=reason, retry_seconds=delay)

    async def youtube(self):
        if not self.grant:
            return
        from .youtube_publisher import youtube_step
        for clip_id, digest, title, description, url, _, raw in self.queue.due("youtube"):
            state = json.loads(raw)
            existing = state.get("session_url") or state.get("remote_id")
            if not existing and not self.can_create("youtube"):
                continue
            def checkpoint(value):
                self.save(clip_id, "youtube", value)
            result = await youtube_step(self.session, self.grant, self.queue.path(digest),
                                        title, f"{description}\n\n{url}", state, checkpoint)
            if result["phase"] == "review" and result.get("reason") in {
                    "Check the YouTube account grant and API permissions",
                    "The authorized Google account does not select the expected YouTube channel",
                    "Configure a YouTube OAuth grant for the expected channel"}:
                result.update(phase="pending", delay=3600, cooldown=3600)
            if not existing:
                delay = self.interval if result.get("session_url") or result.get("remote_id") else result.get("delay", 60)
                self.defer("youtube", delay)
            if result.get("cooldown"):
                self.defer("youtube", result["cooldown"])
            checkpoint(result)
            break

    async def tiktok(self):
        if not self.api or not self.origin:
            return
        due = self.queue.due("tiktok")
        if not due:
            return
        now = time.time()
        if not self.channel or now >= self.recheck_channel:
            selected = self.env.get("BUFFER_TIKTOK_CHANNEL_ID") or self.queue.setting("buffer_channel")
            channel = await self.api.destination(selected)
            identity = self.queue.setting("tiktok_identity", channel["serviceId"])
            if channel["serviceId"] != identity:
                raise BufferFailure("connected TikTok account differs from the activated account", 3600)
            self.queue.set("buffer_channel", channel["id"])
            self.queue.set("tiktok_identity", identity)
            self.channel, self.recheck_channel = channel["id"], now + 86400
        polls = []
        for clip_id, _, _, _, _, _, raw in due:
            state = json.loads(raw)
            if state.get("creating"):
                self.save(clip_id, "tiktok", {"phase": "review",
                    "reason": "interrupted while creating a Buffer post, check its queue and sent posts"})
            elif state.get("url"):
                self.save(clip_id, "tiktok", await confirm_public(self.session, state))
            elif state.get("remote_id"):
                polls.append((clip_id, state))
        if polls and now >= float(self.queue.setting("buffer_poll", "0")):
            batch = polls[:25]
            try:
                results = await self.api.posts([state["remote_id"] for _, state in batch])
            except BufferFailure as exc:
                self.queue.set("buffer_poll", now + max(900, exc.delay))
                self.log("publish_wait", platform="tiktok_receipts", reason=exc.kind)
                if exc.kind in {"rate limit", "check Buffer key and permissions"}:
                    self.defer("tiktok", exc.delay)
            else:
                for (clip_id, previous), post in zip(batch, results):
                    if not post or post.get("id") != previous["remote_id"]:
                        result = {**previous, "phase": "review", "reason": "check the saved post ID in Buffer"}
                    else:
                        result = post_result(post, self.channel)
                    self.save(clip_id, "tiktok", result)
                    if result.get("cooldown"):
                        self.defer("tiktok", result["cooldown"])
                self.queue.set("buffer_poll", now + 10800)
        if not self.can_create("tiktok"):
            return
        for clip_id, _, title, _, _, token, raw in due:
            state = json.loads(raw)
            if state.get("remote_id") or state.get("creating"):
                continue
            def checkpoint(value):
                self.save(clip_id, "tiktok", value)
            result = await create_tiktok(self.api, self.channel, title,
                                         f"{self.origin}/media/{token}.mp4", checkpoint)
            checkpoint(result)
            self.defer("tiktok", max(self.interval, result.get("cooldown", 0)))
            break

    async def repeat(self, name, operation, seconds=10):
        while True:
            delay = seconds
            try:
                await operation()
            except BufferFailure as exc:
                self.log("publish_wait", platform=name, reason=exc.kind)
                delay = max(seconds, exc.delay)
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RuntimeError) as exc:
                # Never log raw provider responses, tokens, signed URLs or callback arguments.
                self.log("publish_wait", platform=name, reason=type(exc).__name__)
                delay = 300
            await asyncio.sleep(delay)

    async def serve_media(self):
        runner = web.AppRunner(media_app(self.queue), access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", int(self.env.get("PORT", "8080")))
            await site.start()
            self.media_ready = True
            await asyncio.Event().wait()
        finally:
            self.media_ready = False
            await runner.cleanup()

    async def run(self):
        cleanup = (self.env.get("PUBLISH_CLEANUP_CONFIRMED") == "1"
                   and bool(self.env.get("RAILWAY_ENVIRONMENT_ID")))
        self.log("publishing", youtube="configured" if self.grant else
                 ("invalid grant JSON" if self.grant_error else "waiting for Google authorization"),
                 tiktok="configured" if self.api and self.origin else "waiting for Buffer connection or media domain",
                 discord_receipts="configured" if self.finished_webhook else "disabled",
                 cleanup_confirmed=cleanup, free_mb=shutil.disk_usage(self.queue.root).free // (1024 * 1024))

        async def tiktok_if_ready():
            if self.media_ready:
                await self.tiktok()

        next_cleanup = 0
        async def collect():
            nonlocal next_cleanup
            if cleanup and time.monotonic() >= next_cleanup:
                from .publish_retention import cleanup_published
                next_cleanup = time.monotonic() + 3600
                await cleanup_published(self.queue, self.log)
            await self.queue.collect(self.log)

        async with asyncio.TaskGroup() as group:
            group.create_task(self.repeat("media", self.serve_media))
            group.create_task(self.repeat("masters", collect))
            group.create_task(self.repeat("youtube", self.youtube))
            group.create_task(self.repeat("tiktok", tiktok_if_ready))
            if self.finished_webhook:
                group.create_task(self.repeat("discord_receipts", self.discord_receipts))

async def run_publisher(state, session, env, log):
    root = state.renders.directory
    with closing(sqlite3.connect(root / "publish-worker.sqlite3", timeout=0)) as lock:
        while True:
            try:
                lock.execute("BEGIN EXCLUSIVE")
                break
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                await asyncio.sleep(1)
        queue = Publications(state.db, root)
        await Publisher(queue, session, env, log).run()


def main():
    from .live_worker import state_directory
    parser = argparse.ArgumentParser(description="Show public posting receipts and clips needing attention.")
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    path = (args.state_dir or state_directory(os.environ)) / "clips.sqlite3"
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='publish_jobs'").fetchone():
            print("Public posting has not been activated on this database.")
            return
        for platform, phase, count in db.execute("SELECT platform,phase,COUNT(*) FROM publish_jobs GROUP BY 1,2"):
            print(platform, phase, count)
        for clip_id, platform, raw in db.execute(
                "SELECT clip_id,platform,state FROM publish_jobs WHERE phase IN ('review','published') ORDER BY rowid DESC LIMIT 30"):
            state = json.loads(raw)
            print(clip_id, platform, state.get("url") or state.get("reason"), state.get("remote_id", ""))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='publish_discord_receipts'").fetchone():
            for phase, count in db.execute("SELECT phase,COUNT(*) FROM publish_discord_receipts GROUP BY phase"):
                print("Discord receipts", phase, count)
            for clip_id, platform, reason in db.execute("""SELECT clip_id,platform,reason FROM publish_discord_receipts
                    WHERE phase='review' ORDER BY rowid DESC LIMIT 30"""):
                print("Discord receipt review", clip_id, platform, reason)


if __name__ == "__main__":
    main()
