"""Ground public copy in confident speech from the edit or its Twitch provenance."""
from functools import lru_cache
import json
import math
import re
import regex


_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")
_EMOJI = regex.compile(r"[0-9#*]\uFE0F?\u20E3|(?=[\p{Extended_Pictographic}\p{Regional_Indicator}\p{Emoji_Modifier}])\X")


def plain_title(text):
    text = " ".join(_EMOJI.sub(" ", text).split())
    return re.sub(r" +([.,!?])", r"\1", text)


_FILLER = set("""
    a an the and or but so if as at by for from in of on to up with is are am was were be been
    i me my mine we our us you your he him his she her they their it its it's that's this that
    these those there here do does did don't didn't can can't could would should has had have will
    i'm you're we're i'll what what's who how when why where then than now not no yes yeah oh ah
    man bro dude chat just really very little okay ok right gonna got get go going need know look
    like liking too thing something wait hold yo guys see want anything exactly already
    game actually literally i'm i've they're you'll let's supposed really keep think
""".split())
_UNSUITABLE = re.compile(
    r"https?://|www\.|[@<>{}\[\]|]|\b(?:\w*fuck\w*|\w*shit\w*|bitch\w*|cunt\w*|nigg\w*|fagg\w*|"
    r"subscribe|subtitles|sponsored)\b|thank(?:s| you) for watching", re.I)


def safe_text(value):
    return (isinstance(value, str) and not _UNSUITABLE.search(value)
            and not any(ord(c) < 32 or 0x202a <= ord(c) <= 0x202e for c in value))


def speech(master, model_name):
    return speech_evidence(master, model_name)[:2]


def speech_evidence(master, model_name):
    try:
        stat = master.stat()
        if stat.st_size:
            return _recognize(str(master), stat.st_mtime_ns, stat.st_size, model_name)
    except Exception as exc:
        print(f"Local speech unavailable ({type(exc).__name__}).", flush=True)
    return "", (), ()


@lru_cache(maxsize=1)
def _recognize(path, mtime_ns, size, model_name):
    # The cache holds text only. Model weights live on disk between clip jobs.
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2, local_files_only=True)
    segments, _ = model.transcribe(path, beam_size=3, vad_filter=True, word_timestamps=True,
                                   condition_on_previous_text=False)
    full_text, phrases, beats = [], {}, []
    for segment in segments:
        full_text.append(segment.text.strip())
        if segment.no_speech_prob > .55 or segment.avg_logprob < -1:
            continue
        sentence = []
        for word in segment.words or []:
            sentence.append(word)
            if word.word.rstrip().endswith((".", "?", "!")):
                _rank_sentence(sentence, phrases, beats)
                sentence = []
        _rank_sentence(sentence, phrases, beats)
    return " ".join(full_text)[:3000], tuple(sorted(phrases, key=phrases.get, reverse=True)), tuple(beats)


def _rank_sentence(sentence, phrases, beats):
    if not sentence or not safe_text(" ".join(w.word.strip() for w in sentence)):
        return
    clause = []
    for word in sentence:
        clause.append(word)
        if word.word.rstrip().endswith(","):
            _rank_phrase(clause, phrases, beats)
            clause = []
    _rank_phrase(clause, phrases, beats)


def _rank_phrase(clause, phrases, beats):
    if clause and clause[-1].word.lower().strip(" ,.?!") in {"bro", "chat", "guys"}:
        clause = clause[:-1]
    if not 3 <= len(clause) <= 26:
        return
    text = " ".join(word.word.strip() for word in clause).strip(" ,.!-")
    words = [w.casefold() for w in _WORDS.findall(text)]
    detail = {w for w in words if len(w) > 1 and w not in _FILLER}
    probabilities = [float(word.probability) for word in clause]
    confidence = sum(probabilities) / len(probabilities)
    if not detail or len(set(words)) * 2 < len(words) or confidence < .72 or min(probabilities) < .20:
        return
    anchors = [float(word.probability) for word in clause
               if any(w.casefold() in detail for w in _WORDS.findall(word.word))]
    if sum(anchors) / len(anchors) < .65:
        return
    if words[0] in {"and", "but", "because"} or words[-1] in {"and", "a", "the", "for", "to", "like"}:
        return
    text = text[0].upper() + text[1:]
    score = min(4, len(detail)) + confidence + .85 * text.endswith("?")
    if words[0] in {"i", "we", "you", "he", "she", "they"}:
        score += .5
    if len(clause) <= 12:
        phrases[text] = max(score, phrases.get(text, 0))
    start, end = getattr(clause[0], "start", None), getattr(clause[-1], "end", None)
    if isinstance(start, (float, int)) and isinstance(end, (float, int)) and 0 <= start < end < math.inf:
        beats.append((round(start, 2), round(end, 2), text))


