"""Persistent, single-consumer queue for rendering confirmed Twitch clips."""
import asyncio
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import sys
import time


def read_chat_lead(state_dir: Path, clip_id: str) -> float | None:
    """Read seconds from chat rise to clip request, including on pre-timing databases."""
    database = (state_dir / "clips.sqlite3").resolve()
    try:
        db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode == sqlite3.SQLITE_CANTOPEN:
            return None
        raise
    with closing(db):
        table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clip_chat_timing'").fetchone()
        if table is None:
            return None
        row = db.execute("SELECT lead_seconds FROM clip_chat_timing WHERE id=?", (clip_id,)).fetchone()
    if row and isinstance(row[0], (int, float)) and 0 <= row[0] < math.inf:
        return float(row[0])
    return None


class RenderQueue:
    def __init__(self, db: sqlite3.Connection, directory: Path):
        self.db = db
        self.directory = directory.resolve()
        self.deliver_finished = True
        with db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS render_jobs (
                    id TEXT PRIMARY KEY, channel TEXT NOT NULL, url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                    ready_at REAL NOT NULL DEFAULT 0
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS render_jobs_ready ON render_jobs(status, ready_at)")

    def enqueue(self, clip_id, channel, url):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO render_jobs(id, channel, url) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (clip_id, channel, url))
        return cursor.rowcount == 1

    def retry(self, clip_id):
        with self.db:
            cursor = self.db.execute(
                "UPDATE render_jobs SET status='queued', attempts=0, ready_at=0 WHERE id=? AND status='review'",
                (clip_id,))
        return cursor.rowcount == 1

    async def run(self, log):
        # Keep the consumer lock in its own SQLite file so confirmation and
        # backfill can still write the jobs database while a child is running.
        with closing(sqlite3.connect(self.directory / "render-worker.sqlite3", timeout=0)) as lock:
            while True:
                try:
                    lock.execute("BEGIN EXCLUSIVE")
                    break
                except sqlite3.OperationalError as exc:
                    if exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    await asyncio.sleep(1)

            # An unclean exit can happen on either side of a successful upload.
            with self.db:
                interrupted = self.db.execute(
                    "UPDATE render_jobs SET status='review' WHERE status='running' RETURNING id").fetchall()
            for (clip_id,) in interrupted:
                log("render_review", clip_id=clip_id, reason="previous worker interrupted")

            while True:
                if not await self.run_one(log):
                    await asyncio.sleep(1)

    async def run_one(self, log):
        with self.db:
            job = self.db.execute("""
                UPDATE render_jobs SET status='running', attempts=attempts+1
                WHERE id=(SELECT id FROM render_jobs WHERE status='queued' AND ready_at<=?
                          ORDER BY ready_at, rowid LIMIT 1)
                RETURNING id, channel, url, attempts
            """, (time.time(),)).fetchone()
        if job is None:
            return False

        # Shield both process creation and the final database write. On a normal
        # shutdown the active render finishes before the consumer lock is released.
        task = asyncio.create_task(self._finish(job, log))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        return True

    async def _finish(self, job, log):
        clip_id, channel, url, attempts = job
        log("render_started", channel=channel, clip_id=clip_id, attempt=attempts)
        destination = [] if self.deliver_finished else ["--render-only", "--prepare-copy"]
        try:
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "clipmatch.finish_clip", "--state-dir", str(self.directory),
                "--channel", channel, "--clip-id", clip_id, "--url", url, *destination)
        except OSError:
            code = 75  # The child never started, so no upload could have taken place.
        else:
            code = await child.wait()

        status = "review"
        ready_at = 0
        if code == 0:
            status = "done"
        elif code == 75 and attempts < 3:
            status = "queued"
            ready_at = time.time() + 60 * 2 ** (attempts - 1)
        with self.db:
            self.db.execute("UPDATE render_jobs SET status=?, ready_at=? WHERE id=?",
                            (status, ready_at, clip_id))
        log(f"render_{status}", channel=channel, clip_id=clip_id, attempt=attempts, exit_code=code)
