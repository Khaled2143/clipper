"""Choose the source window for a finished Twitch clip using local media cues."""
from dataclasses import dataclass
from statistics import median

from .highlight_signals import MediaSample


# Preserve a short original in full. Longer sources can contain both setup and dead air.
SHORT_SOURCE_SECONDS = 18


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    anchor: float | None
    cue: str
    chat_estimate: float | None


def _rises(values: list[float], minimum_change: float, transient_weight: float) -> list[float]:
    """Compare each quarter second with its preceding three seconds of activity."""
    jitter = [abs(current - previous) for previous, current in zip(values, values[1:])]
    scale = max(minimum_change, 3 * median(jitter)) if jitter else minimum_change
    # The first video frame has no predecessor, so its motion is always zero.
    # Require a second of history before interpreting any jump as an onset.
    strengths = [0.0] * min(4, len(values))
    for index in range(4, len(values)):
        baseline = median(values[max(0, index - 12):index])
        instantaneous = values[index] - baseline
        sustained = min(instantaneous, median(values[index:index + 4]) - baseline)
        strengths.append(max(0.0, sustained, transient_weight * instantaneous) / scale)
    return strengths


def _scene_changes(samples: list[MediaSample]) -> list[float]:
    changes = [s.scene_change for s in samples]
    strengths = []
    for index, change in enumerate(changes):
        # Scene detection compares frames within each bin, so even the opening
        # bin can contain a real cut. Use its neighbors until history exists.
        neighbors = changes[:8] if index < 4 else changes[max(0, index - 12):index]
        usual = median(neighbors)
        # A hard cut against a steady shot is more informative than repeated
        # flashes in an already busy scene. A one-point floor rejects tiny noise.
        strengths.append(2 * max(0.0, change - usual) / (1 + usual))
    return strengths


def _near_pause(samples: list[MediaSample], target: float, low: float, high: float) -> float:
    """Prefer a dip near the intended cut. Flat sound keeps the intended boundary."""
    target = max(low, min(high, target))
    candidates = [s for s in samples if low <= s.time <= high]
    if not candidates or target in (low, high):
        return target
    at_target = min(candidates, key=lambda s: abs(s.time - target))
    typical = max(at_target.rms_db, median(s.rms_db for s in candidates))
    threshold = min(min(s.rms_db for s in candidates) + 1.5, typical - 3.5)
    pauses = [s.time for s in candidates if s.rms_db <= threshold]
    # Prefer the preceding pause so an opening keeps its setup and a closing
    # does not start a new line when two nearby pauses are comparably quiet.
    preceding = [t for t in pauses if t <= target]
    return max(preceding) if preceding else min(pauses, default=target)


def _include_setup(samples: list[MediaSample], start: float) -> float:
    """Keep a short audible phrase already in progress at the proposed opening."""
    for index, sample in enumerate(samples):
        if index < 4 or not start - 5 <= sample.time < start:
            continue
        background = max(-55.0, median(s.rms_db for s in samples[max(0, index - 8):index]))
        if sample.rms_db < background + 8:
            continue
        audible = [s.rms_db >= background + 5 for s in samples[index:index + 21] if s.time <= start]
        # Allow breaths, but never connect two phrases across a full second of quiet.
        if (sum(audible[:4]) >= 3 and
                all(any(audible[j:j + 4]) for j in range(len(audible) - 3))):
            return max(0.0, sample.time - .25)
    return start


def _media_cues(samples: list[MediaSample]) -> list[tuple[float, str]]:
    scene = _scene_changes(samples)
    game = _rises([s.game_motion for s in samples], 4, .75)
    face = _rises([s.face_motion for s in samples], 4, .65)
    # Discard the inaudible range and single-sample audio spikes.
    audio = _rises([max(-55, s.rms_db) for s in samples], 6, 0)
    cues = []
    for index in range(len(samples)):
        # Scene changes and overall movement describe the same video panel.
        visual, kind = max((scene[index], "scene_change"), (game[index], "gameplay_motion"))
        # Entering a static menu can cause a huge single cut. Give subsequent
        # dialogue a chance to win when both panels then go nearly still.
        following = samples[index + 1:index + 17]
        stops_moving = len(following) >= 4 and median(max(s.game_motion, s.face_motion)
                                                       for s in following) < 2
        visual_weight = .4 if stops_moving else 1.0
        evidence = [(min(6.0, visual) * visual_weight, kind),
                    (min(6.0, face[index]) * visual_weight, "facecam_motion"),
                    (min(6.0, audio[index]), "audio_rise")]
        strongest, second, third = sorted(evidence, reverse=True)
        cues.append((strongest[0] + .5 * second[0] + .25 * third[0], strongest[1]))
    return cues


def choose_edit(duration: float, samples: list[MediaSample], chat_lead: float | None) -> Highlight:
    # Use five seconds each for post-request capture and viewer/reaction lag.
    # These are estimates. A distinctive event elsewhere can still win.
    chat_estimate = None
    if chat_lead is not None and 0 <= chat_lead <= duration - 10:
        chat_estimate = duration - chat_lead - 10
    if duration <= SHORT_SOURCE_SECONDS:
        return Highlight(0.0, duration, None, "short_source", chat_estimate)

    # A backfill lacks chat timing. Give the middle and later action only a broad preference.
    center = chat_estimate if chat_estimate is not None else duration * .63
    radius = 15 if chat_estimate is not None else max(12, duration * .36)
    cues = _media_cues(samples)
    scores = [strength / (1 + ((sample.time - center) / radius) ** 2)
              for sample, (strength, _) in zip(samples, cues)]

    peak = max(range(len(scores)), key=scores.__getitem__) if scores else None
    if peak is None or (scores[peak] < 1.6 and cues[peak][0] < 3.5):
        # There is too little evidence for a tight edit. Give the likely moment
        # room on both sides, including for archived clips without chat timing.
        length = min(duration, 36.0)
        start = max(0.0, min(duration - length, center - length / 2))
        return Highlight(start, start + length, None,
                         "chat_context" if chat_estimate is not None else "source_context", chat_estimate)

    anchor = samples[peak].time
    cue = cues[peak][1]
    # Treat nearby changes as one beat, e.g. a collision followed by the
    # streamer's response. Preserve the first change and the end of the reaction.
    related = [s.time for s, score in zip(samples, scores)
               if abs(s.time - anchor) <= 4.5 and score >= .4 * scores[peak]]
    first, last = related[0], related[-1]
    competing = any(abs(s.time - anchor) > 10 and score >= .88 * scores[peak]
                    for s, score in zip(samples, scores))
    setup = 7.0 if competing else 3.5
    tail = 11.0 if competing else 8.0
    start = _near_pause(samples, first - setup, max(0, first - setup - 2), max(0, first - setup + 1))
    end = _near_pause(samples, last + tail, min(duration, last + tail - 2), min(duration, last + tail + 2))
    return Highlight(_include_setup(samples, start), end, anchor, cue, chat_estimate)
