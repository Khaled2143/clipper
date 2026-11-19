"""Locate a plausible inset using screenshots on the worker CPU."""
from functools import lru_cache
import math
from pathlib import Path
import tempfile

from .cache import file_hash
from .media import run
from .render import gameplay_rect


# OpenCV Zoo, revision 48b10fa505aeb51fbd127fc60a5c660040e823b7, MIT.
MODEL_FILE = Path(__file__).with_name("models") / "face_detection_yunet_2023mar.onnx"
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
MODEL = "YuNet-2023mar"
SIZE = 640


@lru_cache(maxsize=1)
def _session():
    import onnxruntime as ort
    if file_hash(MODEL_FILE) != MODEL_SHA256:
        raise ValueError("Packaged YuNet model failed its checksum")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    try:
        return ort.InferenceSession(str(MODEL_FILE), options, providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise RuntimeError("Cannot load packaged YuNet model") from exc


def _read_frame(source, timestamp, width, height):
    import numpy as np
    scale = SIZE / max(width, height)
    w, h = max(1, round(width * scale)), max(1, round(height * scale))
    with tempfile.TemporaryDirectory() as directory:
        raw = Path(directory) / "frame.bgr"
        run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-ss", str(timestamp),
             "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={w}:{h}",
             "-pix_fmt", "bgr24", "-threads", "1", "-f", "rawvideo", str(raw)], timeout=20)
        return np.frombuffer(raw.read_bytes(), dtype=np.uint8).reshape(h, w, 3)


