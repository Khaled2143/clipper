"""Per-channel detection from consecutive chat message buckets."""

from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
import sys
from typing import Self


_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


@dataclass(frozen=True)
class SpikeSettings:
    bucket_seconds: int = 10
    baseline_minutes: float = 10
    spike_multiplier: float = 3
    min_messages: int = 20
    warmup_minutes: float = 5
    cooldown_seconds: int = 90

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Self:
        defaults = cls()
        values = {}
        for name, parse in (
            ("bucket_seconds", int),
            ("baseline_minutes", float),
            ("spike_multiplier", float),
            ("min_messages", int),
            ("warmup_minutes", float),
            ("cooldown_seconds", int),
        ):
            key = name.upper()
            if key in env:
                try:
                    values[name] = parse(env[key])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be numeric") from exc
            else:
                values[name] = getattr(defaults, name)
        return cls(**values)

    def __post_init__(self) -> None:
        for key, value, minimum in (
            ("BUCKET_SECONDS", self.bucket_seconds, 1),
            ("MIN_MESSAGES", self.min_messages, 1),
            ("COOLDOWN_SECONDS", self.cooldown_seconds, 0),
        ):
            if type(value) is not int or not minimum <= value <= sys.maxsize:
                raise ValueError(f"{key} must be an integer between {minimum} and {sys.maxsize}")
        if not math.isfinite(self.baseline_minutes) or not (
            self.bucket_seconds <= self.baseline_minutes * 60 <= sys.maxsize
        ):
            raise ValueError("BASELINE_MINUTES must be finite and cover at least one bucket")
        if not math.isfinite(self.spike_multiplier) or self.spike_multiplier <= 0:
            raise ValueError("SPIKE_MULTIPLIER must be positive and finite")
        if not math.isfinite(self.warmup_minutes * 60) or self.warmup_minutes < 0:
            raise ValueError("WARMUP_MINUTES must be nonnegative and finite")


@dataclass(frozen=True)
class Bucket:
    count: int
    baseline: float
    ratio: float | None
    top: tuple[str, ...]
    spike: bool


class Detector:
    """Accumulate one channel's chat. Advance monotonic time before adding events."""

    def __init__(self, settings: SpikeSettings, start: float) -> None:
        self._settings = settings
        self._start = start
        self._index = 0
        self._baseline_size = int(settings.baseline_minutes * 60) // settings.bucket_seconds
        self._history: deque[int] = deque()
        self._history_total = 0
        self._count = 0
        self._reactions: Counter[str] = Counter()
        self._display: dict[str, str] = {}

    def add(self, event: dict) -> None:
        self._count += 1
        message = event.get("message")
        if not isinstance(message, dict):
            return
        fragments = message.get("fragments")
        if not isinstance(fragments, list) or not fragments:
            self._add_text(message.get("text"))
            return
        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue
            kind = fragment.get("type")
            value = fragment.get("text")
            if kind == "text":
                self._add_text(value)
            elif kind in ("cheermote", "emote") and isinstance(value, str) and value:
                self._add_reaction(value)

    def _add_text(self, text: object) -> None:
        if isinstance(text, str):
            for word in _WORD.findall(text):
                self._add_reaction(word)

    def _add_reaction(self, reaction: str) -> None:
        key = reaction.casefold()
        self._display.setdefault(key, reaction)
        self._reactions[key] += 1

    def _record_count(self, count: int) -> None:
        if len(self._history) == self._baseline_size:
            self._history_total -= self._history.popleft()
        self._history.append(count)
        self._history_total += count

    def _clear_current_bucket(self) -> None:
        self._count = 0
        self._reactions.clear()
        self._display.clear()

    def advance(self, now: float) -> list[Bucket]:
        """Close elapsed windows, using only earlier counts for each baseline."""
        seconds = self._settings.bucket_seconds
        end_index = int((now - self._start) // seconds)
        # Compare against the actual boundary when timestamp rounding affects
        # the subtraction used to estimate the elapsed window count.
        if now >= self._start + (end_index + 1) * seconds:
            end_index += 1
        elif now < self._start + end_index * seconds:
            end_index -= 1
        excess = end_index - self._index - self._baseline_size
        if excess > 0:
            # Avoid replaying old detections after a pause. Preserve the counts
            # that can still contribute to the first returned bucket's baseline.
            if excess > self._baseline_size:
                self._history.clear()
                self._history_total = 0
                self._history.extend([0] * self._baseline_size)
            else:
                self._record_count(self._count)
                for _ in range(excess - 1):
                    self._record_count(0)
            self._index += excess
            self._clear_current_bucket()

        buckets = []
        while self._index < end_index:
            baseline = self._history_total / len(self._history) if self._history else 0.0
            ratio = self._count / baseline if baseline else None
            top = tuple(self._display[key] for key, _ in self._reactions.most_common(3))
            spike = (
                self._index * seconds >= self._settings.warmup_minutes * 60
                and self._count >= self._settings.min_messages
                and (ratio is None or ratio >= self._settings.spike_multiplier)
            )
            buckets.append(Bucket(self._count, baseline, ratio, top, spike))
            self._record_count(self._count)
            self._index += 1
            self._clear_current_bucket()
        return buckets
