"""FFmpeg and yt-dlp boundaries; never download an entire remote video as fallback."""
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version, PackageNotFoundError
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .cache import atomic_json, cache_valid, complete, file_hash, fingerprint
from .spec import ClipSpec, Settings


@dataclass(frozen=True)
class MediaInfo:
    width: int
    height: int
    duration: float
    has_audio: bool
    video_codec: str = ''
    audio_codec: str = ''
    video_duration: float | None = None
    audio_duration: float | None = None


@dataclass(frozen=True)
class SourceWindow:
    path: Path
    source_start: float
    trim_start: float
    duration: float
    identity: str


def run(argv: list[str], *, cwd: Path | None = None, timeout: int = 1800,
        diagnostics: list[str] | None = None) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', check=True,
                                timeout=timeout, shell=False)
        if diagnostics is not None:
            diagnostics.extend((result.stderr or '').splitlines())
        return result.stdout
    except FileNotFoundError as exc:
        raise ValueError(f"Missing executable: {Path(argv[0]).name}; check installation and PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{Path(argv[0]).name} exceeded {timeout}s; completed stages are preserved") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or '').lower()
        if any(s in detail for s in ('private', 'subscriber-only', 'login required', 'sign in')):
            reason = 'source requires access or is private'
        elif any(s in detail for s in ('not found', 'unavailable', 'deleted', 'expired')):
            reason = 'source is unavailable or expired'
        elif 'unsupported url' in detail:
            reason = 'unsupported video URL'
        else:
            reason = 'media command failed; check source, codecs, disk space, and tool versions'
        # Do not echo raw stderr/argv: download tools can include signed URLs or credentials.
        raise ValueError(f"{Path(argv[0]).name}: {reason} (exit {exc.returncode})") from exc


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return 'unavailable'


@lru_cache(maxsize=1)
def tool_versions() -> dict:
    return {name: run([name, '-version']).splitlines()[0] for name in ('ffmpeg', 'ffprobe')}


def preflight() -> None:
    for name in ('ffmpeg', 'ffprobe'):
        if not shutil.which(name):
            raise ValueError(f"Missing {name}; install full FFmpeg and add its bin directory to PATH")
    filters = run(['ffmpeg', '-hide_banner', '-filters'])
    encoders = run(['ffmpeg', '-hide_banner', '-encoders'])
    if ' ass ' not in filters or 'libx264' not in encoders or ' aac ' not in encoders:
        raise ValueError('FFmpeg requires the ass/libass filter and libx264/AAC encoders')
    tool_versions()


def probe(path: Path) -> MediaInfo:
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                           '-of', 'json', str(path)]))
    videos = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')]
    audio = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), None)
    if not videos:
        raise ValueError('Source has no video stream')
    video = videos[0]
    duration = float(data.get('format', {}).get('duration', video.get('duration', 0)))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Could not determine a positive source duration')
    def stream_duration(stream):
        try:
            value = float(stream.get('duration', 'nan'))
            return value if math.isfinite(value) and value > 0 else None
        except (TypeError, ValueError):
            return None
    rotation = float(video.get('tags', {}).get('rotate', 0))
    for side in video.get('side_data_list', []):
        if 'rotation' in side:
            rotation = float(side['rotation'])
    width, height = int(video['width']), int(video['height'])
    # Crop coordinates refer to the displayed frame after FFmpeg autorotation.
    if round(rotation) % 180 == 90:
        width, height = height, width
    return MediaInfo(width, height, duration, audio is not None,
                     video.get('codec_name', ''), audio.get('codec_name', '') if audio else '',
                     stream_duration(video), stream_duration(audio) if audio else None)


def section_bounds(clip: ClipSpec, buffer: float, duration: float | None) -> tuple[float, float]:
    if duration is not None and clip.end > duration + 0.05:
        raise ValueError('Selected end exceeds source duration')
    return max(0, clip.start-buffer), min(clip.end+buffer, duration) if duration else clip.end+buffer


def acquire(clip: ClipSpec, settings: Settings, work_dir: Path, force: bool = False) -> SourceWindow:
    if not clip.source.startswith(('https://', 'http://')):
        source = Path(clip.source)
        if not source.is_file():
            raise ValueError(f"Local source not found: {source.name}")
        info = probe(source)
        section_bounds(clip, 0, min(info.duration, info.video_duration or info.duration))
        if not info.has_audio:
            raise ValueError('Source has no audio stream; this milestone requires audio')
        return SourceWindow(source, 0, clip.start, clip.end-clip.start, file_hash(source))

    work_dir.mkdir(parents=True, exist_ok=True)
    request_key = fingerprint(dict(stage='section-v1', url=clip.source, start=clip.start,
                                   end=clip.end, buffer=settings.buffer_seconds,
                                   tools=tool_versions(), yt_dlp=package_version('yt-dlp')))
    output = work_dir / 'section.mp4'
    manifest = work_dir / 'section.manifest.json'
    origin = work_dir / 'section.origin.json'
    if not force and cache_valid(output, manifest, request_key):
        try:
            record = json.loads(origin.read_text(encoding='utf-8'))
            start = float(record['source_start'])
            if record.get('request_key') == request_key:
                return SourceWindow(output, start, clip.start-start, clip.end-clip.start, file_hash(output))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    raw = json.loads(run([sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-warnings',
                          '--dump-single-json', '--', clip.source]))
    length = raw.get('duration')
    if length is not None and (not isinstance(length, (int, float)) or not math.isfinite(length)):
        raise ValueError('Invalid remote duration')
    if raw.get('is_live'):
        raise ValueError('Only completed VODs are supported')
    start, end = section_bounds(clip, settings.buffer_seconds, length)
    with tempfile.TemporaryDirectory(prefix='section-', dir=work_dir) as temp:
        temp = Path(temp)
        printed = run([sys.executable, '-m', 'yt_dlp', '--no-playlist', '--no-warnings',
                       '-f', 'bv*+ba/b', '--merge-output-format', 'mp4',
                       '--download-sections', f'*{start:.6f}-{end:.6f}',
                       '--force-keyframes-at-cuts', '--print', 'after_move:filepath',
                       '-o', str(temp/'download.%(ext)s'), '--', clip.source])
        candidates = [Path(line) for line in printed.splitlines() if Path(line).is_file()]
        if not candidates:
            raise ValueError('Downloader did not report a completed video section')
        downloaded = candidates[-1]
        # Re-encode the already-small section to normalize timestamps/container for downstream trim.
        normalized = temp/'normalized.mp4'
        run(['ffmpeg', '-v', 'error', '-y', '-i', str(downloaded), '-map', '0:v:0',
             '-map', '0:a:0', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
             '-c:a', 'aac', '-avoid_negative_ts', 'make_zero', str(normalized)])
        info = probe(normalized)
        if not info.has_audio or info.duration + 0.15 < clip.end-start:
            raise ValueError('Downloaded section is missing audio or shorter than selected window')
        os.replace(normalized, output)
    atomic_json(origin, dict(request_key=request_key, source_start=start))
    complete(output, manifest, request_key, 'acquire', tool_versions())
    return SourceWindow(output, start, clip.start-start, clip.end-clip.start, file_hash(output))


def extract_audio(window: SourceWindow, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem+'.partial.wav')
    try:
        run(['ffmpeg', '-v', 'error', '-y', '-i', str(window.path),
             '-ss', str(window.trim_start), '-t', str(window.duration),
             '-map', '0:a:0', '-vn', '-af', 'aresample=async=1:first_pts=0,apad', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
             str(temporary)])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output