def _faces(frame):
    """Decode the three YuNet prediction strides in screenshot pixels."""
    import numpy as np
    h, w = frame.shape[:2]
    tensor = np.zeros((1, 3, SIZE, SIZE), dtype=np.float32)
    tensor[0, :, :h, :w] = frame.transpose(2, 0, 1)
    strides = (8, 16, 32)
    names = [f"{name}_{stride}" for stride in strides for name in ("cls", "obj", "bbox")]
    session = _session()
    try:
        outputs = session.run(names, {"input": tensor})
    except Exception as exc:
        raise RuntimeError("CPU face inference failed") from exc
    proposals = []
    for i, stride in enumerate(strides):
        classes, objects, boxes = outputs[i * 3:i * 3 + 3]
        scores = np.sqrt(np.clip(classes * objects, 0, 1)).ravel()
        for index in np.flatnonzero(scores >= .80):
            row, col = divmod(int(index), SIZE // stride)
            dx, dy, log_w, log_h = boxes[0, index]
            bw, bh = math.exp(log_w) * stride, math.exp(log_h) * stride
            x, y = (col + dx) * stride - bw / 2, (row + dy) * stride - bh / 2
            if 0 <= x < w and 0 <= y < h and x + bw <= w and y + bh <= h:
                proposals.append((float(x), float(y), bw, bh, float(scores[index])))
    faces = []
    for face in sorted(proposals, key=lambda b: b[4], reverse=True):
        x, y, w, h, _ = face
        # Adjacent predictions commonly cover the same face at multiple strides.
        if any(a <= x + w / 2 <= a + aw and b <= y + h / 2 <= b + ah
               for a, b, aw, ah, _ in faces):
            continue
        faces.append(face)
        if len(faces) == 16:
            break
    return faces


def _face_patch(frame, face):
    import numpy as np
    x, y, w, h, _ = face
    rows = np.linspace(y, y + h - 1, 12).astype(int).clip(0, frame.shape[0] - 1)
    cols = np.linspace(x, x + w - 1, 12).astype(int).clip(0, frame.shape[1] - 1)
    return frame[rows[:, None], cols].astype(float)


def _has_inset_border(frame, x, y, w, h):
    """Check for a sustained edge near a horizontal and a vertical panel boundary."""
    import numpy as np
    def boundary(image, left, top, width, height):
        for pos in (top, top + height):
            lo = max(0, round(pos - .12 * height))
            hi = min(len(image), round(pos + .12 * height) + 1)
            area = image[lo:hi, round(left + .15 * width):round(left + .85 * width):2]
            if len(area) < 4 or not area.size:
                continue
            changes = np.abs(np.diff(area.astype(np.int16), axis=0)).max(axis=2)
            levels = np.median(changes, axis=1)
            if levels.max() > max(24, 3 * np.median(levels)):
                return True
        return False
    return boundary(frame, x, y, w, h) and boundary(frame.transpose(1, 0, 2), y, x, h, w)


def _separate_game(frame, face, faces, game):
    import numpy as np
    x, y, w, h = game
    pixels = frame[round(y):round(y+h):4, round(x):round(x+w):4]
    if not pixels.size or np.ptp(np.percentile(pixels, [2, 98], axis=(0, 1)), axis=0).max() < 16:
        return False
    # A duplicate of the same picture in the lower panel isn't separate gameplay.
    patch = _face_patch(frame, face)
    for other in faces:
        ox, oy, ow, oh, _ = other
        if x <= ox + ow/2 < x+w and y <= oy + oh/2 < y+h:
            if np.abs(patch - _face_patch(frame, other)).mean() < 12:
                return False
    return True


def _fit_face(crop, face, sx, sy, width, height, relocated):
    x, y, w, h = crop
    fx, fy, fw, fh, score = face
    excess = (x - fx/sx, y - fy/sy, (fx+fw)/sx - (x+w), (fy+fh)/sy - (y+h))
    crossed = [i for i, amount in enumerate(excess) if amount > 0]
    if not crossed:
        return crop
    if relocated or score < .90 or len(crossed) != 1:
        return None
    # A saved overlay can miss a few pixels when the host leans against one
    # edge. Extend just that edge, with two percent clearance and even pixels.
    edge, = crossed
    extent = (w, h, w, h)[edge]
    if excess[edge] > .04 * extent:
        return None
    grow = 2 * math.ceil((excess[edge] + .02 * extent) / 2)
    left = max(0, x - grow) if edge == 0 else x
    top = max(0, y - grow) if edge == 1 else y
    right = min(width, x + w + grow) if edge == 2 else x + w
    bottom = min(height, y + h + grow) if edge == 3 else y + h
    return left, top, right - left, bottom - top


def choose_camera(frame, faces, cameras, width, height):
    if not faces:
        return None, "no_confirmed_inset"
    # Detections are in screenshot pixels. Accepted crops use source pixels.
    sx, sy = frame.shape[1] / width, frame.shape[0] / height
    def scaled(rect):
        x, y, w, h = rect
        return x*sx, y*sy, w*sx, h*sy

    def matches(candidates, relocated=False, border=False):
        found = set()
        for crop in candidates:
            x, y, w, h = scaled(crop)
            for face in faces:
                fx, fy, fw, fh, _ = face
                cx, cy = (fx + fw/2 - x) / w, (fy + fh/2 - y) / h
                margin = .20 if relocated else .14
                # Require a complete, inset-sized face in the expected seat.
                if not (margin <= cx <= 1-margin and .22 <= cy <= .82 and .14 <= fh/h <= .70
                        and .07 <= fw/w <= .58):
                    continue
                fitted = _fit_face(crop, face, sx, sy, width, height, relocated)
                if fitted is None:
                    continue
                if border and not _has_inset_border(frame, x, y, w, h):
                    continue
                game = gameplay_rect(fitted, width, height)
                if game and _separate_game(frame, face, faces, scaled(game)):
                    found.add(fitted)
                    break
        return found

    saved = {tuple(rect) for rect in cameras}
    verified = matches(saved)
    if not verified:
        horizontal = {(width-x-w, y, w, h) for x, y, w, h in saved}
        verified = matches(horizontal - saved, relocated=True)
        if not verified:
            # An avatar in an unfamiliar corner is especially ambiguous. Require
            # a visible panel edge before accepting a vertical layout change.
            vertical = {(x, height-y-h, w, h) for x, y, w, h in saved | horizontal
                        if abs(height-h-2*y) >= .6*h}
            verified = matches(vertical - saved - horizontal, relocated=True, border=True)
    if len(verified) == 1:
        return list(verified.pop()), "face_in_candidate"
    return None, "ambiguous" if verified else "no_confirmed_inset"


def _shared_camera(boxes):
    """Keep the framing only if multiple observations agree on the inset."""
    if len(boxes) < 2:
        return None
    edges = [(x, y, x+w, y+h) for x, y, w, h in boxes]
    left = min(edge[0] for edge in edges)
    top = min(edge[1] for edge in edges)
    right = max(edge[2] for edge in edges)
    bottom = max(edge[3] for edge in edges)
    common_w = min(edge[2] for edge in edges) - max(edge[0] for edge in edges)
    common_h = min(edge[3] for edge in edges) - max(edge[1] for edge in edges)
    if common_w >= .85 * (right-left) and common_h >= .85 * (bottom-top):
        return [left, top, right-left, bottom-top]
    return None


def review_screenshot(source, start, end, cameras, width, height):
    duration = end - start
    sample = start + duration / 2
    result = dict(mode="screenshot", model=MODEL, model_sha256=MODEL_SHA256, sample_source_time=sample,
                  candidates=cameras, segments=[dict(start=0.0, facecam_rect=None)])

    def observe(timestamp):
        frame = _read_frame(source, timestamp, width, height)
        faces = _faces(frame)
        camera, reason = choose_camera(frame, faces, cameras, width, height)
        return camera, reason, len(faces)

    try:
        camera, reason, face_count = observe(sample)
        if reason == "no_confirmed_inset" and duration >= 1:
            # A turn or a hand over the face can obscure a webcam at the
            # midpoint. Require agreement from other frames of this same edit.
            times = [start + duration * fraction for fraction in (.125, .375, .625, .875)]
            observations = [observe(timestamp) for timestamp in times]
            result["additional_sample_times"] = times
            if not any(why == "ambiguous" for _, why, _ in observations):
                camera = _shared_camera([box for box, _, _ in observations if box])
                if camera:
                    reason = "camera_confirmed_across_samples"
    except (OSError, ValueError, ImportError, RuntimeError) as exc:
        print(f"Screenshot layout unavailable ({type(exc).__name__}). Using the full view.", flush=True)
        return {**result, "status": "unavailable"}
    result["segments"][0]["facecam_rect"] = camera
    return {**result, "status": "judged", "reason": reason, "detected_faces": face_count}
