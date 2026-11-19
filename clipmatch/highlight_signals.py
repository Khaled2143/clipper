"""Offline source measurements for choosing highlight boundaries."""
from dataclasses import dataclass
import math
from pathlib import Path

from .media import run


_SILENCE_DB = -96.0


@dataclass(frozen=True)
class MediaSample:
    """Motion since the last sample, plus RMS and peak scene change in this interval."""

    time: float
    game_motion: float
    face_motion: float
    rms_db: float
    scene_change: float = 0.0


def _levels(diagnostics: list[str], instance: str, key: str) -> list[float]:
    # Named metadata filters emit one value per frame in their own stream order.
    prefix = f"[{instance} @ "
    delimiter = f"] {key}="
    return [float(line.split(delimiter, 1)[1]) for line in diagnostics
            if line.startswith(prefix) and delimiter in line]


def measure(source: Path, duration: float,
            facecam_rect: tuple[int, int, int, int]) -> list[MediaSample]:
    """Sample up to 120 seconds of a source with a facecam rectangle in display pixels."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Highlight measurement requires a positive, finite duration.")
    duration = min(duration, 120.0)
    end = f"{duration:.9f}"
    x, y, width, height = facecam_rect
    timing = "start_time=0:round=up:eof_action=pass"
    motion = "print:key=lavfi.signalstats.YDIF"
    video = (
        "[0:v:0]split[full][cam];"
        f"[full]fps=24:{timing},scale=96:54:flags=area,format=yuv420p,"
        "scdet,metadata@scene=print:key=lavfi.scd.score,"
        f"fps=4:{timing},signalstats,metadata@game={motion}[game];"
        f"[cam]fps=4:{timing},crop={width}:{height}:{x}:{y},scale=64:36:flags=area,"
        f"format=yuv420p,signalstats,metadata@face={motion}[face]"
    )
    # Pad timestamp gaps and trailing silence before splitting into quarter seconds.
    # The last bin uses just the remaining clip time.
    audio = (
        "aformat=channel_layouts=mono,aresample=48000:async=1:first_pts=0:min_hard_comp=0.001,"
        f"apad,atrim=end={end},"
        "asetnsamples=n=12000:p=0,astats=metadata=1:reset=1:measure_perchannel=none:"
        "measure_overall=RMS_level,ametadata@rms=print:key=lavfi.astats.Overall.RMS_level"
    )
    diagnostics: list[str] = []
    run([
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        "-t", end, "-i", str(source), "-filter_complex_threads", "1", "-filter_complex", video,
        "-map", "[game]", "-map", "[face]", "-map", "0:a:0?", "-af", audio, "-f", "null", "-",
    ], diagnostics=diagnostics)
    game = _levels(diagnostics, "metadata@game", "lavfi.signalstats.YDIF")
    face = _levels(diagnostics, "metadata@face", "lavfi.signalstats.YDIF")
    rms = _levels(diagnostics, "ametadata@rms", "lavfi.astats.Overall.RMS_level")
    scene = _levels(diagnostics, "metadata@scene", "lavfi.scd.score")[:math.ceil(duration * 24)]
    if not game or not scene or len(game) != len(face):
        raise ValueError("FFmpeg could not measure motion in the source video.")
    count = min(len(game), math.ceil(duration * 4))
    # Each quarter begins on a 24 fps frame and contains up to six scene scores.
    return [
        MediaSample(
            time=i / 4,
            game_motion=game[i],
            face_motion=face[i],
            rms_db=max(_SILENCE_DB, rms[i]) if i < len(rms) else _SILENCE_DB,
            scene_change=max(scene[i * 6:(i + 1) * 6]),
        )
        for i in range(count)
    ]
