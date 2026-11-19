"""Strict manual-job configuration; source times always refer to the original VOD."""
from dataclasses import dataclass, fields, replace
import math
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ClipSpec:
    id: str
    source: str
    start: float
    end: float
    label: str
    hook: str
    facecam_rect: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class Settings:
    model: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    buffer_seconds: float = 2.0
    crf: int = 20
    preset: str = "veryfast"
    font: str = "Arial"
    fps: int = 30
    output_dir: Path = Path("renders")
    work_dir: Path = Path("work")
    layout: str = "manual"


def _unknown(data, allowed, context):
    if not isinstance(data, dict):
        raise ValueError(f"{context} must be a mapping")
    extra = set(data) - set(allowed)
    if extra:
        raise ValueError(f"Unknown {context} field(s): {', '.join(map(str, extra))}")


def seconds(value):
    if isinstance(value, str) and ':' in value:
        if not re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
            raise ValueError("Timestamp must be HH:MM:SS[.sss]")
        h, m, s = map(float, value.split(':'))
        if m >= 60 or s >= 60:
            raise ValueError("Timestamp minutes/seconds must be below 60")
        value = h * 3600 + m * 60 + s
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Timestamp must be seconds or HH:MM:SS")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError("Invalid timestamp") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("Timestamp must be finite and nonnegative")
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")
    if len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be at most 500 characters without control characters")
    return value.strip()


def _path(value, root):
    if not isinstance(value, (str, Path)):
        raise ValueError("Paths must be text")
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def parse_job(data: dict, root: Path, config: dict | None = None,
              config_root: Path | None = None) -> tuple[Settings, list[ClipSpec]]:
    _unknown(data, {'version', 'settings', 'clips'}, 'job')
    if data.get('version') != 1 or isinstance(data.get('version'), bool):
        raise ValueError("Job version must be 1")
    config = config or {}
    _unknown(config, {'version', 'settings'}, 'config')
    if config and config.get('version') != 1:
        raise ValueError("Config version must be 1")
    allowed = {f.name for f in fields(Settings)}
    base = config.get('settings', {})
    override = data.get('settings', {})
    _unknown(base, allowed, 'settings')
    _unknown(override, allowed, 'settings')
    options = {**base, **override}
    destination_root = config_root or root
    for name in ('output_dir', 'work_dir'):
        options[name] = _path(options.get(name, 'renders' if name == 'output_dir' else 'work'), destination_root)
    settings = Settings(**options)
    if settings.layout not in ("manual", "stacked"):
        raise ValueError("layout must be manual or stacked")
    for name, low, high in [('crf', 0, 51), ('fps', 1, 60)]:
        value = getattr(settings, name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer from {low} to {high}")
    if (isinstance(settings.buffer_seconds, bool)
            or not isinstance(settings.buffer_seconds, (int, float))
            or not math.isfinite(settings.buffer_seconds) or settings.buffer_seconds < 0):
        raise ValueError("buffer_seconds must be finite and nonnegative")
    if settings.device not in ('auto', 'cpu', 'cuda'):
        raise ValueError("device must be auto, cpu, or cuda")
    if settings.compute_type not in ('auto', 'int8', 'float16', 'float32', 'int8_float16'):
        raise ValueError("Unsupported compute_type")
    if settings.preset not in ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow'):
        raise ValueError("Unsupported x264 preset")
    _text(settings.model, 'model')
    _text(settings.font, 'font')
    if any(c in settings.font for c in ',{}\\'):
        raise ValueError("font contains unsupported ASS punctuation")
    raw_clips = data.get('clips')
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ValueError("clips must be a nonempty list")
    clips, seen = [], set()
    for raw in raw_clips:
        _unknown(raw, {f.name for f in fields(ClipSpec)}, 'clip')
        required = {'id', 'source', 'start', 'end', 'label'}
        required.add('facecam_rect' if settings.layout == 'stacked' else 'hook')
        if not required <= raw.keys():
            raise ValueError(f"Missing clip fields: {', '.join(sorted(required - raw.keys()))}")
        name = _text(raw['id'], 'id')
        reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{p}{n}' for p in ('COM', 'LPT') for n in range(1, 10)}
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name) or name.upper() in reserved:
            raise ValueError("id must be a safe filename identifier (not a Windows device name)")
        if name.casefold() in seen:
            raise ValueError(f"Duplicate clip id: {name}")
        seen.add(name.casefold())
        source = _text(raw['source'], 'source')
        if '://' in source:
            if urlsplit(source).scheme not in ('http', 'https') or not urlsplit(source).netloc:
                raise ValueError("Source URL must use HTTP or HTTPS")
        else:
            source = str(_path(source, root))
        start, end = seconds(raw['start']), seconds(raw['end'])
        if end <= start:
            raise ValueError("end must be after start")
        rect = raw.get('facecam_rect')
        if rect is not None:
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                    or any(type(n) is not int for n in rect)
                    or rect[0] < 0 or rect[1] < 0 or rect[2] <= 0 or rect[3] <= 0):
                raise ValueError("facecam_rect must be [x, y, positive_width, positive_height]")
            rect = tuple(rect)
        if settings.layout == 'stacked':
            if rect is None or raw.get('hook', '') != '':
                raise ValueError("The stacked layout requires facecam_rect and no hook.")
            hook = ''
        else:
            hook = _text(raw['hook'], 'hook')
        clips.append(ClipSpec(name, source, start, end, _text(raw['label'], 'label'), hook, rect))
    return settings, clips


def load_job(path: Path, config_path: Path | None = None) -> tuple[Settings, list[ClipSpec]]:
    try:
        import yaml
    except ImportError as exc:
        raise ValueError("PyYAML is required; install clipmatch with pip install -e .") from exc
    def read(file):
        try:
            with file.open(encoding='utf-8') as handle:
                return yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {file.name}") from exc
    path = path.resolve()
    return parse_job(read(path), path.parent, read(config_path) if config_path else None,
                     config_path.resolve().parent if config_path else None)
