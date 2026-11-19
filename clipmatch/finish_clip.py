"""Render one confirmed public Twitch clip using its streamer's saved layout."""
import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

from .cache import atomic_json, cache_valid, complete, fingerprint
from .cli import render_job
from .highlight_signals import measure
from .highlights import SHORT_SOURCE_SECONDS, choose_edit
from .layout_judge import review_layout
from .media import MediaInfo, package_version, probe, run
from .render_queue import read_chat_lead
from .screenshot_layout import MODEL_SHA256
from .social_titles import setting


LAYOUTS = Path(__file__).with_name("layouts.json")
MAX_SECONDS = 120


class RetryLater(Exception):
    """A transient failure occurred before anything was sent to Discord."""


def _validate_ids(channel: str, clip_id: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", channel):
        raise ValueError("Expected a Twitch channel login.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}", clip_id):
        raise ValueError("Expected a public Twitch clip slug for --clip-id.")


def validate_clip(channel: str, clip_id: str, url: str) -> str:
    _validate_ids(channel, clip_id)
    if len(url) > 2048 or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise ValueError("Invalid Twitch clip URL.")
    parts = urlsplit(url)
    host = parts.netloc.lower()
    path = parts.path.removesuffix("/")
    allowed = (host == "clips.twitch.tv" and path == f"/{clip_id}")
    if host in {"www.twitch.tv", "twitch.tv"}:
        pieces = path.split("/")
        allowed = (len(pieces) == 4 and pieces[1].lower() == channel.lower()
                   and pieces[2:] == ["clip", clip_id])
    if parts.scheme != "https" or not allowed:
        raise ValueError("Expected a public HTTPS Twitch clip URL matching --clip-id and --channel.")
    return f"https://clips.twitch.tv/{clip_id}"


def clip_directory(state_dir: Path, channel: str, clip_id: str) -> Path:
    _validate_ids(channel, clip_id)
    # The slug is case-sensitive even on a case-insensitive volume.
    digest = hashlib.sha256(clip_id.encode("ascii")).hexdigest()[:16]
    return state_dir.resolve() / "finished" / channel.lower() / f"clip-{clip_id[:40]}-{digest}"


def _layouts(channel: str) -> list[list[float]]:
    profiles = json.loads(LAYOUTS.read_text(encoding="utf-8"))
    profile = profiles.get(channel) if isinstance(profiles, dict) else None
    rects = profile if isinstance(profile, list) and profile and isinstance(profile[0], list) else [profile]
    if len(rects) > 2 or any(
        not isinstance(rect, list) or len(rect) != 4
        or any(type(v) not in (int, float) or not 0 <= v <= 1 for v in rect)
        or rect[2] <= 0 or rect[3] <= 0
        or rect[0] + rect[2] > 1.0000000001 or rect[1] + rect[3] > 1.0000000001
        for rect in rects
    ):
        raise ValueError(f"Set one or two normalized camera rectangles for {channel} in clipmatch/layouts.json.")
    return rects


def _crop(rect: list[float], info: MediaInfo) -> list[int]:
    x, y, width, height = rect
    # Bound both edges to even pixels for the source chroma subsampling.
    def edge(fraction, dimension):
        return min(round(fraction * dimension / 2), dimension // 2) * 2
    left, top = edge(x, info.width), edge(y, info.height)
    right, bottom = edge(x + width, info.width), edge(y + height, info.height)
    if right <= left or bottom <= top:
        raise ValueError("The saved facecam rectangle is too small at this source resolution.")
    return [left, top, right - left, bottom - top]


def _inspect_source(path: Path) -> MediaInfo:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Twitch did not supply a completed clip.")
    info = probe(path)
    if not info.has_audio or not 0 < info.duration <= MAX_SECONDS + 0.15:
        raise ValueError("Expected a completed Twitch clip with audio and a duration of at most 120 seconds.")
    return info


def _download(path: Path, url: str) -> dict:
    try:
        response = run([
            sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
            "--no-warnings", "--use-extractors", "twitch:clips",
            "--format", "best[ext=mp4][format_id!^=portrait]",
            "--match-filter", f"duration <=? {MAX_SECONDS}", "--force-overwrites", "--no-part",
            "--print", "after_move:%(.{display_id,channel,title,categories})j", "--output", str(path), "--", url,
        ], timeout=180)
    except ValueError as exc:
        cause = exc.__cause__
        # Twitch can confirm a new clip before its metadata and MP4 are readable.
        # The render queue bounds retries, including for a genuinely missing clip.
        transient = ("timed out", "connection reset", "connection refused", "connection aborted",
                     "name resolution", "getaddrinfo failed", "nodename nor servname",
                     "network is unreachable", "remote end closed", "incomplete read",
                     "http error 404", "http error 429", "http error 5",
                     "clip is no longer available", "clip is unavailable", "clip is not available",
                     "requested format is not available", "no video formats found")
        if (isinstance(cause, subprocess.TimeoutExpired)
                or (isinstance(cause, subprocess.CalledProcessError)
                    and any(reason in (cause.stderr or "").lower() for reason in transient))):
            raise RetryLater("Twitch download interrupted. The clip can be retried.") from None
        raise
    try:
        metadata = json.loads(response)
    except ValueError:
        raise ValueError("The downloader did not return a completed Twitch clip within the duration limit.") from None
    if not isinstance(metadata, dict):
        raise ValueError("The downloader did not identify the Twitch clip.")
    return metadata


def _source(directory: Path, channel: str, clip_id: str, url: str) -> MediaInfo:
    output = directory / "source.mp4"
    manifest = directory / "source.manifest.json"
    origin = directory / "source.json"
    key = fingerprint(dict(stage="twitch-clip-v1", channel=channel, clip_id=clip_id, url=url))
    if cache_valid(output, manifest, key):
        try:
            record = json.loads(origin.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = {}
        if isinstance(record, dict) and record.get("key") == key and isinstance(record.get("title"), str):
            return _inspect_source(output)

    temporary = directory / "source.partial.mp4"
    try:
        meta = _download(temporary, url)
        if meta.get("display_id") != clip_id or str(meta.get("channel", "")).lower() != channel:
            raise ValueError("Downloaded Twitch metadata does not match the requested clip and channel.")
        info = _inspect_source(temporary)
        title = meta.get("title") if isinstance(meta.get("title"), str) else ""
        temporary.replace(output)
        categories = meta.get("categories")
        category = next((v for v in categories if isinstance(v, str) and v.strip()), None) if isinstance(categories, list) else None
        atomic_json(origin, dict(key=key, channel=channel, clip_id=clip_id, url=url, title=title, category=category))
        complete(output, manifest, key, "twitch_clip", {"yt-dlp": package_version("yt-dlp")})
        return info
    finally:
        temporary.unlink(missing_ok=True)


def finish_clip(state_dir: Path, channel: str, clip_id: str, url: str, *, render_only: bool, prepare_copy: bool=False) -> int:
    url = validate_clip(channel, clip_id, url)
    channel = channel.lower()
    layouts = _layouts(channel)
    directory = clip_directory(state_dir, channel, clip_id)
    directory.mkdir(parents=True, exist_ok=True)
    # A process-scoped SQLite lock also protects a manual rerun racing the queue.
    with closing(sqlite3.connect(directory / "lock.sqlite3", timeout=0)) as lock:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                raise RetryLater("Another process is already working on this clip.") from None
            raise
        info = _source(directory, channel, clip_id, url)
        duration = min(info.duration, info.video_duration or info.duration)
        cameras = [_crop(layout, info) for layout in layouts]
        try:
            chat_lead = read_chat_lead(state_dir, clip_id)
        except sqlite3.OperationalError as exc:
            # SQLite extended result codes keep the primary code in the low byte.
            if (getattr(exc, "sqlite_errorcode", 0) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise RetryLater("The chat timing database is busy. The clip can be retried.") from None
            raise
        samples = measure(directory / "source.mp4", duration, tuple(cameras[0])) if duration > SHORT_SOURCE_SECONDS else []
        edit = choose_edit(duration, samples, chat_lead)
        print(f"[{clip_id}] selected {edit.start:.2f} to {edit.end:.2f}s of {duration:.2f}s ({edit.cue})",
              flush=True)
        mode = setting("CLIP_LAYOUT_MODE").strip().lower() or "screenshot"
        model = setting("SOCIAL_VISION_MODEL") if mode == "vision" else ""
        engine = ({"mode": "screenshot", "framing_version": 3, "model_sha256": MODEL_SHA256,
                   "onnxruntime": package_version("onnxruntime")}
                  if mode == "screenshot" else {"mode": mode, "model": model})
        source_record = json.loads((directory / "source.manifest.json").read_text(encoding="utf-8"))
        key = fingerprint(dict(stage="layout-judge-v5", source=source_record["sha256"], engine=engine,
                               start=edit.start, end=edit.end, cameras=cameras, width=info.width, height=info.height))
        review_path = directory / "layout-review.json"
        manifest = directory / "layout-review.manifest.json"
        if cache_valid(review_path, manifest, key):
            review = json.loads(review_path.read_text(encoding="utf-8"))
        else:
            review = review_layout(directory / "source.mp4", edit.start, edit.end, cameras,
                                   info.width, info.height, model, mode=mode)
            if review["status"] == "judged":
                atomic_json(review_path, review)
                complete(review_path, manifest, key, "layout_judge", engine)
        clip = dict(id=directory.name, source="source.mp4", start=edit.start, end=edit.end,
                    layout_segments=review["segments"], label=channel)
        job = directory / "job.json"
        atomic_json(job, dict(version=1, settings={"layout": "adaptive"}, clips=[clip]))

        def add_edit_provenance(data):
            data["source"]["twitch_url"] = url
            origin = json.loads((directory / "source.json").read_text(encoding="utf-8"))
            data["source"]["category"] = origin.get("category")
            data["source"]["twitch_title"] = origin.get("title")
            data["edit"] = {**asdict(edit), "source_duration": duration, "chat_lead_seconds": chat_lead}
            data["layout_review"] = review

        status = render_job(job, None, no_captions=True, send_finished=not render_only,
                            add_provenance=add_edit_provenance)
        if status == 0 and render_only and prepare_copy:
            from .social_titles import social_content
            master = directory / "renders" / (directory.name + ".mp4")
            record = json.loads(master.with_suffix(".manifest.json").read_text(encoding="utf-8"))
            social_content(master, record["sha256"])
        return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--clip-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--render-only", action="store_true", help="Build the local master for inspection")
    parser.add_argument("--prepare-copy", action="store_true", help="Prepare the public title and description with the master")
    args = parser.parse_args(argv)
    try:
        return finish_clip(**vars(args))
    except RetryLater as exc:
        print(f"finish-clip: {exc}", file=sys.stderr)
        return 75
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"finish-clip: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
