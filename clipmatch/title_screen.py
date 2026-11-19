"""Read game-result text from the selected edit on the worker CPU."""
import math
from pathlib import Path
import re
import shutil
import tempfile

from .media import probe, run
from .render import gameplay_rect
from .screenshot_layout import SIZE, _read_frame


_RESULTS = {phrase.replace(" ", ""): phrase for phrase in (
    "wasted", "busted", "you died", "game over", "victory", "defeat", "victory royale",
    "you win", "you lose", "you won", "you lost", "mission passed", "mission failed",
    "mission complete", "round won", "round lost", "match won", "match lost",
)}
_RESULTS["1victoryroyale"] = "victory royale"


def _window(master, source):
    location = source.get("url_or_path")
    if isinstance(location, str) and location and "://" not in location:
        original = master.parent.parent / location
        try:
            start, end = float(source["start"]), float(source["end"])
            if math.isfinite(end) and 0 <= start < end and original.is_file():
                info = probe(original)
                if end <= (info.video_duration or info.duration) + .05:
                    return original, info, start, end - start, True
        except (OSError, KeyError, TypeError, ValueError):
            pass
    if not master.is_file():
        raise FileNotFoundError(master)
    info = probe(master)
    return master, info, 0.0, info.video_duration or info.duration, False


def _samples(video, info, start, duration, directory):
    import numpy as np
    step = duration / 8
    if duration > 30:  # Bound decoding on longer manual edits.
        for i in range(8):
            offset = (i + .5) * step
            yield offset, _read_frame(video, start + offset, info.width, info.height)
        return
    scale = SIZE / max(info.width, info.height)
    w, h = max(1, round(info.width * scale)), max(1, round(info.height * scale))
    raw = directory / "frames.bgr"
    messages = []
    # Select distinct input frames and keep their original edit-relative timestamps.
    vf = f"select='gte(t,(selected_n+.5)*{step})',scale={w}:{h},showinfo=checksum=0"
    run(["ffmpeg", "-nostdin", "-nostats", "-v", "info", "-threads", "2", "-skip_frame", "noref",
         "-skip_loop_filter", "all", "-ss", str(start), "-t", str(duration), "-i", str(video),
         "-map", "0:v:0", "-filter_threads", "1", "-vf", vf, "-frames:v", "8", "-fps_mode", "passthrough",
         "-pix_fmt", "bgr24", "-threads", "1", "-f", "rawvideo", str(raw)], timeout=30, diagnostics=messages)
    offsets = [float(match[1]) for line in messages if (match := re.search(r"\bpts_time:(\S+)", line))]
    for offset, frame in zip(offsets, np.fromfile(raw, np.uint8).reshape(-1, h, w, 3), strict=True):
        if 0 <= offset < duration:
            yield offset, frame


def _game_frame(frame, data, offset, original, info):
    camera = data.get("facecam_rect")
    for segment in data.get("layout_segments", ()):
        if segment["start"] > offset:
            break
        camera = segment.get("facecam_rect")
    if not camera:
        return frame
    h, w = frame.shape[:2]
    if not original:
        return frame[round(h * .375):]  # In a stacked master the camera occupies the upper panel.
    pane = None
    if camera[2] >= .80 * info.width or camera[3] >= .80 * info.height:
        pane = gameplay_rect(camera, info.width, info.height)
    x, y, cw, ch = pane or camera
    sx, sy = w / info.width, h / info.height
    rows = slice(round(y * sy), round((y + ch) * sy))
    columns = slice(round(x * sx), round((x + cw) * sx))
    if pane:
        return frame[rows, columns]
    # A corner inset overlays the full game. Keep the game's original center.
    frame = frame.copy()
    frame[rows, columns] = 0
    return frame


