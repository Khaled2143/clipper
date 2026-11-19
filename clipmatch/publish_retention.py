"""Remove verified worker media 24 hours after both public posting receipts."""
import asyncio
from contextlib import closing
import json
import re
import sqlite3
import stat
import time

from .cache import file_hash
from .finish_clip import clip_directory, validate_clip


def _path(path, root):
    # Reject redirected components (including Windows junctions) even when
    # they point to a different clip inside this worker's volume.
    if not path.is_relative_to(root) or path.resolve() != path:
        raise ValueError("redirected retention path")
    return path


def _record(path, root):
    if not _path(path, root).is_file():
        raise ValueError("missing retention record")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid retention record")
    return value


def _verified(path, digest, root, size=None):
    path = _path(path, root)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None  # A previous pass can have stopped between unlinks.
    if (not stat.S_ISREG(before.st_mode) or before.st_size <= 0
            or (size is not None and before.st_size != size) or file_hash(path) != digest):
        raise ValueError("media no longer matches its recorded digest")
    return before


def _unlink(path, before, root):
    if before is None:
        return
    after = _path(path, root).lstat()
    if (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (
            before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size):
        raise ValueError("media changed during retention check")
    path.unlink()


def _manifest_stat(path, stage, digest, root):
    manifest = _record(path.with_suffix(".manifest.json"), root)
    size = manifest.get("size")
    if (manifest.get("status") != "complete" or manifest.get("stage") != stage
            or manifest.get("artifact") != path.name or manifest.get("sha256") != digest
            or type(size) is not int or size <= 0):
        raise ValueError("media manifest changed or incomplete")
    return _verified(path, digest, root, size)


def _clean_clip(root, clip_id, channel, url, digest):
    directory = clip_directory(root, channel, clip_id)
    master = directory / "renders" / (directory.name + ".mp4")
    source = directory / "source.mp4"
    lockfile = _path(directory / "lock.sqlite3", root)
    if not lockfile.is_file():
        raise ValueError("clip lock missing")
    with closing(sqlite3.connect(lockfile.as_uri() + "?mode=rw", uri=True, timeout=0)) as lock:
        lock.execute("BEGIN IMMEDIATE")
        sidecar = _record(master.with_suffix(".json"), root)
        origin = _record(directory / "source.json", root)
        src = sidecar.get("source")
        if (sidecar.get("clip_id") != directory.name or sidecar.get("label") != channel
                or not isinstance(src, dict) or src.get("twitch_url") != url
                or src.get("url_or_path") != str(source) or not isinstance(src.get("identity"), str)
                or not re.fullmatch("[0-9a-f]{64}", src["identity"])
                or (origin.get("clip_id"), origin.get("channel"), origin.get("url")) != (clip_id, channel, url)):
            raise ValueError("source or master provenance mismatch")
        # Validate both before removing either. Never substitute hashes from a
        # newer master or a replacement download for the publication's hashes.
        master_stat = _manifest_stat(master, "render", digest, root)
        source_stat = _manifest_stat(source, "twitch_clip", src["identity"], root)
        _unlink(master, master_stat, root)
        _unlink(source, source_stat, root)


def _old_enough(state, deadline):
    try:
        stamp = json.loads(state)["published_at"]
    except (ValueError, TypeError, KeyError):
        return False
    return type(stamp) in (float, int) and 0 < stamp <= deadline


def _pin_ready(db, digest, deadline):
    owners = db.execute("""SELECT d.clip_id,y.state,t.state FROM publish_clips c
        LEFT JOIN publish_media_cleanup d ON c.id=d.clip_id AND c.digest=d.digest
        LEFT JOIN publish_jobs y ON c.id=y.clip_id AND y.platform='youtube' AND y.phase='published'
        LEFT JOIN publish_jobs t ON c.id=t.clip_id AND t.platform='tiktok' AND t.phase='published'
        WHERE c.digest=?""", (digest,)).fetchall()
    return bool(owners) and all(done and _old_enough(y, deadline) and _old_enough(t, deadline)
                                for done, y, t in owners)


async def cleanup_published(queue, log):
    db, root = queue.db, queue.root
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS publish_media_cleanup (
            clip_id TEXT, digest TEXT, pin_removed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(clip_id,digest))""")
        # snapshot once on activation; includes old renders that haven't finished yet.
        db.execute("""INSERT OR IGNORE INTO publish_settings
            SELECT 'retention_first_row', COALESCE(MAX(rowid),0) FROM render_jobs""")
    floor = int(queue.setting("retention_first_row"))
    deadline = time.time() - 86400
    rows = db.execute("""SELECT c.id,c.channel,c.source_url,r.url,c.digest,y.state,t.state
        FROM publish_clips c JOIN render_jobs r ON r.id=c.id AND r.channel=c.channel
        JOIN publish_jobs y ON c.id=y.clip_id AND y.platform='youtube' AND y.phase='published'
        JOIN publish_jobs t ON c.id=t.clip_id AND t.platform='tiktok' AND t.phase='published'
        LEFT JOIN publish_media_cleanup x ON x.clip_id=c.id AND x.digest=c.digest
        WHERE r.rowid>? AND c.digest IS NOT NULL AND x.clip_id IS NULL""", (floor,)).fetchall()
    for clip_id, channel, publication_url, render_url, digest, youtube, tiktok in rows:
        if not (_old_enough(youtube, deadline) and _old_enough(tiktok, deadline)
                and isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest)):
            continue
        try:
            url = validate_clip(channel, clip_id, render_url)
            if validate_clip(channel, clip_id, publication_url) != url:
                raise ValueError("publication does not match render source")
            await asyncio.to_thread(_clean_clip, root, clip_id, channel, url, digest)
        except (OSError, ValueError, TypeError, sqlite3.Error, RuntimeError) as exc:
            log("publish_cleanup_wait", clip_id=clip_id, reason=type(exc).__name__)
            continue
        with db:
            db.execute("INSERT OR IGNORE INTO publish_media_cleanup(clip_id,digest) VALUES(?,?)", (clip_id, digest))
        log("publish_media_removed", clip_id=clip_id)

    # This pass is serialized with collection under the publisher's process
    # lock. A pin stays available until every clip using those bytes qualifies.
    digests = db.execute("SELECT DISTINCT digest FROM publish_media_cleanup WHERE pin_removed=0").fetchall()
    for (digest,) in digests:
        if not _pin_ready(db, digest, deadline):
            continue
        try:
            pin = queue.path(digest)
            if not re.fullmatch("[0-9a-f]{64}", digest) or pin != root / "publish-media" / (digest + ".mp4"):
                raise ValueError("invalid publication pin")
            before = await asyncio.to_thread(_verified, pin, digest, root)
            with db:
                # Hashing yields to the queue. Recheck under a short write lock
                # before unlinking so a newly pinned public job keeps its media.
                db.execute("BEGIN IMMEDIATE")
                if _pin_ready(db, digest, deadline):
                    _unlink(pin, before, root)
                    db.execute("UPDATE publish_media_cleanup SET pin_removed=1 WHERE digest=?", (digest,))
        except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as exc:
            log("publish_pin_cleanup_wait", reason=type(exc).__name__)
