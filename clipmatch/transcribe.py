"""Lazy, cached faster-whisper transcription of exact selected-window audio."""
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import warnings
import wave

from .cache import atomic_json, cache_valid, complete, file_hash, fingerprint
from .media import package_version
from .spec import Settings


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@lru_cache(maxsize=2)
def _model(model: str, device: str, compute: str):
    from faster_whisper import WhisperModel
    return WhisperModel(model, device=device, compute_type=compute)


def load_model(settings: Settings):
    try:
        import ctranslate2
        import faster_whisper  # noqa: F401 -- validate optional installation before CUDA checks
    except ImportError as exc:
        raise ValueError('Captions require faster-whisper; install with pip install -e ".[transcription]"') from exc
    device = settings.device
    if device == 'auto':
        device = 'cuda' if ctranslate2.get_cuda_device_count() else 'cpu'
    compute = settings.compute_type
    if compute == 'auto':
        compute = 'float16' if device == 'cuda' else 'int8'
    try:
        return _model(settings.model, device, compute), device, compute
    except (RuntimeError, ValueError) as exc:
        if settings.device == 'auto' and device == 'cuda':
            warnings.warn('CUDA initialization failed; falling back to CPU int8 for this job', stacklevel=2)
            return _model(settings.model, 'cpu', 'int8'), 'cpu', 'int8'
        raise ValueError('Speech model initialization failed; check model, CUDA/cuDNN and available memory') from exc


def clean_words(words: list[Word], duration: float) -> list[Word]:
    result = []
    for word in sorted(words, key=lambda w: w.start):
        if not math.isfinite(word.start) or not math.isfinite(word.end):
            continue
        start, end = max(0, word.start), min(duration, word.end)
        text = ' '.join(word.text.split())
        if end > start and text:
            result.append(Word(start, end, text))
    return result


def transcribe(audio: Path, settings: Settings, output: Path, force: bool=False) -> list[Word]:
    versions = {name: package_version(name) for name in ('faster-whisper', 'ctranslate2')}
    key = fingerprint(dict(stage='transcription-v1', audio=file_hash(audio), model=settings.model,
                           device=settings.device, compute_type=settings.compute_type,
                           vad_filter=True, word_timestamps=True, tools=versions))
    manifest = output.with_suffix('.manifest.json')
    if not force and cache_valid(output, manifest, key):
        return [Word(**w) for w in json.loads(output.read_text(encoding='utf-8'))['words']]
    with wave.open(str(audio), 'rb') as wav:
        duration = wav.getnframes()/wav.getframerate()
    model, device, compute = load_model(settings)
    segments, info = model.transcribe(str(audio), word_timestamps=True, vad_filter=True)
    words = []
    # Segments are lazy: publish only after the iterator finishes successfully.
    for segment in segments:
        words.extend(Word(w.start, w.end, w.word.strip()) for w in (segment.words or []))
    words = clean_words(words, duration)
    atomic_json(output, dict(version=1, time_base='selected_clip_seconds', duration=duration,
                             model=settings.model, device=device, compute_type=compute,
                             tools=versions, language=info.language, words=[asdict(w) for w in words]))
    complete(output, manifest, key, 'transcribe', versions)
    return words