def _lettering(game):
    """Find substantial centered lines and isolate solid lettering from the moving background."""
    import numpy as np
    h, w = game.shape[:2]
    # Vertical game panels already crop a landscape game's sides.
    margin = .02 if w < 1.3 * h else .125
    crop = game[round(.29*h):round(.71*h), round(margin*w):round((1-margin)*w)].mean(axis=2)
    if not crop.size:
        return
    for gray in (crop, 255 - crop):
        lo, hi = np.percentile(gray, (20, 98))
        if hi - lo < 26:
            continue
        ink = gray > lo + .82 * (hi - lo)
        rows = ink.mean(axis=1) > .10
        edges = np.diff(np.r_[False, rows, False].astype(int))
        for top, bottom in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            height = bottom - top
            center_y = (top + bottom) / (2 * len(rows))
            if not .025 * h <= height <= .22 * h or not .20 < center_y < .80:
                continue
            columns = np.flatnonzero(ink[top:bottom].mean(axis=0) >= .27)
            if not columns.size:
                continue
            left, right = columns[0], columns[-1] + 1
            center_x = (left + right) / (2 * crop.shape[1])
            if not .14 * w <= right-left < .98 * crop.shape[1] or not .34 < center_x < .66:
                continue
            px, py = max(3, int(height * .30)), max(3, int(height * .22))
            band = ink[max(0, top-py):bottom+py, max(0, left-px):right+px]
            # Very heavy display fonts touch after video compression. Separate those strokes.
            if ink[top:bottom, left:right].mean() > .58:
                r = min(2, max(1, round(height * .05)))
                band = np.lib.stride_tricks.sliding_window_view(
                    np.pad(band, r), (2*r+1, 2*r+1)).all(axis=(-1, -2))
            yield np.where(band, 0, 255).astype(np.uint8)


def _read_lines(directory, mode):
    tsv = run(["tesseract", "pages.txt", "stdout", "-l", "eng", "--psm", mode, "tsv"],
              cwd=directory, timeout=15)
    words = {}
    for row in tsv.splitlines()[1:]:
        fields = row.split("\t", 11)
        if len(fields) == 12 and fields[0] == "5":
            words.setdefault(int(fields[1]) - 1, []).append((fields[-1], float(fields[-2])))
    for page, line in words.items():
        phrase = re.sub("[^a-z0-9]", "", "".join(word for word, _ in line).casefold())
        if phrase in _RESULTS and min(conf for _, conf in line) >= 70:
            yield page, _RESULTS[phrase]


def screen_outcome(master, data) -> tuple[str, float] | None:
    """Return the first corroborated sighting, in seconds from the start of this edit."""
    if not shutil.which("tesseract"):
        return None
    try:
        video, info, start, duration, original = _window(Path(master), data.get("source", {}))
        if duration < 1:
            return None
        times, found = [], []
        with tempfile.TemporaryDirectory(prefix="clipmatch-ocr-") as temp:
            directory, pages, owners = Path(temp), [], []
            for i, (offset, frame) in enumerate(_samples(video, info, start, duration, directory)):
                times.append(offset)
                found.append(set())
                for band in _lettering(_game_frame(frame, data, offset, original, info)):
                    path = directory / f"{len(pages)}.pgm"
                    h, w = band.shape
                    path.write_bytes(f"P5\n{w} {h}\n255\n".encode() + band.tobytes())
                    pages.append(path)
                    owners.append(i)
            if pages:
                (directory / "pages.txt").write_text("\n".join(p.name for p in pages), encoding="utf-8")
                # Batch the edit in regular and raw line mode. Stylized logos can need either.
                for mode in ("7", "13"):
                    for page, result in _read_lines(directory, mode):
                        found[owners[page]].add(result)
        results = {}
        for i, (before, after) in enumerate(zip(found, found[1:])):
            for phrase in before & after:
                if sum(phrase not in seen for seen in found) >= 2 and phrase not in found[0] & found[-1]:
                    results.setdefault(phrase, times[i])
        if len(results) == 1:
            return next(iter(results.items()))
    except (OSError, ImportError, KeyError, TypeError, ValueError):
        pass
    return None