def game_name(category):
    return {"Grand Theft Auto VI": "GTA 6", "Grand Theft Auto V": "GTA 5"}.get(category, category)


def contextual_title(text, name):
    if re.search(r"\b" + re.escape(name) + r"\b", text, re.I):
        return text
    question = text.endswith("?")
    return f"{text.rstrip('.!?')} on {name}’s stream" + ("?" if question else "")


def spoken_headline(text, name):
    """Keep an instruction conditional and embedded questions open."""
    pickup = re.fullmatch(r"(collect|claim|retrieve|pick up) (.+) (?:from|at|in) (?:your|the) "
                          r"(mailbox|locker|chest|counter|booth|warehouse|shop|bank|station)", text, re.I)
    if pickup:
        verb, item, location = pickup.groups()
        if re.fullmatch(r"(?:cash|money|pay) (?:from|for) (?:completed |finished )?jobs", item, re.I):
            return f"{name} can get paid at the {location.lower()}"
        return f"{name} can {verb.lower()} {item} at the {location.lower()}"
    question = re.fullmatch(r"(?:see|check|find out|let['’]s see) ((?:what|who|where|which|how)\b.+)",
                            text.rstrip("?"), re.I)
    if question:
        hook = question[1]
        return f"{name}, {hook[0].lower() + hook[1:]}?"
    return contextual_title(text, name)


def local_candidates(master, data, name, phrases):
    source = data["source"]
    category = source.get("category")
    if not safe_text(category) or not category.strip() or len(category) > 50:
        category = None
    title = source.get("twitch_title")
    if not title or not category:
        # Older render sidecars keep the matching Twitch metadata alongside the source.
        try:
            origin = json.loads((master.parent.parent / "source.json").read_text(encoding="utf-8"))
            if isinstance(origin, dict) and origin.get("url") == source.get("twitch_url"):
                title = title or origin.get("title")
                legacy_category = origin.get("category")
                if not category and safe_text(legacy_category) and len(legacy_category) <= 50:
                    category = legacy_category.strip() or None
        except (OSError, ValueError):
            pass

    duration = data.get("output", {}).get("duration")
    seconds = round(duration) if type(duration) in (int, float) and math.isfinite(duration) and duration > 0 else 0
    stream = f"{name}’s {category} stream" if category else f"{name}’s Twitch stream"
    description = f"{seconds} seconds from {stream}." if seconds else f"From {stream}."
    for phrase in phrases:
        yield "speech", dict(title=spoken_headline(phrase, name), description=description)

    if safe_text(title):
        title = " ".join(title.split())
        words = {w.casefold() for w in _WORDS.findall(title)}
        vague = {"clip", "stream", "highlight", "highlights", "live", "twitch", "untitled", name.casefold()}
        vague.update(_FILLER)
        vague.update(w.casefold() for w in _WORDS.findall(category or ""))
        if (2 <= len(title.split()) <= 12 and words - vague and "!!" not in title
                and not re.search(r"(?:^|\s)[!#]\w|\b(first (?:time|playthrough)|marathon|day \d+)\b", title, re.I)):
            yield "twitch_title", dict(title=contextual_title(title, name), description=description)

    if category:
        yield "metadata", dict(title=f"From {name}’s {game_name(category)} stream", description=description)
    else:
        yield "metadata", dict(title=f"From {name} on Twitch", description=description)
